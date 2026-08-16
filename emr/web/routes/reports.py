"""Daily MIS — the numbers a small hospital is actually run on."""

from __future__ import annotations

from datetime import date, timedelta

from ... import db, hospital
from .. import ui
from ..common import render
from ..router import Request

st = ui.st


def register(app) -> None:
    app.add("GET", "/reports", index)
    app.add("GET", "/reports/dues", dues)


def _range(request: Request) -> tuple[str, str]:
    today = db.today_iso()
    start = request.q("from") or (date.fromisoformat(today)
                                  - timedelta(days=6)).isoformat()
    end = request.q("to") or today
    return start, end


def index(request: Request):
    start, end = _range(request)
    args = (start, end)

    admissions = db.scalar(
        "SELECT COUNT(*) FROM encounter WHERE kind = 'IPD' "
        "AND date(period_start) BETWEEN ? AND ?", args, default=0)
    discharges = db.scalar(
        "SELECT COUNT(*) FROM encounter WHERE kind = 'IPD' AND discharge_ts IS NOT NULL "
        "AND date(discharge_ts) BETWEEN ? AND ?", args, default=0)
    opd = db.scalar(
        "SELECT COUNT(*) FROM encounter WHERE kind = 'OPD' "
        "AND date(period_start) BETWEEN ? AND ?", args, default=0)
    new_patients = db.scalar(
        "SELECT COUNT(*) FROM patient WHERE date(created_at) BETWEEN ? AND ?",
        args, default=0)
    billed = db.scalar(
        "SELECT COALESCE(SUM(total_gross), 0) FROM invoice "
        "WHERE status <> 'cancelled' AND date(date) BETWEEN ? AND ?", args, default=0)
    collected = db.scalar(
        "SELECT COALESCE(SUM(amount), 0) FROM payment "
        "WHERE date(received_at) BETWEEN ? AND ?", args, default=0)
    outstanding = db.scalar(
        "SELECT COALESCE(SUM(total_gross - amount_paid), 0) FROM invoice "
        "WHERE status <> 'cancelled'", default=0)
    beds = hospital.bed_stats()

    tiles = "".join([
        ui.stat("OPD visits", opd, "stethoscope", "info"),
        ui.stat("Admissions", admissions, "hospital", "warning"),
        ui.stat("Discharges", discharges, "log-out", "success"),
        ui.stat("New patients", new_patients, "user-plus"),
        ui.stat("Bed occupancy", f'{beds["occupancy_pct"]}%', "bed", "danger"),
        ui.stat("Billed", f"₹ {billed:,.0f}", "receipt-indian-rupee"),
        ui.stat("Collected", f"₹ {collected:,.0f}", "banknote", "success"),
        ui.stat("Outstanding", f"₹ {outstanding:,.0f}", "circle-alert", "danger"),
    ])

    picker = ('<form method="get" action="/reports" '
              f'class="display-flex gap items-end flex-wrap"{st(gap=2)}>'
              + ui.text_input("from", start, type_="date")
              + ui.text_input("to", end, type_="date")
              + ui.button("Apply", style="z-button-primary", type_="submit")
              + "</form>")

    collections = db.query(
        "SELECT mode, COUNT(*) AS n, SUM(amount) AS total FROM payment "
        "WHERE date(received_at) BETWEEN ? AND ? GROUP BY mode ORDER BY total DESC",
        args)
    collection_rows = [[
        ui.esc(dict(hospital.PAYMENT_MODES).get(r["mode"], r["mode"])),
        str(r["n"]), f'₹ {r["total"]:,.2f}',
    ] for r in collections]
    if collection_rows:
        collection_rows.append(["<strong>Total</strong>",
                                f'<strong>{sum(r["n"] for r in collections)}</strong>',
                                f'<strong>₹ {collected:,.2f}</strong>'])

    diagnoses = db.query(
        "SELECT text, icd10_code, COUNT(*) AS n FROM condition "
        "WHERE category = 'diagnosis' AND date(recorded_at) BETWEEN ? AND ? "
        "GROUP BY text ORDER BY n DESC LIMIT 10", args)
    diagnosis_rows = [[ui.esc(d["text"]), ui.esc(d["icd10_code"] or "—"), str(d["n"])]
                      for d in diagnoses]

    labs = db.query(
        "SELECT panel_display, COUNT(*) AS n, SUM(price) AS value FROM lab_order "
        "WHERE date(ordered_at) BETWEEN ? AND ? GROUP BY panel_display "
        "ORDER BY n DESC LIMIT 10", args)
    lab_rows = [[ui.esc(r["panel_display"]), str(r["n"]), f'₹ {r["value"] or 0:,.0f}']
                for r in labs]

    pharmacy = db.query(
        "SELECT i.name, SUM(-t.quantity) AS qty, SUM(-t.quantity * t.rate) AS value "
        "FROM stock_txn t JOIN stock_item i ON i.id = t.item_id "
        "WHERE t.kind = 'issue' AND date(t.created_at) BETWEEN ? AND ? "
        "GROUP BY i.id ORDER BY value DESC LIMIT 10", args)
    pharmacy_rows = [[ui.esc(r["name"]), f'{r["qty"]:g}', f'₹ {r["value"]:,.0f}']
                     for r in pharmacy]

    ward_rows = [[
        ui.esc(group["ward"]["name"]),
        str(len(group["beds"])),
        str(group["occupied"]),
        str(group["vacant"]),
        f'{round(100 * group["occupied"] / len(group["beds"])) if group["beds"] else 0}%',
    ] for group in hospital.ward_board()]

    low = hospital.low_stock()
    expiring = hospital.expiring_batches()
    alert_rows = [[ui.label_chip("reorder", "danger"), ui.esc(i["name"]),
                   f'{i["on_hand"]:g} {ui.esc(i["unit"])} left '
                   f'(reorder at {i["reorder_level"]:g})'] for i in low]
    alert_rows += [[ui.label_chip("expiry", "warning"), ui.esc(b["name"]),
                    f'batch {ui.esc(b["batch_no"])} expires {ui.esc(b["expiry_date"])}']
                   for b in expiring]

    body = (
        f'<div class="display-grid gap sm:grid-cols lg:grid-cols"'
        + st(gap=4, sm_grid_cols=2, lg_grid_cols=4) + ">" + tiles + "</div>"
        + f'<div class="mt"{st(mt=5)}>'
        + ui.card(f"Reporting period {start} to {end}",
                  f'<p class="color"{st(color="var(--z-muted-f)")}>'
                  "Bed occupancy and outstanding dues are live figures; everything "
                  "else is for the selected range.</p>", actions=picker)
        + "</div>"
        + f'<div class="mt display-grid gap lg:grid-cols"{st(mt=5, gap=4, lg_grid_cols=2)}>'
        + ui.card("Collections by mode", ui.table(
            ["Mode", "Receipts", "Amount"], collection_rows,
            empty="Nothing collected in this period.", align_right=(1, 2)),
            actions=ui.button("Outstanding dues", "/reports/dues", ico="circle-alert"))
        + ui.card("Ward occupancy", ui.table(
            ["Ward", "Beds", "Occupied", "Vacant", "Occupancy"], ward_rows,
            empty="No wards configured.", align_right=(1, 2, 3, 4)))
        + ui.card("Top diagnoses", ui.table(
            ["Diagnosis", "ICD-10", "Count"], diagnosis_rows,
            empty="No diagnoses recorded in this period.", align_right=(2,)))
        + ui.card("Laboratory workload", ui.table(
            ["Panel", "Orders", "Value"], lab_rows,
            empty="No lab orders in this period.", align_right=(1, 2)))
        + ui.card("Pharmacy consumption", ui.table(
            ["Item", "Quantity", "Value"], pharmacy_rows,
            empty="Nothing issued in this period.", align_right=(1, 2)))
        + ui.card("Stock alerts", ui.table(
            ["Type", "Item", "Detail"], alert_rows,
            empty="No reorder or expiry alerts."))
        + "</div>")
    return render(request, "Reports", "reports", body)


