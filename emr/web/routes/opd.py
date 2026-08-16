"""OPD registration and the OPD consultation note."""

from __future__ import annotations

from typing import Any

from ... import db, services
from .. import ui
from ..common import (
    patient_cell,
    collect_rows, doctor_options, get_patient, not_found, patient_options, render,
    status_label, term_options,
)
from ..router import Request, redirect

st = ui.st


def register(app) -> None:
    app.add("GET", "/opd", index)
    app.add("GET", "/opd/new", new)
    app.add("POST", "/opd", create)
    app.add("GET", "/opd/<int:eid>", detail)
    app.add("POST", "/opd/<int:eid>/note", save_note)
    app.add("POST", "/opd/<int:eid>/close", close_visit)


# ---------------------------------------------------------------------------
def index(request: Request):
    rows = services.list_encounters("OPD")
    table_rows = [[
        f'<a class="z-link" href="/opd/{r["id"]}">{ui.esc(r["encounter_no"])}</a>',
        patient_cell(r),
        ui.when(r["period_start"]),
        ui.esc(r["doctor_name"] or "—"),
        ui.esc(r["department"] or "—"),
        f'₹ {r["consultation_fee"]:.0f}',
        status_label(r["status"]),
    ] for r in rows]
    body = ui.card(
        f"{len(table_rows)} OPD visit(s)",
        ui.table(["Visit", "Patient", "When", "Doctor", "Department", "Fee", "Status"],
                 table_rows, empty="No OPD visits registered yet.", align_right=(5,)),
        actions=ui.button("New OPD registration", "/opd/new", style="z-button-primary",
                          ico="calendar-plus"))
    return render(request, "OPD visits", "opd", body)


# ---------------------------------------------------------------------------
def new(request: Request):
    preset = request.q("patient")
    body = (
        f'<form method="post" action="/opd">'
        + ui.card("Visit details", (
            ui.field("Patient", ui.select("patient_id", patient_options(), preset,
                                          blank="Select a registered patient",
                                          required=True),
                     help_text="Not registered yet? Register the patient first.",
                     required=True)
            + ui.grid(
                ui.field("Department", ui.select("department",
                                                 term_options("department", False), "",
                                                 blank="Select department")),
                ui.field("Consulting doctor",
                         ui.select("practitioner_id", doctor_options(), "",
                                   blank="Select doctor", required=True), required=True),
                ui.field("Encounter type", ui.select(
                    "type_code", term_options("encounter_type"), "11429006")),
                ui.field("Service type", ui.select(
                    "service_type_code", term_options("service_type"), "394802001")),
                ui.field("Priority", ui.select(
                    "priority_code", term_options("priority", False), "R")),
                ui.field("Visit class", ui.select("class_code", [
                    ("AMB", "Ambulatory (AMB)"), ("EMER", "Emergency (EMER)")], "AMB")),
                ui.field("Reason for visit", ui.select(
                    "reason_code", term_options("complaint"), "", blank="Not stated")),
                ui.field("Consultation fee (₹)",
                         ui.text_input("consultation_fee", "", type_="number",
                                       attrs='step="1" min="0"'),
                         help_text="Defaults to the doctor's fee when left blank."),
                cols=2)),
            footer=(f'<div class="display-flex justify-between gap"{st(gap=2)}>'
                    + ui.button("Cancel", "/opd", style="z-button-secondary")
                    + ui.button("Register visit", style="z-button-primary",
                                size="z-button-medium", type_="submit")
                    + "</div>"))
        + "</form>")
    return render(request, "New OPD registration", "opd-new", body,
                  breadcrumb=[("OPD", "/opd"), ("New registration", None)])


