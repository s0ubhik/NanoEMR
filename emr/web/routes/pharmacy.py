"""Pharmacy and consumables — stock, receipts, and issue against a patient."""

from __future__ import annotations

from ... import db, hospital
from .. import ui
from ..common import not_found, patient_options, render
from ..router import Request, redirect

st = ui.st


def register(app) -> None:
    app.add("GET", "/pharmacy", index)
    app.add("GET", "/pharmacy/issue", issue_form)
    app.add("POST", "/pharmacy/issue", issue)
    app.add("POST", "/pharmacy/receive", receive)
    app.add("GET", "/pharmacy/<int:iid>", item_detail)


def index(request: Request):
    search = request.q("q")
    kind = request.q("kind")
    items = hospital.stock_list(search, kind)
    low = hospital.low_stock()
    expiring = hospital.expiring_batches()
    value = sum(i["on_hand"] * i["purchase_price"] for i in items)

    rows = []
    for item in items:
        short = item["on_hand"] <= item["reorder_level"]
        rows.append([
            f'<a class="z-link" href="/pharmacy/{item["id"]}">{ui.esc(item["name"])}</a>'
            f'<div class="text-xs color"{st(color="var(--z-muted-f)")}>'
            f'{ui.esc(item["code"])}'
            + (f' · {ui.esc(item["strength"])}' if item["strength"] else "") + "</div>",
            ui.label_chip(item["kind"], "info" if item["kind"] == "drug" else ""),
            ui.esc(item["form"] or "—"),
            (f'<strong>{item["on_hand"]:g}</strong> {ui.esc(item["unit"])}'
             + (" " + ui.label_chip("reorder", "danger") if short else "")),
            f'{item["reorder_level"]:g}',
            f'₹ {item["mrp"]:.2f}',
            ui.esc(item["next_expiry"] or "—"),
        ])

    alerts = ""
    if low:
        alerts += (f'<div class="z-alert z-alert-danger mb" data-z-alert{st(mb=3)}>'
                   f"<strong>{len(low)} item(s) at or below reorder level:</strong> "
                   + ui.esc(", ".join(i["name"] for i in low[:8]))
                   + ("…" if len(low) > 8 else "") + "</div>")
    if expiring:
        alerts += (f'<div class="z-alert z-alert-warning mb" data-z-alert{st(mb=3)}>'
                   f"<strong>{len(expiring)} batch(es) expiring within 90 days:</strong> "
                   + ui.esc(", ".join(f'{b["name"]} ({b["expiry_date"]})'
                                      for b in expiring[:6])) + "</div>")

    filters = ('<form method="get" action="/pharmacy" '
               f'class="display-flex gap flex-wrap"{st(gap=2)}>'
               + ui.text_input("q", search, placeholder="Name or code",
                               attrs='style="min-width:12rem"')
               + ui.select("kind", [("drug", "Drugs"), ("consumable", "Consumables")],
                           kind, blank="All items")
               + ui.button("Filter", style="z-button-primary", type_="submit")
               + ui.button("Clear", "/pharmacy", style="z-button-secondary")
               + "</form>")

    body = (
        alerts
        + ui.card(f"{len(rows)} item(s) · stock value ₹ {value:,.0f}",
                  ui.table(["Item", "Kind", "Form", "On hand", "Reorder at", "MRP",
                            "Next expiry"], rows,
                           empty="No items match that filter.", align_right=(3, 4, 5)),
                  actions=filters)
        + f'<div class="mt"{st(mt=5)}>' + _receive_card() + "</div>")
    return render(request, "Pharmacy & stock", "pharmacy", body,
                  actions=ui.button("Issue to patient", "/pharmacy/issue",
                                    style="z-button-primary", ico="pill"))


