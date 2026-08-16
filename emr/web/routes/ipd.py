"""IPD admission, in-patient clinical record and discharge summary."""

from __future__ import annotations

from typing import Any

from ... import db, hospital, services
from .. import ui
from ..common import (
    patient_cell,
    collect_rows, doctor_options, get_patient, not_found, patient_options, render,
    status_label, term_options,
)
from ..router import Request, redirect
from .opd import (
    _complaint_row, _diagnosis_row, _medication_row, _replace_complaints,
    _replace_diagnoses, _replace_medications,
)

st = ui.st


def register(app) -> None:
    app.add("GET", "/ipd", index)
    app.add("GET", "/ipd/new", new)
    app.add("POST", "/ipd", create)
    app.add("GET", "/ipd/<int:eid>", detail)
    app.add("POST", "/ipd/<int:eid>/clinical", save_clinical)
    app.add("GET", "/ipd/<int:eid>/discharge", discharge_form)
    app.add("POST", "/ipd/<int:eid>/discharge", discharge)


# ---------------------------------------------------------------------------
def index(request: Request):
    rows = services.list_encounters("IPD")
    table_rows = [[
        f'<a class="z-link" href="/ipd/{r["id"]}">{ui.esc(r["encounter_no"])}</a>',
        patient_cell(r),
        ui.esc(r["ward"] or "—") + (f' / {ui.esc(r["bed"])}' if r["bed"] else ""),
        ui.when(r["period_start"]),
        ui.when(r["discharge_ts"]),
        ui.esc(r["doctor_name"] or "—"),
        status_label(r["status"]),
        (ui.button("Discharge", f'/ipd/{r["id"]}/discharge', size="z-button-xsmall",
                   style="z-button-primary")
         if r["status"] != "finished" else
         ui.button("Summary", f'/ipd/{r["id"]}', size="z-button-xsmall")),
    ] for r in rows]
    body = ui.card(
        f"{len(table_rows)} admission(s)",
        ui.table(["Admission", "Patient", "Ward / bed", "Admitted", "Discharged",
                  "Doctor", "Status", ""], table_rows,
                 empty="No admissions recorded yet."),
        actions=ui.button("New admission", "/ipd/new", style="z-button-primary",
                          ico="hospital"))
    return render(request, "IPD admissions", "ipd", body)


# ---------------------------------------------------------------------------
def new(request: Request):
    preset = request.q("patient")
    beds = hospital.vacant_beds()
    bed_options = [(b["id"], f'{b["ward_name"]} · {b["code"]} — '
                             f'₹{hospital.bed_tariff(b):.0f}/day') for b in beds]
    body = (
        '<form method="post" action="/ipd">'
        + ui.card("Admission details", (
            ui.field("Patient", ui.select("patient_id", patient_options(), preset,
                                          blank="Select a registered patient",
                                          required=True), required=True)
            + ui.grid(
                ui.field("Admitting doctor", ui.select(
                    "practitioner_id", doctor_options(), "", blank="Select doctor",
                    required=True), required=True),
                ui.field("Department", ui.select(
                    "department", term_options("department", False), "",
                    blank="Select department")),
                ui.field("Bed", ui.select("bed_id", bed_options, "",
                                          blank="Select a vacant bed", required=True),
                         help_text=f"{len(beds)} bed(s) free right now. "
                                   "Assigning one marks it occupied on the ward board.",
                         required=True),
                ui.field("Admission type", ui.select(
                    "admission_type", term_options("admission_type", False), "elective")),
                ui.field("Encounter class", ui.select("class_code", [
                    ("IMP", "Inpatient (IMP)"), ("EMER", "Emergency (EMER)")], "IMP")),
                ui.field("Encounter type", ui.select(
                    "type_code", term_options("encounter_type"), "32485007")),
                ui.field("Service type", ui.select(
                    "service_type_code", term_options("service_type"), "394802001")),
                ui.field("Priority", ui.select(
                    "priority_code", term_options("priority", False), "R")),
                ui.field("Reason for admission", ui.select(
                    "reason_code", term_options("complaint"), "", blank="Not stated")),
                ui.field("Consultant visit charge (₹)", ui.text_input(
                    "consultation_fee", "", type_="number", attrs='min="0" step="1"')),
                cols=2)),
            footer=(f'<div class="display-flex justify-between gap"{st(gap=2)}>'
                    + ui.button("Cancel", "/ipd", style="z-button-secondary")
                    + ui.button("Admit patient", style="z-button-primary",
                                size="z-button-medium", type_="submit") + "</div>"))
        + "</form>")
    return render(request, "New admission", "ipd-new", body,
                  breadcrumb=[("IPD", "/ipd"), ("New admission", None)])


