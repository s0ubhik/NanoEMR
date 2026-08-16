"""Billing — invoices with NRCES price components."""

from __future__ import annotations

from typing import Any

from ... import db, hospital, services
from ...terminology import INVOICE_TYPE
from .. import ui
from ..common import (
    patient_cell,
    collect_rows, doctor_options, get_patient, not_found, patient_options, render,
    status_label,
)
from ..router import Request, redirect

st = ui.st

LINE_FIELDS = ["code", "description", "qty", "price", "discount", "cgst", "sgst"]


def register(app) -> None:
    app.add("GET", "/billing", index)
    app.add("GET", "/billing/new", new)
    app.add("POST", "/billing", create)
    app.add("GET", "/billing/<int:iid>", detail)
    app.add("POST", "/billing/<int:iid>", update)
    app.add("POST", "/billing/<int:iid>/pay", pay)


def index(request: Request):
    rows = db.query(
        "SELECT i.*, p.name AS patient_name, p.mrn FROM invoice i "
        "JOIN patient p ON p.id = i.patient_id ORDER BY i.id DESC LIMIT 200")
    table_rows = [[
        f'<a class="z-link" href="/billing/{r["id"]}">{ui.esc(r["invoice_no"])}</a>',
        patient_cell(r),
        ui.esc(f'{r["type_display"]} ({r["type_code"]})'),
        ui.when(r["date"], 10),
        f'₹ {r["total_net"]:.2f}',
        f'₹ {r["total_gross"]:.2f}',
        f'₹ {r["amount_paid"]:.2f}',
        status_label(r["status"]),
    ] for r in rows]
    total = db.scalar("SELECT COALESCE(SUM(total_gross), 0) FROM invoice", default=0)
    body = ui.card(
        f"{len(table_rows)} invoice(s) · ₹ {total:,.2f} billed",
        ui.table(["Invoice", "Patient", "Type", "Date", "Net", "Gross", "Paid", "Status"],
                 table_rows, empty="No invoices raised yet.", align_right=(4, 5, 6)),
        actions=ui.button("New invoice", "/billing/new", style="z-button-primary",
                          ico="receipt-indian-rupee"))
    return render(request, "Billing", "billing", body)


# ---------------------------------------------------------------------------
def _line_row(values: dict[str, Any] | None = None) -> str:
    v = values or {}
    charges = [(c["code"], f'{c["display"]} ({c["code"]})') for c in db.terms("charge")]
    return ui.row_shell(
        ui.cell("Charge head", ui.select("ln_code", charges, v.get("code", "MISC")),
                "14rem")
        + ui.cell("Description", ui.text_input("ln_description", v.get("description", "")),
                  "16rem")
        + ui.cell("Qty", ui.text_input("ln_qty", v.get("qty", 1), type_="number",
                                       attrs='step="0.5" min="0"'), "5rem")
        + ui.cell("Rate (₹)", ui.text_input("ln_price", v.get("price", 0), type_="number",
                                            attrs='step="0.01" min="0"'), "8rem")
        + ui.cell("Discount %", ui.text_input("ln_discount", v.get("discount", 0),
                                              type_="number",
                                              attrs='step="0.01" min="0" max="100"'), "7rem")
        + ui.cell("CGST %", ui.text_input("ln_cgst", v.get("cgst", 0), type_="number",
                                          attrs='step="0.01" min="0"'), "6rem")
        + ui.cell("SGST %", ui.text_input("ln_sgst", v.get("sgst", 0), type_="number",
                                          attrs='step="0.01" min="0"'), "6rem"))


def _lines_from_request(request: Request) -> list[dict[str, Any]]:
    out = []
    for row in collect_rows(request, "ln", LINE_FIELDS):
        master = db.term("charge", row["code"])
        if master is None:
            continue

        def number(key: str, default: float = 0.0) -> float:
            try:
                return float(row[key] or default)
            except ValueError:
                return default

        price = number("price")
        quantity = number("qty", 1) or 1
        if price <= 0:
            continue
        out.append({
            "description": row["description"] or master["display"],
            "charge_system": master["system"],
            "charge_code": master["code"],
            "charge_display": master["display"],
            "quantity": quantity,
            "unit_price": price,
            "discount_pct": number("discount"),
            "cgst_pct": number("cgst"),
            "sgst_pct": number("sgst"),
        })
    return out