def _receive_card() -> str:
    items = [(i["id"], f'{i["name"]} ({i["code"]}) — {i["on_hand"]:g} {i["unit"]}')
             for i in hospital.stock_list()]
    form = ('<form method="post" action="/pharmacy/receive">'
            + ui.grid(
                ui.field("Item", ui.select("item_id", items, "", blank="Select item",
                                           required=True), required=True),
                ui.field("Batch number", ui.text_input("batch_no", "",
                                                       placeholder="e.g. B24H09")),
                ui.field("Expiry date", ui.text_input("expiry", "", type_="date")),
                ui.field("Quantity received", ui.text_input(
                    "quantity", "", type_="number", attrs='step="0.01" min="0"',
                    required=True), required=True),
                ui.field("Cost price per unit (₹)", ui.text_input(
                    "cost_price", "", type_="number", attrs='step="0.01" min="0"')),
                ui.field("Supplier", ui.text_input("supplier", "")),
                cols=3)
            + f'<div class="display-flex justify-end"{st()}>'
            + ui.button("Receive stock", style="z-button-primary", type_="submit",
                        ico="package-plus")
            + "</div></form>")
    return ui.card("Goods receipt", ui.accordion([("Receive a batch into stock", form)],
                                                 open_first=False))


def receive(request: Request):
    try:
        hospital.receive_stock(
            request.f_int("item_id"), request.f("batch_no"),
            request.f_or_none("expiry"), request.f_float("quantity"),
            request.f_float("cost_price"), request.f_or_none("supplier"))
    except (ValueError, TypeError) as error:
        return redirect("/pharmacy", f"Could not receive stock: {error}", "danger")
    return redirect("/pharmacy", "Stock received.")


# ---------------------------------------------------------------------------
def issue_form(request: Request):
    preset_patient = request.q("patient")
    encounter_id = request.q("encounter")
    if encounter_id:
        enc = db.one("SELECT * FROM encounter WHERE id = ?", (encounter_id,))
        if enc is not None:
            preset_patient = str(enc["patient_id"])

    items = [(i["id"], f'{i["name"]} — {i["on_hand"]:g} {i["unit"]} in stock '
                       f'@ ₹{i["mrp"]:.2f}')
             for i in hospital.stock_list() if i["on_hand"] > 0]
    encounters = [(e["id"], f'{e["encounter_no"]} · {e["kind"]} · '
                            f'{ui.when(e["period_start"], 10)}')
                  for e in db.query(
                      "SELECT * FROM encounter WHERE status <> 'finished' "
                      "ORDER BY id DESC LIMIT 50")]

    body = (
        '<form method="post" action="/pharmacy/issue">'
        + ui.card("Issue from stock", (
            ui.grid(
                ui.field("Patient", ui.select("patient_id", patient_options(),
                                              preset_patient, blank="Select patient",
                                              required=True), required=True),
                ui.field("Against encounter", ui.select(
                    "encounter_id", encounters, encounter_id,
                    blank="Not linked (cash sale)"),
                    help_text="Linked issues are picked up automatically when the "
                              "encounter is billed."),
                cols=2)
            + ui.grid(
                ui.field("Item", ui.select("item_id", items, "", blank="Select item",
                                           required=True), required=True),
                ui.field("Quantity", ui.text_input(
                    "quantity", "1", type_="number", attrs='step="0.01" min="0.01"',
                    required=True), required=True),
                ui.field("Note", ui.text_input("note", "",
                                               placeholder="e.g. ward indent 12")),
                cols=3)),
            footer=(f'<div class="display-flex justify-between gap"{st(gap=2)}>'
                    + ui.button("Back to stock", "/pharmacy",
                                style="z-button-secondary")
                    + ui.button("Issue", style="z-button-primary",
                                size="z-button-medium", type_="submit")
                    + "</div>"))
        + "</form>"
        + f'<div class="mt"{st(mt=5)}>' + _recent_issues() + "</div>")
    return render(request, "Issue from pharmacy", "pharmacy", body,
                  breadcrumb=[("Pharmacy", "/pharmacy"), ("Issue", None)])


def _recent_issues() -> str:
    rows = db.query(
        "SELECT t.*, i.name, i.unit, p.name AS patient_name, e.encounter_no "
        "FROM stock_txn t JOIN stock_item i ON i.id = t.item_id "
        "LEFT JOIN patient p ON p.id = t.patient_id "
        "LEFT JOIN encounter e ON e.id = t.encounter_id "
        "WHERE t.kind = 'issue' ORDER BY t.id DESC LIMIT 30")
    table_rows = [[
        ui.when(r["created_at"]),
        ui.esc(r["name"]),
        f'{-r["quantity"]:g} {ui.esc(r["unit"])}',
        (f'<a class="z-link" href="/patients/{r["patient_id"]}">'
         f'{ui.esc(r["patient_name"])}</a>' if r["patient_id"] else "—"),
        ui.esc(r["encounter_no"] or "—"),
        f'₹ {-r["quantity"] * r["rate"]:.2f}',
        (ui.label_chip("billed", "success") if r["billed"]
         else ui.label_chip("unbilled", "warning")),
    ] for r in rows]
    return ui.card("Recent issues", ui.table(
        ["When", "Item", "Quantity", "Patient", "Encounter", "Value", "Billing"],
        table_rows, empty="Nothing issued yet.", align_right=(2, 5)))


