"""Dialysis unit — machine board, courses, sessions and the flowsheet."""

from __future__ import annotations

from typing import Any

from ... import db, dialysis
from .. import ui
from ..common import (
    patient_cell,
    doctor_options, not_found, patient_options, render, term_options,
)
from ..router import Request, redirect

st = ui.st


def register(app) -> None:
    app.add("GET", "/dialysis", index)
    # machines
    app.add("GET", "/dialysis/machines", machine_list)
    app.add("POST", "/dialysis/machines", add_machine)
    app.add("POST", "/dialysis/machines/<int:mid>/status", machine_status)
    app.add("POST", "/dialysis/machines/<int:mid>/service", machine_service)
    # courses
    app.add("GET", "/dialysis/courses", course_list)
    app.add("GET", "/dialysis/courses/new", new_course)
    app.add("POST", "/dialysis/courses", create_course)
    app.add("GET", "/dialysis/courses/<int:cid>", course_detail)
    app.add("POST", "/dialysis/courses/<int:cid>/close", close_course)
    app.add("POST", "/dialysis/courses/<int:cid>/reopen", reopen_course)
    # sessions
    app.add("GET", "/dialysis/sessions", session_list)
    app.add("GET", "/dialysis/sessions/new", new_session)
    app.add("POST", "/dialysis/sessions", create_session)
    app.add("GET", "/dialysis/sessions/<int:sid>", session_detail)
    app.add("POST", "/dialysis/sessions/<int:sid>/start", start_session)
    app.add("POST", "/dialysis/sessions/<int:sid>/parameters", save_parameters)
    app.add("POST", "/dialysis/sessions/<int:sid>/vitals", save_vitals)
    app.add("POST", "/dialysis/sessions/<int:sid>/readings", add_reading)
    app.add("POST", "/dialysis/sessions/<int:sid>/readings/<int:rid>/delete",
            delete_reading)
    app.add("POST", "/dialysis/sessions/<int:sid>/complete", complete_session)


# ===========================================================================
# unit dashboard
# ===========================================================================
def index(request: Request):
    day = request.q("date") or db.today_iso()
    stats = dialysis.unit_stats(day)
    today = dialysis.sessions_on(day)

    tiles = "".join([
        ui.stat("Machines", stats["machines_total"], "server"),
        ui.stat("Running now", stats["machines"]["in-use"], "activity", "danger"),
        ui.stat("Available", stats["machines"]["available"], "circle-check", "success"),
        ui.stat("Maintenance", stats["machines"]["maintenance"], "wrench", "warning"),
        ui.stat("Sessions today", stats["scheduled"], "calendar-clock", "info"),
        ui.stat("Active courses", stats["active_courses"], "repeat", "primary"),
    ])


    session_rows = [[
        f'<a class="z-link" href="/dialysis/sessions/{s["id"]}">'
        f'{ui.esc(s["session_no"])}</a>'
        + ui.muted(f'{s["course_no"]} · {s["modality_display"]}'),
        patient_cell(s),
        ui.esc((s["scheduled_at"] or "")[11:16] or "—")
        + (ui.muted(s["machine_code"]) if s["machine_code"] else ""),
        ui.label_chip(*dialysis.SESSION_STATUS[s["status"]]),
    ] for s in today]

    picker = ('<form method="get" action="/dialysis" '
              f'class="display-flex gap items-end"{st(gap=2)}>'
              + ui.text_input("date", day, type_="date")
              + ui.button("Show", style="z-button-primary", type_="submit")
              + "</form>")


    quick = ui.card("Quick actions", (
        f'<div class="display-flex gap flex-wrap"{st(gap=2)}>'
        + ui.button("Create session", "/dialysis/sessions/new",
                    style="z-button-primary", ico="calendar-plus")
        + ui.button("New course", "/dialysis/courses/new", ico="plus")
        + ui.button("Courses", "/dialysis/courses", ico="clipboard-list")
        + ui.button("Machines", "/dialysis/machines", ico="server")
        + ui.button("All sessions", "/dialysis/sessions", ico="list")
        + "</div>"))

    body = (
        f'<div class="display-grid gap sm:grid-cols lg:grid-cols"'
        + st(gap=4, sm_grid_cols=2, lg_grid_cols=3) + ">" + tiles + "</div>"
        + f'<div class="mt"{st(mt=5)}>' + quick + "</div>"
        + f'<div class="mt"{st(mt=5)}>'
        + ui.card(f"Sessions on {day} — {len(session_rows)}",
                  ui.table(["Session", "Patient", "Time", "Status"], session_rows,
                           empty="Nothing scheduled for this date."),
                  actions=picker)
        + "</div>")
    return render(request, "Dialysis unit", "dialysis", body)


def _machine_actions(machine) -> str:
    targets = {"available": ("maintenance", "Send to service", "z-button-secondary"),
               "maintenance": ("available", "Back in service", "z-button-primary")}
    entry = targets.get(machine["status"])
    if not entry:
        return ""
    status, label, style = entry
    return ui.post_button(f'/dialysis/machines/{machine["id"]}/status', label,
                          style=style, fields={"status": status})


def machine_status(request: Request):
    try:
        dialysis.set_machine_status(request.params["mid"], request.f("status"))
    except ValueError as error:
        return redirect("/dialysis", str(error), "danger")
    return redirect("/dialysis", "Machine updated.")


