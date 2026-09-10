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
from frappe.utils import cint, now

DATETIME_FORMAT = "%Y-%m-%d %H:%M:%S"

# Transient network hiccups (a dropped packet, a momentarily busy device)
# shouldn't fail an entire fetch — retry the connect+fetch as a unit a
# couple of times before giving up.
_CONNECT_ATTEMPTS = 3
_CONNECT_RETRY_DELAY = 3  # seconds

# Per-socket-read timeout used while talking to the device. This is NOT a
# cap on the whole fetch (the job itself has _JOB_TIMEOUT for that) — it's
# how long we wait for any single reply. A device with a large stored log
# can take well over 30s just to marshal and start streaming the response
# to the very first data request, so this needs real headroom.
_DEVICE_SOCKET_TIMEOUT = 120  # seconds

# Socket timeout for the cheap "how many records are on the device" probe
# behind the Get Logs / Clear Device Logs confirm dialogs. Small because the
# probe is a single command and the user is waiting on the answer; a device
# that cannot answer this quickly is better reported as unreachable.
_PREFLIGHT_TIMEOUT = 10  # seconds

# Hard limit on how long a single device background job may run.
# Must comfortably exceed the time needed to fetch a large attendance
# history, dedupe/insert it, sync employees, and create checkins — a full
# historical fetch (see get_device_log's fetch_from_date handling) can take
# well beyond a few minutes. Kept in line with the "long" queue's own
# default RQ timeout (1500s) rather than the much shorter "short"/"default"
# queues, since a lower value here silently overrides the queue's timeout.
_JOB_TIMEOUT = 1800  # 30 minutes

# The all-devices job runs a batch's devices sequentially inside one RQ
# job (see get_active_device_logs), so its budget must cover a whole
# batch, not just one device. Generous on purpose since this now runs
# once a day (see hooks.py) rather than every 5 minutes.
_ALL_DEVICES_JOB_TIMEOUT = 6 * 3600  # 6 hours

# Fixed number of background jobs used to fetch the whole device fleet —
# a middle ground between one job per device (unbounded job count, what
# this used to do) and one job for everything (unbounded time-to-finish
# on a large fleet, no parallelism). Devices are spread across this many
# batches regardless of fleet size; each batch runs as its own job and
# batches can run in parallel across whatever workers are available.
_DEVICE_BATCH_COUNT = 5


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