def create(request: Request):
    patient_id = request.f_int("patient_id")
    if not patient_id:
        return redirect("/opd/new", "Select a patient first.", "danger")
    practitioner_id = request.f_int("practitioner_id")
    doctor = db.one("SELECT * FROM practitioner WHERE id = ?", (practitioner_id,)) \
        if practitioner_id else None
    fee = request.f_float("consultation_fee", 0.0) or (
        doctor["consultation_fee"] if doctor else 0.0)

    type_row = db.term("encounter_type", request.f("type_code"))
    service_row = db.term("service_type", request.f("service_type_code"))
    priority_row = db.term("priority", request.f("priority_code"))
    reason_row = db.term("complaint", request.f("reason_code"))
    department = request.f("department")
    dept_row = db.term("department", department) if department else None

    encounter_id = services.create_encounter("OPD", {
        "patient_id": patient_id,
        "status": "in-progress",
        "class_code": request.f("class_code") or "AMB",
        "type_code": type_row["code"] if type_row else None,
        "type_display": type_row["display"] if type_row else None,
        "service_type_code": service_row["code"] if service_row else None,
        "service_type_display": service_row["display"] if service_row else None,
        "priority_code": priority_row["code"] if priority_row else None,
        "priority_display": priority_row["display"] if priority_row else None,
        "priority_system": priority_row["system"] if priority_row else None,
        "practitioner_id": practitioner_id,
        "department": dept_row["display"] if dept_row else None,
        "period_start": db.now_iso(),
        "reason_code": reason_row["code"] if reason_row else None,
        "reason_display": reason_row["display"] if reason_row else None,
        "consultation_fee": fee,
    })
    number = db.scalar("SELECT encounter_no FROM encounter WHERE id = ?", (encounter_id,))
    return redirect(f"/opd/{encounter_id}", f"OPD visit {number} registered.")


# ---------------------------------------------------------------------------
def detail(request: Request):
    eid = request.params["eid"]
    enc = services.encounter_with_names(eid)
    if enc is None or enc["kind"] != "OPD":
        return not_found(request, "OPD visit")
    patient = get_patient(enc["patient_id"])
    note = db.one("SELECT * FROM clinical_note WHERE encounter_id = ? AND kind = 'OPD_NOTE'",
                  (eid,))

    header = ui.patient_header(patient, extra=(
        ui.label_chip(enc["encounter_no"], "info")
        + status_label(enc["status"])
        + ui.button("Order lab", f"/lab/new?encounter={eid}", ico="flask-conical")
        + ui.button("Raise bill", f"/billing/new?encounter={eid}",
                    ico="receipt-indian-rupee")))

    visit_facts = ui.card("Visit", ui.dl([
        ("Visit number", enc["encounter_no"]),
        ("Started", ui.when(enc["period_start"])),
        ("Doctor", enc["doctor_name"]),
        ("Department", enc["department"]),
        ("Encounter type", f'{enc["type_display"]} ({enc["type_code"]})'
         if enc["type_code"] else None),
        ("Service type", f'{enc["service_type_display"]} ({enc["service_type_code"]})'
         if enc["service_type_code"] else None),
        ("Priority", enc["priority_display"]),
        ("Consultation fee", f'₹ {enc["consultation_fee"]:.2f}'),
    ], cols=3), actions=(
        ui.button("Close visit", attrs=f'form="close-visit-{eid}"', type_="submit",
                  style="z-button-secondary") if enc["status"] != "finished" else ""))


    body = (
        header
        + f'<form id="close-visit-{eid}" method="post" action="/opd/{eid}/close"></form>'
        + f'<div class="mt"{st(mt=5)}>' + visit_facts + "</div>"
        + f'<div class="mt"{st(mt=5)}>' + _note_form(eid, patient, note) + "</div>")

    return render(request, f"OPD visit {enc['encounter_no']}", "opd", body,
                  breadcrumb=[("OPD", "/opd"), (enc["encounter_no"], None)])


# ---------------------------------------------------------------------------
def _complaint_row(values: dict[str, Any] | None = None) -> str:
    v = values or {}
    return ui.row_shell(
        ui.cell("Complaint (SNOMED CT)", ui.select(
            "cc_code", term_options("complaint"), v.get("code", ""),
            blank="Free text only"), "16rem")
        + ui.cell("Description", ui.text_input("cc_text", v.get("text", ""),
                                               placeholder="e.g. fever since 3 days"),
                  "16rem")
        + ui.cell("Onset", ui.text_input("cc_onset", v.get("onset", ""), type_="date"),
                  "10rem"))