# ===========================================================================
# courses
# ===========================================================================
def new_course(request: Request):
    preset = request.q("patient")
    encounters = [(e["id"], f'{e["encounter_no"]} · {e["kind"]} · '
                            f'{ui.when(e["period_start"], 10)}')
                  for e in db.query("SELECT * FROM encounter "
                                    "WHERE status <> 'finished' ORDER BY id DESC "
                                    "LIMIT 50")]
    body = (
        '<form method="post" action="/dialysis/courses">'
        + ui.card("Patient and prescription", (
            ui.grid(
                ui.field("Patient", ui.select("patient_id", patient_options(), preset,
                                              blank="Select patient", required=True),
                         required=True),
                ui.field("Nephrologist", ui.select("practitioner_id",
                                                   doctor_options(), "",
                                                   blank="Select doctor")),
                ui.field("Against encounter", ui.select(
                    "encounter_id", encounters, "", blank="Not linked"),
                    help_text="Linked sessions bill onto that encounter and appear "
                              "in its clinical document."),
                cols=3)
            + ui.grid(
                ui.field("Modality", ui.select(
                    "modality_code", term_options("dialysis_modality"), "302497006",
                    required=True), required=True),
                ui.field("Indication", ui.select(
                    "indication_code", term_options("diagnosis"), "",
                    blank="Not stated")),
                ui.field("Vascular access", ui.select(
                    "access_code", term_options("vascular_access", False), "av-fistula")),
                ui.field("Access site", ui.text_input(
                    "access_site", "", placeholder="Left radiocephalic")),
                ui.field("Sessions per week", ui.text_input(
                    "sessions_per_week", "3", type_="number",
                    attrs='step="0.5" min="0.5" max="7"')),
                ui.field("Session duration (minutes)", ui.text_input(
                    "duration_minutes", "240", type_="number",
                    attrs='step="15" min="30"')),
                cols=3)), style=""))
    body += (
        f'<div class="mt"{st(mt=4)}>'
        + ui.card("Standing parameters", (
            ui.grid(
                ui.field("Dry weight (kg)", ui.text_input(
                    "dry_weight_kg", "", type_="number", attrs='step="0.1" min="0"')),
                ui.field("Dialyser", ui.select("dialyser",
                                               term_options("dialyser", False), "",
                                               blank="Not specified")),
                ui.field("Blood flow rate Qb (mL/min)", ui.text_input(
                    "blood_flow_rate", "300", type_="number", attrs='step="10"')),
                ui.field("Dialysate flow Qd (mL/min)", ui.text_input(
                    "dialysate_flow_rate", "500", type_="number", attrs='step="100"')),
                cols=4)
            + ui.grid(
                ui.field("Anticoagulant", ui.select(
                    "anticoagulant_code", term_options("anticoagulant", False),
                    "372877000")),
                ui.field("Heparin bolus (units)", ui.text_input(
                    "heparin_bolus_units", "", type_="number", attrs='step="100"')),
                ui.field("Heparin hourly (units/hr)", ui.text_input(
                    "heparin_hourly_units", "", type_="number", attrs='step="100"')),
                ui.field("Charge per session (₹)", ui.text_input(
                    "session_charge", "2200", type_="number", attrs='step="1" min="0"')),
                cols=4)
            + ui.grid(
                ui.field("Dialysate Na (mmol/L)", ui.text_input(
                    "dialysate_na", "138", type_="number", attrs='step="1"')),
                ui.field("Dialysate K (mmol/L)", ui.text_input(
                    "dialysate_k", "2", type_="number", attrs='step="0.5"')),
                ui.field("Dialysate Ca (mmol/L)", ui.text_input(
                    "dialysate_ca", "1.25", type_="number", attrs='step="0.25"')),
                ui.field("Bicarbonate (mmol/L)", ui.text_input(
                    "dialysate_bicarb", "32", type_="number", attrs='step="1"')),
                cols=4)
            + ui.field("Note", ui.textarea("note", "", rows=2))),
            footer=(f'<div class="display-flex justify-between gap"{st(gap=2)}>'
                    + ui.button("Cancel", "/dialysis", style="z-button-secondary")
                    + ui.button("Create course", style="z-button-primary",
                                size="z-button-medium", type_="submit") + "</div>"))
        + "</div></form>")
    return render(request, "New dialysis course", "dialysis", body,
                  breadcrumb=[("Dialysis", "/dialysis"), ("New course", None)])


def _term(kind: str, code: str):
    return db.term(kind, code) if code else None


def create_course(request: Request):
    patient_id = request.f_int("patient_id")
    if not patient_id:
        return redirect("/dialysis/courses/new", "Select a patient first.", "danger")
    modality = _term("dialysis_modality", request.f("modality_code"))
    if modality is None:
        return redirect("/dialysis/courses/new", "Pick a modality.", "danger")
    indication = _term("diagnosis", request.f("indication_code"))
    access = _term("vascular_access", request.f("access_code"))
    anticoag = _term("anticoagulant", request.f("anticoagulant_code"))

    def num(name: str) -> float | None:
        raw = request.f(name)
        try:
            return float(raw) if raw else None
        except ValueError:
            return None

    course_id = dialysis.create_course({
        "patient_id": patient_id,
        "encounter_id": request.f_int("encounter_id"),
        "practitioner_id": request.f_int("practitioner_id"),
        "modality_code": modality["code"], "modality_display": modality["display"],
        "indication_code": indication["code"] if indication else None,
        "indication_display": indication["display"] if indication else None,
        "access_code": access["code"] if access else None,
        "access_display": access["display"] if access else None,
        "access_site": request.f_or_none("access_site"),
        "sessions_per_week": num("sessions_per_week") or 3,
        "duration_minutes": int(num("duration_minutes") or 240),
        "dry_weight_kg": num("dry_weight_kg"),
        "dialyser": request.f_or_none("dialyser"),
        "anticoagulant_code": anticoag["code"] if anticoag else None,
        "anticoagulant_display": anticoag["display"] if anticoag else None,
        "heparin_bolus_units": num("heparin_bolus_units"),
        "heparin_hourly_units": num("heparin_hourly_units"),
        "blood_flow_rate": num("blood_flow_rate"),
        "dialysate_flow_rate": num("dialysate_flow_rate"),
        "dialysate_na": num("dialysate_na"), "dialysate_k": num("dialysate_k"),
        "dialysate_ca": num("dialysate_ca"),
        "dialysate_bicarb": num("dialysate_bicarb"),
        "session_charge": num("session_charge") or 0,
        "note": request.f_or_none("note"),
    })
    number = db.scalar("SELECT course_no FROM dialysis_course WHERE id = ?",
                       (course_id,))
    return redirect(f"/dialysis/courses/{course_id}", f"Course {number} created.")


