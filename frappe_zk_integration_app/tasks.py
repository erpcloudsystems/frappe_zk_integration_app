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
            );
        """
    )
