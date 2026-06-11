# Copyright (c) 2023, Creative Advanced Technologies and contributors
# For license information, please see license.txt

import frappe


def update_employee_name_from_checkin():
    frappe.db.sql(
        """
        UPDATE `tabEmployee Checkin` log
        SET log.employee_name = (
            SELECT emp.employee_name
            FROM tabEmployee emp
            WHERE emp.name = log.employee
            LIMIT 1
        )
        """
    )


def create_employee_checkin_internal():
    """
    Insert Employee Checkin rows for every Device Log that has an employee
    linked but has not yet been turned into a checkin record.

    Uses LEFT JOIN anti-pattern which is faster than NOT IN (subquery)
    on large tables.

    Called at the end of each device background job (after sync_employee).
    Also used by the API-callable create_employee_checkin() in device_log.py.
    """
    frappe.db.sql(
        """
        INSERT INTO `tabEmployee Checkin`
            (name, employee, time, log_type, device_log, device_id,
             creation, modified, owner)
        SELECT
            dl.name, dl.employee, dl.time, dl.type, dl.name, dl.device,
            dl.creation, dl.modified, dl.owner
        FROM `tabDevice Log` dl
        LEFT JOIN `tabEmployee Checkin` ec ON ec.device_log = dl.name
        WHERE dl.employee IS NOT NULL
          AND ec.device_log IS NULL
        """
    )
    frappe.db.commit()
