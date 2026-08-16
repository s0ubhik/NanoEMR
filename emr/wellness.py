"""Wellness records — the NRCES `WellnessRecord` artifact.

Periodic, largely self-reported health data: vitals, body measurements, physical
activity, general assessment, women's health and lifestyle. Each section maps to
its own NRCES Observation profile, and each of those binds `code` to a published
ValueSet — so the pickers here offer exactly the bound codes rather than free
text.

One structural quirk worth knowing: unlike the OP consult and discharge summary,
`WellnessRecord` slices its sections by a **fixed `title` string** and puts no
`code` on them at all.
"""

from __future__ import annotations

from typing import Any

from . import db
from .terminology import PROFILE, SNOMED

# section key -> (fixed title, terminology kind, observation profile, value kind)
# value kind mirrors the `value[x]` types each profile permits.
SECTIONS: dict[str, tuple[str, str, str, str]] = {
    "vital-signs": ("Vital Signs", "wellness_vital",
                    "ObservationVitalSigns", "quantity-or-string"),
    "body-measurement": ("Body Measurement", "wellness_body",
                         "ObservationBodyMeasurement", "quantity-or-string"),
    "physical-activity": ("Physical Activity", "wellness_activity",
                          "ObservationPhysicalActivity", "quantity-or-string"),
    "general-assessment": ("General Assessment", "wellness_assessment",
                           "ObservationGeneralAssessment", "quantity-or-coded"),
    "women-health": ("Women Health", "wellness_women",
                     "ObservationWomenHealth", "quantity-or-string"),
    "lifestyle": ("Lifestyle", "wellness_lifestyle",
                  "ObservationLifestyle", "coded-only"),
    # Generated only — the base NRCES Observation profile, which is the one
    # place in this artifact that will carry a concept with no bound code.
    "other": ("Other Observations", "", "Observation", "generated"),
}

SECTION_ORDER = list(SECTIONS)

STATUS = {"draft": ("Draft", "warning"), "final": ("Final", "success")}

# Sections an operator types into; "other" is machine-populated.
EDITABLE_SECTIONS = [k for k, v in SECTIONS.items() if v[3] != "generated"]

# The dialysis flowsheet uses the most specific LOINC for each sign; the
# WellnessRecord vital-signs ValueSet prefers these. An extensible binding says
# use a member where one is suitable — 2708-6 is, 8310-5 has no counterpart
# (the set carries body *surface* temperature, a different concept).
DIALYSIS_VITAL_MAP = {
    "8867-4": ("8867-4", "Heart rate", "/min"),
    "9279-1": ("9279-1", "Respiratory rate", "/min"),
    "59408-5": ("2708-6", "Oxygen saturation in Arterial blood", "%"),
    "8310-5": ("8310-5", "Body temperature", "Cel"),
}


def section_profile(category: str) -> str:
    """The NRCES Observation profile a wellness section writes under."""
    entry = SECTIONS.get(category)
    return PROFILE[entry[2]] if entry else PROFILE["Observation"]


# ===========================================================================
# records
# ===========================================================================
def create_record(patient_id: int, encounter_id: int | None,
                  practitioner_id: int | None, recorded_on: str | None,
                  source: str | None, note: str | None) -> int:
    """Open a hand-entered wellness record as a draft."""
    stamp = db.now_iso()
    return db.insert("wellness_record", {
        "record_no": db.next_number("wellness", "WEL-"),
        "patient_id": patient_id, "encounter_id": encounter_id,
        "practitioner_id": practitioner_id, "status": "draft",
        "recorded_on": recorded_on or db.today_iso(),
        "source": source, "note": note,
        "created_at": stamp, "updated_at": stamp,
    })


def record(record_id: int):
    """One record joined to its patient, doctor and encounter."""
    return db.one(
        "SELECT w.*, p.name AS patient_name, p.mrn, p.gender, p.birth_date, "
        "       p.age_years, p.phone, d.name AS doctor_name, e.encounter_no "
        "FROM wellness_record w JOIN patient p ON p.id = w.patient_id "
        "LEFT JOIN practitioner d ON d.id = w.practitioner_id "
        "LEFT JOIN encounter e ON e.id = w.encounter_id WHERE w.id = ?",
        (record_id,))


def records(status: str = "") -> list:
    """Wellness records with their observation counts, newest first."""
    sql = ("SELECT w.*, p.name AS patient_name, p.mrn, d.name AS doctor_name, "
           "       (SELECT COUNT(*) FROM observation o "
           "        WHERE o.wellness_record_id = w.id) AS entries "
           "FROM wellness_record w JOIN patient p ON p.id = w.patient_id "
           "LEFT JOIN practitioner d ON d.id = w.practitioner_id")
    params: list[Any] = []
    if status:
        sql += " WHERE w.status = ?"
        params.append(status)
    return db.query(sql + " ORDER BY w.id DESC", params)