def course_detail(request: Request):
    cid = request.params["cid"]
    row = dialysis.course(cid)
    if row is None:
        return not_found(request, "Dialysis course")
    sessions = dialysis.sessions_for_course(cid)

    prescription = ui.dl([
        ("Course", row["course_no"]),
        ("Modality", f'{row["modality_display"]} ({row["modality_code"]})'),
        ("Indication", row["indication_display"]),
        ("Access", " · ".join(filter(None, [row["access_display"],
                                            row["access_site"]]))),
        ("Prescription", f'{row["sessions_per_week"]:g} per week × '
                         f'{row["duration_minutes"]} min'),
        ("Dry weight", f'{row["dry_weight_kg"]:g} kg' if row["dry_weight_kg"] else None),
        ("Dialyser", row["dialyser"]),
        ("Qb / Qd", f'{row["blood_flow_rate"]:g} / {row["dialysate_flow_rate"]:g} mL/min'
         if row["blood_flow_rate"] and row["dialysate_flow_rate"] else None),
        ("Anticoagulant", " · ".join(filter(None, [
            row["anticoagulant_display"],
            f'{row["heparin_bolus_units"]:g} U bolus'
            if row["heparin_bolus_units"] else None,
            f'{row["heparin_hourly_units"]:g} U/hr'
            if row["heparin_hourly_units"] else None]))),
        ("Dialysate", " · ".join(filter(None, [
            f'Na {row["dialysate_na"]:g}' if row["dialysate_na"] else None,
            f'K {row["dialysate_k"]:g}' if row["dialysate_k"] else None,
            f'Ca {row["dialysate_ca"]:g}' if row["dialysate_ca"] else None,
            f'HCO3 {row["dialysate_bicarb"]:g}' if row["dialysate_bicarb"] else None]))),
        ("Nephrologist", row["doctor_name"]),
        ("Charge per session", f'₹ {row["session_charge"]:,.2f}'),
        ("Started", row["started_on"]),
        ("Ended", row["ended_on"]),
        ("Note", row["note"]),
    ], cols=3)

    session_rows = [[
        f'<a class="z-link" href="/dialysis/sessions/{s["id"]}">'
        f'#{s["seq"]} · {ui.esc(s["session_no"])}</a>',
        ui.esc(ui.when(s["scheduled_at"]))
        + (ui.muted(s["machine_code"]) if s["machine_code"] else ""),
        ui.esc(_treatment_summary(s)),
        ui.label_chip(*dialysis.SESSION_STATUS[s["status"]])
        + (" " + ui.label_chip(s["complication_display"], "danger")
           if s["complication_display"] else ""),
    ] for s in sessions]

    actions = ui.label_chip(*dialysis.COURSE_STATUS[row["status"]])
    if row["status"] == "active":
        actions += ui.button("Create session", f"/dialysis/sessions/new?course={cid}",
                             style="z-button-primary", size="z-button-small",
                             ico="calendar-plus")
        actions += ui.confirm_form(
            f"/dialysis/courses/{cid}/close", "Close course",
            f'Close course {row["course_no"]}? No further sessions can be added.',
            size="z-button-small")
    else:
        actions += ui.post_button(f"/dialysis/courses/{cid}/reopen", "Reopen",
                                  size="z-button-small", ico="rotate-ccw")

    body = (
        ui.patient_header(dict(row) | {"id": row["patient_id"], "name":
                                       row["patient_name"]}, extra=actions)
        + f'<div class="mt"{st(mt=5)}>'
        + ui.card("Prescription", prescription) + "</div>"
        + f'<div class="mt"{st(mt=5)}>'
        + ui.card(f"Sessions — {len(session_rows)}",
                  ui.table(["Session", "Scheduled", "Treatment", "Status"],
                           session_rows,
                           empty="No sessions yet — create the first one."),
                  actions=(ui.button("Create session",
                                     f"/dialysis/sessions/new?course={cid}",
                                     style="z-button-primary", ico="calendar-plus")
                           if row["status"] == "active" else ""))
        + "</div>")
    return render(request, f'Course {row["course_no"]}', "dialysis", body,
                  breadcrumb=[("Dialysis", "/dialysis"), (row["course_no"], None)])


def close_course(request: Request):
    cid = request.params["cid"]
    try:
        dialysis.close_course(cid)
    except ValueError as error:
        return redirect(f"/dialysis/courses/{cid}", str(error), "danger")
    return redirect(f"/dialysis/courses/{cid}", "Course closed.")




# ===========================================================================
# machines
# ===========================================================================
def machine_list(request: Request):
    rows = dialysis.machines(include_retired=True)
    stats = dialysis.unit_stats(db.today_iso())

    tiles = "".join([
        ui.stat("Machines", stats["machines_total"], "server"),
        ui.stat("Available", stats["machines"]["available"], "circle-check", "success"),
        ui.stat("In use", stats["machines"]["in-use"], "activity", "danger"),
        ui.stat("Maintenance", stats["machines"]["maintenance"], "wrench", "warning"),
    ])

    table_rows = []
    for m in rows:
        label, kind = dialysis.MACHINE_STATUS[m["status"]]
        occupant = "—"
        if m["status"] == "in-use" and m["patient_name"]:
            occupant = (f'<a class="z-link" href="/dialysis/sessions/{m["session_id"]}">'
                        f'{ui.esc(m["patient_name"])}</a>'
                        + ui.muted(m["session_no"]))
        table_rows.append([
            f'<strong>{ui.esc(m["code"])}</strong>'
            + ui.muted(" · ".join(filter(None, [m["model"], m["serial_no"]]))),
            ui.esc(m["location"] or "—"),
            ui.label_chip(label, kind)
            + (occupant if occupant != "—" else ""),
            ui.esc(m["last_service"] or "—"),
            f'<div class="display-flex gap flex-wrap"{st(gap=1)}>'
            + _machine_actions(m) + _service_form(m) + "</div>",
        ])

    add = ('<form method="post" action="/dialysis/machines">'
           + ui.grid(
               ui.field("Machine code", ui.text_input("code", "", required=True,
                                                      placeholder="HD-06"),
                        required=True),
               ui.field("Model", ui.text_input("model", "",
                                               placeholder="Fresenius 4008S")),
               ui.field("Manufacturer", ui.text_input("manufacturer", "")),
               ui.field("Serial number", ui.text_input("serial_no", "")),
               ui.field("Location", ui.text_input("location", "",
                                                  placeholder="Dialysis unit")),
               ui.field("Last serviced", ui.text_input("last_service", "",
                                                       type_="date")),
               cols=3)
           + f'<div class="display-flex justify-end"{st()}>'
           + ui.button("Add machine", style="z-button-primary", type_="submit",
                       ico="plus")
           + "</div></form>")

    body = (
        f'<div class="display-grid gap sm:grid-cols lg:grid-cols"'
        + st(gap=4, sm_grid_cols=2, lg_grid_cols=4) + ">" + tiles + "</div>"
        + f'<div class="mt"{st(mt=5)}>'
        + ui.card(f"{len(table_rows)} machine(s)",
                  ui.table(["Machine", "Location", "Status", "Last service", ""],
                           table_rows,
                           empty="No machines configured yet."))
        + "</div>"
        + f'<div class="mt"{st(mt=5)}>'
        + ui.card("Add a machine", ui.accordion([("New dialysis machine", add)],
                                                open_first=not table_rows))
        + "</div>")
    return render(request, "Dialysis machines", "dialysis-machines", body,
                  breadcrumb=[("Dialysis", "/dialysis"), ("Machines", None)])