def create(request: Request):
    patient_id = request.f_int("patient_id")
    if not patient_id:
        return redirect("/ipd/new", "Select a patient first.", "danger")
    bed_id = request.f_int("bed_id")
    if not bed_id:
        return redirect("/ipd/new", "Pick a vacant bed for the patient.", "danger")
    type_row = db.term("encounter_type", request.f("type_code"))
    service_row = db.term("service_type", request.f("service_type_code"))
    priority_row = db.term("priority", request.f("priority_code"))
    reason_row = db.term("complaint", request.f("reason_code"))
    dept_row = db.term("department", request.f("department")) if request.f("department") else None
    admission_row = db.term("admission_type", request.f("admission_type"))

    encounter_id = services.create_encounter("IPD", {
        "patient_id": patient_id,
        "status": "in-progress",
        "class_code": request.f("class_code") or "IMP",
        "type_code": type_row["code"] if type_row else None,
        "type_display": type_row["display"] if type_row else None,
        "service_type_code": service_row["code"] if service_row else None,
        "service_type_display": service_row["display"] if service_row else None,
        "priority_code": priority_row["code"] if priority_row else None,
        "priority_display": priority_row["display"] if priority_row else None,
        "priority_system": priority_row["system"] if priority_row else None,
        "practitioner_id": request.f_int("practitioner_id"),
        "department": dept_row["display"] if dept_row else None,
        "period_start": db.now_iso(),
        "reason_code": reason_row["code"] if reason_row else None,
        "reason_display": reason_row["display"] if reason_row else None,
        "admission_type": admission_row["display"] if admission_row else None,
        "consultation_fee": request.f_float("consultation_fee", 0.0),
    })
    try:
        placed = hospital.occupy_bed(bed_id, encounter_id, reason="Admission")
    except ValueError as error:
        db.execute("DELETE FROM encounter WHERE id = ?", (encounter_id,))
        return redirect("/ipd/new", f"Could not admit: {error}", "danger")
    number = db.scalar("SELECT encounter_no FROM encounter WHERE id = ?", (encounter_id,))
    return redirect(f"/ipd/{encounter_id}",
                    f'Patient admitted as {number} into {placed["ward"]} / '
                    f'{placed["bed"]}.')


# ---------------------------------------------------------------------------
def _procedure_row(values: dict[str, Any] | None = None) -> str:
    v = values or {}
    return ui.row_shell(
        ui.cell("Procedure (SNOMED CT)", ui.select(
            "pr_code", term_options("procedure"), v.get("code", ""),
            blank="Select procedure"), "18rem")
        + ui.cell("Category", ui.select("pr_category",
                                        term_options("procedure_category", False),
                                        v.get("category", ""), blank="None"), "12rem")
        + ui.cell("Performed on", ui.text_input("pr_when", v.get("when", ""),
                                                type_="datetime-local"), "13rem")
        + ui.cell("Outcome", ui.select("pr_outcome",
                                       term_options("procedure_outcome", False),
                                       v.get("outcome", ""), blank="Not recorded"), "12rem")
        + ui.cell("Note", ui.text_input("pr_note", v.get("note", "")), "14rem"))


