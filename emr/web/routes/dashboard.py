"""Dashboard."""

from __future__ import annotations

from ... import db, hospital
from .. import ui
from ..common import render, status_label
from ..router import Request

st = ui.st


def register(app) -> None:
    app.add("GET", "/", index)


def index(request: Request):
    today = db.today_iso()
    beds = hospital.bed_stats()
    counts = {
        "patients": db.scalar("SELECT COUNT(*) FROM patient", default=0),
        "opd_today": db.scalar(
            "SELECT COUNT(*) FROM encounter WHERE kind = 'OPD' AND date(period_start) = ?",
            (today,), default=0),
        "waiting": db.scalar(
            "SELECT COUNT(*) FROM appointment WHERE slot_date = ? "
            "AND status IN ('booked','arrived')", (today,), default=0),
        "labs_pending": db.scalar(
            "SELECT COUNT(*) FROM lab_order WHERE status <> 'final'", default=0),
        "collected_today": db.scalar(
            "SELECT COALESCE(SUM(amount), 0) FROM payment WHERE date(received_at) = ?",
            (today,), default=0),
        "dues": db.scalar(
            "SELECT COALESCE(SUM(total_gross - amount_paid), 0) FROM invoice "
            "WHERE status <> 'cancelled'", default=0),
    }
    low_stock = len(hospital.low_stock())

    tiles = "".join([
        ui.stat("Registered patients", counts["patients"], "users"),
        ui.stat("OPD visits today", counts["opd_today"], "stethoscope", "info"),
        ui.stat("Queue today", counts["waiting"], "calendar-clock", "warning"),
        ui.stat("Beds occupied", f'{beds["occupied"]}/{beds["total"]}', "bed", "danger"),
        ui.stat("Occupancy", f'{beds["occupancy_pct"]}%', "percent", "info"),
        ui.stat("Lab orders pending", counts["labs_pending"], "flask-conical", "warning"),
        ui.stat("Collected today", f"₹ {counts['collected_today']:,.0f}",
                "banknote", "success"),
        ui.stat("Outstanding dues", f"₹ {counts['dues']:,.0f}", "circle-alert", "danger"),
    ])

    alerts = ""
    if low_stock:
        alerts = (f'<div class="z-alert z-alert-warning" data-z-alert>'
                  f"<strong>{low_stock} pharmacy item(s)</strong> at or below reorder "
                  'level — <a class="z-link" href="/pharmacy">check stock</a>.</div>')

    recent_visits = db.query(
        "SELECT e.*, p.name AS patient_name, p.mrn, d.name AS doctor_name "
        "FROM encounter e JOIN patient p ON p.id = e.patient_id "
        "LEFT JOIN practitioner d ON d.id = e.practitioner_id "
        "ORDER BY e.id DESC LIMIT 8")
    visit_rows = [[
        f'<a class="z-link" href="/{"opd" if v["kind"] == "OPD" else "ipd"}/{v["id"]}">'
        f'{ui.esc(v["encounter_no"])}</a>',
        f'<a class="z-link" href="/patients/{v["patient_id"]}">'
        f'{ui.esc(v["patient_name"])}</a>',
        ui.label_chip(v["kind"], "info" if v["kind"] == "OPD" else "warning"),
        ui.esc(v["doctor_name"] or "—"),
        ui.when(v["period_start"]),
        status_label(v["status"]),
    ] for v in recent_visits]


    quick = ui.card("Quick actions", (
        f'<div class="display-flex gap flex-wrap"{st(gap=2)}>'
        + ui.button("Register patient", "/patients/new", style="z-button-primary",
                    ico="user-plus")
        + ui.button("Book appointment", "/appointments", ico="calendar-clock")
        + ui.button("OPD registration", "/opd/new", ico="calendar-plus")
        + ui.button("Ward board", "/beds", ico="layout-grid")
        + ui.button("IPD admission", "/ipd/new", ico="hospital")
        + ui.button("Lab order", "/lab/new", ico="flask-conical")
        + ui.button("Issue medicines", "/pharmacy/issue", ico="pill")
        + ui.button("Raise invoice", "/billing/new", ico="receipt-indian-rupee")
        + ui.button("Reports", "/reports", ico="chart-column")
        + "</div>"))

    body = (
        (f'<div class="mb"{st(mb=4)}>' + alerts + "</div>" if alerts else "")
        + f'<div class="display-grid gap sm:grid-cols lg:grid-cols"'
        + st(gap=4, sm_grid_cols=2, lg_grid_cols=4) + ">" + tiles + "</div>"
        + f'<div class="mt"{st(mt=5)}>' + quick + "</div>"
        + f'<div class="mt"{st(mt=5)}>'
        + ui.card("Recent encounters", ui.table(
            ["Number", "Patient", "Type", "Doctor", "When", "Status"], visit_rows,
            empty="No encounters yet — register a patient to begin."))
        + "</div>")

    return render(request, "Dashboard", "dashboard", body)