def _service_form(machine) -> str:
    if machine["status"] == "in-use":
        return ""
    return (f'<form method="post" action="/dialysis/machines/{machine["id"]}/service" '
            f'class="display-flex gap items-center"{st(gap=1)}>'
            + ui.text_input("last_service", db.today_iso(), type_="date",
                            size="z-form-small", attrs='style="max-width:9.5rem"')
            + ui.button("Log service", size="z-button-xsmall", type_="submit")
            + "</form>")


def add_machine(request: Request):
    code = request.f("code")
    if not code:
        return redirect("/dialysis/machines", "A machine needs a code.", "danger")
    if db.one("SELECT id FROM dialysis_machine WHERE code = ?", (code,)):
        return redirect("/dialysis/machines", f"{code} already exists.", "danger")
    db.insert("dialysis_machine", {
        "code": code, "model": request.f_or_none("model"),
        "manufacturer": request.f_or_none("manufacturer"),
        "serial_no": request.f_or_none("serial_no"),
        "location": request.f_or_none("location"),
        "last_service": request.f_or_none("last_service"),
        "status": "available", "active": 1,
    })
    return redirect("/dialysis/machines", f"{code} added.")


def machine_service(request: Request):
    mid = request.params["mid"]
    db.update("dialysis_machine", mid,
              {"last_service": request.f("last_service") or db.today_iso()})
    return redirect("/dialysis/machines", "Service date recorded.")


# ===========================================================================
# course list
# ===========================================================================
COURSE_FILTERS = [("active", "Active"), ("closed", "Closed / completed"),
                  ("all", "All")]


def course_list(request: Request):
    scope = request.q("status") or "active"
    if scope not in dict(COURSE_FILTERS):
        scope = "active"
    everything = dialysis.courses()
    if scope == "active":
        rows = [c for c in everything if c["status"] == "active"]
    elif scope == "closed":
        rows = [c for c in everything if c["status"] != "active"]
    else:
        rows = everything

    counts = {
        "active": sum(1 for c in everything if c["status"] == "active"),
        "closed": sum(1 for c in everything if c["status"] != "active"),
        "all": len(everything),
    }

    active_attr = ' class="z-active"'
    tabs = "".join(
        "<li" + (active_attr if key == scope else "") + ">"
        + f'<a class="px pb pt"{st(px=4, pb=3, pt=2)} '
        + f'href="/dialysis/courses?status={key}">{ui.esc(label)} '
        + f'({counts[key]})</a></li>'
        for key, label in COURSE_FILTERS)

    table_rows = [[
        f'<a class="z-link" href="/dialysis/courses/{c["id"]}">'
        f'{ui.esc(c["course_no"])}</a>' + ui.muted(c["modality_display"]),
        patient_cell(c),
        f'{c["sessions_per_week"]:g}/week × {c["duration_minutes"]} min'
        + (ui.muted(" · ".join(filter(None, [c["access_display"],
                                             c["access_site"]])))
           if c["access_display"] else ""),
        (f'{c["done"]} done'
         + (ui.muted(f'last {ui.when(c["last_run"], 10)}') if c["last_run"] else "")),
        ui.label_chip(*dialysis.COURSE_STATUS[c["status"]]),
        (ui.button("Create session", f'/dialysis/sessions/new?course={c["id"]}',
                   size="z-button-xsmall", style="z-button-primary")
         if c["status"] == "active" else
         ui.button("Open", f'/dialysis/courses/{c["id"]}', size="z-button-xsmall")),
    ] for c in rows]

    empty = {"active": "No active courses. Create one to start a patient on dialysis.",
             "closed": "No closed or completed courses yet.",
             "all": "No dialysis courses yet."}[scope]

    body = (
        f'<ul data-z-tab>{tabs}</ul>'
        + f'<div class="mt"{st(mt=4)}>'
        + ui.card(f"{len(table_rows)} course(s)",
                  ui.table(["Course", "Patient", "Prescription", "Progress",
                            "Status", ""], table_rows, empty=empty),
                  actions=(ui.button("New course", "/dialysis/courses/new",
                                     style="z-button-primary", ico="plus")))
        + "</div>")
    return render(request, "Dialysis courses", "dialysis-courses", body,
                  breadcrumb=[("Dialysis", "/dialysis"), ("Courses", None)])


def _treatment_summary(s) -> str:
    """What the run delivered, in one line: duration, fluid removed, adequacy."""
    parts = []
    if s["duration_minutes"]:
        parts.append(f'{s["duration_minutes"]} min')
    if s["uf_achieved_ml"]:
        parts.append(f'UF {s["uf_achieved_ml"]:.0f} mL')
    if s["ktv"]:
        parts.append(f'Kt/V {s["ktv"]:g}')
    return " · ".join(parts) or "—"


def reopen_course(request: Request):
    cid = request.params["cid"]
    db.update("dialysis_course", cid, {"status": "active", "ended_on": None})
    return redirect(f"/dialysis/courses/{cid}", "Course reopened.")


# ===========================================================================
# session list and creation
# ===========================================================================
def session_list(request: Request):
    scope = request.q("status") or ""
    sql = ("SELECT s.*, c.course_no, c.modality_display, p.name AS patient_name, "
           "       p.mrn, m.code AS machine_code FROM dialysis_session s "
           "JOIN dialysis_course c ON c.id = s.course_id "
           "JOIN patient p ON p.id = s.patient_id "
           "LEFT JOIN dialysis_machine m ON m.id = s.machine_id")
    params: list[Any] = []
    if scope in dialysis.SESSION_STATUS:
        sql += " WHERE s.status = ?"
        params.append(scope)
    rows = db.query(sql + " ORDER BY s.id DESC LIMIT 300", params)

    filters = ('<form method="get" action="/dialysis/sessions" '
               f'class="display-flex gap items-end"{st(gap=2)}>'
               + ui.select("status", [(k, v[0]) for k, v
                                      in dialysis.SESSION_STATUS.items()],
                           scope, blank="All statuses")
               + ui.button("Filter", style="z-button-primary", type_="submit")
               + "</form>")

    table_rows = [[
        f'<a class="z-link" href="/dialysis/sessions/{s["id"]}">'
        f'{ui.esc(s["session_no"])}</a>' + ui.muted(f'#{s["seq"]}'),
        patient_cell(s),
        f'<a class="z-link" href="/dialysis/courses/{s["course_id"]}">'
        f'{ui.esc(s["course_no"])}</a>',
        ui.esc(ui.when(s["scheduled_at"]))
        + (ui.muted(s["machine_code"]) if s["machine_code"] else ""),
        ui.esc(_treatment_summary(s)),
        ui.label_chip(*dialysis.SESSION_STATUS[s["status"]]),
    ] for s in rows]

    body = ui.card(
        f"{len(table_rows)} session(s)",
        ui.table(["Session", "Patient", "Course", "Scheduled", "Treatment",
                  "Status"], table_rows,
                 empty="No sessions match that filter."),
        actions=filters + ui.button("Create session", "/dialysis/sessions/new",
                                    style="z-button-primary", ico="calendar-plus"))
    return render(request, "Dialysis sessions", "dialysis-sessions", body,
                  breadcrumb=[("Dialysis", "/dialysis"), ("Sessions", None)])


