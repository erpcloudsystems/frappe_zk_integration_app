// Copyright (c) 2023, Creative Advanced Technologies and contributors
// For license information, please see license.txt
/* eslint-disable */
var today = new Date();
var lastDay = new Date(today.getFullYear(), today.getMonth() + 1, 0);

frappe.query_reports["Monthly Attendance"] = {
	"filters": [
		{
            "fieldname": "from_date",
            "label": "From Date",
            "fieldtype": "Date",
			"default": new Date(new Date().getFullYear(), new Date().getMonth(), 1),  // Sets default to first day of the current month
            "width": 80
        },
        {
            "fieldname": "to_date",
            "label": "To Date",
            "fieldtype": "Date",
			"default": lastDay,
            "width": 80
        },
        {
            "fieldname": "employee",
            "label": "Employee",
            "fieldtype": "Link",
            "options": "Employee",
            "default": "",
            "width": 80
        },
        {
            "fieldname": "department",
            "label": "Department",
            "fieldtype": "Link",
            "options": "Department",
            "default": "",
            "width": 80
        },
        {
            "fieldname": "branch",
            "label": "Branch",
            "fieldtype": "Link",
            "options": "Branch",
            "default": "",
            "width": 80
        }
	]
};