def _diagnosis_row(values: dict[str, Any] | None = None) -> str:
    v = values or {}
    return ui.row_shell(
        ui.cell("Diagnosis (SNOMED CT + ICD-10)", ui.select(
            "dx_code", term_options("diagnosis"), v.get("code", ""),
            blank="Free text only"), "20rem")
        + ui.cell("Clinical note", ui.text_input("dx_text", v.get("text", "")), "16rem")
        + ui.cell("Status", ui.select("dx_status", [
            ("active", "Active"), ("recurrence", "Recurrence"),
            ("remission", "Remission"), ("resolved", "Resolved")],
            v.get("status", "active")), "10rem"))


def _medication_row(values: dict[str, Any] | None = None) -> str:
    v = values or {}
    return ui.row_shell(
        ui.cell("Medicine (SNOMED CT)", ui.select(
            "rx_code", term_options("medicine"), v.get("code", ""),
            blank="Select medicine"), "16rem")
        + ui.cell("Dose", ui.text_input("rx_dose", v.get("dose", ""), type_="number",
                                        attrs='step="0.5" min="0"'), "6rem")
        + ui.cell("Unit", ui.text_input("rx_unit", v.get("unit", ""),
                                        placeholder="mg / tablet"), "8rem")
        + ui.cell("Route", ui.select("rx_route", term_options("route", False),
                                     v.get("route", "26643006")), "12rem")
        + ui.cell("Times/day", ui.text_input("rx_freq", v.get("freq", ""), type_="number",
                                             attrs='min="1" max="12"'), "7rem")
        + ui.cell("Days", ui.text_input("rx_days", v.get("days", ""), type_="number",
                                        attrs='min="1"'), "6rem")
        + ui.cell("Instruction", ui.select(
            "rx_instruction", term_options("dose_instruction", False),
            v.get("instruction", ""), blank="None"), "12rem"))


