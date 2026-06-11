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
    """API-callable wrapper: sync employees then create checkin records."""
    sync_employee()
    from frappe_zk_integration_app.tasks import create_employee_checkin_internal
    create_employee_checkin_internal()


def execute(names=None):
    """Cron entry point — only enqueues background jobs, never blocks the scheduler."""
    try:
        get_active_device_logs(names=names)
    except Exception as e:
        frappe.log_error(message=str(e), title="ZK Scheduler Error")
