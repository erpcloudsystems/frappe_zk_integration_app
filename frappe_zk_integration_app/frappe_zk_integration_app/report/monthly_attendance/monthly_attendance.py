import frappe
from datetime import datetime, timedelta

def execute(filters=None):
    # Initialize columns
    columns = [
        {"label": "Employee ID", "fieldname": "employee_id", "fieldtype": "Data", "width": 100},
        {"label": "Employee Name", "fieldname": "employee_name", "fieldtype": "Data", "width": 150},
        {"label": "Department", "fieldname": "department", "fieldtype": "Data", "width": 120},
        {"label": "Branch", "fieldname": "branch", "fieldtype": "Data", "width": 120}
    ]

    # Get the date range from filters
    from_date = datetime.strptime(filters.get("from_date"), "%Y-%m-%d")
    to_date = datetime.strptime(filters.get("to_date"), "%Y-%m-%d")

    # Loop through each date in the date range
    current_date = from_date
    while current_date <= to_date:
        # date_str = current_date.strftime("%Y-%m-%d")
        date_str = str(current_date)
        day_number = current_date.day
        day_name = current_date.strftime("%A")

        # Append columns for "Day {date}-in," "Day {date}-out," and "Day {date}-hours"
        columns.append({"label": f"{day_name}-{day_number}-IN", "fieldname": f"day_{day_number}_in", "fieldtype": "Data", "width": 130})
        columns.append({"label": f"{day_name}-{day_number}-OUT", "fieldname": f"day_{day_number}_out", "fieldtype": "Data", "width": 130})
        columns.append({"label": f"{day_name}-{day_number}-H", "fieldname": f"day_{day_number}_hours", "fieldtype": "Data", "width": 130})

        # Move to the next date
        current_date += timedelta(days=1)

    # Get employee logs
    data = get_employee_logs(from_date, to_date, filters)

    return columns, data

def get_employee_logs(from_date, to_date, filters):
    # Initialize an empty list to store the results
    result = []

    # Define the filters for employees if specified
    conditions = ""
    params = {}

    if filters.get("employee"):
        conditions += " and `tabEmployee`.name = %(employee)s"

    if filters.get("department"):
        conditions += " and `tabEmployee`.department = %(department)s"

    if filters.get("branch"):
        conditions += " and `tabEmployee`.branch = %(branch)s"

    # Construct the SQL query
    employees = frappe.db.sql(f"""
        SELECT name as employee_id, employee_name, department, branch
        FROM `tabEmployee`
        WHERE  status = 'Active'
       {conditions} """.format(conditions=conditions), filters, as_dict=1)


    # Loop through each employee
    for employee in employees:
        employee_id = employee.get("employee_id")
        employee_name = employee.get("employee_name")
        department = employee.get("department")
        branch = employee.get("branch")

        # Initialize a dictionary to store attendance for each date
        attendance = {}

        # Loop through each date in the date range
        current_date = from_date
        while current_date <= to_date:
            date_str = current_date.strftime("%Y-%m-%d")
            day_number = current_date.day

            in_time, out_time, time_difference = get_attendance_for_day(employee_id, date_str)

            attendance[f"day_{day_number}_in"] = in_time
            attendance[f"day_{day_number}_out"] = out_time
            attendance[f"day_{day_number}_hours"] = time_difference

            # Move to the next date
            current_date += timedelta(days=1)

        # Append the employee's attendance to the result list
        result.append({
            "employee_id": employee_id,
            "employee_name": employee_name,
            "department": department,
            "branch": branch,
            **attendance
        })

    return result

def get_attendance_for_day(employee_id, date_str):
    sql_query = f"""
        SELECT
            TIME(MIN(time)) as in_time,
            TIME(MAX(time)) as out_time

        FROM
            `tabDevice Log`
        WHERE
            employee = '{employee_id}'
            AND date = '{date_str}'
    """

    attendance_data = frappe.db.sql(sql_query, as_dict=True)

    in_time = attendance_data[0].in_time if attendance_data and attendance_data[0].in_time else "00:00:00"
    out_time = attendance_data[0].out_time if attendance_data and attendance_data[0].out_time else "00:00:00"

    in_time_str = str(in_time)
    out_time_str = str(out_time)

    in_time_obj = datetime.strptime(in_time_str, "%H:%M:%S")
    out_time_obj = datetime.strptime(out_time_str, "%H:%M:%S")

    time_difference = out_time_obj - in_time_obj

    return in_time_str, out_time_str, str(time_difference)