def _note_form(eid: int, patient: dict, note) -> str:
    complaints = db.query(
        "SELECT * FROM condition WHERE encounter_id = ? AND category = 'chief-complaint' "
        "ORDER BY id", (eid,))
    diagnoses = db.query(
        "SELECT * FROM condition WHERE encounter_id = ? AND category = 'diagnosis' "
        "ORDER BY id", (eid,))
    medications = db.query(
        "SELECT * FROM medication_request WHERE encounter_id = ? ORDER BY sort_order, id",
        (eid,))
    advice = {r["code"] for r in db.query(
        "SELECT code FROM service_request WHERE encounter_id = ? AND purpose = 'investigation'",
        (eid,))}
    vitals = {r["loinc_code"]: r for r in db.query(
        "SELECT * FROM observation WHERE encounter_id = ? AND category = 'vital-signs'",
        (eid,))}

    complaint_rows = [_complaint_row({
        "code": c["snomed_code"], "text": c["text"], "onset": (c["onset"] or "")[:10]})
        for c in complaints] or [_complaint_row()]
    diagnosis_rows = [_diagnosis_row({
        "code": d["snomed_code"], "text": d["note"] or d["text"],
        "status": d["clinical_status"]}) for d in diagnoses] or [_diagnosis_row()]
    medication_rows = [_medication_row({
        "code": m["snomed_code"], "dose": m["dose_quantity"] or "",
        "unit": m["dose_unit"] or "", "route": m["route_code"] or "",
        "freq": m["frequency"] or "", "days": m["duration_days"] or "",
        "instruction": m["additional_code"] or ""}) for m in medications] \
        or [_medication_row()]

    vital_fields = []
    for master in db.terms("vital"):
        current = vitals.get(master["code"])
        value = current["value_quantity"] if current else ""
        rng = ""
        if master["ref_low"] is not None or master["ref_high"] is not None:
            rng = f'{master["ref_low"] or ""}–{master["ref_high"] or ""} {master["unit"]}'
        vital_fields.append(ui.field(
            f'{master["display"].split("[")[0].strip()} ({master["unit"]})',
            ui.text_input(f'vital_{master["code"]}', value, type_="number",
                          attrs='step="0.1"'),
            help_text=f"LOINC {master['code']} · reference {rng}" if rng
            else f"LOINC {master['code']}"))

    panels = db.terms("lab_panel")
    advice_boxes = "".join(
        f'<div>' + ui.checkbox("adv_panel", p["display"], p["code"] in advice, p["code"])
        + "</div>" for p in panels)

    sections = ui.accordion([
        ("Chief complaints", ui.repeater("cc", _complaint_row(), complaint_rows,
                                         "Add complaint")),
        ("Vitals & physical examination", (
            f'<div class="display-grid gap sm:grid-cols lg:grid-cols"'
            + st(gap=3, sm_grid_cols=2, lg_grid_cols=3) + ">"
            + "".join(vital_fields) + "</div>"
            + ui.field("Examination findings",
                       ui.textarea("examination_text",
                                   note["examination_text"] if note else "", rows=4,
                                   placeholder="General, systemic examination …"))
            + ui.field("History of presenting illness",
                       ui.textarea("history_text", note["history_text"] if note else "",
                                   rows=3)))),
        ("Diagnosis", ui.repeater("dx", _diagnosis_row(), diagnosis_rows,
                                  "Add diagnosis")),
        ("Prescription", ui.repeater("rx", _medication_row(), medication_rows,
                                     "Add medicine")),
        ("Investigation advice", (
            f'<div class="display-grid gap sm:grid-cols"'
            + st(gap=2, sm_grid_cols=2) + ">" + advice_boxes + "</div>")),
        ("Advice & follow-up", (
            ui.field("Advice", ui.textarea("advice_text",
                                           note["advice_text"] if note else "", rows=3))
            + ui.grid(
                ui.field("Follow-up date", ui.text_input(
                    "follow_up_date", (note["follow_up_date"] if note else "") or "",
                    type_="date")),
                ui.field("Follow-up note", ui.text_input(
                    "follow_up_note", (note["follow_up_note"] if note else "") or "")),
                cols=2)
            + ui.field("Referral", ui.text_input(
                "referral_text", "", placeholder="e.g. Refer to Cardiology OPD")))),
    ], multiple=True)

    return (f'<form method="post" action="/opd/{eid}/note">'
            + ui.card("OPD consultation note", sections, footer=(
                f'<div class="display-flex justify-between items-center gap flex-wrap"'
                f'{st(gap=2)}>'
                f'<span class="text-xs color"{st(color="var(--z-muted-f)")}>'
                "Saved content maps to the NRCES OPConsultRecord sections."
                "</span>"
                + ui.button("Save consultation note", style="z-button-primary",
                            size="z-button-medium", type_="submit")
                + "</div>"))
            + "</form>")


# ---------------------------------------------------------------------------
def save_note(request: Request):
    eid = request.params["eid"]
    enc = db.one("SELECT * FROM encounter WHERE id = ?", (eid,))
    if enc is None:
        return not_found(request, "OPD visit")
    patient_id = enc["patient_id"]

    _replace_complaints(request, enc, "cc")
    _replace_diagnoses(request, enc)
    _replace_medications(request, enc)
    _replace_advice(request, enc)

    db.execute("DELETE FROM observation WHERE encounter_id = ? AND category = 'vital-signs'",
               (eid,))
    readings = {master["code"]: request.f(f'vital_{master["code"]}')
                for master in db.terms("vital")}
    services.record_vitals(patient_id, eid, readings)

    services.upsert_note(eid, patient_id, "OPD_NOTE", {
        "author_id": enc["practitioner_id"],
        "history_text": request.f_or_none("history_text"),
        "examination_text": request.f_or_none("examination_text"),
        "advice_text": request.f_or_none("advice_text"),
        "follow_up_date": request.f_or_none("follow_up_date"),
        "follow_up_note": request.f_or_none("follow_up_note"),
        "status": "final",
    })
    return redirect(f"/opd/{eid}", "Consultation note saved.")


def _replace_complaints(request: Request, enc, prefix: str) -> None:
    db.execute("DELETE FROM condition WHERE encounter_id = ? AND category = 'chief-complaint'",
               (enc["id"],))
    for row in collect_rows(request, prefix, ["code", "text", "onset"]):
        master = db.term("complaint", row["code"]) if row["code"] else None
        text = row["text"] or (master["display"] if master else "")
        if not text:
            continue
        db.insert("condition", {
            "patient_id": enc["patient_id"], "encounter_id": enc["id"],
            "category": "chief-complaint",
            "clinical_status": "active", "verification_status": "confirmed",
            "snomed_code": master["code"] if master else None,
            "snomed_display": master["display"] if master else None,
            "text": text, "onset": row["onset"] or None,
            "recorded_at": db.now_iso(),
        })