def detail(request: Request):
    eid = request.params["eid"]
    enc = services.encounter_with_names(eid)
    if enc is None or enc["kind"] != "IPD":
        return not_found(request, "Admission")
    patient = get_patient(enc["patient_id"])
    labs = db.query("SELECT * FROM lab_order WHERE encounter_id = ? ORDER BY id DESC",
                    (eid,))

    actions = (
        ui.label_chip(enc["encounter_no"], "warning") + status_label(enc["status"])
        + ui.button("Order lab", f"/lab/new?encounter={eid}", ico="flask-conical")
        + ui.button("Issue medicines", f"/pharmacy/issue?encounter={eid}", ico="pill")
        + ui.button("Raise bill", f"/billing/new?encounter={eid}",
                    ico="receipt-indian-rupee"))
    if enc["status"] != "finished":
        actions += ui.button("Discharge patient", f"/ipd/{eid}/discharge",
                             style="z-button-primary", ico="log-out")

    facts = ui.card("Admission", ui.dl([
        ("Admission number", enc["encounter_no"]),
        ("Admitted", ui.when(enc["period_start"])),
        ("Discharged", ui.when(enc["discharge_ts"])),
        ("Ward / bed", f'{enc["ward"] or "—"} / {enc["bed"] or "—"}'),
        ("Bed rate", f'₹ {enc["bed_rate"]:.0f} per day'),
        ("Admission type", enc["admission_type"]),
        ("Consultant", enc["doctor_name"]),
        ("Department", enc["department"]),
        ("Reason", enc["reason_display"]),
        ("Disposition", enc["discharge_disposition_display"]),
    ], cols=3))

    lab_rows = [[
        f'<a class="z-link" href="/lab/{o["id"]}">{ui.esc(o["order_no"])}</a>',
        ui.esc(o["panel_display"]), ui.when(o["ordered_at"]),
        status_label(o["status"])] for o in labs]


    body = (
        ui.patient_header(patient, extra=actions)
        + f'<div class="mt"{st(mt=5)}>' + facts + "</div>"
        + f'<div class="mt"{st(mt=5)}>' + _stay_card(enc) + "</div>"
        + f'<div class="mt"{st(mt=5)}>' + _clinical_form(eid) + "</div>"
        + f'<div class="mt"{st(mt=5)}>'
        + ui.card("Laboratory", ui.table(["Order", "Panel", "Ordered", "Status"],
                                         lab_rows, empty="No lab orders."))
        + "</div>")

    return render(request, f"Admission {enc['encounter_no']}", "ipd", body,
                  breadcrumb=[("IPD", "/ipd"), (enc["encounter_no"], None)])


