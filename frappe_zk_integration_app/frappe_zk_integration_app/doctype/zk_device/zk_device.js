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

        // Safety net: the background job has a 5-minute hard timeout on the
        // server, but if it (or the notification itself) ever fails silently
        // — worker crash, dropped Redis message, stuck RQ job — the realtime
        // events above may never arrive. Without this, the progress dialog
        // would stay open forever with no feedback to the user.
        frm._zk_timeout = setTimeout(function () {
            cleanup();
            frappe.msgprint({
                title: __("Device Error"),
                message: __("No response from the background job after 6 minutes. Check the Error Log for details."),
                indicator: "red"
            });
            frm.reload_doc();
        }, 6 * 60 * 1000);

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
