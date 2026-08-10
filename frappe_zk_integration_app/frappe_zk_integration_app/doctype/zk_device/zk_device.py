# Copyright (c) 2023, Creative Advanced Technologies and contributors
# For license information, please see license.txt

from __future__ import unicode_literals
import json
import time
from datetime import datetime
import frappe
from dateutil import parser
from frappe import _
from frappe.model.document import Document
from frappe_zk_integration_app.zk import ZK
from frappe.utils import now

DATETIME_FORMAT = "%Y-%m-%d %H:%M:%S"

# Transient network hiccups (a dropped packet, a momentarily busy device)
# shouldn't fail an entire fetch — retry the connect a couple of times
# before giving up.
_CONNECT_ATTEMPTS = 3
_CONNECT_RETRY_DELAY = 3  # seconds

# Hard limit on how long a single device background job may run.
# Must comfortably exceed the time needed to fetch a large attendance
# history, dedupe/insert it, sync employees, and create checkins — a full
# historical fetch (see get_device_log's fetch_from_date handling) can take
# well beyond a few minutes. Kept in line with the "long" queue's own
# default RQ timeout (1500s) rather than the much shorter "short"/"default"
# queues, since a lower value here silently overrides the queue's timeout.
_JOB_TIMEOUT = 1800  # 30 minutes


def _reconnect_db():
    """
    Re-establish the MariaDB connection if it was dropped during a long
    external operation (e.g. waiting for a ZK device to stream all records).

    MySQL/MariaDB silently closes idle connections when wait_timeout expires.
    Call this before any DB operation that follows a long network call.
    """
    try:
        frappe.db.sql("SELECT 1")
    except Exception:
        frappe.db.connect()


def _connect_with_retry(zk):
    """Retry the device connect a few times before giving up — a single
    dropped packet or a device that's momentarily busy servicing another
    client shouldn't fail the whole fetch."""
    last_exc = None
    for attempt in range(_CONNECT_ATTEMPTS):
        try:
            return zk.connect()
        except Exception as exc:
            last_exc = exc
            if attempt < _CONNECT_ATTEMPTS - 1:
                time.sleep(_CONNECT_RETRY_DELAY)
    raise last_exc


