frappe.listview_settings["Device Log"] = {
    refresh: function (listview) {
        listview.page.add_menu_item(__("Create Employee Checkin"), function () {
            create_employee_checkin(listview);
        });
        listview.page.add_menu_item(__("Get Logs"), function () {
            get_active_device_logs(listview);
        });
        listview.page.add_menu_item(__("Sync Employee"), function () {
            sync_employee(listview);
        });
    },
};
var get_active_device_logs = function (listview) {
    const method =
        "frappe_zk_integration_app.frappe_zk_integration_app.doctype.zk_device.zk_device.get_active_device_logs";
    frappe.call({
        method: method,
        callback: function (r) {
            listview.refresh();
        },
    });
};
var sync_employee = function (listview) {
    const method = "frappe_zk_integration_app.frappe_zk_integration_app.doctype.zk_device.zk_device.sync_employee";
    frappe.call({
        method: method,
        callback: function (r) {
            listview.refresh();
        },
    });
};
var create_employee_checkin = function (listview) {
    const method =
        "frappe_zk_integration_app.frappe_zk_integration_app.doctype.device_log.device_log.create_employee_checkin";
    frappe.call({
        method: method,
        callback: function (r) {
            listview.refresh();
        },
    });
};