def dues(request: Request):
    rows = hospital.outstanding_invoices()
    total = sum(r["balance"] for r in rows)
    table_rows = [[
        f'<a class="z-link" href="/billing/{r["id"]}">{ui.esc(r["invoice_no"])}</a>',
        f'<a class="z-link" href="/patients/{r["patient_id"]}">'
        f'{ui.esc(r["patient_name"])}</a>'
        + ui.muted(f'{r["mrn"]} · {r["phone"]}'),
        ui.esc(r["type_display"]),
        ui.when(r["date"], 10),
        f'₹ {r["total_gross"]:,.2f}',
        f'₹ {r["amount_paid"]:,.2f}',
        f'<strong>₹ {r["balance"]:,.2f}</strong>',
    ] for r in rows]
    body = ui.card(
        f"{len(table_rows)} unpaid invoice(s) · ₹ {total:,.2f} outstanding",
        ui.table(["Invoice", "Patient", "Type", "Date", "Billed", "Paid", "Balance"],
                 table_rows, empty="Everything is settled.", align_right=(4, 5, 6)),
        actions=ui.button("Back to reports", "/reports", style="z-button-secondary"))
    return render(request, "Outstanding dues", "reports", body,
                  breadcrumb=[("Reports", "/reports"), ("Outstanding dues", None)])
