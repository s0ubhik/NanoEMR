"""Appointments and the OPD queue."""

from __future__ import annotations

from ... import db, hospital, services
from .. import ui
from ..common import (
    doctor_options, not_found, patient_options, render, term_options,
)
from ..router import Request, redirect

st = ui.st


def register(app) -> None:
    app.add("GET", "/appointments", index)
    app.add("POST", "/appointments", create)
    app.add("POST", "/appointments/<int:aid>/status", set_status)
    app.add("POST", "/appointments/<int:aid>/arrive", arrive)


def index(request: Request):
    slot_date = request.q("date") or db.today_iso()
    rows = hospital.appointments_for(slot_date)
    counts = {key: sum(1 for r in rows if r["status"] == key)
              for key in hospital.APPOINTMENT_STATUS}

    tiles = "".join([
        ui.stat("Booked", counts["booked"], "calendar-clock", "info"),
        ui.stat("Waiting", counts["arrived"], "users", "warning"),
        ui.stat("Seen", counts["fulfilled"], "circle-check", "success"),
        ui.stat("No show / cancelled",
                counts["no-show"] + counts["cancelled"], "circle-x", "danger"),
    ])

    table_rows = []
    for appt in rows:
        label, kind = hospital.APPOINTMENT_STATUS[appt["status"]]
        actions = []
        if appt["status"] == "booked":
            actions.append(_status_form(appt["id"], "arrived", "Check in",
                                        "z-button-primary"))
            actions.append(_status_form(appt["id"], "cancelled", "Cancel",
                                        "z-button-secondary"))
        elif appt["status"] == "arrived":
            actions.append(ui.post_button(
                f'/appointments/{appt["id"]}/arrive', "Start visit",
                style="z-button-primary"))
            actions.append(_status_form(appt["id"], "no-show", "No show",
                                        "z-button-secondary"))
        elif appt["encounter_id"]:
            actions.append(ui.button("Open visit", f'/opd/{appt["encounter_id"]}',
                                     size="z-button-xsmall"))
        table_rows.append([
            f'<strong>{appt["token"]}</strong>',
            ui.esc(appt["slot_time"] or "—"),
            f'<a class="z-link" href="/patients/{appt["patient_id"]}">'
            f'{ui.esc(appt["patient_name"])}</a>'
            + ui.muted(f'{appt["mrn"]} · {appt["phone"]}'),
            ui.esc(appt["doctor_name"] or "—"),
            ui.esc(appt["department"] or "—"),
            ui.esc(appt["reason"] or "—"),
            ui.label_chip(label, kind),
            f'<div class="display-flex gap"{st(gap=1)}>' + "".join(actions) + "</div>",
        ])

    picker = ('<form method="get" action="/appointments" '
              f'class="display-flex gap items-end"{st(gap=2)}>'
              + ui.text_input("date", slot_date, type_="date")
              + ui.button("Show", style="z-button-primary", type_="submit")
              + "</form>")

    body = (
        f'<div class="display-grid gap sm:grid-cols lg:grid-cols"'
        + st(gap=4, sm_grid_cols=2, lg_grid_cols=4) + ">" + tiles + "</div>"
        + f'<div class="mt"{st(mt=5)}>'
        + ui.card(f"Queue for {slot_date} — {len(table_rows)} appointment(s)",
                  ui.table(["Token", "Time", "Patient", "Doctor", "Department",
                            "Reason", "Status", ""], table_rows,
                           empty="Nothing booked for this date."),
                  actions=picker)
        + "</div>"
        + f'<div class="mt"{st(mt=5)}>' + _book_card(slot_date) + "</div>")
    return render(request, "Appointments", "appointments", body)


def _status_form(appointment_id: int, status: str, label: str, style: str) -> str:
    return ui.post_button(f"/appointments/{appointment_id}/status", label,
                          style=style, fields={"status": status})