def finalise(record_id: int) -> None:
    """Sign a record so it can be exported. Refused when it is empty."""
    if not observations(record_id):
        raise ValueError("there is nothing in this record to sign")
    db.update("wellness_record", record_id,
              {"status": "final", "updated_at": db.now_iso()})


def reopen(record_id: int) -> None:
    """Put a hand-entered record back into draft.

    Refused for generated records: nothing on screen can edit them, so a draft
    would be a dead end — it can never be signed again and drops out of the
    export picker. Use :func:`regenerate_for_session` instead.
    """
    row = db.one("SELECT * FROM wellness_record WHERE id = ?", (record_id,))
    if row is None:
        raise ValueError("unknown wellness record")
    if row["dialysis_session_id"]:
        raise ValueError(
            "this record was generated from a dialysis run — regenerate it "
            "rather than reopening it")
    db.update("wellness_record", record_id,
              {"status": "draft", "updated_at": db.now_iso()})


def regenerate_for_session(session_id: int) -> int | None:
    """Throw the generated record away and rebuild it from the run as it stands."""
    existing = record_for_session(session_id)
    with db.transaction():
        if existing is not None:
            db.execute("DELETE FROM observation WHERE wellness_record_id = ?",
                       (existing["id"],))
            db.execute("DELETE FROM wellness_record WHERE id = ?", (existing["id"],))
        return create_from_dialysis_session(session_id)


# ===========================================================================
# observations
# ===========================================================================
def observations(record_id: int, category: str = "") -> list:
    """The observations of one record, optionally one section only."""
    sql = "SELECT * FROM observation WHERE wellness_record_id = ?"
    params: list[Any] = [record_id]
    if category:
        sql += " AND category = ?"
        params.append(category)
    return db.query(sql + " ORDER BY category, sort_order, id", params)


def grouped(record_id: int) -> dict[str, list]:
    """A record's observations bucketed by section."""
    out: dict[str, list] = {key: [] for key in SECTION_ORDER}
    for row in observations(record_id):
        out.setdefault(row["category"], []).append(row)
    return out


def _interpret(value: float | None, low: float | None,
               high: float | None) -> str | None:
    from .services import interpret
    return interpret(value, low, high)


def save_section(record_id: int, category: str, values: dict[str, str],
                 coded: dict[str, str] | None = None) -> int:
    """Replace one section of a record.

    `values` maps a concept code to a typed figure or free text; `coded` maps a
    concept code to the code of a picked answer. Which one applies depends on the
    `value[x]` types the section's profile permits.
    """
    entry = SECTIONS.get(category)
    if entry is None:
        raise ValueError(f"unknown wellness section {category!r}")
    _title, kind, _profile, value_kind = entry
    if value_kind == "generated":
        # Otherwise a hand-made POST to this section would silently delete the
        # whole generated parameter set.
        raise ValueError(f"the {_title} section is populated automatically "
                         "and cannot be edited by hand")
    row = db.one("SELECT * FROM wellness_record WHERE id = ?", (record_id,))
    if row is None:
        raise ValueError("unknown wellness record")
    if row["dialysis_session_id"]:
        raise ValueError("this record was generated from a dialysis run and "
                         "cannot be edited by hand")
    if row["status"] == "final":
        raise ValueError("this record is signed; reopen it before editing")

    db.execute("DELETE FROM observation WHERE wellness_record_id = ? AND category = ?",
               (record_id, category))
    coded = coded or {}
    stamp = db.to_instant(row["recorded_on"]) or db.now_iso()
    written = 0
    for index, master in enumerate(db.terms(kind)):
        code = master["code"]
        raw = (values.get(code) or "").strip()
        answer_code = (coded.get(code) or "").strip()
        if not raw and not answer_code:
            continue

        payload: dict[str, Any] = {
            "wellness_record_id": record_id,
            "patient_id": row["patient_id"],
            "encounter_id": row["encounter_id"],
            "category": category,
            "status": "final",
            "effective_ts": stamp,
            "sort_order": index,
            "note": f"Wellness record {row['record_no']}",
        }
        # The ValueSets are LOINC apart from the SNOMED lifestyle topics.
        if master["system"] == SNOMED:
            payload["snomed_code"] = code
            payload["snomed_display"] = master["display"]
        else:
            payload["loinc_code"] = code
            payload["loinc_display"] = master["display"]

        if answer_code and value_kind in ("coded-only", "quantity-or-coded"):
            answer = _answer(master, answer_code)
            if answer is None:
                continue
            payload.update({"value_system": answer["system"],
                            "value_code": answer["code"],
                            "value_display": answer["display"]})
        elif value_kind == "coded-only":
            continue          # Lifestyle takes nothing but a coded answer
        else:
            try:
                number = float(raw)
            except ValueError:
                payload["value_string"] = raw
            else:
                payload.update({
                    "value_quantity": number, "value_unit": master["unit"],
                    "ref_low": master["ref_low"], "ref_high": master["ref_high"],
                    "interpretation": _interpret(number, master["ref_low"],
                                                 master["ref_high"]),
                })
        db.insert("observation", payload)
        written += 1

    db.update("wellness_record", record_id, {"updated_at": db.now_iso()})
    return written