def new(request: Request):
    encounter_id = request.q("encounter")
    preset_patient = request.q("patient")
    suggested: list[dict[str, Any]] = []
    default_type = "99"
    if encounter_id:
        enc = db.one("SELECT * FROM encounter WHERE id = ?", (encounter_id,))
        if enc is not None:
            preset_patient = str(enc["patient_id"])
            suggested = services.suggest_invoice_lines(int(encounter_id))
            default_type = "03" if enc["kind"] == "OPD" else "02"

    rows = [_line_row({
        "code": line["charge_code"], "description": line["description"],
        "qty": line["quantity"], "price": line["unit_price"],
        "discount": 0, "cgst": 0, "sgst": 0}) for line in suggested] or [_line_row()]

    body = (
        '<form method="post" action="/billing">'
        + f'<input type="hidden" name="encounter_id" value="{ui.esc(encounter_id)}">'
        + ui.card("Invoice header", ui.grid(
            ui.field("Patient", ui.select("patient_id", patient_options(), preset_patient,
                                          blank="Select patient", required=True),
                     required=True),
            ui.field("Invoice type", ui.select(
                "type_code", list(INVOICE_TYPE.items()), default_type),
                help_text="ndhm-invoice-types value set."),
            ui.field("Billing doctor", ui.select("participant_id", doctor_options(), "",
                                                 blank="None")),
            ui.field("Note", ui.text_input("note", "")),
            cols=2))
        + f'<div class="mt"{st(mt=4)}>'
        + ui.card("Line items", ui.repeater("ln", _line_row(), rows, "Add line item"),
                  footer=(f'<div class="display-flex justify-between gap flex-wrap"'
                          f'{st(gap=2)}>'
                          + ui.button("Cancel", "/billing", style="z-button-secondary")
                          + ui.button("Create invoice", style="z-button-primary",
                                      size="z-button-medium", type_="submit")
                          + "</div>"))
        + "</div></form>")
    return render(request, "New invoice", "billing", body,
                  breadcrumb=[("Billing", "/billing"), ("New invoice", None)])


def create(request: Request):
    patient_id = request.f_int("patient_id")
    if not patient_id:
        return redirect("/billing/new", "Select a patient first.", "danger")
    type_code = request.f("type_code") or "99"
    invoice_id = services.create_invoice(
        patient_id, request.f_int("encounter_id"), type_code,
        INVOICE_TYPE.get(type_code, "Others"), request.f_int("participant_id"),
        request.f_or_none("note"))
    services.save_invoice(invoice_id, _lines_from_request(request))
    encounter_id = request.f_int("encounter_id")
    if encounter_id:
        hospital.mark_issues_billed(encounter_id)
    number = db.scalar("SELECT invoice_no FROM invoice WHERE id = ?", (invoice_id,))
    return redirect(f"/billing/{invoice_id}", f"Invoice {number} created.")


# ---------------------------------------------------------------------------
def detail(request: Request):
    iid = request.params["iid"]
    invoice = db.one("SELECT * FROM invoice WHERE id = ?", (iid,))
    if invoice is None:
        return not_found(request, "Invoice")
    patient = get_patient(invoice["patient_id"])
    lines = db.query("SELECT * FROM invoice_line WHERE invoice_id = ? ORDER BY seq", (iid,))
    org = db.default_org()
    doctor = db.one("SELECT * FROM practitioner WHERE id = ?", (invoice["participant_id"],))

    printable_rows = [[
        str(line["seq"]),
        f'{ui.esc(line["description"])}'
        + ui.muted(f'{line["charge_display"]} · {line["charge_code"]}'),
        f'{line["quantity"]:g}',
        f'{line["unit_price"]:.2f}',
        f'{line["discount_pct"]:g}%',
        f'{line["cgst_pct"]:g}% / {line["sgst_pct"]:g}%',
        f'{line["line_net"]:.2f}',
        f'{line["line_gross"]:.2f}',
    ] for line in lines]

    totals = (
        f'<div class="display-flex justify-end mt"{st(mt=4)}>'
        '<div style="min-width:18rem">'
        + ui.dl([
            ("Total net", f'₹ {invoice["total_net"]:.2f}'),
            ("Total gross (payable)", f'₹ {invoice["total_gross"]:.2f}'),
            ("Amount paid", f'₹ {invoice["amount_paid"]:.2f}'),
            ("Balance", f'₹ {invoice["total_gross"] - invoice["amount_paid"]:.2f}'),
        ], cols=2)
        + "</div></div>")

    header = ui.dl([
        ("Invoice number", invoice["invoice_no"]),
        ("Type", f'{invoice["type_display"]} ({invoice["type_code"]})'),
        ("Date", ui.when(invoice["date"])),
        ("Issuer", org["name"] if org else None),
        ("GSTIN", org["gstin"] if org else None),
        ("Billing doctor", doctor["name"] if doctor else None),
        ("Encounter", db.scalar("SELECT encounter_no FROM encounter WHERE id = ?",
                                (invoice["encounter_id"],)) if invoice["encounter_id"]
         else None),
        ("Note", invoice["note"]),
    ], cols=3)

    edit_rows = [_line_row({
        "code": line["charge_code"], "description": line["description"],
        "qty": line["quantity"], "price": line["unit_price"],
        "discount": line["discount_pct"], "cgst": line["cgst_pct"],
        "sgst": line["sgst_pct"]}) for line in lines] or [_line_row()]

    edit_form = (
        f'<form method="post" action="/billing/{iid}">'
        + ui.card("Edit line items", ui.repeater("ln", _line_row(), edit_rows,
                                                 "Add line item")
                  + f'<div class="mt"{st(mt=4)}>'
                  + ui.field("Status", ui.select("status", [
                      ("draft", "Draft"), ("issued", "Issued"),
                      ("balanced", "Balanced"), ("cancelled", "Cancelled")],
                      invoice["status"]))
                  + "</div>",
                  footer=(f'<div class="display-flex justify-end"{st()}>'
                          + ui.button("Save invoice", style="z-button-primary",
                                      size="z-button-medium", type_="submit") + "</div>"))
        + "</form>")


    body = (
        ui.patient_header(patient, extra=(
            ui.label_chip(invoice["invoice_no"], "info")
            + status_label(invoice["status"])))
        + f'<div class="mt"{st(mt=5)}>'
        + ui.card("Invoice", header + f'<div class="mt"{st(mt=4)}>' + ui.table(
            ["#", "Particulars", "Qty", "Rate", "Discount", "CGST / SGST", "Net", "Gross"],
            printable_rows, empty="No line items.", align_right=(2, 3, 4, 5, 6, 7))
            + "</div>" + totals)
        + "</div>"
        + f'<div class="mt"{st(mt=5)}>' + _payments_card(invoice) + "</div>"
        + f'<div class="mt"{st(mt=5)}>' + edit_form + "</div>")

    return render(request, f"Invoice {invoice['invoice_no']}", "billing", body,
                  breadcrumb=[("Billing", "/billing"), (invoice["invoice_no"], None)])


