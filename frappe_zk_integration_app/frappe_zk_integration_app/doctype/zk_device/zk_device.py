# Copyright (c) 2023, Creative Advanced Technologies and contributors
# For license information, please see license.txt

from __future__ import unicode_literals
import json
from datetime import datetime
import frappe
from dateutil import parser
from frappe import _
from frappe.model.document import Document
from frappe_zk_integration_app.zk import ZK
from frappe.utils import now

DATETIME_FORMAT = "%Y-%m-%d %H:%M:%S"

# Hard limit on how long a single device background job may run.
_JOB_TIMEOUT = 300  # 5 minutes


class ZKDevice(Document):
    @frappe.whitelist()
    def get_device_log(self, show_progress=False):
        conn = None
        zk = ZK(
            self.ip,
            port=self.port,
            password=self.password,
            timeout=30,                  # per-operation socket timeout (seconds)
            force_udp=self.udp or True,
            ommit_ping=self.ping or True,
        )

        try:
            conn = zk.connect()
            all_logs = conn.get_attendance() or []
            total_logs_before_filter = len(all_logs)

            # Lower bound for timestamps we care about
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

            if not logs:
                self.last_connection_error = _(
                    "No new logs found after {0}"
                ).format(start_datetime or "all time")
                return

            # ── Period filter + name generation ───────────────────────────
            period = self.period or 0
            last_log_users = {}
            candidates = []  # (name, log)

            for log in logs:
                log.status = "IN" if log.status == 1 else "OUT"
                name = "{}_{}_{}" .format(
                    log.user_id,
                    log.timestamp.strftime("%Y%m%d%H%M%S"),
                    log.punch,
                )
                last_ts = last_log_users.get(log.user_id)
                if period and last_ts:
                    if (log.timestamp - last_ts).total_seconds() / 3600 < period:
                        continue
                last_log_users[log.user_id] = log.timestamp
                candidates.append((name, log))

            if not candidates:
                return

            # ── Batch deduplication (one query per 500 names) ─────────────
            candidate_names = [n for n, _ in candidates]
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

            # ── Build list of truly new records ───────────────────────────
            ts = now()
            owner = frappe.session.user
            new_records = []
            last = None
            total = len(candidates)

            for idx, (name, log) in enumerate(candidates):
                if show_progress:
                    frappe.publish_progress(
                        (idx + 1) * 100 / total,
                        title=_("Fetching Logs for {0}...").format(self.device_name),
                    )
                if name in existing_names:
                    continue
                new_records.append((
                    name,
                    log.user_id,
                    log.timestamp,
                    str(log.timestamp.date()),
                    log.status,
                    str(log.punch) if log.punch is not None else "",
                    self.device_name,
                    ts, ts, owner, owner,
                ))
                last = log.timestamp

            # ── Bulk INSERT (one round-trip per 100 rows) ─────────────────
            inserted_count = 0
            if new_records:
                inserted_count = len(new_records)
                insert_batch = 100
                col = (
                    "INSERT IGNORE INTO `tabDevice Log` "
                    "(name, employee, enroll_no, time, `date`, type, punch, device, "
                    "creation, modified, modified_by, owner, docstatus) "
                    "VALUES "
                )
                row_placeholder = "(%s, NULL, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, 0)"
                for i in range(0, inserted_count, insert_batch):
                    batch = new_records[i : i + insert_batch]
                    flat_values = [v for row in batch for v in row]
                    frappe.db.sql(
                        col + ", ".join([row_placeholder] * len(batch)),
                        flat_values,
                    )
                frappe.db.commit()

            # Advance cursor
            if last:
                self.last_log_row = last if last <= current_datetime else current_datetime
            self.fetch_from_date = None
            self.last_connection_error = _(
                "Executed {0}: {1} on device / {2} after date filter / {3} inserted"
            ).format(now(), total_logs_before_filter, total_logs_after_filter, inserted_count)

            # Sync employee mapping only when new rows were added
            if inserted_count:
                try:
                    self.sync_employee()
                except Exception as e:
                    frappe.log_error(message=str(e), title="ZK Sync Employee Error")

        except Exception as e:
            frappe.log_error(
                message=str(e),
                title=_("ZK Device Error: {0}").format(self.device_name),
            )
            self.last_connection_error = str(e)

        finally:
            self.last_connection_time = datetime.now()
            self.save()
            if conn:
                try:
                    conn.enable_device()
                    conn.disconnect()
                except Exception:
                    pass

    def sync_employee(self):
        frappe.db.sql(
            """
            UPDATE `tabDevice Log` log
            SET log.employee = (
                SELECT name FROM tabEmployee
                WHERE attendance_device_id = log.enroll_no
                LIMIT 1
            )
            WHERE log.employee IS NULL OR log.employee = ''
            """
        )
        frappe.db.commit()


@frappe.whitelist()
def sync_employee():
    try:
        frappe.db.sql(
            """
            UPDATE `tabDevice Log` log
            SET log.employee = (
                SELECT name FROM tabEmployee
                WHERE attendance_device_id = log.enroll_no
                LIMIT 1
            )
            WHERE log.employee IS NULL OR log.employee = ''
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
    """Background worker: fetch → sync → create checkins → notify frontend."""
    doc = frappe.get_doc("ZK Device", device)
    current_user = frappe.session.user
    try:
        doc.get_device_log(show_progress=True)
        from frappe_zk_integration_app.tasks import create_employee_checkin_internal
        create_employee_checkin_internal()
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
        frappe.log_error(
            message=str(e),
            title=_("ZK Background Job Error: {0}").format(device),
        )
        frappe.publish_realtime(
            "zk_job_error",
            {"device": device, "error": str(e)},
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
            force_udp=doc.udp or True,
            ommit_ping=doc.ping or True,
        )
        conn = zk.connect()
        if conn:
            conn.disconnect()
            return "Success"
    except Exception as e:
        return str(e)
