# Copyright (c) 2026, Creative Advanced Technologies and contributors
# For license information, please see license.txt

"""
ADMS / iclock push endpoints.

The polling integration in zk_device.py works the other way round: Frappe
opens a socket to the device and asks for the whole stored log, because the
ZK protocol's CMD_ATTLOG_RRQ has no date range. That means every pull
transfers the device's entire memory, which is what makes a large device
slow enough to hit the background job timeout.

ADMS inverts it. The device is configured with a server URL ("Comm ->
Cloud Server Setting" on most ZKTeco firmware) and pushes each punch over
HTTP as it happens. Nothing is polled, no scheduler is involved, and the
connection is outbound from the device — so the device may sit on a network
Frappe cannot reach.

Routing: devices request fixed paths like /iclock/cdata, which Frappe's
website router does not map to methods. A `page_renderer` hook (see
hooks.py) claims those paths instead. frappe/app.py dispatches GET, HEAD
and POST alike through the path resolver, so both the handshake and the
uploads land here.

Responses are bare text/plain — the firmware parses them literally and will
retry or go idle if it gets HTML or JSON.
"""

import frappe
from frappe.utils import cint, get_datetime, now
from frappe.website.page_renderers.base_renderer import BaseRenderer
from werkzeug.wrappers import Response

# Paths the firmware talks to. Anything else falls through to Frappe's own
# renderers so this hook cannot shadow the rest of the site.
ICLOCK_PATHS = (
    "iclock/cdata",
    "iclock/getrequest",
    "iclock/devicecmd",
    "iclock/ping",
)

# ATTLOG columns, tab separated:
#   PIN  DateTime  Status  Verify  WorkCode  Reserved...
# "Status" is the punch state the device recorded (0 in, 1 out, 2/3 break,
# 4/5 overtime) and "Verify" is how the person was identified (fingerprint,
# face, card). Only the first three are needed here.
_ATTLOG_MIN_COLUMNS = 2


def _text(body, status=200):
    """Bare text/plain reply — the only thing the firmware understands."""
    return Response(body, status=status, content_type="text/plain; charset=utf-8")


def _log(title, message):
    try:
        frappe.log_error(message=message, title=title)
    except Exception:
        pass


class IClockRenderer(BaseRenderer):
    """Page renderer that serves the ADMS endpoints."""

    def can_render(self):
        return self.path in ICLOCK_PATHS

    def render(self):
        sn = (frappe.form_dict.get("SN") or "").strip()
        try:
            if self.path == "iclock/cdata":
                return handle_cdata(sn)
            if self.path == "iclock/getrequest":
                return handle_getrequest(sn)
            # devicecmd (command results) and ping need an acknowledgement and
            # nothing more — replying anything else makes the device retry.
            return _text("OK")
        except Exception as e:
            _log(_title("ZK ADMS Error", sn), frappe.get_traceback() or str(e))
            # Deliberately 200 with an error body: a 5xx makes some firmware
            # drop the punch it was holding instead of retrying it.
            return _text("OK")


def _title(prefix, sn):
    return "{0}: {1}".format(prefix, sn or "unknown SN")


def handle_cdata(sn):
    """GET = handshake and config. POST = an upload of one of the tables."""
    if not sn:
        return _text("OK")

    if frappe.request.method == "POST":
        table = (frappe.form_dict.get("table") or "").upper()
        body = frappe.request.get_data(as_text=True) or ""
        if table == "ATTLOG":
            return handle_attlog(sn, body)
        # OPERLOG (user edits, door events), and the options/BIODATA tables
        # some firmware sends, are acknowledged and dropped.
        return _text("OK")

    return _text(build_handshake(sn))