def new_session(request: Request):
    preset = request.q("course")
    active = [c for c in dialysis.courses("active")]
    machines = dialysis.available_machines()

    if not active:
        body = ui.empty_state(
            "There are no active dialysis courses. A session always belongs to a "
            "course, so create the course first.",
            ui.button("New course", "/dialysis/courses/new",
                      style="z-button-primary", ico="plus"))
        return render(request, "Create dialysis session", "dialysis-sessions", body,
                      breadcrumb=[("Dialysis", "/dialysis"), ("Create session", None)])

    options = [(c["id"], f'{c["course_no"]} · {c["patient_name"]} ({c["mrn"]}) · '
                         f'{c["modality_display"]} · {c["done"]} done')
               for c in active]
    machine_options = [(m["id"], f'{m["code"]} — {m["model"] or ""}') for m in machines]

    # what the chosen course will hand down to the session
    prescriptions = []
    for c in active:
        prescriptions.append([
            f'<a class="z-link" href="/dialysis/courses/{c["id"]}">'
            f'{ui.esc(c["course_no"])}</a>' + ui.muted(c["modality_display"]),
            ui.esc(c["patient_name"]),
            f'{c["sessions_per_week"]:g}/week × {c["duration_minutes"]} min'
            + (ui.muted(c["access_display"]) if c["access_display"] else ""),
            f'{c["done"]} done' + ui.muted(
                f'last {ui.when(c["last_run"], 10)}' if c["last_run"] else "no runs yet"),
        ])

    form = (
        '<form method="post" action="/dialysis/sessions">'
        + ui.card("Session", (
            ui.field("Active course", ui.select("course_id", options, preset,
                                                blank="Select the patient's course",
                                                required=True), required=True,
                     help_text="The session inherits this course's prescription — "
                               "access, dialyser, Qb/Qd, dialysate bath and "
                               "anticoagulation. Edit what was actually delivered "
                               "on the session screen.")
            + ui.grid(
                ui.field("Machine", ui.select("machine_id", machine_options, "",
                                              blank="Assign later"),
                         help_text=f"{len(machines)} machine(s) free right now."),
                ui.field("Scheduled for", ui.text_input(
                    "scheduled_at", db.now_iso()[:16], type_="datetime-local",
                    required=True), required=True),
                ui.field("Performed by", ui.select("practitioner_id",
                                                   doctor_options(), "",
                                                   blank="Course nephrologist")),
                cols=3)
            + ui.checkbox("start_now", "Start the run immediately "
                                       "(takes the machine)", False)),
            footer=(f'<div class="display-flex justify-between gap"{st(gap=2)}>'
                    + ui.button("Cancel", "/dialysis/sessions",
                                style="z-button-secondary")
                    + ui.button("Create session", style="z-button-primary",
                                size="z-button-medium", type_="submit")
                    + "</div>"))
        + "</form>")

    body = (form + f'<div class="mt"{st(mt=5)}>'
            + ui.card(f"Active courses — {len(active)}",
                      ui.table(["Course", "Patient", "Prescription", "Progress"],
                               prescriptions))
            + "</div>")
    return render(request, "Create dialysis session", "dialysis-sessions", body,
                  breadcrumb=[("Dialysis", "/dialysis"), ("Create session", None)])


def create_session(request: Request):
    course_id = request.f_int("course_id")
    if not course_id:
        return redirect("/dialysis/sessions/new", "Pick an active course.", "danger")
    try:
        session_id = dialysis.schedule_session(
            course_id, request.f_int("machine_id"),
            request.f_or_none("scheduled_at"), request.f_int("practitioner_id"))
    except ValueError as error:
        return redirect(f"/dialysis/sessions/new?course={course_id}", str(error),
                        "danger")
    message = "Session scheduled."
    if request.f("start_now"):
        try:
            dialysis.start_session(session_id)
            message = "Session scheduled and started."
        except ValueError as error:
            message = f"Session scheduled, but it could not start: {error}"
    return redirect(f"/dialysis/sessions/{session_id}", message)

# ===========================================================================
# sessions
# ===========================================================================
def session_detail(request: Request):
    sid = request.params["sid"]
    row = dialysis.session(sid)
    if row is None:
        return not_found(request, "Dialysis session")
    editable = row["status"] in ("planned", "in-progress")

    header_extra = (
        ui.label_chip(row["session_no"], "info")
        + ui.label_chip(*dialysis.SESSION_STATUS[row["status"]])
        + ui.button("Course", f'/dialysis/courses/{row["course_id"]}',
                    ico="clipboard-list"))
    if row["status"] == "planned":
        header_extra += ui.post_button(
            f"/dialysis/sessions/{sid}/start", "Start run",
            style="z-button-primary", size="z-button-small", ico="play")
    elif row["status"] == "in-progress":
        header_extra += ui.post_button(
            f"/dialysis/sessions/{sid}/complete", "Complete run",
            style="z-button-primary", size="z-button-small", ico="circle-check")
        header_extra += ui.confirm_form(
            f"/dialysis/sessions/{sid}/complete?abandon=1", "Abandon",
            "Abandon this run? It will not be billed or recorded as a procedure.",
            size="z-button-small")

    facts = ui.card("Run", ui.dl([
        ("Session", f'{row["session_no"]} (#{row["seq"]} of the course)'),
        ("Course", f'{row["course_no"]} — {row["modality_display"]}'),
        ("Machine", f'{row["machine_code"]} · {row["machine_model"] or ""}'
         if row["machine_code"] else None),
        ("Scheduled", ui.when(row["scheduled_at"])),
        ("Started", ui.when(row["started_at"])),
        ("Ended", ui.when(row["ended_at"])),
        ("Duration", f'{row["duration_minutes"]} min' if row["duration_minutes"]
         else None),
        ("Performed by", row["doctor_name"]),
        ("Complication", row["complication_display"]),
        ("Wellness record", _wellness_link(sid)),
    ], cols=3))

    body = (
        ui.patient_header(dict(row) | {"id": row["patient_id"],
                                       "name": row["patient_name"]},
                          extra=header_extra)
        + f'<div class="mt"{st(mt=5)}>' + facts + "</div>"
        + f'<div class="mt"{st(mt=5)}>' + _flowsheet(sid, row, editable) + "</div>"
        + f'<div class="mt"{st(mt=5)}>' + _parameters_form(sid, row, editable) + "</div>"
        + f'<div class="mt"{st(mt=5)}>' + _monitoring(sid, row, editable) + "</div>")
    return render(request, f'Session {row["session_no"]}', "dialysis", body,
                  breadcrumb=[("Dialysis", "/dialysis"),
                              (row["course_no"], f'/dialysis/courses/{row["course_id"]}'),
                              (row["session_no"], None)])


