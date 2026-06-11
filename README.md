# Frappe ZK Integration App

Connect ZKTeco fingerprint / face / card attendance devices to Frappe/ERPNext.  
The app pulls punch records from every active device every 5 minutes, maps them to employees, and creates `Employee Checkin` documents automatically.

---

## Requirements

| Requirement | Version |
|---|---|
| Frappe | v13 / v14 / v15 |
| Python | 3.8+ |
| ZKTeco device | Any model supported by the ZK protocol (TCP or UDP) |

---

## Installation

```bash
# From your bench directory
bench get-app https://github.com/<your-org>/frappe_zk_integration_app
bench --site <site-name> install-app frappe_zk_integration_app
bench --site <site-name> migrate
bench restart
```

---

## Setup

### 1 — Configure ZK Device

Go to **ZK Device** list and create one record per physical device.

| Field | Description |
|---|---|
| Device Name | Unique label (used as `device_id` in checkins) |
| IP Address | LAN IP of the device (e.g. `192.168.1.201`) |
| Port | Default `4370` |
| Password | Device password — leave `0` if none |
| Auto Attendance | Check to include this device in the automatic 5-minute pull |
| Period (mins) | Minimum gap between two accepted punches for the same user. Set `0` to keep every punch. |
| Fetch From Date | One-time override: pull all records from this date onwards on the next run (cleared automatically after use) |

### 2 — Map Employees to Device Enrolment Numbers

On each **Employee** record set the field **Attendance Device ID** to the enrolment number that is stored on the ZK device for that person.  
The sync step matches `tabDevice Log.enroll_no` → `tabEmployee.attendance_device_id`.

### 3 — Test the Connection

Open a ZK Device record and click **Check Connection**.  
A green "Success" message confirms the device is reachable.

### 4 — First Pull (optional back-fill)

To pull historical records:

1. Set **Fetch From Date** on the ZK Device to the earliest date you need.
2. Click **Get Device Log** (or wait for the next 5-minute cron tick).
3. The field is cleared automatically once the pull completes.

---

## How It Works

```
Scheduler (every 5 min)
  └─ execute()
       └─ get_active_device_logs()          # enqueues one RQ job per device
            └─ device_log_background_job()  # runs in the "long" worker queue
                 ├─ ZKDevice.get_device_log()
                 │    ├─ connect to device (30 s socket timeout)
                 │    ├─ fetch all attendance records
                 │    ├─ filter by last_log_row / fetch_from_date
                 │    ├─ apply per-user period filter
                 │    ├─ batch dedup check (single SQL query per 500 names)
                 │    ├─ insert new Device Log records
                 │    ├─ single frappe.db.commit()
                 │    └─ sync_employee()  (enroll_no → Employee)
                 └─ create_employee_checkin_internal()
                      └─ INSERT Employee Checkin from unprocessed Device Logs

Hourly
  └─ update_employee_name_from_checkin()   # denormalise employee_name
```

Each background job has a **5-minute hard timeout**. If a device is unreachable or stalls, only that device's job is aborted — all other devices and all other Frappe queue jobs continue normally.

Duplicate job prevention: if a job for a device is already queued or running when the next 5-minute tick fires, a second job is **not** added.

---

## Doctypes

| Doctype | Purpose |
|---|---|
| **ZK Device** | Configuration + connection state for one physical device |
| **Device Log** | Raw punch record as received from the device |
| **Employee Checkin** (core Frappe HR) | Normalised checkin used for attendance processing |

---

## Reports

### Monthly Attendance

**Path:** Reports → Monthly Attendance

Filters: Company, Branch, Department, date range.

Columns: one pair of IN/OUT columns per calendar day, plus working hours, variance vs shift, and late time.

---

## Whitelisted API Endpoints

All endpoints accept `frappe.call` from the browser or the Frappe REST API.

| Endpoint | Parameters | Description |
|---|---|---|
| `zk_device.get_device_log` | *(called on a ZK Device doc)* | Pull logs for this device now |
| `zk_device.check_connection` | `device_id` | Test TCP/UDP reachability |
| `zk_device.get_active_device_logs` | `names` (optional JSON list) | Enqueue background pull for all or selected devices |
| `zk_device.send_specific_device_log` | `device_name` | Enqueue background pull for one device |
| `zk_device.sync_employee` | — | Re-run enrol-no → Employee mapping |
| `device_log.create_employee_checkin` | — | Sync employees then create any missing checkins |

---

## Troubleshooting

| Symptom | Likely cause | Fix |
|---|---|---|
| No Device Logs created | Device unreachable | Check IP/port, firewall, and use **Check Connection** |
| Device Logs exist but no Employee Checkins | `attendance_device_id` not set on Employee | Set the field and run **Sync Employee** |
| `InterfaceError (0, '')` in RQ logs | DB connection dropped during a very long job | Fixed in current version (job timeout is now 5 min, socket timeout 30 s) |
| Duplicate checkins | `device_log` FK not set | The INSERT query guards against this; check if checkins were created outside this app |
| `last_log_row` keeps resetting | `fetch_from_date` set on device | Clear **Fetch From Date** on the ZK Device record |

---

## License

MIT
