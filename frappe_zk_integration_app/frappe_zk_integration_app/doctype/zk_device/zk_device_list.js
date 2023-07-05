frappe.listview_settings['Device Log'] = {
    refresh:function (listview){
        const method = "frappe_zk_integration_app.frappe_zk_integration_app.doctype.zk_device.zk_device.get_active_device_logs"
        listview.page.add_menu_item(__("Get Logs"), function () {
			listview.call_for_selected_items(method);
		});
    }
}