def _stay_card(enc) -> str:
    """Bed history, running room charges, and the transfer control."""
    eid = enc["id"]
    moves = db.query(
        "SELECT m.*, b.code AS bed_code, w.name AS ward_name FROM bed_movement m "
        "JOIN bed b ON b.id = m.bed_id JOIN ward w ON w.id = b.ward_id "
        "WHERE m.encounter_id = ? ORDER BY m.from_ts", (eid,))
    move_rows = [[
        ui.esc(m["ward_name"]), ui.esc(m["bed_code"]),
        ui.when(m["from_ts"]),
        ui.when(m["to_ts"]) if m["to_ts"] else "current",
        f'₹ {m["tariff"]:.0f}', f'₹ {m["nursing_rate"]:.0f}',
        ui.esc(m["reason"] or "—"),
    ] for m in moves]

    charges = hospital.stay_charges(eid)
    total = sum(c["rate"] * c["days"] for c in charges)
    charge_rows = [[ui.esc(c["label"]), f'{c["days"]}', f'₹ {c["rate"]:,.0f}',
                    f'₹ {c["rate"] * c["days"]:,.2f}'] for c in charges]
    if charge_rows:
        charge_rows.append(["<strong>Room and nursing to date</strong>", "", "",
                            f"<strong>₹ {total:,.2f}</strong>"])

    transfer = ""
    if enc["status"] != "finished":
        patient = db.one("SELECT gender FROM patient WHERE id = ?", (enc["patient_id"],))
        beds = hospital.vacant_beds(patient["gender"] if patient else None)
        options = [(b["id"], f'{b["ward_name"]} · {b["code"]} — '
                             f'₹{hospital.bed_tariff(b):.0f}/day') for b in beds]
        form = (f'<form method="post" action="/beds/transfer/{eid}">'
                + ui.grid(
                    ui.field("Move to", ui.select("bed_id", options, "",
                                                  blank="Select a vacant bed",
                                                  required=True), required=True),
                    ui.field("Reason", ui.text_input(
                        "reason", "", placeholder="e.g. stepped down from ICU")),
                    cols=2)
                + f'<div class="display-flex justify-end"{st()}>'
                + ui.button("Transfer bed", style="z-button-primary", type_="submit",
                            ico="arrow-right-left")
                + "</div></form>")
        transfer = (f'<div class="mt"{st(mt=4)}>'
                    + ui.accordion([("Transfer to another bed", form)],
                                   open_first=False) + "</div>")

    return ui.card(
        "Stay and room charges",
        ui.table(["Ward", "Bed", "From", "To", "Bed/day", "Nursing/day", "Reason"],
                 move_rows, empty="No bed assigned.", align_right=(4, 5))
        + f'<div class="mt"{st(mt=4)}>'
        + ui.table(["Charge", "Days", "Rate", "Amount"], charge_rows,
                   empty="Nothing accrued yet.", align_right=(1, 2, 3))
        + "</div>" + transfer)


def _clinical_form(eid: int) -> str:
    complaints = db.query(
        "SELECT * FROM condition WHERE encounter_id = ? AND category = 'chief-complaint' "
        "ORDER BY id", (eid,))
    diagnoses = db.query(
        "SELECT * FROM condition WHERE encounter_id = ? AND category = 'diagnosis' "
        "ORDER BY id", (eid,))
    medications = db.query(
        "SELECT * FROM medication_request WHERE encounter_id = ? ORDER BY sort_order, id",
        (eid,))
    procedures = db.query("SELECT * FROM procedure WHERE encounter_id = ? ORDER BY id",
                          (eid,))
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
    procedure_rows = [_procedure_row({
        "code": p["snomed_code"], "category": p["category_code"] or "",
        "when": (p["performed_ts"] or "")[:16], "outcome": p["outcome_code"] or "",
        "note": p["note"] or ""}) for p in procedures] or [_procedure_row()]

    vital_fields = []
    for master in db.terms("vital"):
        current = vitals.get(master["code"])
        vital_fields.append(ui.field(
            f'{master["display"].split("[")[0].strip()} ({master["unit"]})',
            ui.text_input(f'vital_{master["code"]}',
                          current["value_quantity"] if current else "",
                          type_="number", attrs='step="0.1"'),
            help_text=f"LOINC {master['code']}"))

    sections = ui.accordion([
        ("Presenting complaints", ui.repeater("cc", _complaint_row(), complaint_rows,
                                              "Add complaint")),
        ("Vitals on record", (
            f'<div class="display-grid gap sm:grid-cols lg:grid-cols"'
            + st(gap=3, sm_grid_cols=2, lg_grid_cols=3) + ">"
            + "".join(vital_fields) + "</div>")),
        ("Diagnoses", ui.repeater("dx", _diagnosis_row(), diagnosis_rows,
                                  "Add diagnosis")),
        ("Procedures performed", ui.repeater("pr", _procedure_row(), procedure_rows,
                                             "Add procedure")),
        ("In-patient medication", ui.repeater("rx", _medication_row(), medication_rows,
                                              "Add medicine")),
    ], multiple=True)

    return (f'<form method="post" action="/ipd/{eid}/clinical">'
            + ui.card("In-patient clinical record", sections, footer=(
                f'<div class="display-flex justify-end"{st()}>'
                + ui.button("Save clinical record", style="z-button-primary",
                            size="z-button-medium", type_="submit") + "</div>"))
            + "</form>")