def answer_options(master) -> list:
    """The permitted coded answers for a lifestyle / assessment concept."""
    kind = master["extra"] if master["extra"] else None
    return db.terms(kind) if kind else []


def _answer(master, code: str):
    for option in answer_options(master):
        if option["code"] == code:
            return option
    # General Assessment concepts have no answer list of their own; accept any
    # SNOMED finding the caller passes through.
    return None


def summary(record_id: int) -> dict[str, int]:
    """Observation count per section."""
    return {key: len(rows) for key, rows in grouped(record_id).items()}


# ===========================================================================
# generated from a dialysis session
# ===========================================================================
# Dialysis parameters have no LOINC or SNOMED concept the author could verify,
# and the NRCES Observation profile closes `code.coding` to exactly those two
# systems — so a local coding would be a profile violation. They are therefore
# carried as text-only CodeableConcepts, which is legal and honest.
#   attribute, label, unit
DIALYSIS_PARAMETERS = [
    ("duration_minutes", "Dialysis session duration", "min"),
    ("blood_flow_rate", "Blood flow rate (Qb)", "mL/min"),
    ("dialysate_flow_rate", "Dialysate flow rate (Qd)", "mL/min"),
    ("dialysate_temp", "Dialysate temperature", "Cel"),
    ("conductivity", "Dialysate conductivity", "mS/cm"),
    ("dialysate_na", "Dialysate sodium", "mmol/L"),
    ("dialysate_k", "Dialysate potassium", "mmol/L"),
    ("dialysate_ca", "Dialysate calcium", "mmol/L"),
    ("dialysate_bicarb", "Dialysate bicarbonate", "mmol/L"),
    ("heparin_bolus_units", "Heparin bolus dose", "[iU]"),
    ("heparin_hourly_units", "Heparin maintenance dose", "[iU]/h"),
    ("dry_weight_kg", "Dry weight", "kg"),
    ("pre_weight_kg", "Pre-dialysis weight", "kg"),
    ("uf_goal_ml", "Ultrafiltration goal", "mL"),
    ("uf_achieved_ml", "Ultrafiltration achieved", "mL"),
    ("ktv", "Dialysis adequacy Kt/V", "1"),
    ("urr_pct", "Urea reduction ratio", "%"),
    ("dialyser_reuse", "Dialyser reuse count", "1"),
]

# Free-text attributes recorded as valueString rather than a quantity.
DIALYSIS_TEXT_PARAMETERS = [
    ("modality_display", "Dialysis modality"),
    ("access_display", "Vascular access used"),
    ("dialyser", "Dialyser"),
    ("anticoagulant_display", "Anticoagulant"),
    ("machine_code", "Dialysis machine"),
    ("complication_display", "Intra-dialytic complication"),
]


def record_for_session(session_id: int):
    """The wellness record generated from a dialysis run, if any."""
    return db.one("SELECT * FROM wellness_record WHERE dialysis_session_id = ?",
                  (session_id,))


def create_from_dialysis_session(session_id: int) -> int | None:
    """One wellness record per completed run, carrying the whole parameter set.

    Post-dialysis vitals go to Vital Signs, the post weight to Body Measurement,
    and every dialysis parameter to Other Observations — the only section of the
    artifact whose target profile will accept a concept with no bound code.
    Returns the record id, or None when there is nothing to record.
    """
    from . import dialysis

    existing = record_for_session(session_id)
    if existing is not None:
        return existing["id"]

    run = dialysis.session(session_id)
    if run is None or run["status"] != "completed":
        return None

    # One transaction: a failure part-way through must leave nothing behind,
    # because a truncated record would be indistinguishable from a real one and
    # the duplicate guard above would then protect it forever.
    with db.transaction():
        return _write_session_record(run, session_id)


