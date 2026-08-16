"""Domain services — the bits of logic the route handlers share."""

from __future__ import annotations

from datetime import date
from typing import Any, Sequence

from . import db, dialysis, hospital
from .db import now_iso, to_instant
from .terminology import ENCOUNTER_CLASS, SNOMED


# ---------------------------------------------------------------- patients
def display_age(patient: Any) -> str:
    """Age rendered for the screen, e.g. ``"47 y"`` or ``"age unknown"``."""
    years = age_years(patient)
    return f"{years} y" if years is not None else "age unknown"


def age_years(patient: Any) -> int | None:
    """Age in whole years from the date of birth, or the recorded age."""
    birth = patient["birth_date"] if _has(patient, "birth_date") else None
    if birth:
        try:
            born = date.fromisoformat(birth)
        except ValueError:
            return patient["age_years"]
        today = date.today()
        return today.year - born.year - ((today.month, today.day) < (born.month, born.day))
    return patient["age_years"] if _has(patient, "age_years") else None


def _has(row: Any, key: str) -> bool:
    try:
        return key in row.keys()
    except AttributeError:
        return key in row


def search_patients(term: str, limit: int = 100) -> list:
    """Patients matching a name, MRN, phone or ABHA fragment."""
    term = (term or "").strip()
    if not term:
        return db.query(
            "SELECT * FROM patient ORDER BY id DESC LIMIT ?", (limit,))
    like = f"%{term}%"
    return db.query(
        "SELECT * FROM patient WHERE name LIKE ? OR mrn LIKE ? OR phone LIKE ? "
        "OR abha_number LIKE ? OR abha_address LIKE ? ORDER BY id DESC LIMIT ?",
        (like, like, like, like, like, limit))


def create_patient(values: dict[str, Any]) -> int:
    """Register a patient, allocating the MRN and the timestamps."""
    values = dict(values)
    values["mrn"] = values.get("mrn") or db.next_number("mrn", "MRN")
    values["created_at"] = db.now_iso()
    values["updated_at"] = db.now_iso()
    org = db.default_org()
    values["managing_org_id"] = org["id"] if org else None
    return db.insert("patient", values)


# -------------------------------------------------------------- encounters
def create_encounter(kind: str, values: dict[str, Any]) -> int:
    """Open an OPD visit or IPD admission and allocate its number."""
    prefix = "OPD" if kind == "OPD" else "IPD"
    values = dict(values)
    values["kind"] = kind
    values["encounter_no"] = db.next_number(f"encounter_{kind}", f"{prefix}-")
    values["created_at"] = db.now_iso()
    class_code = values.get("class_code") or ("AMB" if kind == "OPD" else "IMP")
    values["class_code"] = class_code
    values["class_display"] = ENCOUNTER_CLASS.get(class_code, class_code)
    values.setdefault("type_system", SNOMED)
    values.setdefault("service_type_system", SNOMED)
    return db.insert("encounter", values)


def encounter_with_names(encounter_id: int):
    """One encounter joined to its patient and doctor."""
    return db.one(
        "SELECT e.*, p.name AS patient_name, p.mrn, p.gender, p.birth_date, "
        "       p.age_years, p.phone, d.name AS doctor_name "
        "FROM encounter e JOIN patient p ON p.id = e.patient_id "
        "LEFT JOIN practitioner d ON d.id = e.practitioner_id WHERE e.id = ?",
        (encounter_id,))


def list_encounters(kind: str, status: str | None = None, limit: int = 200) -> list:
    """Encounters of one kind, newest first, joined for display."""
    sql = ("SELECT e.*, p.name AS patient_name, p.mrn, d.name AS doctor_name "
           "FROM encounter e JOIN patient p ON p.id = e.patient_id "
           "LEFT JOIN practitioner d ON d.id = e.practitioner_id WHERE e.kind = ?")
    params: list[Any] = [kind]
    if status:
        sql += " AND e.status = ?"
        params.append(status)
    sql += " ORDER BY e.id DESC LIMIT ?"
    params.append(limit)
    return db.query(sql, params)


