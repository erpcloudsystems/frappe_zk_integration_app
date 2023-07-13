from __future__ import unicode_literals
from frappe import _

def get_data():
	return [
		{
			"label": _("ZK Attendance"),
			"icon": "fa fa-star",
			"items": [
				{
					"type": "doctype",
					"name": "ZK Device",
					"description": _("ZK Device"),
					"onboard": 1,
				},
				{
					"type": "doctype",
					"name": "Device Log",
					"description": _("Device Log"),
					"onboard": 1,
				},{
					"type": "doctype",
					"name": "Employee Checkin",
					"description": _("Employee Checkin"),
					"onboard": 1,
				}
                ]
		}

	]