def _wellness_link(session_id: int) -> str | None:
    """The wellness record this run generated, with a way to rebuild it."""
    from ... import wellness
    row = wellness.record_for_session(session_id)
    if row is None:
        return None
    return (f'<div class="display-flex items-center gap flex-wrap"{st(gap=2)}>'
            f'<a class="z-link" href="/wellness/{row["id"]}">'
            f'{ui.esc(row["record_no"])}</a>'
            f'<form method="post" action="/wellness/{row["id"]}/regenerate">'
            + ui.button("Regenerate", size="z-button-xsmall", type_="submit")
            + "</form></div>")


def _flowsheet(sid: int, row, editable: bool) -> str:
    """Pre and post dialysis vitals, stored as ordinary LOINC observations."""
    captured = dialysis.phase_vitals(sid)
    masters = [db.term("vital", code) for code in dialysis.FLOWSHEET_VITALS]
    masters = [m for m in masters if m]

    table_rows = []
    for master in masters:
        cells = []
        for phase in ("pre", "post"):
            obs = captured[phase].get(master["code"])
            if obs is None:
                cells.append("—")
                continue
            value = f'{obs["value_quantity"]:g}'
            chip = ui.flag_chip(obs["interpretation"], show_normal=False)
            cells.append(f"{value} {chip}" if chip else value)
        table_rows.append([f'{ui.esc(ui.short_label(master["display"]))} '
                           f'<span class="text-xs color"'
                           f'{st(color="var(--z-muted-f)")}>{ui.esc(master["unit"])}</span>',
                           cells[0], cells[1]])

    forms = ""
    if editable:
        panels = []
        for phase in ("pre", "post"):
            fields = "".join(
                ui.field(f'{ui.short_label(m["display"])} ({m["unit"]})',
                         ui.text_input(f'vital_{m["code"]}',
                                       (captured[phase].get(m["code"]) or {})
                                       ["value_quantity"]
                                       if captured[phase].get(m["code"]) else "",
                                       type_="number", attrs='step="0.1"'),
                         help_text=f'LOINC {m["code"]}')
                for m in masters)
            weight_field = ui.field(
                "Weight (kg)",
                ui.text_input(f"{phase}_weight_kg",
                              row[f"{phase}_weight_kg"] or "", type_="number",
                              attrs='step="0.1" min="0"'),
                help_text="Pre/post weight drives the ultrafiltration figure.")
            panels.append((
                f"{phase.title()}-dialysis observations",
                f'<form method="post" action="/dialysis/sessions/{sid}/vitals">'
                + f'<input type="hidden" name="phase" value="{phase}">'
                + f'<div class="display-grid gap sm:grid-cols lg:grid-cols"'
                + st(gap=3, sm_grid_cols=2, lg_grid_cols=3) + ">"
                + fields + weight_field + "</div>"
                + f'<div class="display-flex justify-end"{st()}>'
                + ui.button(f"Save {phase} readings", style="z-button-primary",
                            type_="submit", ico="heart-pulse")
                + "</div></form>"))
        forms = (f'<div class="mt"{st(mt=5)}>'
                 + ui.accordion(panels, multiple=True,
                                open_first=not captured["pre"]) + "</div>")

    weights = ui.dl([
        ("Dry weight", f'{row["dry_weight_kg"]:g} kg' if row["dry_weight_kg"] else None),
        ("Pre weight", f'{row["pre_weight_kg"]:g} kg' if row["pre_weight_kg"] else None),
        ("Post weight", f'{row["post_weight_kg"]:g} kg'
         if row["post_weight_kg"] else None),
        ("UF goal", f'{row["uf_goal_ml"]:.0f} mL' if row["uf_goal_ml"] else None),
        ("UF achieved", f'{row["uf_achieved_ml"]:.0f} mL'
         if row["uf_achieved_ml"] else None),
        ("Kt/V", f'{row["ktv"]:g}' if row["ktv"] else None),
        ("URR", f'{row["urr_pct"]:g} %' if row["urr_pct"] else None),
    ], cols=4)

    return ui.card("Flowsheet — pre and post dialysis",
                   weights + f'<div class="mt"{st(mt=4)}>'
                   + ui.table(["Observation", "Pre", "Post"], table_rows,
                              empty="No vitals recorded.")
                   + "</div>" + forms)