def upsert_note(encounter_id: int, patient_id: int, kind: str,
                values: dict[str, Any]) -> int:
    """Create or replace the narrative note attached to an encounter."""
    existing = db.one(
        "SELECT id FROM clinical_note WHERE encounter_id = ? AND kind = ?",
        (encounter_id, kind))
    values = dict(values)
    values["updated_at"] = db.now_iso()
    if existing:
        db.update("clinical_note", existing["id"], values)
        return existing["id"]
    values.update({"encounter_id": encounter_id, "patient_id": patient_id,
                   "kind": kind, "created_at": db.now_iso()})
    return db.insert("clinical_note", values)


# -------------------------------------------------------------- observations
def interpret(value: float | None, low: float | None, high: float | None) -> str | None:
    """Flag a reading against its reference range: H, L, N or ``None``."""
    if value is None:
        return None
    if low is not None and value < low:
        return "L"
    if high is not None and value > high:
        return "H"
    if low is None and high is None:
        return None
    return "N"


BMI_CODE = "39156-5"
HEIGHT_CODE = "8302-2"
WEIGHT_CODE = "29463-7"


def derive_bmi(readings: dict[str, str]) -> dict[str, str]:
    """Fill BMI from height + weight when the operator did not type one."""
    readings = dict(readings)
    if (readings.get(BMI_CODE) or "").strip():
        return readings
    try:
        height_cm = float(readings.get(HEIGHT_CODE) or "")
        weight_kg = float(readings.get(WEIGHT_CODE) or "")
    except ValueError:
        return readings
    if height_cm <= 0 or weight_kg <= 0:
        return readings
    metres = height_cm / 100.0
    readings[BMI_CODE] = f"{weight_kg / (metres * metres):.1f}"
    return readings


def record_vitals(patient_id: int, encounter_id: int | None,
                  readings: dict[str, str], effective_ts: str | None = None,
                  note: str | None = None,
                  extra: dict[str, Any] | None = None) -> int:
    """`readings` maps a LOINC code to the entered value; blanks are skipped.

    `encounter_id` may be None for vitals captured from the patient chart
    outside a visit — those stay on the chart and do not enter an encounter
    document.
    """
    readings = derive_bmi(readings)
    taken_at = to_instant(effective_ts) or now_iso()
    count = 0
    for index, code in enumerate(readings):
        raw = (readings.get(code) or "").strip()
        if not raw:
            continue
        master = db.term("vital", code)
        if master is None:
            continue
        try:
            value = float(raw)
        except ValueError:
            continue
        db.insert("observation", {
            **(extra or {}),
            "patient_id": patient_id,
            "encounter_id": encounter_id,
            "note": note,
            "category": "vital-signs",
            "status": "final",
            "loinc_code": master["code"],
            "loinc_display": master["display"],
            "snomed_code": master["alt_code"],
            "snomed_display": master["alt_display"],
            "value_quantity": value,
            "value_unit": master["unit"],
            "ref_low": master["ref_low"],
            "ref_high": master["ref_high"],
            "interpretation": interpret(value, master["ref_low"], master["ref_high"]),
            "effective_ts": taken_at,
            "sort_order": index,
        })
        count += 1
    return count


def vital_sets(patient_id: int, limit: int = 20) -> list[dict[str, Any]]:
    """Group a patient's vitals into the batches they were captured in."""
    rows = db.query(
        "SELECT o.*, e.encounter_no, e.kind AS encounter_kind FROM observation o "
        "LEFT JOIN encounter e ON e.id = o.encounter_id "
        "WHERE o.patient_id = ? AND o.category = 'vital-signs' "
        "ORDER BY o.effective_ts DESC, o.sort_order", (patient_id,))
    grouped: dict[str, dict[str, Any]] = {}
    for row in rows:
        key = f'{row["effective_ts"]}|{row["encounter_id"]}'
        batch = grouped.setdefault(key, {
            "effective_ts": row["effective_ts"],
            "encounter_id": row["encounter_id"],
            "encounter_no": row["encounter_no"],
            "encounter_kind": row["encounter_kind"],
            "note": row["note"],
            "readings": {},
        })
        batch["readings"][row["loinc_code"]] = row
    return list(grouped.values())[:limit]