def build_handshake(sn):
    """
    Config block the device reads on every handshake.

    Realtime=1 is the point of the whole exercise — it tells the device to
    POST each punch as it happens instead of batching until TransTimes.

    Stamp is the device's own bookmark for what it has already uploaded. It
    is echoed back from what the device last reported, so a device that
    reconnects does not re-send its whole buffer; on first contact it is 0,
    which makes the device upload what it is holding once (duplicates are
    dropped on insert).
    """
    device = get_or_create_device(sn)
    stamp = (device.push_stamp or "0") if device else "0"
    return "\n".join(
        [
            "GET OPTION FROM: {0}".format(sn),
            "Stamp={0}".format(stamp),
            "OpStamp={0}".format(stamp),
            "ErrorDelay=30",
            "Delay=10",
            "TransTimes=00:00;12:00",
            "TransInterval=1",
            # Bit 1 enables attendance transfer; the rest of the tables are
            # left off so the device does not push data nothing here reads.
            "TransFlag=1000000000",
            "Realtime=1",
            "Encrypt=0",
        ]
    ) + "\n"


def handle_getrequest(sn):
    """
    The device polls this for pending commands. Returning a bare OK means
    "nothing for you"; issuing commands (user sync, door unlock) would be
    answered here and is not implemented.
    """
    return _text("OK")


def handle_attlog(sn, body):
    """
    Store an ATTLOG upload.

    Replies "OK: <n>" with the number of lines accepted, which is the
    acknowledgement the firmware waits for before dropping those records
    from its own buffer.
    """
    device = get_or_create_device(sn)
    if not device:
        _log(_title("ZK ADMS Unregistered Device", sn), "Could not resolve or create a device for SN {0}".format(sn))
        return _text("OK")

    punches = parse_attlog(body)
    if punches:
        create_device_logs(device, punches)

    stamp = (frappe.form_dict.get("Stamp") or "").strip()
    update_push_state(device, stamp)

    return _text("OK: {0}".format(len(punches)))


def parse_attlog(body):
    """
    Parse the tab separated upload into (enroll_no, datetime, punch) tuples.

    Unparseable lines are skipped rather than failing the whole upload — one
    malformed row must not cost the device the rest of its buffer.
    """
    punches = []
    for line in (body or "").replace("\r", "\n").split("\n"):
        line = line.strip()
        if not line:
            continue
        parts = line.split("\t")
        if len(parts) < _ATTLOG_MIN_COLUMNS:
            continue
        enroll_no = parts[0].strip()
        raw_time = parts[1].strip()
        if not enroll_no or not raw_time:
            continue
        try:
            timestamp = get_datetime(raw_time)
        except Exception:
            continue
        if not timestamp:
            continue
        punch = parts[2].strip() if len(parts) > 2 else ""
        punches.append((enroll_no, timestamp, punch))
    return punches


def get_or_create_device(sn):
    """
    Resolve a serial number to a ZK Device, creating one if it is unknown.

    Lookup order: the serial_number field (so a device someone created by
    hand with a friendly name still matches), then the record name (which is
    the device_name, since ZK Device is autonamed field:device_name).

    Auto-created records are left with auto_attendance = 0: a pushing device
    does not need to be polled, and enabling both would have the scheduler
    dragging the device's whole memory over the wire for no benefit.
    """
    name = frappe.db.get_value("ZK Device", {"serial_number": sn}, "name")
    if not name and frappe.db.exists("ZK Device", sn):
        name = sn

    if name:
        device = frappe.get_doc("ZK Device", name)
        # Backfill the SN on a record that was created by hand before the
        # first push arrived, so later lookups hit the indexed field.
        if not device.serial_number:
            frappe.db.set_value("ZK Device", name, "serial_number", sn, update_modified=False)
            device.serial_number = sn
        return device

    try:
        device = frappe.get_doc(
            {
                "doctype": "ZK Device",
                "device_name": sn,
                "serial_number": sn,
                # The device told us where it is by connecting; ip/port/password
                # are only used by the polling path, which is off for pushers.
                "ip": (getattr(frappe.request, "remote_addr", None) or "0.0.0.0"),
                "port": 4370,
                "password": 0,
                "auto_attendance": 0,
                "active": 1,
                "period": 0,
            }
        )
        device.flags.ignore_permissions = True
        device.insert(ignore_permissions=True)
        frappe.db.commit()
        return device
    except Exception as e:
        frappe.db.rollback()
        # Lost a race with a concurrent push from the same device.
        name = frappe.db.get_value("ZK Device", {"serial_number": sn}, "name")
        if name:
            return frappe.get_doc("ZK Device", name)
        _log(_title("ZK ADMS Device Create Failed", sn), str(e))
        return None