def _parameters_form(sid: int, row, editable: bool) -> str:
    if not editable:
        return ui.card("Dialysis parameters as delivered", ui.dl([
            ("Access", " · ".join(filter(None, [row["access_display"]]))),
            ("Dialyser", " ".join(filter(None, [
                row["dialyser"],
                f'(reuse {row["dialyser_reuse"]})' if row["dialyser_reuse"] else None]))),
            ("Blood flow Qb", f'{row["blood_flow_rate"]:g} mL/min'
             if row["blood_flow_rate"] else None),
            ("Dialysate flow Qd", f'{row["dialysate_flow_rate"]:g} mL/min'
             if row["dialysate_flow_rate"] else None),
            ("Dialysate temperature", f'{row["dialysate_temp"]:g} °C'
             if row["dialysate_temp"] else None),
            ("Conductivity", f'{row["conductivity"]:g} mS/cm'
             if row["conductivity"] else None),
            ("Dialysate bath", " · ".join(filter(None, [
                f'Na {row["dialysate_na"]:g}' if row["dialysate_na"] else None,
                f'K {row["dialysate_k"]:g}' if row["dialysate_k"] else None,
                f'Ca {row["dialysate_ca"]:g}' if row["dialysate_ca"] else None,
                f'HCO3 {row["dialysate_bicarb"]:g}'
                if row["dialysate_bicarb"] else None]))),
            ("Anticoagulation", " · ".join(filter(None, [
                row["anticoagulant_display"],
                f'{row["heparin_bolus_units"]:g} U bolus'
                if row["heparin_bolus_units"] else None,
                f'{row["heparin_hourly_units"]:g} U/hr'
                if row["heparin_hourly_units"] else None]))),
            ("Note", row["note"]),
        ], cols=3))

    form = (
        f'<form method="post" action="/dialysis/sessions/{sid}/parameters">'
        + ui.grid(
            ui.field("Vascular access used", ui.select(
                "access_code", term_options("vascular_access", False),
                row["access_code"] or "", blank="Not stated")),
            ui.field("Dialyser", ui.select("dialyser", term_options("dialyser", False),
                                           row["dialyser"] or "",
                                           blank="Not specified")),
            ui.field("Dialyser reuse count", ui.text_input(
                "dialyser_reuse", row["dialyser_reuse"] or "", type_="number",
                attrs='step="1" min="0"')),
            ui.field("Blood flow Qb (mL/min)", ui.text_input(
                "blood_flow_rate", row["blood_flow_rate"] or "", type_="number",
                attrs='step="10"')),
            ui.field("Dialysate flow Qd (mL/min)", ui.text_input(
                "dialysate_flow_rate", row["dialysate_flow_rate"] or "",
                type_="number", attrs='step="100"')),
            ui.field("Dialysate temperature (°C)", ui.text_input(
                "dialysate_temp", row["dialysate_temp"] or "", type_="number",
                attrs='step="0.1"')),
            ui.field("Conductivity (mS/cm)", ui.text_input(
                "conductivity", row["conductivity"] or "", type_="number",
                attrs='step="0.1"')),
            ui.field("Duration (minutes)", ui.text_input(
                "duration_minutes", row["duration_minutes"] or "", type_="number",
                attrs='step="15" min="0"')),
            cols=4)
        + ui.grid(
            ui.field("Dialysate Na (mmol/L)", ui.text_input(
                "dialysate_na", row["dialysate_na"] or "", type_="number",
                attrs='step="1"')),
            ui.field("Dialysate K (mmol/L)", ui.text_input(
                "dialysate_k", row["dialysate_k"] or "", type_="number",
                attrs='step="0.5"')),
            ui.field("Dialysate Ca (mmol/L)", ui.text_input(
                "dialysate_ca", row["dialysate_ca"] or "", type_="number",
                attrs='step="0.25"')),
            ui.field("Bicarbonate (mmol/L)", ui.text_input(
                "dialysate_bicarb", row["dialysate_bicarb"] or "", type_="number",
                attrs='step="1"')),
            cols=4)
        + ui.grid(
            ui.field("Anticoagulant", ui.select(
                "anticoagulant_code", term_options("anticoagulant", False),
                row["anticoagulant_code"] or "", blank="None")),
            ui.field("Heparin bolus (units)", ui.text_input(
                "heparin_bolus_units", row["heparin_bolus_units"] or "",
                type_="number", attrs='step="100"')),
            ui.field("Heparin hourly (units/hr)", ui.text_input(
                "heparin_hourly_units", row["heparin_hourly_units"] or "",
                type_="number", attrs='step="100"')),
            ui.field("UF goal (mL)", ui.text_input(
                "uf_goal_ml", row["uf_goal_ml"] or "", type_="number",
                attrs='step="50" min="0"')),
            cols=4)
        + ui.grid(
            ui.field("UF achieved (mL)", ui.text_input(
                "uf_achieved_ml", row["uf_achieved_ml"] or "", type_="number",
                attrs='step="50" min="0"'),
                help_text="Left blank, it is derived from pre minus post weight."),
            ui.field("Kt/V", ui.text_input("ktv", row["ktv"] or "", type_="number",
                                           attrs='step="0.01" min="0"')),
            ui.field("URR (%)", ui.text_input(
                "urr_pct", row["urr_pct"] or "", type_="number",
                attrs='step="0.1" min="0" max="100"')),
            ui.field("Complication", ui.select(
                "complication_code", term_options("dialysis_complication", False),
                row["complication_code"] or "", blank="None")),
            cols=4)
        + ui.field("Complication detail", ui.text_input(
            "complication_note", row["complication_note"] or ""))
        + ui.field("Session note", ui.textarea("note", row["note"] or "", rows=2))
        + f'<div class="display-flex justify-end"{st()}>'
        + ui.button("Save parameters", style="z-button-primary", type_="submit",
                    ico="save")
        + "</div></form>")
    return ui.card("Dialysis parameters as delivered", form)


def _monitoring(sid: int, row, editable: bool) -> str:
    entries = dialysis.readings(sid)
    def num(value, fmt="{:g}"):
        return fmt.format(value) if value is not None else "–"

    rows = [[
        f'{r["elapsed_minutes"]} min' + ui.muted(r["recorded_at"][11:16]),
        f'{r["bp_systolic"]:g}/{r["bp_diastolic"]:g}'
        if r["bp_systolic"] and r["bp_diastolic"] else "—",
        num(r["pulse"]),
        num(r["blood_flow_rate"]),
        f'{num(r["arterial_pressure"])} / {num(r["venous_pressure"])} / '
        f'{num(r["tmp"])}',
        num(r["uf_volume_ml"], "{:.0f}"),
        ui.esc(r["note"] or ""),
        (ui.confirm_form(f"/dialysis/sessions/{sid}/readings/{r['id']}/delete",
                         "Delete", "Delete this monitoring row?") if editable else ""),
    ] for r in entries]

    form = ""
    if editable:
        next_elapsed = (entries[-1]["elapsed_minutes"] + 30) if entries else 0
        form = (f'<div class="mt"{st(mt=5)}>' + ui.accordion([(
            "Add a monitoring row",
            f'<form method="post" action="/dialysis/sessions/{sid}/readings">'
            + ui.grid(
                ui.field("Elapsed (minutes)", ui.text_input(
                    "elapsed_minutes", next_elapsed, type_="number",
                    attrs='step="15" min="0"')),
                ui.field("BP systolic", ui.text_input("bp_systolic", "",
                                                      type_="number", attrs='step="1"')),
                ui.field("BP diastolic", ui.text_input("bp_diastolic", "",
                                                       type_="number", attrs='step="1"')),
                ui.field("Pulse", ui.text_input("pulse", "", type_="number",
                                                attrs='step="1"')),
                ui.field("Qb (mL/min)", ui.text_input("blood_flow_rate", "",
                                                      type_="number", attrs='step="10"')),
                ui.field("Arterial pressure (mmHg)", ui.text_input(
                    "arterial_pressure", "", type_="number", attrs='step="1"')),
                ui.field("Venous pressure (mmHg)", ui.text_input(
                    "venous_pressure", "", type_="number", attrs='step="1"')),
                ui.field("TMP (mmHg)", ui.text_input("tmp", "", type_="number",
                                                     attrs='step="1"')),
                cols=4)
            + ui.grid(
                ui.field("Cumulative UF (mL)", ui.text_input(
                    "uf_volume_ml", "", type_="number", attrs='step="50" min="0"')),
                ui.field("Note", ui.text_input("note", "",
                                               placeholder="e.g. saline 200 mL given")),
                cols=2)
            + f'<div class="display-flex justify-end"{st()}>'
            + ui.button("Add row", style="z-button-primary", type_="submit", ico="plus")
            + "</div></form>")], open_first=not entries) + "</div>")

    return ui.card(
        f"Intra-dialytic monitoring — {len(rows)} row(s)",
        ui.table(["Elapsed", "BP", "Pulse", "Qb", "Art / Ven / TMP", "UF mL",
                  "Note", ""], rows,
                 empty="No monitoring rows yet. Nursing usually charts every "
                       "30 minutes.", align_right=(2, 3, 4, 5))
        + form)


