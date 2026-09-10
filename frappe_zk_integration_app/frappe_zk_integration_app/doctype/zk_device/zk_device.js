// Copyright (c) 2023, Creative Advanced Technologies and contributors
// For license information, please see license.txt

frappe.ui.form.on("ZK Device", {
    refresh: function (frm) {
        if (!frm.is_new()) {
            frm.add_custom_button(__("Get Logs"), function () {
                frm.events.get_device_logs(frm);
            });
            frm.add_custom_button(__("Sync Employee"), function () {
                frm.events.sync_employee(frm);
            });
            frm.add_custom_button(__("Check Connection"), function () {
                frm.events.check_connection(frm);
            });
            // Destructive and irreversible on the hardware — styled apart
            // from the read-only actions above on purpose.
            frm.add_custom_button(__("Clear Device Logs"), function () {
                frm.events.clear_device_logs(frm);
            }).addClass("btn-danger");
        }
    },

    get_device_logs: function (frm) {
        // Guard: ask the user to save unsaved changes first so fetch_from_date etc. are persisted
        if (frm.is_dirty()) {
            frappe.show_alert({
                message: __("Please save your changes before fetching logs."),
                indicator: "orange"
            }, 5);
            return;
        }

        // Pre-flight so the user sees how much the device is holding before
        // committing to a pull that transfers the whole log. It runs as a
        // background job rather than in this request: answering needs a full
        // ZK handshake, which on a slow device outlasts nginx's
        // proxy_read_timeout and would return a gateway timeout instead of a
        // number. Nothing here blocks — the answer arrives over realtime.
        if (frm._zk_probe_cleanup) {
            frm._zk_probe_cleanup();
        }

        let cleanup = function () {
            frappe.realtime.off("zk_storage_info", handler);
            clearTimeout(frm._zk_probe_timeout);
            frm._zk_probe_cleanup = null;
        };

        let handler = function (d) {
            if (d.device !== frm.doc.name) return;
            cleanup();
            if (d.error) {
                frappe.msgprint({
                    title: __("Device Unreachable"),
                    message: __("Could not read the device, so nothing was fetched.") + "<br><pre>" +
                             frappe.utils.escape_html(d.error) + "</pre>",
                    indicator: "red"
                });
                return;
            }
            frm.events.confirm_get_device_logs(frm, d);
        };

        frm._zk_probe_cleanup = cleanup;
        frappe.realtime.on("zk_storage_info", handler);

        // If the probe never reports back (no worker, dropped realtime
        // message, device wedged mid-handshake), don't leave the user with
        // nothing — offer the pull without the record count.
        frm._zk_probe_timeout = setTimeout(function () {
            cleanup();
            frappe.confirm(
                "<p>" + __("The device did not report its record count in time.") + "</p>" +
                "<p>" + __("Fetch anyway, without knowing how many records are stored?") + "</p>",
                function () {
                    frm.events.run_get_device_logs(frm);
                }
            );
        }, 90 * 1000);

        frappe.show_alert({ message: __("Checking device..."), indicator: "blue" }, 5);

        frappe.call({
            method: "frappe_zk_integration_app.frappe_zk_integration_app.doctype.zk_device.zk_device.request_device_storage",
            args: { device_name: frm.doc.name },
            error: function () { cleanup(); }
        });
    },

    confirm_get_device_logs: function (frm, d) {
        let records = d.records || 0;

        if (!records) {
            frappe.msgprint({
                title: __("Nothing to Fetch"),
                message: __("The device is not holding any attendance records."),
                indicator: "orange"
            });
            return;
        }

        // The ZK protocol's CMD_ATTLOG_RRQ takes no date range, so the whole
        // stored log crosses the wire on every pull no matter what the cursor
        // says — only the insert step is incremental. Say so plainly, since
        // it is what governs how long this will take.
        let scope = d.last_log_row
            ? __("Only punches after {0} will be inserted.").replace("{0}", frappe.utils.escape_html(d.last_log_row))
            : __("The fetch cursor is not set, so every punch on the device is eligible for insert.");

        let note = "";
        if (records > 50000) {
            note = "<div class='alert alert-warning'>" +
                __("This device holds a large log. The whole log is transferred on every pull, and a transfer that runs past 30 minutes is killed by the background job timeout. Consider clearing the device log after this pull succeeds.") +
                "</div>";
        }

        frappe.confirm(
            "<p>" + __("Punches stored on this device:") + " <b>" + records + "</b>" +
                (d.rec_cap ? " / " + d.rec_cap : "") + "</p>" +
            "<p>" + __("Device Log rows already in the database:") + " <b>" + (d.db_logs || 0) + "</b></p>" +
            "<p>" + scope + "</p>" +
            "<p class='text-muted'>" + __("The entire stored log is downloaded from the device regardless of the cursor — the cursor only limits what gets inserted.") + "</p>" +
            note +
            "<p>" + __("Fetch them now?") + "</p>",
            function () {
                frm.events.run_get_device_logs(frm);
            }
        );
    },

    run_get_device_logs: function (frm) {
        // Remove leftover listeners from a previous (possibly failed) click
        if (frm._zk_cleanup) {
            frm._zk_cleanup();
        }

        let device_label = frm.doc.device_name || frm.doc.name;

        // --- cleanup helper ---------------------------------------------------
        let cleanup = function () {
            frappe.realtime.off("zk_job_done", handlers.done);
            frappe.realtime.off("zk_job_error", handlers.error);
            clearTimeout(frm._zk_timeout);
            frappe.hide_progress();
            frm._zk_cleanup = null;
        };

        // --- completion handlers ----------------------------------------------
        let handlers = {
            done: function (data) {
                if (data.device !== frm.doc.name) return;
                cleanup();
                frappe.show_alert({ message: data.message, indicator: "green" }, 10);
                frm.reload_doc();
            },
            error: function (data) {
                if (data.device !== frm.doc.name) return;
                cleanup();
                frappe.msgprint({
                    title: __("Device Error"),
                    message: data.error,
                    indicator: "red"
                });
                frm.reload_doc();
            }
        };

        frm._zk_cleanup = cleanup;
        frappe.realtime.on("zk_job_done", handlers.done);
        frappe.realtime.on("zk_job_error", handlers.error);

        // Safety net: the background job has a 30-minute hard timeout on the
        // server (_JOB_TIMEOUT in zk_device.py — keep this in sync with it),
        // but if it (or the notification itself) ever fails silently —
        // worker crash, dropped Redis message, stuck RQ job — the realtime
        // events above may never arrive. Without this, the progress dialog
        // would stay open forever with no feedback to the user.
        frm._zk_timeout = setTimeout(function () {
            cleanup();
            frappe.msgprint({
                title: __("Device Error"),
                message: __("No response from the background job after 32 minutes. Check the Error Log for details."),
                indicator: "red"
            });
            frm.reload_doc();
        }, 32 * 60 * 1000);

        // Show progress bar immediately.
        // Frappe's built-in realtime listener automatically keeps it updated
        // as the background job calls frappe.publish_progress().
        frappe.show_progress(
            __("Fetching Logs from {0}").replace("{0}", device_label),
            0, 100,
            __("Connecting to device...")
        );

        frappe.call({
            method: "frappe_zk_integration_app.frappe_zk_integration_app.doctype.zk_device.zk_device.send_specific_device_log",
            args: { device_name: frm.doc.name },
            callback: function (r) {
                if (r.exc) {
                    cleanup();
                    frappe.msgprint({
                        title: __("Queue Error"),
                        message: __("Could not queue the job — check the error log."),
                        indicator: "red"
                    });
                } else {
                    frappe.show_alert({
                        message: __("Job queued — fetching from device..."),
                        indicator: "blue"
                    }, 4);
                }
            },
            error: function () { cleanup(); }
        });
    },

    clear_device_logs: function (frm) {
        // Two-stage on purpose. Stage 1 is a cheap CMD_GET_FREE_SIZES read so
        // the confirm dialog can state exactly how many punches are about to
        // be destroyed; the expensive full fetch only happens server-side
        // once the user has actually confirmed.
        frappe.call({
            method: "frappe_zk_integration_app.frappe_zk_integration_app.doctype.zk_device.zk_device.get_device_storage",
            args: { device_id: frm.doc.name },
            freeze: true,
            freeze_message: __("Reading device storage..."),
            callback: function (r) {
                let d = r.message || {};
                if (d.error) {
                    frappe.msgprint({
                        title: __("Device Unreachable"),
                        message: __("Cannot read the device, so nothing was cleared.") + "<br><pre>" +
                                 frappe.utils.escape_html(d.error) + "</pre>",
                        indicator: "red"
                    });
                    return;
                }
                frm.events.confirm_clear_device_logs(frm, d);
            }
        });
    },

    confirm_clear_device_logs: function (frm, d) {
        let device_label = frm.doc.device_name || frm.doc.name;
        let warnings = [];

        if (d.unconverted) {
            warnings.push(__("{0} device log(s) have an employee but no Employee Checkin. The wipe will be refused unless you tick Force below.")
                .replace("{0}", d.unconverted));
        }
        if (d.unmapped) {
            warnings.push(__("{0} device log(s) are not linked to any Employee (unmapped Attendance Device ID). They stay in the database, but can only become attendance while they are within the 90-day window.")
                .replace("{0}", d.unmapped));
        }
        if (d.period) {
            warnings.push(__("Period Difference is set to {0} minute(s), so some punches on the device were deliberately never stored. Those are lost permanently once the device is wiped.")
                .replace("{0}", d.period));
        }

        let dialog = new frappe.ui.Dialog({
            title: __("Clear Device Logs"),
            fields: [
                {
                    fieldtype: "HTML",
                    fieldname: "summary",
                    options:
                        "<p>" + __("This erases the attendance log stored on the device itself. <b>It cannot be undone.</b>") + "</p>" +
                        "<p>" + __("Before erasing, the server pulls everything the device holds into Device Log and creates the matching Employee Checkin records. Your existing Device Log rows in the database are kept.") + "</p>" +
                        "<table class='table table-bordered' style='margin-bottom:12px'>" +
                        "<tr><td>" + __("Punches stored on device") + "</td><td><b>" + (d.records || 0) + "</b>" +
                            (d.rec_cap ? " / " + d.rec_cap : "") + "</td></tr>" +
                        "<tr><td>" + __("Device Log rows in database") + "</td><td><b>" + (d.db_logs || 0) + "</b></td></tr>" +
                        "<tr><td>" + __("Fetch cursor (Last Log Row)") + "</td><td>" +
                            frappe.utils.escape_html(d.last_log_row || __("not set")) + "</td></tr>" +
                        "</table>" +
                        (warnings.length
                            ? "<div class='alert alert-warning'><ul style='margin:0;padding-left:18px'><li>" +
                              warnings.join("</li><li>") + "</li></ul></div>"
                            : "")
                },
                {
                    fieldtype: "Data",
                    fieldname: "confirm_name",
                    label: __("Type the device name to confirm"),
                    description: __("Expected: {0}").replace("{0}", device_label),
                    reqd: 1
                },
                {
                    fieldtype: "Check",
                    fieldname: "force",
                    label: __("Force — clear even with half-processed logs"),
                    default: 0,
                    depends_on: "eval:" + (d.unconverted ? "true" : "false")
                }
            ],
            primary_action_label: __("Erase device log"),
            primary_action: function (values) {
                if ((values.confirm_name || "").trim() !== device_label) {
                    frappe.msgprint({
                        title: __("Name Mismatch"),
                        message: __("Nothing was cleared. Type the device name exactly to confirm."),
                        indicator: "orange"
                    });
                    return;
                }
                dialog.hide();
                frm.events.run_clear_device_logs(frm, values.force ? 1 : 0);
            }
        });
        dialog.get_primary_btn().addClass("btn-danger").removeClass("btn-primary");
        dialog.show();
    },

    run_clear_device_logs: function (frm, force) {
        if (frm._zk_clear_cleanup) {
            frm._zk_clear_cleanup();
        }

        let cleanup = function () {
            frappe.realtime.off("zk_clear_done", handlers.done);
            frappe.realtime.off("zk_clear_error", handlers.error);
            clearTimeout(frm._zk_clear_timeout);
            frappe.hide_progress();
            frm._zk_clear_cleanup = null;
        };

        let handlers = {
            done: function (data) {
                if (data.device !== frm.doc.name) return;
                cleanup();
                frappe.msgprint({
                    title: __("Device Log Cleared"),
                    message: data.message,
                    indicator: "green"
                });
                frm.reload_doc();
            },
            error: function (data) {
                if (data.device !== frm.doc.name) return;
                cleanup();
                frappe.msgprint({
                    title: __("Clear Failed"),
                    message: data.error,
                    indicator: "red"
                });
                frm.reload_doc();
            }
        };

        frm._zk_clear_cleanup = cleanup;
        frappe.realtime.on("zk_clear_done", handlers.done);
        frappe.realtime.on("zk_clear_error", handlers.error);

        // Step 1 of the job is a full fetch, so this shares the fetch job's
        // 30-minute server-side timeout (_JOB_TIMEOUT) — keep in sync.
        frm._zk_clear_timeout = setTimeout(function () {
            cleanup();
            frappe.msgprint({
                title: __("Clear Failed"),
                message: __("No response from the background job after 32 minutes. Check the Error Log to confirm whether the device was cleared."),
                indicator: "red"
            });
            frm.reload_doc();
        }, 32 * 60 * 1000);

        frappe.show_progress(
            __("Clearing {0}").replace("{0}", frm.doc.device_name || frm.doc.name),
            0, 100,
            __("Pulling remaining logs before erasing...")
        );

        frappe.call({
            method: "frappe_zk_integration_app.frappe_zk_integration_app.doctype.zk_device.zk_device.clear_device_attendance",
            args: { device_name: frm.doc.name, force: force },
            callback: function (r) {
                if (r.exc) {
                    cleanup();
                    frappe.msgprint({
                        title: __("Queue Error"),
                        message: __("Could not queue the job — nothing was cleared."),
                        indicator: "red"
                    });
                } else {
                    frappe.show_alert({
                        message: __("Job queued — fetching remaining logs, then erasing..."),
                        indicator: "blue"
                    }, 5);
                }
            },
            error: function () { cleanup(); }
        });
    },

    sync_employee: function (frm) {
        frappe.call({
            method: "frappe_zk_integration_app.frappe_zk_integration_app.doctype.zk_device.zk_device.sync_employee",
            freeze: true,
            freeze_message: __("Syncing employee data..."),
            callback: function () {
                frm.reload_doc();
                frappe.show_alert({ message: __("Employee sync complete"), indicator: "green" }, 4);
            },
            error: function () {
                frappe.show_alert({ message: __("Sync failed — check error log"), indicator: "red" }, 5);
            }
        });
    },

    check_connection: function (frm) {
        frappe.call({
            method: "frappe_zk_integration_app.frappe_zk_integration_app.doctype.zk_device.zk_device.check_connection",
            args: { device_id: frm.doc.name },
            freeze: true,
            freeze_message: __("Testing connection..."),
            callback: function (r) {
                if (r.message === "Success") {
                    frappe.show_alert({ message: __("Connection Successful"), indicator: "green" }, 5);
                } else {
                    frappe.msgprint({
                        title: __("Connection Failed"),
                        message: r.message,
                        indicator: "red"
                    });
                }
            }
        });
    }
});
