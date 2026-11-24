# Copyright (c) 2026, Creative Advanced Technologies and contributors
# For license information, please see license.txt

import frappe
from frappe.model.document import Document


class ZKSettings(Document):
	pass


def get_adms_time_offset_hours():
	"""Hours to add to every ADMS punch; 0 when unset or unreadable."""
	try:
		return frappe.utils.flt(frappe.db.get_single_value("ZK Settings", "adms_time_offset_hours"))
	except Exception:
		# Before `bench migrate` has created the doctype, push must keep working.
		return 0.0