# ---------------------------------------------------------------------------
def start_session(request: Request):
    sid = request.params["sid"]
    try:
        dialysis.start_session(sid, request.f_int("machine_id"))
    except ValueError as error:
        return redirect(f"/dialysis/sessions/{sid}", str(error), "danger")
    return redirect(f"/dialysis/sessions/{sid}", "Run started.")


def _num(request: Request, name: str) -> float | None:
    raw = request.f(name)
    try:
        return float(raw) if raw != "" else None
    except ValueError:
        return None


def save_parameters(request: Request):
    sid = request.params["sid"]
    if dialysis.session(sid) is None:
        return not_found(request, "Dialysis session")
    access = _term("vascular_access", request.f("access_code"))
    anticoag = _term("anticoagulant", request.f("anticoagulant_code"))
    complication = _term("dialysis_complication", request.f("complication_code"))

    values: dict[str, Any] = {
        "access_code": access["code"] if access else None,
        "access_display": access["display"] if access else None,
        "dialyser": request.f_or_none("dialyser"),
        "dialyser_reuse": request.f_int("dialyser_reuse"),
        "blood_flow_rate": _num(request, "blood_flow_rate"),
        "dialysate_flow_rate": _num(request, "dialysate_flow_rate"),
        "dialysate_temp": _num(request, "dialysate_temp"),
        "conductivity": _num(request, "conductivity"),
        "dialysate_na": _num(request, "dialysate_na"),
        "dialysate_k": _num(request, "dialysate_k"),
        "dialysate_ca": _num(request, "dialysate_ca"),
        "dialysate_bicarb": _num(request, "dialysate_bicarb"),
        "anticoagulant_code": anticoag["code"] if anticoag else None,
        "anticoagulant_display": anticoag["display"] if anticoag else None,
        "heparin_bolus_units": _num(request, "heparin_bolus_units"),
        "heparin_hourly_units": _num(request, "heparin_hourly_units"),
        "duration_minutes": request.f_int("duration_minutes"),
        "uf_goal_ml": _num(request, "uf_goal_ml"),
        "uf_achieved_ml": _num(request, "uf_achieved_ml"),
        "ktv": _num(request, "ktv"),
        "urr_pct": _num(request, "urr_pct"),
        "complication_code": complication["code"] if complication else None,
        "complication_display": complication["display"] if complication else None,
        "complication_note": request.f("complication_note"),
        "note": request.f("note"),
    }
    dialysis.save_parameters(sid, values)
    return redirect(f"/dialysis/sessions/{sid}", "Parameters saved.")


def save_vitals(request: Request):
    sid = request.params["sid"]
    phase = request.f("phase")
    row = dialysis.session(sid)
    if row is None:
        return not_found(request, "Dialysis session")
    readings = {code: request.f(f"vital_{code}")
                for code in dialysis.FLOWSHEET_VITALS}
    weight = _num(request, f"{phase}_weight_kg")
    if weight is not None:
        dialysis.save_parameters(sid, {f"{phase}_weight_kg": weight})
        readings["29463-7"] = str(weight)
    try:
        count = dialysis.record_phase_vitals(sid, phase, readings)
    except ValueError as error:
        return redirect(f"/dialysis/sessions/{sid}", str(error), "danger")
    return redirect(f"/dialysis/sessions/{sid}",
                    f"Recorded {count} {phase}-dialysis observation(s).")


def add_reading(request: Request):
    sid = request.params["sid"]
    if dialysis.session(sid) is None:
        return not_found(request, "Dialysis session")
    dialysis.add_reading(sid, {
        "elapsed_minutes": request.f_int("elapsed_minutes", 0) or 0,
        "bp_systolic": _num(request, "bp_systolic"),
        "bp_diastolic": _num(request, "bp_diastolic"),
        "pulse": _num(request, "pulse"),
        "blood_flow_rate": _num(request, "blood_flow_rate"),
        "arterial_pressure": _num(request, "arterial_pressure"),
        "venous_pressure": _num(request, "venous_pressure"),
        "tmp": _num(request, "tmp"),
        "uf_volume_ml": _num(request, "uf_volume_ml"),
        "note": request.f_or_none("note"),
    })
    return redirect(f"/dialysis/sessions/{sid}", "Monitoring row added.")


def delete_reading(request: Request):
    sid = request.params["sid"]
    dialysis.delete_reading(request.params["rid"], sid)
    return redirect(f"/dialysis/sessions/{sid}", "Monitoring row removed.")


def complete_session(request: Request):
    sid = request.params["sid"]
    abandon = request.q("abandon") == "1"
    try:
        dialysis.complete_session(sid, abandoned=abandon)
    except ValueError as error:
        return redirect(f"/dialysis/sessions/{sid}", str(error), "danger")
    return redirect(f"/dialysis/sessions/{sid}",
                    "Run abandoned." if abandon
                    else "Run completed and recorded as a procedure.")
