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
        }
    },
    get_device_logs: function (frm) {
        frm.save();
        frappe.call({
            method: "get_device_log",
            doc: frm.doc,
            args: {
                show_progress: 1,
            },
            freeze: true,
            callback: function () {
                frappe.hide_progress();

                frm.refresh();
            },
        });
    },
    sync_employee: function (frm) {
        frappe.call({
            method: "frappe_zk_integration_app.frappe_zk_integration_app.doctype.zk_device.zk_device.sync_employee",
            callback: function () {
                frappe.hide_progress();
                frm.refresh();
            },
        });
    },
    test_job: function (frm) {
        frappe.call({
            method:
                "frappe_zk_integration_app.frappe_zk_integration_app.doctype.zk_device.zk_device.get_active_device_logs",
            callback: function () {
                frappe.hide_progress();
                frm.refresh();
            },
        });
    },
});