def issue(request: Request):
    try:
        picked = hospital.issue_stock(
            request.f_int("item_id"), request.f_float("quantity"),
            request.f_int("patient_id"), request.f_int("encounter_id"),
            request.f_or_none("note"))
    except (ValueError, TypeError) as error:
        return redirect("/pharmacy/issue", f"Could not issue: {error}", "danger")
    detail = ", ".join(f'{p["quantity"]:g} from batch {p["batch_no"]}' for p in picked)
    return redirect("/pharmacy/issue", f"Issued {detail}.")


# ---------------------------------------------------------------------------
def item_detail(request: Request):
    iid = request.params["iid"]
    item = db.one("SELECT * FROM stock_item WHERE id = ?", (iid,))
    if item is None:
        return not_found(request, "Stock item")
    batches = db.query(
        "SELECT * FROM stock_batch WHERE item_id = ? ORDER BY "
        "COALESCE(expiry_date, '9999-12-31')", (iid,))
    txns = db.query(
        "SELECT t.*, p.name AS patient_name FROM stock_txn t "
        "LEFT JOIN patient p ON p.id = t.patient_id "
        "WHERE t.item_id = ? ORDER BY t.id DESC LIMIT 50", (iid,))
    on_hand = hospital.stock_on_hand(iid)

    facts = ui.dl([
        ("Code", item["code"]),
        ("Kind", item["kind"].title()),
        ("Form / strength", " ".join(filter(None, [item["form"], item["strength"]]))),
        ("Unit", item["unit"]),
        ("On hand", f'{on_hand:g} {item["unit"]}'),
        ("Reorder level", f'{item["reorder_level"]:g}'),
        ("MRP", f'₹ {item["mrp"]:.2f}'),
        ("Purchase price", f'₹ {item["purchase_price"]:.2f}'),
        ("GST", f'{item["gst_pct"]:g}%'),
        ("HSN", item["hsn_code"]),
        ("SNOMED CT", (f'{item["snomed_display"]} ({item["snomed_code"]})'
                       if item["snomed_code"] else None)),
    ], cols=3)

    batch_rows = [[
        ui.esc(b["batch_no"]),
        ui.esc(b["expiry_date"] or "—"),
        f'{b["quantity"]:g} {ui.esc(item["unit"])}',
        f'₹ {b["cost_price"]:.2f}',
        ui.esc(b["supplier"] or "—"),
        ui.when(b["received_at"], 10),
    ] for b in batches]

    txn_rows = [[
        ui.when(t["created_at"]),
        ui.label_chip(t["kind"], "success" if t["quantity"] > 0 else "warning"),
        f'{t["quantity"]:+g}',
        ui.esc(t["patient_name"] or "—"),
        ui.esc(t["note"] or ""),
    ] for t in txns]

    body = (
        ui.card(item["name"], facts,
                actions=ui.button("Issue this item",
                                  f'/pharmacy/issue?item={iid}',
                                  style="z-button-primary", ico="pill"))
        + f'<div class="mt"{st(mt=5)}>'
        + ui.card("Batches", ui.table(
            ["Batch", "Expiry", "Remaining", "Cost", "Supplier", "Received"],
            batch_rows, empty="No stock received yet.", align_right=(2, 3)))
        + "</div>"
        + f'<div class="mt"{st(mt=5)}>'
        + ui.card("Movement", ui.table(
            ["When", "Type", "Quantity", "Patient", "Note"], txn_rows,
            empty="No movement recorded.", align_right=(2,)))
        + "</div>")
    return render(request, item["name"], "pharmacy", body,
                  breadcrumb=[("Pharmacy", "/pharmacy"), (item["name"], None)])