def latest_vitals(patient_id: int) -> dict[str, Any]:
    sets = vital_sets(patient_id, limit=1)
    return sets[0] if sets else {}


# ------------------------------------------------------- allergies & problems
def add_allergy(patient_id: int, encounter_id: int | None, code: str | None,
                text: str, category: str | None, criticality: str | None,
                reaction_code: str | None, reaction_text: str | None) -> int:
    """AllergyIntolerance.code is 1..1 in the NRCES profile — a coded allergen
    when one was picked, otherwise the typed text carries the CodeableConcept."""
    master = db.term("allergen", code) if code else None
    reaction = db.term("reaction", reaction_code) if reaction_code else None
    label = text or (master["display"] if master else "")
    if not label:
        raise ValueError("an allergy needs either a coded allergen or a description")
    manifestation = reaction_text or (reaction["display"] if reaction else None)
    return db.insert("allergy", {
        "patient_id": patient_id,
        "encounter_id": encounter_id,
        "snomed_code": master["code"] if master else None,
        "snomed_display": master["display"] if master else None,
        "text": label,
        "category": category or (master["extra"] if master else None),
        "criticality": criticality or None,
        "clinical_status": "active",
        "reaction": manifestation,
        "recorded_at": db.now_iso(),
    })


def add_problem(patient_id: int, encounter_id: int | None, code: str | None,
                text: str, clinical_status: str = "active",
                onset: str | None = None, note: str | None = None) -> int:
    """A problem-list entry. Left unattached to an encounter it still reaches the
    Medical history section of every document generated for this patient."""
    master = db.term("diagnosis", code) if code else None
    label = (master["display"] if master else "") or text
    if not label:
        raise ValueError("a problem needs either a coded diagnosis or a description")
    return db.insert("condition", {
        "patient_id": patient_id,
        "encounter_id": encounter_id,
        "category": "medical-history",
        "clinical_status": clinical_status or "active",
        "verification_status": "confirmed",
        "snomed_code": master["code"] if master else None,
        "snomed_display": master["display"] if master else None,
        "icd10_code": master["alt_code"] if master else None,
        "icd10_display": master["alt_display"] if master else None,
        "text": label,
        "onset": onset or None,
        "note": note or None,
        "recorded_at": db.now_iso(),
    })


# -------------------------------------------------------------------- labs
def create_lab_order(patient_id: int, encounter_id: int | None, panel_code: str,
                     interpreter_id: int | None, specimen_code: str | None) -> int:
    panel = db.term("lab_panel", panel_code)
    if panel is None:
        raise ValueError("unknown panel")
    category, analytes, price = (panel["extra"] or "||0").split("|")
    org = db.default_org()
    specimen = db.term("specimen", specimen_code) if specimen_code else None
    order_id = db.insert("lab_order", {
        "order_no": db.next_number("lab", "LAB-"),
        "patient_id": patient_id,
        "encounter_id": encounter_id,
        "status": "registered",
        # SNOMED "Laboratory procedure" is the DiagnosticReport.category the
        # NRCES profile fixes the system for; the local name rides in the display.
        "category_code": "108252007",
        "category_display": "Laboratory procedure",
        "panel_code": panel["code"],
        "panel_display": panel["display"],
        "specimen_code": specimen["code"] if specimen else panel["alt_code"],
        "specimen_display": specimen["display"] if specimen else panel["alt_display"],
        "specimen_collected_ts": db.now_iso(),
        "specimen_received_ts": db.now_iso(),
        "performer_org_id": org["id"] if org else None,
        "interpreter_id": interpreter_id,
        "ordered_at": db.now_iso(),
        "price": float(price or 0),
    })
    for index, code in enumerate([c for c in analytes.split(",") if c]):
        analyte = db.term("lab_analyte", code)
        if analyte is None:
            continue
        db.insert("observation", {
            "patient_id": patient_id,
            "encounter_id": encounter_id,
            "lab_order_id": order_id,
            "category": "laboratory",
            "status": "registered",
            "loinc_code": analyte["code"],
            "loinc_display": analyte["display"],
            "value_unit": analyte["unit"],
            "ref_low": analyte["ref_low"],
            "ref_high": analyte["ref_high"],
            "effective_ts": db.now_iso(),
            "sort_order": index,
        })
    return order_id


