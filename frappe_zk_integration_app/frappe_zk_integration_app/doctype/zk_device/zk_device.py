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
            count = 1
            total = len(logs)
            if not total:
                frappe.throw(_("Empty Logs"))

            if self.last_log_row:
                self.last_log_row = parser.parse(str(self.last_log_row))
            last = self.last_log_row

            for log in logs:
                
                # if show_progress:
                #     frappe.publish_progress(
                #         count * 100 / total, title=_("Getting Logs...")
                #     )
                count += 1

                if self.last_log_row and (log.timestamp < self.last_log_row):
                    continue

                last_timestamp = last_log_users.get(log.user_id)
                if period and last_timestamp:
                    diff = (log.timestamp - last_timestamp).seconds / 3600
                    if diff < period:
                        continue

                try:
                    log.status = "IN" if log.status == 1 else "OUT"
                    # Generate the `name` using the desired format
                    name = f"{log.user_id}_{log.timestamp.strftime('%Y%m%d%H%M%S')}_{log.punch}"

                    # Define the SQL query
                    sql = """
                        INSERT INTO `tabDevice Log`
                            (name, employee, enroll_no, time, date, type, punch, creation, modified, owner, device)
                        VALUES
                            (%s, NULL, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                        ON DUPLICATE KEY UPDATE
                            employee = VALUES(employee),
                            enroll_no = VALUES(enroll_no),
                            time = VALUES(time),
                            date = VALUES(date),
                            type = VALUES(type),
                            punch = VALUES(punch),
                            modified = VALUES(modified),
                            owner = VALUES(owner),
                            device = VALUES(device)
                    """

                    # Define the values to be used in the query
                    values = (
                        name,                
                        log.user_id,         
                        log.timestamp,       
                        log.timestamp.date(),
                        log.status,          
                        log.punch,           
                        frappe.utils.now(),  
                        frappe.utils.now(),  
                        frappe.session.user, 
                        self.name            
                    )

                    # Execute the query
                    frappe.db.sql(sql, values)
                    frappe.db.commit()  

                    last_log_users[log.user_id] = log.timestamp

                except Exception as e:
                    # Log error with details
                    frappe.log_error(message=str(e), title=_("Log Insertion Error"))

                last = log.timestamp

            if last:
                self.last_log_row = min(last, datetime.now())
            self.reload()
            conn.enable_device()
        except Exception as e:
            frappe.msgprint(_("Process terminated: {}").format(e), indicator="red")
            self.last_connection_error = str(e)
        finally:
            self.last_connection_time = datetime.now()
            if conn:
                conn.enable_device()
                conn.disconnect()

        self.get_after_mins = self.get_after_mins or 5
        self.excecution_time = datetime.now() + timedelta(minutes=self.get_after_mins)
        self.save()
        self.sync_employee()

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
    devices = names or frappe.db.sql_list(
        """
        SELECT name FROM `tabZK Device`
        WHERE docstatus < 2 AND auto_attendance = 1
        """
    )

    for device in devices:
        doc = frappe.get_doc("ZK Device", device)
        try:
            doc.get_device_log(show_progress=True)
        except Exception as e:
            frappe.msgprint(_("Process terminated: {}").format(e), indicator="red")


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