def _replace_diagnoses(request: Request, enc) -> None:
    db.execute("DELETE FROM condition WHERE encounter_id = ? AND category = 'diagnosis'",
               (enc["id"],))
    for row in collect_rows(request, "dx", ["code", "text", "status"]):
        master = db.term("diagnosis", row["code"]) if row["code"] else None
        text = (master["display"] if master else "") or row["text"]
        if not text:
            continue
        db.insert("condition", {
            "patient_id": enc["patient_id"], "encounter_id": enc["id"],
            "category": "diagnosis",
            "clinical_status": row["status"] or "active",
            "verification_status": "confirmed",
            "snomed_code": master["code"] if master else None,
            "snomed_display": master["display"] if master else None,
            "icd10_code": master["alt_code"] if master else None,
            "icd10_display": master["alt_display"] if master else None,
            "text": text, "note": row["text"] or None,
            "recorded_at": db.now_iso(),
        })


def _replace_medications(request: Request, enc) -> None:
    db.execute("DELETE FROM medication_request WHERE encounter_id = ?", (enc["id"],))
    fields = ["code", "dose", "unit", "route", "freq", "days", "instruction"]
    for index, row in enumerate(collect_rows(request, "rx", fields)):
        master = db.term("medicine", row["code"]) if row["code"] else None
        if master is None:
            continue
        route = db.term("route", row["route"]) if row["route"] else None
        instruction = db.term("dose_instruction", row["instruction"]) \
            if row["instruction"] else None
        freq = int(row["freq"]) if row["freq"].isdigit() else None
        days = int(row["days"]) if row["days"].isdigit() else None
        timing = master["display"]
        if freq and days:
            timing = f"{master['display']} — {freq} time(s) a day for {days} day(s)"
        db.insert("medication_request", {
            "patient_id": enc["patient_id"], "encounter_id": enc["id"],
            "status": "active", "intent": "order",
            "snomed_code": master["code"], "snomed_display": master["display"],
            "dose_quantity": float(row["dose"]) if row["dose"] else None,
            "dose_unit": row["unit"] or None,
            "route_code": route["code"] if route else None,
            "route_display": route["display"] if route else None,
            "frequency": freq, "period": 1 if freq else None,
            "period_unit": "d" if freq else None,
            "duration_days": days,
            "timing_text": timing,
            "additional_code": instruction["code"] if instruction else None,
            "additional_display": instruction["display"] if instruction else None,
            "authored_on": db.now_iso(),
            "requester_id": enc["practitioner_id"],
            "sort_order": index,
        })


def _replace_advice(request: Request, enc) -> None:
    db.execute("DELETE FROM service_request WHERE encounter_id = ?", (enc["id"],))
    for code in request.f_all("adv_panel"):
        master = db.term("lab_panel", code)
        if master is None:
            continue
        db.insert("service_request", {
            "patient_id": enc["patient_id"], "encounter_id": enc["id"],
            "purpose": "investigation", "status": "active", "intent": "order",
            "code_system": master["system"], "code": master["code"],
            "display": master["display"],
            "authored_on": db.now_iso(), "requester_id": enc["practitioner_id"],
        })
    referral = request.f("referral_text")
    if referral:
        db.insert("service_request", {
            "patient_id": enc["patient_id"], "encounter_id": enc["id"],
            "purpose": "referral", "status": "active", "intent": "order",
            "code_system": "http://snomed.info/sct", "code": "306206005",
            "display": "Referral to service", "note": referral,
            "authored_on": db.now_iso(), "requester_id": enc["practitioner_id"],
        })


def close_visit(request: Request):
    eid = request.params["eid"]
    db.update("encounter", eid, {"status": "finished", "period_end": db.now_iso()})
    return redirect(f"/opd/{eid}", "Visit closed.")
