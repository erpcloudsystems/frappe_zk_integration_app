# Copyright (c) 2023, Creative Advanced Technologies and contributors
# For license information, please see license.txt

from __future__ import unicode_literals

import frappe
from frappe import _
from frappe.model.document import Document
from frappe_zk_integration_app.frappe_zk_integration_app.doctype.zk_device.zk_device import (
    get_active_device_logs,
    sync_employee,
)


class DeviceLog(Document):
    pass


@frappe.whitelist()
def create_employee_checkin(names=None):
    sync_employee()
    sql = """
		INSERT INTO `tabEmployee Checkin` (name, employee, time, log_type, device_log, device_id, creation, modified, owner)
		SELECT name, employee, time, type, name, device, creation, modified, owner
		FROM `tabDevice Log`
		WHERE employee IS NOT NULL
		AND name NOT IN (SELECT device_log FROM `tabEmployee Checkin` WHERE device_log IS NOT NULL);
	"""

    frappe.db.sql(sql)
    frappe.db.commit()


def execute(names=None):
    try:
        get_active_device_logs()
    except:
        pass

    try:
        sync_employee()
    except:
        pass

    try:
        create_employee_checkin()
    except:
        pass