def save_lab_results(order_id: int, values: dict[str, str], conclusion: str,
                     finalise: bool) -> None:
    """Store analyte results and set the report status and conclusion."""
    rows = db.query("SELECT * FROM observation WHERE lab_order_id = ?", (order_id,))
    abnormal = False
    for row in rows:
        raw = (values.get(f"result_{row['id']}") or "").strip()
        patch: dict[str, Any] = {"status": "final" if finalise else "preliminary"}
        if raw:
            try:
                number = float(raw)
                flag = interpret(number, row["ref_low"], row["ref_high"])
                abnormal = abnormal or flag in ("H", "L", "A")
                patch.update({
                    "value_quantity": number,
                    "value_string": None,
                    "interpretation": flag,
                })
            except ValueError:
                patch.update({"value_quantity": None, "value_string": raw,
                              "interpretation": None})
            patch["effective_ts"] = db.now_iso()
        db.update("observation", row["id"], patch)

    # DiagnosticReportLab.conclusionCode is SNOMED-fixed; derive it from the flags.
    verdict = ("263654008", "Abnormal") if abnormal else ("17621005", "Normal")
    db.update("lab_order", order_id, {
        "conclusion": conclusion or (
            "One or more analytes fall outside the reference range."
            if abnormal else "All analytes within the reference range."),
        "conclusion_code": verdict[0] if finalise else None,
        "conclusion_display": verdict[1] if finalise else None,
        "status": "final" if finalise else "preliminary",
        "effective_ts": db.now_iso(),
        "issued_ts": db.now_iso() if finalise else None,
    })


# ----------------------------------------------------------------- billing
def compute_line(unit_price: float, quantity: float, discount_pct: float,
                 cgst_pct: float, sgst_pct: float) -> tuple[float, float]:
    """Net and gross for one invoice line, after discount and GST."""
    gross = unit_price * quantity
    net = gross - gross * discount_pct / 100.0
    total = net + net * cgst_pct / 100.0 + net * sgst_pct / 100.0
    return round(net, 2), round(total, 2)


def save_invoice(invoice_id: int, lines: Sequence[dict[str, Any]]) -> None:
    """Replace an invoice's lines and recompute its totals."""
    db.execute("DELETE FROM invoice_line WHERE invoice_id = ?", (invoice_id,))
    total_net = total_gross = 0.0
    for seq, line in enumerate(lines, start=1):
        net, gross = compute_line(line["unit_price"], line["quantity"],
                                  line["discount_pct"], line["cgst_pct"],
                                  line["sgst_pct"])
        total_net += net
        total_gross += gross
        db.insert("invoice_line", {
            "invoice_id": invoice_id, "seq": seq,
            "description": line["description"],
            "charge_system": line["charge_system"],
            "charge_code": line["charge_code"],
            "charge_display": line["charge_display"],
            "quantity": line["quantity"],
            "unit_price": line["unit_price"],
            "discount_pct": line["discount_pct"],
            "cgst_pct": line["cgst_pct"],
            "sgst_pct": line["sgst_pct"],
            "line_net": net, "line_gross": gross,
        })
    db.update("invoice", invoice_id, {
        "total_net": round(total_net, 2),
        "total_gross": round(total_gross, 2),
    })


