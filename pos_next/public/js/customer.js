// "Award Loyalty Points" on the Customer form, for POS managers.
// The server (pos_next.api.loyalty_award) checks the role and validates everything;
// the button is only hidden from users who could not use it.

const POS_NEXT_POINT_MANAGER_ROLES = ["Nexus POS Manager", "System Manager"];

frappe.ui.form.on("Customer", {
	refresh(frm) {
		if (frm.is_new() || !frm.doc.loyalty_program) return;
		if (!POS_NEXT_POINT_MANAGER_ROLES.some((role) => frappe.user.has_role(role))) return;

		frm.add_custom_button(__("Award Loyalty Points"), () => pos_next_award_points(frm));
	},
});

function pos_next_award_points(frm) {
	const dialog = new frappe.ui.Dialog({
		title: __("Award Loyalty Points"),
		fields: [
			{
				fieldtype: "HTML",
				options: `<p class="text-muted">${__(
					"Points are added to {0}'s balance and can be spent at the till like earned points.",
					[frappe.utils.escape_html(frm.doc.customer_name || frm.doc.name)]
				)}</p>`,
			},
			{
				fieldname: "points",
				fieldtype: "Int",
				label: __("Points"),
				reqd: 1,
				description: __("Points to add (a whole number greater than zero)."),
			},
			{
				fieldname: "reason",
				fieldtype: "Data",
				label: __("Reason"),
				reqd: 1,
				description: __("For example: apology for a late order."),
			},
		],
		primary_action_label: __("Award"),
		primary_action(values) {
			dialog.disable_primary_action();
			frappe.call({
				method: "pos_next.api.loyalty_award.award_loyalty_points",
				args: { customer: frm.doc.name, points: values.points, reason: values.reason },
				freeze: true,
				callback(r) {
					dialog.hide();
					const result = r.message || {};
					frappe.show_alert({
						message: __("Done. New balance: {0} points", [result.balance]),
						indicator: "green",
					});
					frm.reload_doc();
				},
				error() {
					// The server's message is shown by Frappe; let the manager fix and retry.
					dialog.enable_primary_action();
				},
			});
		},
	});
	dialog.show();
}
