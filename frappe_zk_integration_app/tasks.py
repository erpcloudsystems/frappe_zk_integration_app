# Copyright (c) 2023, Creative Advanced Technologies and contributors
# For license information, please see license.txt

import frappe


def create_employee_checkin_internal():
    """
    Insert Employee Checkin rows for Device Logs from the last 90 days that
    have an employee linked but no corresponding checkin record yet.

    - LEFT JOIN anti-join is faster than NOT IN (subquery) on large tables.
    - Date filter (90 days) avoids a full-table scan on tabDevice Log.
    - INNER JOIN to tabEmployee both fills employee_name at insert time
      (raw SQL bypasses the doctype's fetch_from) and guards against a
      dangling dl.employee that no longer matches a real Employee.

    Called at the end of each device background job (after sync_employee).
    Also used by the API-callable create_employee_checkin() in device_log.py.
    """
    frappe.db.sql(
        """
        INSERT INTO `tabEmployee Checkin`
            (name, employee, employee_name, time, log_type, device_log, device_id,
             creation, modified, owner)
        SELECT
            dl.name, dl.employee, emp.employee_name, dl.time, dl.type, dl.name, dl.device,
            dl.creation, dl.modified, dl.owner
        FROM `tabDevice Log` dl
        INNER JOIN `tabEmployee` emp ON emp.name = dl.employee
        LEFT JOIN `tabEmployee Checkin` ec ON ec.device_log = dl.name
        WHERE dl.employee IS NOT NULL
          AND ec.device_log IS NULL
          AND dl.date >= DATE_SUB(CURDATE(), INTERVAL 90 DAY)
        """
    )
    frappe.db.commit()