def suggest_invoice_lines(encounter_id: int) -> list[dict[str, Any]]:
    """Pre-fill a bill from what the encounter actually consumed."""
    enc = db.one("SELECT * FROM encounter WHERE id = ?", (encounter_id,))
    if enc is None:
        return []
    out: list[dict[str, Any]] = []

    def charge(code: str, description: str, price: float, quantity: float = 1) -> None:
        master = db.term("charge", code)
        if master is None or price <= 0:
            return
        out.append({
            "description": description,
            "charge_system": master["system"],
            "charge_code": master["code"],
            "charge_display": master["display"],
            "quantity": quantity,
            "unit_price": price,
            "discount_pct": 0.0,
            "cgst_pct": 0.0,
            "sgst_pct": 0.0,
        })

    if enc["kind"] == "OPD":
        charge("REG-OPD", "OPD registration charge", 100)
        if enc["consultation_fee"]:
            doctor = db.one("SELECT name FROM practitioner WHERE id = ?",
                            (enc["practitioner_id"],))
            label = f"Consultation — {doctor['name']}" if doctor else "Consultation"
            charge("CONS-SPL", label, enc["consultation_fee"])
    else:
        # Bed and nursing come from the bed-movement ledger, so a mid-stay
        # transfer to a different ward bills at each ward's own tariff.
        stay = hospital.stay_charges(encounter_id)
        for line in stay:
            code = "BED-DAY" if line["kind"] == "bed" else "NURS-DAY"
            charge(code, f'{line["label"]} × {line["days"]} day(s)',
                   line["rate"], line["days"])
        if not stay:
            days = max(1, _stay_days(enc))
            charge("BED-DAY", f"Bed charges — {enc['ward'] or 'ward'}",
                   enc["bed_rate"] or 1500, days)
            charge("NURS-DAY", "Nursing charges", 600, days)
        if enc["consultation_fee"]:
            days = max(1, _stay_days(enc))
            charge("CONS-SPL", "Consultant visit charges",
                   enc["consultation_fee"], days)

    # Dialysis runs bill per session from the course tariff below, so the row
    # the run wrote into `procedure` must not also attract a theatre charge.
    for proc in db.query(
            "SELECT * FROM procedure WHERE encounter_id = ? "
            "AND dialysis_session_id IS NULL", (encounter_id,)):
        charge("OT-MINOR", f"Procedure — {proc['snomed_display']}", 4500)

    for order in db.query(
            "SELECT * FROM lab_order WHERE encounter_id = ? AND price > 0",
            (encounter_id,)):
        charge("LAB-TEST", f"Lab — {order['panel_display']}", order["price"])

    # Completed dialysis runs that have not yet been billed.
    for run in dialysis.unbilled_sessions(encounter_id):
        if run["session_charge"]:
            charge("DIAL-HD", f'{run["modality_display"]} — session {run["seq"]}',
                   run["session_charge"])

    # Anything issued from the pharmacy that has not yet been billed.
    for issue in hospital.unbilled_issues(encounter_id):
        master = db.term("charge", "PHARM")
        if master is None:
            continue
        out.append({
            "description": f'{issue["name"]} × {issue["qty"]:g} {issue["unit"]}',
            "charge_system": master["system"], "charge_code": master["code"],
            "charge_display": master["display"], "quantity": issue["qty"],
            "unit_price": issue["rate"], "discount_pct": 0.0,
            "cgst_pct": (issue["gst_pct"] or 0) / 2,
            "sgst_pct": (issue["gst_pct"] or 0) / 2,
        })

    return out


def _stay_days(enc) -> int:
    """Billable days of a stay, ending at discharge, period end, or now."""
    end = enc["discharge_ts"] or enc["period_end"] or db.now_iso()
    return hospital.bed_days(enc["period_start"], end)


def create_invoice(patient_id: int, encounter_id: int | None, type_code: str,
                   type_display: str, participant_id: int | None,
                   note: str | None = None) -> int:
    """Open an invoice and allocate its number."""
    org = db.default_org()
    return db.insert("invoice", {
        "invoice_no": db.next_number("invoice", "INV-"),
        "patient_id": patient_id,
        "encounter_id": encounter_id,
        "status": "issued",
        "type_code": type_code,
        "type_display": type_display,
        "date": db.now_iso(),
        "issuer_org_id": org["id"] if org else None,
        "participant_id": participant_id,
        "note": note,
        "created_at": db.now_iso(),
    })