def _payments_card(invoice) -> str:
    payments = db.query(
        "SELECT * FROM payment WHERE invoice_id = ? ORDER BY id DESC", (invoice["id"],))
    balance = round(invoice["total_gross"] - invoice["amount_paid"], 2)
    rows = [[
        ui.esc(p["receipt_no"]),
        ui.when(p["received_at"]),
        ui.esc(dict(hospital.PAYMENT_MODES).get(p["mode"], p["mode"])),
        ui.esc(p["reference"] or "—"),
        f'₹ {p["amount"]:,.2f}',
    ] for p in payments]

    if balance > 0.005:
        form = (f'<form method="post" action="/billing/{invoice["id"]}/pay">'
                + ui.grid(
                    ui.field("Amount (₹)", ui.text_input(
                        "amount", f"{balance:.2f}", type_="number",
                        attrs=f'step="0.01" min="0.01" max="{balance:.2f}"',
                        required=True), required=True),
                    ui.field("Mode", ui.select("mode", hospital.PAYMENT_MODES, "cash")),
                    ui.field("Reference", ui.text_input(
                        "reference", "", placeholder="UPI ref / card last 4")),
                    cols=3)
                + ui.field("Note", ui.text_input("note", ""))
                + f'<div class="display-flex justify-end"{st()}>'
                + ui.button("Record receipt", style="z-button-primary", type_="submit",
                            ico="banknote")
                + "</div></form>")
        capture = f'<div class="mt"{st(mt=4)}>' + ui.accordion(
            [(f"Collect the ₹{balance:,.2f} balance", form)], open_first=True) + "</div>"
        chip = ui.label_chip(f"₹{balance:,.2f} due", "danger")
    else:
        capture = ""
        chip = ui.label_chip("settled", "success")

    return ui.card(
        f"Receipts — ₹ {invoice['amount_paid']:,.2f} of ₹ "
        f"{invoice['total_gross']:,.2f} collected",
        ui.table(["Receipt", "When", "Mode", "Reference", "Amount"], rows,
                 empty="No payment recorded yet.", align_right=(4,)) + capture,
        actions=chip)


def update(request: Request):
    iid = request.params["iid"]
    invoice = db.one("SELECT * FROM invoice WHERE id = ?", (iid,))
    if invoice is None:
        return not_found(request, "Invoice")
    services.save_invoice(iid, _lines_from_request(request))
    db.update("invoice", iid, {"status": request.f("status") or invoice["status"]})
    if invoice["encounter_id"]:
        hospital.mark_issues_billed(invoice["encounter_id"])
    hospital.refresh_invoice_payment(iid)
    return redirect(f"/billing/{iid}", "Invoice updated.")


def pay(request: Request):
    iid = request.params["iid"]
    try:
        hospital.record_payment(iid, request.f_float("amount"), request.f("mode"),
                                request.f_or_none("reference"),
                                request.f_or_none("note"))
    except ValueError as error:
        return redirect(f"/billing/{iid}", f"Could not record the receipt: {error}",
                        "danger")
    receipt = db.scalar("SELECT receipt_no FROM payment WHERE invoice_id = ? "
                        "ORDER BY id DESC LIMIT 1", (iid,))
    return redirect(f"/billing/{iid}", f"Receipt {receipt} recorded.")