def _fetch_all_logs_with_retry(ip, port, password, force_udp, ommit_ping):
    """
    Connect and pull the full attendance log as one retryable unit.

    A timeout can happen mid-transfer (not just on the initial handshake),
    and once a socket read times out the connection is not safe to reuse —
    so each attempt opens a fresh connection rather than retrying only the
    connect() call. Returns (conn, logs); caller is responsible for
    disconnecting `conn` once done with it.
    """
    last_exc = None
    for attempt in range(_CONNECT_ATTEMPTS):
        conn = None
        try:
            zk = ZK(
                ip,
                port=port,
                password=password,
                timeout=_DEVICE_SOCKET_TIMEOUT,
                force_udp=force_udp,
                ommit_ping=ommit_ping,
            )
            conn = zk.connect()
            return conn, (conn.get_attendance() or [])
        except Exception as exc:
            last_exc = exc
            if conn:
                try:
                    conn.disconnect()
                except Exception:
                    pass
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

        Note: a fetch failure (device unreachable, timeout, ...) is caught
        internally and stored in last_connection_error rather than raised,
        so callers must check self.flags.zk_fetch_failed to distinguish a
        real failure from "ran fine, nothing new to fetch".
        """
        conn = None
        inserted_count = 0
        self.flags.zk_fetch_failed = False

        # ── Phase 1: ZK network I/O (DB intentionally not touched) ───────
        try:
            conn, all_logs = _fetch_all_logs_with_retry(
                self.ip, self.port, self.password, bool(self.udp), bool(self.ping)
            )
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

            # Apply per-user period filter + generate deterministic names.
            # `period` ("Period Difference (Mins)") is in MINUTES — the gap
            # below must be too, or a device with the default period=5 ends
            # up dropping any repeat punch within 5 HOURS instead of 5
            # minutes, silently eating legitimate OUT/break punches.
            period = self.period or 0
            last_log_users = {}
            candidates = []  # list of (name, log)

            for log in logs:
                # Ignore the device's own punch state (1/0) and derive IN/OUT
                # from time of day instead: before noon is IN, noon or later is OUT.
                log.status = "IN" if log.timestamp.hour < 12 else "OUT"
                enroll_no = str(log.user_id).strip()
                name = "{}_{}_{}".format(
                    enroll_no,
                    log.timestamp.strftime("%Y%m%d%H%M%S"),
                    log.punch,
                )
                last_ts = last_log_users.get(enroll_no)
                if period and last_ts:
                    if (log.timestamp - last_ts).total_seconds() / 60 < period:
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
                self.flags.zk_fetch_failed = True
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
            self.flags.zk_fetch_failed = True

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


def _create_checkins_safely():
    """Isolated like sync_employee: this table is shared across every
    device's job, so a lock wait / deadlock here shouldn't turn an
    otherwise-successful fetch into a reported failure."""
    try:
        from frappe_zk_integration_app.tasks import create_employee_checkin_internal
        create_employee_checkin_internal()
    except Exception as e:
        _reconnect_db()
        try:
            frappe.log_error(message=str(e), title="ZK Create Checkin Error")
        except Exception:
            pass


def _run_device_fetch_and_sync(doc, current_user, create_checkins):
    """
    Fetch + sync (+ optionally checkins) for a single device, and notify
    the frontend user via realtime. Shared by the single-device job and
    the all-devices job so both report the same success/error shape.
    """
    device = doc.name
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

        # Step 3 — create checkins for all linked device logs (skipped here
        # when the caller will do this once for every device at the end).
        if create_checkins:
            _create_checkins_safely()

        # Step 4 — notify frontend. get_device_log() catches its own fetch
        # failures (device unreachable, timeout, DB error) internally and
        # returns normally rather than raising, so a plain try/except here
        # would always land on the success path — check its flag instead.
        if doc.flags.zk_fetch_failed:
            frappe.publish_realtime(
                "zk_job_error",
                {"device": device, "error": doc.last_connection_error},
                user=current_user,
            )
        else:
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
        # The DB connection may have been dropped during the blocking ZK
        # I/O, so reconnect before log_error touches it — and never let
        # log_error's own failure prevent the frontend from being notified.
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


def _split_into_batches(items, batch_count):
    """Spread items round-robin across up to `batch_count` groups (fewer
    groups if there are fewer items than batches), so batch sizes stay
    close to even regardless of fleet size."""
    if not items:
        return []
    batch_count = min(batch_count, len(items))
    batches = [[] for _ in range(batch_count)]
    for i, item in enumerate(items):
        batches[i % batch_count].append(item)
    return batches


@frappe.whitelist()
def get_active_device_logs(names=None):
    """
    Enqueue up to _DEVICE_BATCH_COUNT background jobs, each fetching a
    subset of the active devices in sequence — a fixed, small job count
    regardless of fleet size (unlike one job per device), while still
    letting batches run in parallel across whatever workers are free
    (unlike a single job for the entire fleet).

    Within a batch, a device that fails is logged and the batch simply
    moves on to the next device (see device_logs_background_job_all) —
    one bad device never stops the rest of the batch.
    """
    if names:
        names = json.loads(str(names))

    devices = names or frappe.db.sql_list(
        """
        SELECT name FROM `tabZK Device`
        WHERE docstatus < 2 AND auto_attendance = 1
        """
    )

    if not devices:
        return

    for i, batch in enumerate(_split_into_batches(devices, _DEVICE_BATCH_COUNT)):
        frappe.enqueue(
            "frappe_zk_integration_app.frappe_zk_integration_app.doctype.zk_device.zk_device.device_logs_background_job_all",
            devices=batch,
            queue="long",
            timeout=_ALL_DEVICES_JOB_TIMEOUT,
            job_id="zk_device_log_batch_{}".format(i),
            deduplicate=True,
        )


@frappe.whitelist()
def send_specific_device_log(device_name):
    frappe.enqueue(
        "frappe_zk_integration_app.frappe_zk_integration_app.doctype.zk_device.zk_device.device_log_background_job",
        device=device_name,
        queue="long",
        timeout=_JOB_TIMEOUT,
        job_id="zk_device_log_{}".format(device_name),
        deduplicate=True,
    )


@frappe.whitelist()
def device_log_background_job(device):
    """Single-device pipeline: fetch → insert → sync employees → create
    checkins → notify. Used by the per-device "Get Logs" button."""
    doc = frappe.get_doc("ZK Device", device)
    _run_device_fetch_and_sync(doc, frappe.session.user, create_checkins=True)


@frappe.whitelist()
def device_logs_background_job_all(devices):
    """All-devices pipeline: run every device's fetch+sync sequentially in
    this one job, then create checkins once for everyone at the end."""
    if isinstance(devices, str):
        devices = json.loads(devices)

    current_user = frappe.session.user
    for device in devices:
        try:
            doc = frappe.get_doc("ZK Device", device)
        except Exception as e:
            _reconnect_db()
            try:
                frappe.log_error(
                    message=str(e),
                    title=_("ZK Background Job Error: {0}").format(device),
                )
            except Exception:
                pass
            continue
        _run_device_fetch_and_sync(doc, current_user, create_checkins=False)

    _create_checkins_safely()


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


def _connect(doc, timeout=20):
    """Open a ZK connection using a device doc's own settings."""
    zk = ZK(
        doc.ip,
        port=doc.port,
        password=doc.password,
        timeout=timeout,
        force_udp=bool(doc.udp),
        ommit_ping=bool(doc.ping),
    )
    return zk.connect()