def _write_session_record(run, session_id: int) -> int | None:
    """Body of :func:`create_from_dialysis_session`, run inside one transaction."""
    from . import dialysis

    record_id = db.insert("wellness_record", {
        "record_no": db.next_number("wellness", "WEL-"),
        "patient_id": run["patient_id"],
        "encounter_id": run["encounter_id"],
        "practitioner_id": run["practitioner_id"],
        "dialysis_session_id": session_id,
        "status": "final",
        "recorded_on": (run["ended_at"] or db.now_iso())[:10],
        "source": f'Dialysis session {run["session_no"]}',
        "note": f'Generated from {run["modality_display"]} session '
                f'{run["session_no"]} of course {run["course_no"]}.',
        "created_at": db.now_iso(), "updated_at": db.now_iso(),
    })
    stamp = db.to_instant(run["ended_at"]) or db.now_iso()
    order = 0

    def write(category: str, payload: dict[str, Any]) -> None:
        nonlocal order
        db.insert("observation", {
            "wellness_record_id": record_id,
            "patient_id": run["patient_id"],
            "encounter_id": run["encounter_id"],
            "category": category, "status": "final",
            "effective_ts": stamp, "sort_order": order,
            "note": f'Dialysis session {run["session_no"]}',
            **payload,
        })
        order += 1

    # --- Vital Signs: the post-dialysis flowsheet, remapped to bound codes
    post = dialysis.phase_vitals(session_id).get("post", {})
    for source_code, (code, display, unit) in DIALYSIS_VITAL_MAP.items():
        obs = post.get(source_code)
        if obs is None or obs["value_quantity"] is None:
            continue
        write("vital-signs", {
            "loinc_code": code, "loinc_display": display,
            "value_quantity": obs["value_quantity"], "value_unit": unit,
            "ref_low": obs["ref_low"], "ref_high": obs["ref_high"],
            "interpretation": obs["interpretation"],
        })
    systolic, diastolic = post.get("8480-6"), post.get("8462-4")
    if (systolic is not None and diastolic is not None
            and systolic["value_quantity"] is not None
            and diastolic["value_quantity"] is not None):
        write("vital-signs", {
            "loinc_code": "85354-9",
            "loinc_display": "Blood pressure panel with all children optional",
            "value_string": f'{systolic["value_quantity"]:g}/'
                            f'{diastolic["value_quantity"]:g} mm[Hg]',
        })

    # --- Body Measurement: the post-dialysis weight, and BMI when height is known
    if run["post_weight_kg"]:
        write("body-measurement", {
            "loinc_code": "29463-7", "loinc_display": "Body weight",
            "value_quantity": run["post_weight_kg"], "value_unit": "kg",
        })
        height = db.scalar(
            "SELECT value_quantity FROM observation WHERE patient_id = ? "
            "AND loinc_code = '8302-2' AND value_quantity IS NOT NULL "
            "ORDER BY effective_ts DESC LIMIT 1", (run["patient_id"],))
        if height:
            metres = height / 100.0
            bmi = round(run["post_weight_kg"] / (metres * metres), 1)
            write("body-measurement", {
                "loinc_code": "39156-5",
                "loinc_display": "Body mass index (BMI) [Ratio]",
                "value_quantity": bmi, "value_unit": "kg/m2",
                "ref_low": 18.5, "ref_high": 24.9,
                "interpretation": _interpret(bmi, 18.5, 24.9),
            })

    # --- Other Observations: the dialysis parameter set
    for attribute, label, unit in DIALYSIS_PARAMETERS:
        value = run[attribute] if attribute in run.keys() else None
        if value in (None, ""):
            continue
        write("other", {"code_text": label, "value_quantity": float(value),
                        "value_unit": unit})
    for attribute, label in DIALYSIS_TEXT_PARAMETERS:
        value = run[attribute] if attribute in run.keys() else None
        if not value:
            continue
        write("other", {"code_text": label, "value_string": str(value)})
    if run["complication_note"]:
        write("other", {"code_text": "Intra-dialytic complication detail",
                        "value_string": run["complication_note"]})

    chart = dialysis.readings(session_id)
    if chart:
        nadir = [r["bp_systolic"] for r in chart if r["bp_systolic"] is not None]
        if nadir:
            write("other", {"code_text": "Lowest intra-dialytic systolic pressure",
                            "value_quantity": min(nadir), "value_unit": "mm[Hg]"})
        write("other", {"code_text": "Intra-dialytic monitoring entries",
                        "value_quantity": len(chart), "value_unit": "1"})

    if not observations(record_id):
        db.execute("DELETE FROM wellness_record WHERE id = ?", (record_id,))
        return None
    return record_id