def _book_card(slot_date: str) -> str:
    form = ('<form method="post" action="/appointments">'
            + ui.grid(
                ui.field("Patient", ui.select("patient_id", patient_options(), "",
                                              blank="Select patient", required=True),
                         required=True),
                ui.field("Doctor", ui.select("practitioner_id", doctor_options(), "",
                                             blank="Any available")),
                ui.field("Department", ui.select(
                    "department", term_options("department", False), "",
                    blank="Not specified")),
                ui.field("Date", ui.text_input("slot_date", slot_date, type_="date",
                                               required=True), required=True),
                ui.field("Time", ui.text_input("slot_time", "", type_="time")),
                ui.field("Reason", ui.select("reason", term_options("complaint"), "",
                                             blank="Not stated")),
                cols=3)
            + ui.field("Note", ui.text_input("note", ""))
            + f'<div class="display-flex justify-end"{st()}>'
            + ui.button("Book appointment", style="z-button-primary", type_="submit",
                        ico="calendar-plus")
            + "</div></form>")
    return ui.card("Book an appointment",
                   ui.accordion([("New appointment", form)], open_first=False))


def create(request: Request):
    patient_id = request.f_int("patient_id")
    if not patient_id:
        return redirect("/appointments", "Select a patient first.", "danger")
    reason = db.term("complaint", request.f("reason")) if request.f("reason") else None
    dept = db.term("department", request.f("department")) if request.f("department") \
        else None
    appointment_id = hospital.book_appointment(
        patient_id, request.f_int("practitioner_id"),
        dept["display"] if dept else "", request.f("slot_date"),
        request.f_or_none("slot_time"),
        reason["display"] if reason else request.f_or_none("note"),
        request.f_or_none("note"))
    row = db.one("SELECT * FROM appointment WHERE id = ?", (appointment_id,))
    return redirect(f'/appointments?date={row["slot_date"]}',
                    f'Booked {row["appointment_no"]} — token {row["token"]}.')


def set_status(request: Request):
    appt = db.one("SELECT * FROM appointment WHERE id = ?", (request.params["aid"],))
    if appt is None:
        return not_found(request, "Appointment")
    try:
        hospital.set_appointment_status(appt["id"], request.f("status"))
    except ValueError as error:
        return redirect("/appointments", str(error), "danger")
    return redirect(f'/appointments?date={appt["slot_date"]}', "Appointment updated.")


def arrive(request: Request):
    """Turn a checked-in appointment into a real OPD visit."""
    appt = db.one("SELECT * FROM appointment WHERE id = ?", (request.params["aid"],))
    if appt is None:
        return not_found(request, "Appointment")
    if appt["encounter_id"]:
        return redirect(f'/opd/{appt["encounter_id"]}', "Visit already started.")

    doctor = db.one("SELECT * FROM practitioner WHERE id = ?",
                    (appt["practitioner_id"],)) if appt["practitioner_id"] else None
    encounter_id = services.create_encounter("OPD", {
        "patient_id": appt["patient_id"],
        "status": "in-progress",
        "class_code": "AMB",
        "type_code": "11429006", "type_display": "Consultation",
        "service_type_code": doctor["specialty_code"] if doctor else None,
        "service_type_display": doctor["specialty_display"] if doctor else None,
        "priority_code": "R", "priority_display": "routine",
        "priority_system": "http://terminology.hl7.org/CodeSystem/v3-ActPriority",
        "practitioner_id": appt["practitioner_id"],
        "department": appt["department"] or (doctor["department"] if doctor else None),
        "period_start": db.now_iso(),
        "consultation_fee": doctor["consultation_fee"] if doctor else 0,
        "appointment_slot": f'{appt["slot_date"]} {appt["slot_time"] or ""}'.strip(),
    })
    hospital.set_appointment_status(appt["id"], "fulfilled", encounter_id)
    number = db.scalar("SELECT encounter_no FROM encounter WHERE id = ?",
                       (encounter_id,))
    return redirect(f"/opd/{encounter_id}", f"Visit {number} started from the queue.")