def create_device_logs(device, punches):
    """
    Insert the punches as Device Log rows, then link employees and create the
    matching Employee Checkins for just those rows.

    Deduplication is by (device, enroll_no, time) rather than by the row name
    alone. The polling path builds its names as
    "<enroll>_<YYYYmmddHHMMSS>_<punch>", and the punch component there comes
    from a different field of the binary record than the ATTLOG column here,
    so the two paths can describe the same punch under different names.
    Matching on the timestamp instead makes push and poll idempotent against
    each other, which is what lets the scheduled pull stay on as a safety net
    for punches that arrived while this endpoint was unreachable.
    """
    device_name = device.device_name

    times = [t for _, t, _ in punches]
    existing = set()
    if times:
        rows = frappe.db.sql(
            """
            SELECT enroll_no, time FROM `tabDevice Log`
            WHERE device = %s AND time IN ({0})
            """.format(", ".join(["%s"] * len(times))),
            [device_name] + times,
        )
        existing = {(str(r[0]).strip(), get_datetime(r[1])) for r in rows}

    ts = now()
    owner = "Administrator"
    rows = []
    names = []
    for enroll_no, timestamp, punch in punches:
        if (enroll_no, timestamp) in existing:
            continue
        # Same IN/OUT rule as the polling path: derived from time of day, not
        # from the state the device reported.
        log_type = "IN" if timestamp.hour < 12 else "OUT"
        name = "{0}_{1}_{2}".format(enroll_no, timestamp.strftime("%Y%m%d%H%M%S"), punch)
        names.append(name)
        rows.append(
            (
                name, enroll_no, timestamp, str(timestamp.date()), log_type,
                punch, device_name, ts, ts, owner, owner,
            )
        )
        existing.add((enroll_no, timestamp))

    if not rows:
        return

    frappe.db.sql(
        "INSERT IGNORE INTO `tabDevice Log` "
        "(name, employee, enroll_no, time, `date`, type, punch, device, "
        "creation, modified, modified_by, owner, docstatus) VALUES "
        + ", ".join(["(%s, NULL, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, 0)"] * len(rows)),
        [v for row in rows for v in row],
    )

    link_employees(names)
    create_checkins(names)
    frappe.db.commit()


def link_employees(names):
    """Point the new rows at their Employee via Attendance Device ID."""
    frappe.db.sql(
        """
        UPDATE `tabDevice Log` log
        INNER JOIN tabEmployee emp ON emp.attendance_device_id = TRIM(log.enroll_no)
        SET log.employee = emp.name
        WHERE log.name IN ({0})
          AND (log.employee IS NULL OR log.employee = '')
        """.format(", ".join(["%s"] * len(names))),
        names,
    )


def create_checkins(names):
    """
    Create Employee Checkin for the rows just inserted.

    Scoped to these names on purpose. The scheduled pipeline's
    create_employee_checkin_internal() scans a 90-day window, which is far
    too much work to repeat on every single pushed punch — and that window is
    also why a punch older than 90 days never becomes attendance. Addressing
    rows directly sidesteps both.
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
        WHERE dl.name IN ({0})
          AND dl.employee IS NOT NULL
          AND ec.device_log IS NULL
        """.format(", ".join(["%s"] * len(names))),
        names,
    )


def update_push_state(device, stamp):
    """Record the device's bookmark and when it last reported in."""
    values = {"last_push_time": now()}
    if stamp:
        values["push_stamp"] = stamp
    frappe.db.set_value("ZK Device", device.name, values, update_modified=False)
    frappe.db.commit()