def save_clinical(request: Request):
    eid = request.params["eid"]
    enc = db.one("SELECT * FROM encounter WHERE id = ?", (eid,))
    if enc is None:
        return not_found(request, "Admission")

    _replace_complaints(request, enc, "cc")
    _replace_diagnoses(request, enc)
    _replace_medications(request, enc)
    _replace_procedures(request, enc)

    db.execute("DELETE FROM observation WHERE encounter_id = ? AND category = 'vital-signs'",
               (eid,))
    services.record_vitals(enc["patient_id"], eid, {
        master["code"]: request.f(f'vital_{master["code"]}')
        for master in db.terms("vital")})
    return redirect(f"/ipd/{eid}", "Clinical record saved.")


def _replace_procedures(request: Request, enc) -> None:
    db.execute("DELETE FROM procedure WHERE encounter_id = ?", (enc["id"],))
    fields = ["code", "category", "when", "outcome", "note"]
    for row in collect_rows(request, "pr", fields):
        master = db.term("procedure", row["code"]) if row["code"] else None
        if master is None:
            continue
        category = db.term("procedure_category", row["category"]) if row["category"] else None
        outcome = db.term("procedure_outcome", row["outcome"]) if row["outcome"] else None
        db.insert("procedure", {
            "patient_id": enc["patient_id"], "encounter_id": enc["id"],
            "status": "completed",
            "snomed_code": master["code"], "snomed_display": master["display"],
            "category_code": category["code"] if category else None,
            "category_display": category["display"] if category else None,
            "outcome_code": outcome["code"] if outcome else None,
            "outcome_display": outcome["display"] if outcome else None,
            "performed_ts": db.to_instant(row["when"]) if row["when"] else db.now_iso(),
            "performer_id": enc["practitioner_id"],
            "note": row["note"] or None,
        })


