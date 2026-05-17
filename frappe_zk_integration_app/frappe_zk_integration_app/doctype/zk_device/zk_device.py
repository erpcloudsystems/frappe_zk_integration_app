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
            timeout=43200,
            force_udp=self.udp or True,
            ommit_ping=self.ping or True,
        )

        try:
            # Connect to the device
            conn = zk.connect()

            # Fetch all logs initially, without filtering
            all_logs = conn.get_attendance() or []
            total_logs_before_filter = len(all_logs)

            # Determine start date time from fetch_from_date or last_log_row
            start_datetime = None
            if self.fetch_from_date:
                fetch_date = parser.parse(str(self.fetch_from_date)).date()
                start_datetime = datetime.combine(fetch_date, datetime.min.time())
            elif self.last_log_row:
                start_datetime = parser.parse(str(self.last_log_row))

            # Get current time as a datetime object for filtering
            current_datetime = datetime.now()

            # Filter logs based on start date time and current time
            logs = [
                log for log in all_logs
                if (not start_datetime or log.timestamp >= start_datetime) 
                and log.timestamp < current_datetime
            ]
            
            total_logs_after_filter = len(logs)

            # Initialize counters
            total_inserted_logs = 0
            last_log_users = {}
            period = self.period or 0
            total = len(logs)

            if not total:
                frappe.throw(_("No logs found"))

            count = 0
            last = None  # To track the last processed log timestamp

            for log in logs:
                count += 1

                # Update progress bar
                if show_progress:
                    frappe.publish_progress(
                        count * 100 / total,
                        title=_("Fetching Logs for {0}...").format(self.device_name)
                    )

                # Period filter check (skip if the log is within the restricted period)
                last_timestamp = last_log_users.get(log.user_id)
                if period and last_timestamp:
                    diff = (log.timestamp - last_timestamp).seconds / 3600
                    if diff < period:
                        continue

                # Process each log entry
                try:
                    log.status = "IN" if log.status == 1 else "OUT"

                    # Generate a unique name for the log entry
                    name = f"{log.user_id}_{log.timestamp.strftime('%Y%m%d%H%M%S')}_{log.punch}"

                    # Check if the log already exists in the database
                    existing_log = frappe.db.exists('Device Log', name)

                    if not existing_log:
                        # Create and insert new device log entry
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
                            'device': self.device_name
                        })
                        device_log.insert(ignore_permissions=True)
                        frappe.db.commit()
                        total_inserted_logs += 1

                    last_log_users[log.user_id] = log.timestamp
                except Exception as e:
                    # Log error for any issues encountered with a specific log
                    frappe.log_error(message=str(e), title=_("Log Insertion Error"))

                last = log.timestamp  # Update last timestamp processed

            # After all logs are processed, update the last_log_row to the latest timestamp
            if last:
                if last > datetime.now():
                    self.last_log_row = now()  # Set to current time if last is in the future
                else:
                    self.last_log_row = last

            # Clear the manual fetch date so normal cursor resumes next time
            self.fetch_from_date = None

            # Capture the execution date and time for logging
            execution_datetime = now()

            # Log the processing summary in the last_connection_error field
            self.last_connection_error = _(
                "Log Processing Summary (Executed on {0}):\n"
                "Total logs before filtering: {1}.\n"
                "Total logs after filtering by start date: {2}.\n"
                "Total logs inserted into the system: {3}."
            ).format(execution_datetime, total_logs_before_filter, total_logs_after_filter, total_inserted_logs)

            # Re-enable the device connection
            conn.enable_device()

        except Exception as e:
            # Handle errors in the overall process
            frappe.msgprint(_("Process terminated: {}").format(e), indicator="red")
            self.last_connection_error = str(e)

        finally:
            # Ensure the last connection time is updated and device is disconnected
            self.last_connection_time = datetime.now()
            self.save()
            self.sync_employee()

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
        
@frappe.whitelist()
def send_specific_device_log(device_name):
    # Enqueue the job with the specific device name
    frappe.enqueue(
        'frappe_zk_integration_app.frappe_zk_integration_app.doctype.zk_device.zk_device.device_log_background_job',
        device=device_name,
        queue='long',
        timeout=150000
    )

@frappe.whitelist()
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