def _count_unconverted(device_name):
    """Device Logs that have an employee but never became an Employee Checkin.

    After a successful fetch + sync + checkin pass this must be 0. Anything
    left means a row was dropped somewhere in the pipeline, so wiping the
    device would destroy the only other copy of that punch.
    """
    return frappe.db.sql(
        """
        SELECT COUNT(*)
        FROM `tabDevice Log` dl
        LEFT JOIN `tabEmployee Checkin` ec ON ec.device_log = dl.name
        WHERE dl.device = %s
          AND dl.employee IS NOT NULL AND dl.employee != ''
          AND ec.device_log IS NULL
        """,
        (device_name,),
    )[0][0]


@frappe.whitelist()
def get_device_storage(device_id):
    """
    Pre-flight for the "Clear Device Logs" button.

    Deliberately cheap: read_sizes() is a single CMD_GET_FREE_SIZES command,
    so this reports how many punches the device is holding WITHOUT pulling
    the log itself (which is the expensive, minutes-long part).

    Returns the numbers the confirm dialog needs to tell the user exactly
    what is about to be destroyed, or {"error": ...} if the device is
    unreachable.
    """
    doc = frappe.get_doc("ZK Device", device_id)
    conn = None
    try:
        # Short timeout on purpose: this only has to answer "how much is on
        # the device", so an unresponsive device should fail fast rather than
        # sit through the full per-read budget several times over.
        conn = _connect(doc, timeout=_PREFLIGHT_TIMEOUT)
        conn.read_sizes()
        records = conn.records
        rec_cap = conn.rec_cap
    except Exception as e:
        return {"error": str(e)}
    finally:
        if conn:
            try:
                conn.disconnect()
            except Exception:
                pass

    return {
        "records": records,
        "rec_cap": rec_cap,
        "db_logs": frappe.db.count("Device Log", {"device": doc.device_name}),
        "unmapped": frappe.db.sql(
            """
            SELECT COUNT(*) FROM `tabDevice Log`
            WHERE device = %s AND (employee IS NULL OR employee = '')
            """,
            (doc.device_name,),
        )[0][0],
        "unconverted": _count_unconverted(doc.device_name),
        "period": doc.period or 0,
        "last_log_row": str(doc.last_log_row or ""),
    }


@frappe.whitelist()
def clear_device_attendance(device_name, force=0):
    """
    Enqueue the guarded wipe of a device's on-board attendance log.

    Runs in the background because step 1 is a full fetch, which is the same
    minutes-long operation as a normal pull (see _JOB_TIMEOUT).
    """
    frappe.only_for("System Manager")
    frappe.enqueue(
        "frappe_zk_integration_app.frappe_zk_integration_app.doctype.zk_device.zk_device.clear_device_attendance_job",
        device=device_name,
        force=cint(force),
        user=frappe.session.user,
        queue="long",
        timeout=_JOB_TIMEOUT,
        job_id="zk_clear_attendance_{}".format(device_name),
        deduplicate=True,
    )