# ---------------------------------------------------------------------------
def discharge_form(request: Request):
    eid = request.params["eid"]
    enc = services.encounter_with_names(eid)
    if enc is None or enc["kind"] != "IPD":
        return not_found(request, "Admission")
    patient = get_patient(enc["patient_id"])
    note = db.one(
        "SELECT * FROM clinical_note WHERE encounter_id = ? AND kind = 'DISCHARGE_SUMMARY'",
        (eid,))
    n = dict(note) if note else {}

    diagnoses = db.query(
        "SELECT * FROM condition WHERE encounter_id = ? AND category = 'diagnosis'", (eid,))
    procedures = db.query("SELECT * FROM procedure WHERE encounter_id = ?", (eid,))
    labs = db.query(
        "SELECT * FROM lab_order WHERE encounter_id = ? AND status = 'final'", (eid,))
    medications = db.query(
        "SELECT * FROM medication_request WHERE encounter_id = ?", (eid,))

    captured = ui.card("Pulled into the summary automatically", ui.dl([
        ("Diagnoses", f"{len(diagnoses)} recorded"),
        ("Procedures", f"{len(procedures)} recorded"),
        ("Finalised lab reports", f"{len(labs)} report(s)"),
        ("Medications", f"{len(medications)} item(s)"),
    ], cols=4) + f'<p class="mt text-sm color"'
        + st(mt=3, color="var(--z-muted-f)") + ">"
        + "Edit these on the admission screen; they map to the Investigations, "
          "Medications, Procedures and Medical history sections of the NRCES "
          "DischargeSummaryRecord.</p>")

    form = (
        f'<form method="post" action="/ipd/{eid}/discharge">'
        + ui.card("Discharge summary", (
            ui.grid(
                ui.field("Discharge date & time", ui.text_input(
                    "discharge_ts", (enc["discharge_ts"] or db.now_iso())[:16],
                    type_="datetime-local", required=True), required=True),
                ui.field("Discharge disposition", ui.select(
                    "disposition", term_options("discharge_disposition", False),
                    enc["discharge_disposition_code"] or "home"), required=True),
                cols=2)
            + ui.field("Reason for admission", ui.textarea(
                "admission_reason", n.get("admission_reason", ""), rows=2))
            + ui.field("History / clinical background", ui.textarea(
                "history_text", n.get("history_text", ""), rows=3))
            + ui.field("Examination on admission", ui.textarea(
                "examination_text", n.get("examination_text", ""), rows=3))
            + ui.field("Course in hospital", ui.textarea(
                "course_in_hospital", n.get("course_in_hospital", ""), rows=5,
                placeholder="Day-by-day progress, response to treatment, complications…"))
            + ui.field("Condition at discharge", ui.textarea(
                "condition_at_discharge", n.get("condition_at_discharge", ""), rows=2))
            + ui.field("Discharge instructions", ui.textarea(
                "discharge_instructions", n.get("discharge_instructions", ""), rows=3))
            + ui.field("Diet advice", ui.textarea("diet_advice", n.get("diet_advice", ""),
                                                  rows=2))
            + ui.field("Care plan", ui.textarea("care_plan_text",
                                                n.get("care_plan_text", ""), rows=3))
            + ui.grid(
                ui.field("Follow-up date", ui.text_input(
                    "follow_up_date", n.get("follow_up_date", "") or "", type_="date")),
                ui.field("Follow-up instructions", ui.text_input(
                    "follow_up_note", n.get("follow_up_note", "") or "")),
                cols=2)),
            footer=(f'<div class="display-flex justify-between gap flex-wrap"{st(gap=2)}>'
                    + ui.button("Back", f"/ipd/{eid}", style="z-button-secondary")
                    + ui.button("Save & discharge", style="z-button-primary",
                                size="z-button-medium", type_="submit") + "</div>"))
        + "</form>")

    body = (ui.patient_header(patient, extra=ui.label_chip(enc["encounter_no"], "warning"))
            + f'<div class="mt"{st(mt=5)}>' + captured + "</div>"
            + f'<div class="mt"{st(mt=5)}>' + form + "</div>")
    return render(request, "Discharge summary", "ipd", body,
                  breadcrumb=[("IPD", "/ipd"), (enc["encounter_no"], f"/ipd/{eid}"),
                              ("Discharge", None)])


def discharge(request: Request):
    eid = request.params["eid"]
    enc = db.one("SELECT * FROM encounter WHERE id = ?", (eid,))
    if enc is None:
        return not_found(request, "Admission")
    disposition = db.term("discharge_disposition", request.f("disposition"))
    discharge_ts = db.to_instant(request.f("discharge_ts")) or db.now_iso()

    services.upsert_note(eid, enc["patient_id"], "DISCHARGE_SUMMARY", {
        "author_id": enc["practitioner_id"],
        "admission_reason": request.f_or_none("admission_reason"),
        "history_text": request.f_or_none("history_text"),
        "examination_text": request.f_or_none("examination_text"),
        "course_in_hospital": request.f_or_none("course_in_hospital"),
        "condition_at_discharge": request.f_or_none("condition_at_discharge"),
        "discharge_instructions": request.f_or_none("discharge_instructions"),
        "diet_advice": request.f_or_none("diet_advice"),
        "care_plan_text": request.f_or_none("care_plan_text"),
        "follow_up_date": request.f_or_none("follow_up_date"),
        "follow_up_note": request.f_or_none("follow_up_note"),
        "status": "final",
    })
    db.update("encounter", eid, {
        "status": "finished",
        "period_end": discharge_ts,
        "discharge_ts": discharge_ts,
        "discharge_disposition_code": disposition["code"] if disposition else None,
        "discharge_disposition_display": disposition["display"] if disposition else None,
    })
    hospital.release_bed(eid, discharge_ts)
    return redirect(f"/ipd/{eid}",
                    "Patient discharged and summary saved. You can now export the "
                    "NRCES Discharge Summary bundle.")
