# Copyright (c) 2023, Creative Advanced Technologies and contributors
# For license information, please see license.txt

from __future__ import unicode_literals
import json
from datetime import datetime, timedelta
import frappe
from dateutil import parser
from frappe import _
from frappe.model.document import Document
from frappe_zk_integration_app.zk import ZK
from frappe.utils import now
import secrets
DATETIME_FORMAT = "%Y-%m-%d %H:%M:%S"


class ZKDevice(Document):
    @frappe.whitelist()
    def get_device_log(self, show_progress=False):
        conn = None
        zk = ZK(
            self.ip,
            port=self.port,
            password=self.password,
            timeout=20,
            force_udp=self.udp or True,
            ommit_ping=self.ping or True,
        )

        try:
            conn = zk.connect()
            logs = conn.get_attendance() or []
            last_log_users = {}
            period = self.period or 0
            total = len(logs)

            if not total:
                frappe.throw(_("No logs found"))

            # Handle last log row, ensuring it's properly parsed
            if self.last_log_row:
                self.last_log_row = parser.parse(str(self.last_log_row))

            last = self.last_log_row
            count = 0

            for log in logs:
                count += 1

                # Update progress bar
                if show_progress:
                    frappe.publish_progress(
                        count * 100 / total,
                        title=_("Fetching Logs...")
                    )

                # Skip logs before last log row
                if self.last_log_row and (log.timestamp < self.last_log_row):
                    continue

                # Check for period filtering (e.g., only logs after the last log for the user)
                last_timestamp = last_log_users.get(log.user_id)
                if period and last_timestamp:
                    diff = (log.timestamp - last_timestamp).seconds / 3600
                    if diff < period:
                        continue

                try:
                    log.status = "IN" if log.status == 1 else "OUT"

                    # Generate a unique name for the log entry
                    name = f"{log.user_id}_{log.timestamp.strftime('%Y%m%d%H%M%S')}_{log.punch}"

                    # Check if the log already exists in the database using Frappe ORM
                    existing_log = frappe.db.exists('Device Log', name)

                    if not existing_log:
                        # Create a new device log entry using Frappe ORM
                        device_log = frappe.get_doc({
                            'doctype': 'Device Log',
                            'name': name,
                            'employee': None,  # Set the employee if needed
                            'enroll_no': log.user_id,
                            'time': log.timestamp,
                            'date': log.timestamp.date(),
                            'type': log.status,
                            'punch': log.punch,
                            'creation': now(),
                            'modified': now(),
                            'owner': frappe.session.user,
                            'device': self.name  # Assuming `self.name` refers to the device
                        })
                        device_log.insert(ignore_permissions=True)  # Insert without permissions check

                    last_log_users[log.user_id] = log.timestamp

                except Exception as e:
                    # Log the error with specific details
                    frappe.log_error(message=str(e), title=_("Log Insertion Error"))

                last = log.timestamp

            # Update the last log row timestamp to the latest processed log
            if last:
                self.last_log_row = min(last, datetime.now())

            # Reload the instance to reflect changes
            self.reload()

            # Re-enable the device connection
            conn.enable_device()

        except Exception as e:
            # Display error message and save error details in the instance
            frappe.msgprint(_("Process terminated: {}").format(e), indicator="red")
            self.last_connection_error = str(e)

        finally:
            # Always update the last connection time and ensure the device is disconnected
            self.last_connection_time = datetime.now()
            if conn:
                conn.enable_device()
                conn.disconnect()

    def sync_employee(self):
        try:
            frappe.db.sql(
                """
                UPDATE `tabDevice Log` log
                SET log.employee = (
                    SELECT name FROM tabEmployee WHERE attendance_device_id = log.enroll_no LIMIT 1
                )
                WHERE log.employee IS NULL OR log.employee = ''
                """
            )
            frappe.db.commit()
            frappe.msgprint(_("Employee synchronization complete"))
        except Exception as e:
            frappe.log_error(message=str(e), title=_("Employee Synchronization Error"))


@frappe.whitelist()
def sync_employee():
    try:
        frappe.db.sql(
            """
            UPDATE `tabDevice Log` log
            SET log.employee = (
                SELECT name FROM tabEmployee WHERE attendance_device_id = log.enroll_no LIMIT 1
            )
            WHERE log.employee IS NULL OR log.employee = ''
            """
        )
        frappe.db.commit()
        frappe.msgprint(_("Employee synchronization complete"))
    except Exception as e:
        frappe.log_error(message=str(e), title=_("Employee Synchronization Error"))


@frappe.whitelist()
def get_active_device_logs(names=None):
    if names:
        names = json.loads(str(names))
    cur_time = datetime.now()
    
    # Get the list of devices to process
    devices = names or frappe.db.sql_list(
        """
        SELECT name FROM `tabZK Device`
        WHERE docstatus < 2 AND auto_attendance = 1
        """
    )

    for device in devices:
        # Enqueue the job to run in the background with long timeout
        frappe.enqueue(
            'frappe_zk_integration_app.frappe_zk_integration_app.doctype.zk_device.zk_device.device_log_background_job',
            device=device,
            queue='long',
            timeout=150000
        )

def device_log_background_job(device):
    """Background job to get device log for a specific device."""
    doc = frappe.get_doc("ZK Device", device)
    try:
        # Call the function to get device logs in the background
        doc.get_device_log(show_progress=True)
    except Exception as e:
        # Log any errors encountered during the process
        frappe.msgprint(_("Process terminated for device {}: {}").format(device, e), indicator="red")

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
            return "Success"
    except Exception as e:
        return str(e)