class ZKDevice(Document):
    @frappe.whitelist()
    def get_device_log(self, show_progress=False):
        """
        Pull attendance records from the ZK device, insert new ones, and
        advance the cursor (last_log_row).

        Architecture note — two clearly separated phases:
          Phase 1  Network I/O only.  ZK socket is open, DB is idle.
          Phase 2  DB I/O only.  ZK socket is closed, DB is reconnected.

        This separation avoids the InterfaceError that occurs when the DB
        connection is silently dropped by MySQL during a long ZK fetch.
        """
        conn = None
        inserted_count = 0

        # ── Phase 1: ZK network I/O (DB intentionally not touched) ───────
        try:
            zk = ZK(
                self.ip,
                port=self.port,
                password=self.password,
                timeout=30,
                force_udp=bool(self.udp),
                ommit_ping=bool(self.ping),
            )
            conn = _connect_with_retry(zk)
            all_logs = conn.get_attendance() or []
            total_logs_before_filter = len(all_logs)

            # Determine lower-bound timestamp
            start_datetime = None
            if self.fetch_from_date:
                fetch_date = parser.parse(str(self.fetch_from_date)).date()
                start_datetime = datetime.combine(fetch_date, datetime.min.time())
            elif self.last_log_row:
                start_datetime = parser.parse(str(self.last_log_row))

            current_datetime = datetime.now()
            logs = [
                log for log in all_logs
                if (not start_datetime or log.timestamp >= start_datetime)
                and log.timestamp < current_datetime
            ]
            total_logs_after_filter = len(logs)

            # Apply per-user period filter + generate deterministic names
            period = self.period or 0
            last_log_users = {}
            candidates = []  # list of (name, log)

            for log in logs:
                log.status = "IN" if log.status == 1 else "OUT"
                enroll_no = str(log.user_id).strip()
                name = "{}_{}_{}".format(
                    enroll_no,
                    log.timestamp.strftime("%Y%m%d%H%M%S"),
                    log.punch,
                )
                last_ts = last_log_users.get(enroll_no)
                if period and last_ts:
                    if (log.timestamp - last_ts).total_seconds() / 3600 < period:
                        continue
                last_log_users[enroll_no] = log.timestamp
                candidates.append((name, enroll_no, log))

        except Exception as fetch_exc:
            # Store error; Phase 2 will persist it via self.save()
            total_logs_before_filter = 0
            total_logs_after_filter = 0
            candidates = []
            fetch_error = str(fetch_exc)
            # The ZK call may have blocked long enough for MySQL's
            # wait_timeout to drop the idle connection — reconnect before
            # log_error touches the DB, or it raises a fresh, unrelated
            # OperationalError that masks this one and escapes uncaught.
            _reconnect_db()
            try:
                frappe.log_error(
                    message=fetch_error,
                    title=_("ZK Device Fetch Error: {0}").format(self.device_name),
                )
            except Exception:
                pass
        else:
            fetch_error = None
        finally:
            # Close ZK socket BEFORE touching the DB
            if conn:
                try:
                    conn.enable_device()
                    conn.disconnect()
                except Exception:
                    pass
                conn = None

        # ── Phase 2: DB I/O (ZK socket is already closed) ────────────────
        # The DB connection may have been dropped while the ZK socket was
        # open and streaming.  Reconnect before any DB operation.
        _reconnect_db()

        try:
            if fetch_error:
                self.last_connection_error = fetch_error
                return

            if not candidates:
                self.last_connection_error = _(
                    "No new logs found after {0}"
                ).format(start_datetime or "all time")
                return

            # Batch deduplication (one query per 500 names)
            candidate_names = [n for n, _, _ in candidates]
            existing_names = set()
            dedup_batch = 500
            for i in range(0, len(candidate_names), dedup_batch):
                batch = candidate_names[i : i + dedup_batch]
                rows = frappe.db.sql(
                    "SELECT name FROM `tabDevice Log` WHERE name IN ({})".format(
                        ",".join(["%s"] * len(batch))
                    ),
                    batch,
                )
                existing_names.update(row[0] for row in rows)

            # Build the list of truly new records
            ts = now()
            owner = frappe.session.user
            new_records = []
            last = None
            total = len(candidates)

            # Publish at most once per whole percent — with thousands of
            # candidates, publishing on every single row floods the realtime
            # channel and adds a network round-trip per row for no visible
            # benefit to the progress bar.
            last_reported_pct = -1
            for idx, (name, enroll_no, log) in enumerate(candidates):
                if show_progress:
                    pct = (idx + 1) * 100 // total
                    if pct != last_reported_pct:
                        frappe.publish_progress(
                            pct,
                            title=_("Fetching Logs for {0}...").format(self.device_name),
                        )
                        last_reported_pct = pct
                if name in existing_names:
                    continue
                new_records.append((
                    name,
                    enroll_no,
                    log.timestamp,
                    str(log.timestamp.date()),
                    log.status,
                    str(log.punch) if log.punch is not None else "",
                    self.device_name,
                    ts, ts, owner, owner,
                ))
                last = log.timestamp

            # Bulk INSERT — one round-trip per 500 rows
            if new_records:
                inserted_count = len(new_records)
                insert_batch = 500
                col = (
                    "INSERT IGNORE INTO `tabDevice Log` "
                    "(name, employee, enroll_no, time, `date`, type, punch, device, "
                    "creation, modified, modified_by, owner, docstatus) "
                    "VALUES "
                )
                row_ph = "(%s, NULL, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, 0)"
                for i in range(0, inserted_count, insert_batch):
                    batch = new_records[i : i + insert_batch]
                    frappe.db.sql(
                        col + ", ".join([row_ph] * len(batch)),
                        [v for row in batch for v in row],
                    )
                frappe.db.commit()

            # Advance the fetch cursor
            if last:
                self.last_log_row = (
                    last if last <= current_datetime else current_datetime
                )
            self.fetch_from_date = None
            self.last_connection_error = _(
                "Executed {0}: {1} on device / {2} after date filter / {3} inserted"
            ).format(
                now(),
                total_logs_before_filter,
                total_logs_after_filter,
                inserted_count,
            )

        except Exception as e:
            _reconnect_db()
            try:
                frappe.log_error(
                    message=str(e),
                    title=_("ZK Device DB Error: {0}").format(self.device_name),
                )
            except Exception:
                pass
            self.last_connection_error = str(e)

        finally:
            _reconnect_db()
            self.last_connection_time = datetime.now()
            self.save()

    def sync_employee(self):
        """Update device logs from the last 90 days that are missing an employee link."""
        frappe.db.sql(
            """
            UPDATE `tabDevice Log` log
            INNER JOIN tabEmployee emp
                ON emp.attendance_device_id = TRIM(log.enroll_no)
            SET log.employee = emp.name
            WHERE (log.employee IS NULL OR log.employee = '')
              AND log.date >= DATE_SUB(CURDATE(), INTERVAL 90 DAY)
            """
        )
        frappe.db.commit()


