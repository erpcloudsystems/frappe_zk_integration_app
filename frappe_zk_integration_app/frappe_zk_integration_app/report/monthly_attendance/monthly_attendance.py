import frappe
from datetime import datetime, timedelta

def execute(filters=None):
    # Initialize columns
    columns = [
        {"label": "Employee ID", "fieldname": "employee_id", "fieldtype": "Data", "width": 100},
        {"label": "Employee Name", "fieldname": "employee_name", "fieldtype": "Data", "width": 150},
        {"label": "Department", "fieldname": "department", "fieldtype": "Data", "width": 120},
        {"label": "Branch", "fieldname": "branch", "fieldtype": "Data", "width": 120},
        {"label": "Daily Hours", "fieldname": "total_daily_hours", "fieldtype": "Data", "width": 120}
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
        month_number = current_date.month
        day_name = current_date.strftime("%A")

        # Append columns for "Day {date}-in," "Day {date}-out," and "Day {date}-hours"
        columns.append({"label": f"{day_name}-{day_number}-{month_number}", "fieldname": f"day_{day_number}_in", "fieldtype": "Data", "width": 130})
        columns.append({"label": f"{day_name}-{day_number}-{month_number}", "fieldname": f"day_{day_number}_out", "fieldtype": "Data", "width": 130})
        columns.append({"label": f"{day_name}-{day_number}-{month_number}", "fieldname": f"day_{day_number}_hours", "fieldtype": "Data", "width": 130})
        columns.append({"label": f"{day_name}-{day_number}-{month_number}", "fieldname": f"day_{day_number}_ovl", "fieldtype": "Data", "width": 130})

        # Move to the next date
        current_date += timedelta(days=1)

    # Get employee logs
    data = get_employee_logs(from_date, to_date, filters)

    return columns, data

def get_employee_logs(from_date, to_date, filters):
    # Initialize an empty list to store the results
    result = []
    attendance2 = {}
    current_date = from_date
    while current_date <= to_date:
        date_str = current_date.strftime("%Y-%m-%d")
        day_number = current_date.day

        attendance2[f"day_{day_number}_in"] = "<b><center>From</center></b>"
        attendance2[f"day_{day_number}_out"] = "<b><center>To</center></b>"
        attendance2[f"day_{day_number}_hours"] = "<b><center>Working Hours</center></b>"
        attendance2[f"day_{day_number}_ovl"] = "<b><center>Overtime - Delay</center></b>"


        current_date += timedelta(days=1)


    empty_attendance = {
        "employee_id": "",
        "employee_name": "",
        "department": "",
        "branch": "",
        "total_daily_hours": "<b><center>Hours as Shift Type</center></b>",
         **attendance2
    }

    # Insert the empty attendance record at the beginning of the result list
    result.append(empty_attendance)

    # Define the filters for employees if specified
    conditions = ""
    params = {}

    if filters.get("employee"):
        conditions += " and dl.employee = %(employee)s"

    if filters.get("department"):
        conditions += " and em.department = %(department)s"

    if filters.get("branch"):
        conditions += " and em.branch = %(branch)s"

    # Construct the SQL query
    sql_query = f"""
        SELECT
            dl.employee as employee_id,
            em.employee_name as employee_name,
            em.department as department,
            em.branch as branch,
            TIMEDIFF(st.end_time, st.start_time) AS total_daily_hours
        FROM
            `tabDevice Log` dl
            JOIN
            `tabEmployee` em ON em.name = dl.employee
            LEFT JOIN
            `tabShift Type` st ON em.default_shift = st.name
        WHERE
            dl.date BETWEEN %(from_date)s AND %(to_date)s
            {conditions}
        GROUP BY
            dl.employee
    """

    # Execute the query
    employees = frappe.db.sql(sql_query, {"from_date": from_date, "to_date": to_date, **filters}, as_dict=True)


    # Loop through each employee
    for employee in employees:
        employee_id = employee.get("employee_id")
        employee_name = employee.get("employee_name")
        department = employee.get("department")
        branch = employee.get("branch")
        total_daily_hours = employee.get("total_daily_hours")
        if total_daily_hours is None or total_daily_hours == 'None':
            total_daily_hours = "08:00:00"


        # Initialize a dictionary to store attendance for each date
        attendance = {}

        # Loop through each date in the date range
        current_date = from_date
        while current_date <= to_date:
            date_str = current_date.strftime("%Y-%m-%d")
            day_number = current_date.day
            in_time, out_time, working_on_day, time_diff_wor_shift_daily = get_attendance_for_day(employee_id, date_str,total_daily_hours)

            attendance[f"day_{day_number}_in"] = in_time
            attendance[f"day_{day_number}_out"] = out_time
            attendance[f"day_{day_number}_hours"] = working_on_day
            attendance[f"day_{day_number}_ovl"] = time_diff_wor_shift_daily

            # Move to the next date
            current_date += timedelta(days=1)

        # Append the employee's attendance to the result list
        result.append({
            "employee_id": employee_id,
            "employee_name": employee_name,
            "department": department,
            "branch": branch,
            "total_daily_hours": total_daily_hours,
            **attendance
        })

    return result

def get_attendance_for_day(employee_id, date_str, total_daily_hours):
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
    total_shift_hours_str = str(total_daily_hours)

    if total_shift_hours_str is None or total_shift_hours_str == 'None':
        total_shift_hours_str = "08:00:00"
    else:
        total_shift_hours_str = total_shift_hours_str

    in_time_str = str(in_time)
    out_time_str = str(out_time)

    in_time_obj = datetime.strptime(in_time_str, "%H:%M:%S")
    out_time_obj = datetime.strptime(out_time_str, "%H:%M:%S")
    total_shift_hours_obj = datetime.strptime(total_shift_hours_str, "%H:%M:%S")

    working_on_day = out_time_obj - in_time_obj
    working_on_day_str = str(working_on_day)
    working_on_day_obj =  datetime.strptime(working_on_day_str, "%H:%M:%S")
    time_diff_wor_shift_daily = working_on_day_obj - total_shift_hours_obj
    on_leave = is_employee_on_leave(employee_id, date_str)
    if on_leave:
        working_on_day = "<span style='color: green;'>On Leave</span>"
        time_diff_wor_shift_daily = "<span style='color: green;'>On Leave</span>"
        in_time = "<span style='color: green;'>On Leave</span>"
        out_time = "<span style='color: green;'>On Leave</span>"
    elif in_time == "00:00:00" and out_time == "00:00:00":
        working_on_day = "<span style='color: red;'>Absent</span>"
        time_diff_wor_shift_daily = "<span style='color: red;'>Absent</span>"
        in_time = "<span style='color: red;'>Absent</span>"
        out_time = "<span style='color: red;'>Absent</span>"

    # Convert the timedelta to a string in the format HH:MM:SS

    return in_time, out_time, working_on_day, time_diff_wor_shift_daily

def is_employee_on_leave(employee_id, date_str):
    sql_query = """
        SELECT name
        FROM `tabLeave Application`
        WHERE
            employee = %(employee_id)s
            AND %(date_str)s BETWEEN from_date AND to_date
            AND status != 'Cancelled'
    """

    leave_application_exists = frappe.db.sql(sql_query, {"employee_id": employee_id, "date_str": date_str})

    return leave_application_exists