def clear_device_attendance_job(device, force=0, user=None):
    """
    Wipe the device's stored attendance log — irreversible on the hardware.

    Order matters: everything the device holds is pulled into Device Log and
    turned into Employee Checkin BEFORE anything is erased, and the wipe is
    skipped entirely if that pipeline did not come out clean.

    Note this sends CMD_CLEAR_ATTLOG (attendance only) via clear_attendance().
    It must never use clear_data(), which also erases users and fingerprint
    templates and would mean re-enrolling every employee on the device.
    """
    user = user or frappe.session.user
    doc = frappe.get_doc("ZK Device", device)

    def fail(msg):
        _reconnect_db()
        frappe.publish_realtime("zk_clear_error", {"device": device, "error": msg}, user=user)

    # ── Step 1: final fetch, so the DB holds everything the device holds ──
    doc.get_device_log(show_progress=True)
    if doc.flags.zk_fetch_failed:
        return fail(
            _("Aborted — the final fetch failed, so the device log was NOT cleared: {0}")
            .format(doc.last_connection_error)
        )

    # ── Step 2: link employees and materialise checkins ──────────────────
    try:
        doc.sync_employee()
    except Exception as e:
        return fail(_("Aborted — employee sync failed, nothing was cleared: {0}").format(e))
    _create_checkins_safely()

    # ── Step 3: refuse to wipe while any punch is only half-processed ────
    _reconnect_db()
    unconverted = _count_unconverted(doc.device_name)
    if unconverted and not cint(force):
        return fail(
            _("Aborted — {0} device log(s) have an employee but no Employee Checkin. "
              "Nothing was cleared. Resolve these first, or re-run with Force.")
            .format(unconverted)
        )

    # ── Step 4: the irreversible part ────────────────────────────────────
    conn = None
    try:
        conn = _connect(doc, timeout=_DEVICE_SOCKET_TIMEOUT)
        conn.read_sizes()
        cleared = conn.records
        conn.clear_attendance()
    except Exception as e:
        _reconnect_db()
        try:
            frappe.log_error(
                message=str(e), title=_("ZK Clear Attendance Error: {0}").format(device)
            )
        except Exception:
            pass
        return fail(str(e))
    finally:
        if conn:
            try:
                conn.enable_device()
                conn.disconnect()
            except Exception:
                pass

    # ── Step 5: audit trail ──────────────────────────────────────────────
    _reconnect_db()
    message = _("Cleared {0} attendance record(s) from the device at {1}. "
                "{2} Device Log row(s) are retained in the database.").format(
        cleared, now(), frappe.db.count("Device Log", {"device": doc.device_name})
    )
    try:
        doc.add_comment("Info", message)
        frappe.db.commit()
    except Exception:
        pass

    frappe.publish_realtime("zk_clear_done", {"device": device, "message": message}, user=user)


@frappe.whitelist()
def request_device_storage(device_name):
    """
    Enqueue the storage probe instead of running it inside the web request.

    get_device_storage() has to complete a ZK handshake before it can answer,
    and that handshake is worth tens of seconds on a slow or unreachable
    device (ping subprocess, then test_tcp's own 10s, then the session and
    auth commands). Doing that synchronously blocks a gunicorn worker and
    trips nginx's proxy_read_timeout, so the caller gets a gateway timeout
    instead of an answer. Queue it and deliver the result over realtime.
    """
    frappe.enqueue(
        "frappe_zk_integration_app.frappe_zk_integration_app.doctype.zk_device.zk_device.device_storage_job",
        device=device_name,
        user=frappe.session.user,
        queue="short",
        timeout=180,
        job_id="zk_device_storage_{}".format(device_name),
        deduplicate=True,
    )


def device_storage_job(device, user=None):
    """Probe the device and push the numbers to the waiting browser."""
    user = user or frappe.session.user
    try:
        result = get_device_storage(device)
    except Exception as e:
        _reconnect_db()
        result = {"error": str(e)}
    result["device"] = device
    frappe.publish_realtime("zk_storage_info", result, user=user)