@frappe.whitelist()
def sync_employee():
    """
    Global sync: update ALL device logs (last 90 days) missing an employee link.
    Uses INNER JOIN for speed — far faster than a correlated subquery.
    """
    try:
        frappe.db.sql(
            """
            UPDATE `tabDevice Log` log
            INNER JOIN tabEmployee emp
                ON emp.attendance_device_id = TRIM(log.enroll_no)
            SET log.employee = emp.name
            WHERE (log.employee IS NULL OR log.employee = '')
              AND log.date >= DATE_SUB(CURDATE(), INTERVAL 90 DAY)
            """
        )
        frappe.db.commit()
    except Exception as e:
        frappe.log_error(message=str(e), title=_("Employee Synchronization Error"))
        raise


@frappe.whitelist()
def get_active_device_logs(names=None):
    if names:
        names = json.loads(str(names))

    devices = names or frappe.db.sql_list(
        """
        SELECT name FROM `tabZK Device`
        WHERE docstatus < 2 AND auto_attendance = 1
        """
    )

    for device in devices:
        frappe.enqueue(
            "frappe_zk_integration_app.frappe_zk_integration_app.doctype.zk_device.zk_device.device_log_background_job",
            device=device,
            queue="long",
            timeout=_JOB_TIMEOUT,
            job_id="zk_device_log_{}".format(device),
        )


@frappe.whitelist()
def send_specific_device_log(device_name):
    frappe.enqueue(
        "frappe_zk_integration_app.frappe_zk_integration_app.doctype.zk_device.zk_device.device_log_background_job",
        device=device_name,
        queue="long",
        timeout=_JOB_TIMEOUT,
        job_id="zk_device_log_{}".format(device_name),
    )


@frappe.whitelist()
def device_log_background_job(device):
    """
    Background worker pipeline:
      1. Fetch logs from ZK device → insert Device Logs
      2. Sync employee links (always — not just for new records)
      3. Create Employee Checkin records
      4. Notify the frontend user via realtime
    """
    current_user = frappe.session.user
    doc = frappe.get_doc("ZK Device", device)
    try:
        # Step 1 — fetch & insert (handles its own DB reconnect internally)
        doc.get_device_log(show_progress=True)

        # Step 2 — sync employees unconditionally so old unlinked records are
        #           fixed even when no new logs were fetched this run
        try:
            doc.sync_employee()
        except Exception as e:
            _reconnect_db()
            try:
                frappe.log_error(message=str(e), title="ZK Sync Employee Error")
            except Exception:
                pass

        # Step 3 — create checkins for all linked device logs. Isolated like
        # Step 2: this table is shared across every device's job, so a lock
        # wait / deadlock here shouldn't turn an otherwise-successful fetch
        # into a reported failure.
        try:
            from frappe_zk_integration_app.tasks import create_employee_checkin_internal
            create_employee_checkin_internal()
        except Exception as e:
            _reconnect_db()
            try:
                frappe.log_error(message=str(e), title="ZK Create Checkin Error")
            except Exception:
                pass

        # Step 4 — notify frontend
        frappe.publish_realtime(
            "zk_job_done",
            {
                "device": device,
                "message": doc.last_connection_error
                    or _("Logs fetched and checkins created successfully"),
            },
            user=current_user,
        )
    except Exception as e:
        error_message = str(e)
        # Same reasoning as in get_device_log: the DB connection may have
        # been dropped during the blocking ZK I/O, so reconnect before
        # log_error touches it — and never let log_error's own failure
        # prevent the frontend from being notified.
        _reconnect_db()
        try:
            frappe.log_error(
                message=error_message,
                title=_("ZK Background Job Error: {0}").format(device),
            )
        except Exception:
            pass
        frappe.publish_realtime(
            "zk_job_error",
            {"device": device, "error": error_message},
            user=current_user,
        )


@frappe.whitelist()
def check_connection(device_id, show_progress=False):
    try:
        doc = frappe.get_doc("ZK Device", device_id)
        zk = ZK(
            doc.ip,
            port=doc.port,
            password=doc.password,
            timeout=20,
            force_udp=bool(doc.udp),
            ommit_ping=bool(doc.ping),
        )
        conn = zk.connect()
        if conn:
            conn.disconnect()
            return "Success"
    except Exception as e:
        return str(e)
