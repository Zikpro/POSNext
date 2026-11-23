// "Import Opening Points" on the Loyalty Program form, for System Managers.
// One-off: loads customers' point balances from a previous system. The server
// (pos_next.api.loyalty_import) checks the role and validates every row.

frappe.ui.form.on("Loyalty Program", {
	refresh(frm) {
		if (frm.is_new() || !frappe.user.has_role("System Manager")) return;
		frm.add_custom_button(__("Import Opening Points"), () => pos_next_import_opening_points(frm));
	},
});

function pos_next_import_opening_points(frm) {
	let dialog = null;
	dialog = new frappe.ui.Dialog({
		title: __("Import Opening Points"),
		size: "large",
		fields: [
			{
				fieldtype: "HTML",
				options: `<p class="text-muted">${__(
					"Upload a CSV file with two columns: <b>customer</b> (the Customer ID) and <b>points</b>. Click Check File first; nothing is saved until you click Import. Customers that already have opening points are skipped."
				)}</p>`,
			},
			{
				fieldname: "file_url",
				fieldtype: "Attach",
				label: __("CSV file"),
				reqd: 1,
				onchange: () => disableImport(),
			},
			{
				fieldname: "enroll_missing",
				fieldtype: "Check",
				label: __("Enrol customers who have no Loyalty Program in {0}", [frm.doc.name]),
				onchange: () => disableImport(),
			},
			{ fieldname: "report", fieldtype: "HTML" },
		],
		primary_action_label: __("Check File"),
		primary_action: () => run(1),
		secondary_action_label: __("Import"),
		secondary_action: () => run(0),
	});

	const importButton = () => dialog.get_secondary_btn();
	// A new file or setting needs a fresh check before importing.
	function disableImport() {
		if (dialog) importButton().prop("disabled", true);
	}
	disableImport();

	function run(dryRun) {
		const values = dialog.get_values();
		if (!values) return;
		frappe.call({
			method: "pos_next.api.loyalty_import.import_opening_points",
			args: {
				loyalty_program: frm.doc.name,
				file_url: values.file_url,
				dry_run: dryRun,
				enroll_missing: values.enroll_missing ? 1 : 0,
			},
			freeze: true,
			freeze_message: dryRun ? __("Checking...") : __("Importing..."),
			callback(r) {
				const s = r.message || {};
				dialog.fields_dict.report.$wrapper.html(render(s));
				importButton().prop("disabled", !!s.imported || s.problems > 0 || !s.to_import);
				if (s.imported) {
					frappe.show_alert({
						message: __("Imported {0} points for {1} customers", [s.points, s.to_import]),
						indicator: "green",
					});
				}
			},
		});
	}

	function render(s) {
		const esc = frappe.utils.escape_html;
		const rows = (list, key) =>
			list
				.map((p) => `<tr><td>${p.row}</td><td>${esc(p.customer || "")}</td><td>${esc(p[key])}</td></tr>`)
				.join("");
		let html = `<div class="mt-3"><p><b>${s.imported ? __("Imported") : __("Check result")}</b>: ${__(
			"{0} customers with {1} points to import, {2} to enrol, {3} skipped, {4} problems.",
			[s.to_import, s.points, s.to_enroll, s.skipped, s.problems]
		)}</p>`;
		if (s.problems) {
			html += `<p class="text-danger">${__("Correct these rows and check again. Nothing can be imported while there are problems.")}</p>
				<table class="table table-bordered table-sm"><thead><tr><th>${__("Row")}</th><th>${__("Customer")}</th><th>${__("Problem")}</th></tr></thead>
				<tbody>${rows(s.problem_rows, "problem")}</tbody></table>`;
		}
		if (s.skipped) {
			html += `<details><summary>${__("Skipped rows")}</summary>
				<table class="table table-bordered table-sm"><thead><tr><th>${__("Row")}</th><th>${__("Customer")}</th><th>${__("Reason")}</th></tr></thead>
				<tbody>${rows(s.skipped_rows, "reason")}</tbody></table></details>`;
		}
		return html + "</div>";
	}

	dialog.show();
}
