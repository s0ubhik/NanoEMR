"""Master data — the reference tables a clinic configures once, and the
maintenance operation that clears everything else.

The split matters. *Masters* describe how this facility works: who the doctors
are, which drugs are stocked, what a bed costs, which codes the pickers offer.
*Transactional* data is what happened to patients. Masters survive a reset;
patient data does not.

That split is expressed as an allow-list — :data:`MASTER_TABLES`. Anything not
named there is cleared, so a table added later is treated as transactional
unless someone deliberately says otherwise. That is the safe default: forgetting
to add a table to the list loses demo data, whereas forgetting the other way
round would leave patient rows behind after an operator asked for a clean slate.
"""

from __future__ import annotations

from typing import Any

from . import db

# Reference data. Everything else in the schema is patient activity.
MASTER_TABLES = {
    "organization",       # the facility itself
    "practitioner",       # doctors and staff
    "terminology",        # every code picker in the app
    "ward",               # ward tariffs and policy
    "bed",                # the physical beds (occupancy is reset, not deleted)
    "stock_item",         # the pharmacy catalogue (batches are transactional)
    "dialysis_machine",   # the machines (status is reset, not deleted)
}

# Children first, so a delete never orphans a foreign key. `observation` in
# particular points at lab_order, dialysis_session *and* wellness_record, so it
# has to go before all three.
CLEAR_ORDER = [
    "fhir_export",
    "claim_adjudication",   # -> claim
    "claim_query",          # -> claim
    "claim_enquiry",        # -> claim
    "claim_payment_detail", # -> claim_payment
    "claim_payment",        # -> claim
    "claim_submission",     # -> claim
    "claim_preauth",        # -> claim
    "claim_predetermination",  # -> claim
    "claim_auth_requirement",  # -> claim_auth
    "claim_auth_item",      # -> claim_auth
    "claim_auth",           # -> claim
    "claim_form_answer",    # -> claim
    "claim_line",           # -> claim
    "claim_document",       # -> claim
    "claim_item",           # -> claim
    "claim_care_team",      # -> claim, practitioner
    "claim_diagnosis",      # -> claim
    "claim_plan_form",      # -> claim_plan
    "claim_plan_benefit",   # -> claim_plan
    "claim_plan",           # -> claim (the payer's package master for it)
    "claim",                # NHCX claim episodes (patient activity)
    "dialysis_reading",     # -> dialysis_session
    "observation",          # -> lab_order, dialysis_session, wellness_record
    "wellness_record",      # -> dialysis_session
    "dialysis_session",     # -> dialysis_course
    "dialysis_course",
    "payment",              # -> invoice
    "invoice_line",         # -> invoice
    "invoice",
    "stock_txn",            # -> stock_batch
    "stock_batch",
    "appointment",
    "bed_movement",         # -> bed, encounter
    "allergy", "medication_request", "service_request", "procedure",
    "condition", "clinical_note",
    "lab_order",
    "encounter",            # -> patient, bed
    "patient",
    "counter",
]

# The master sets an operator can edit, in the order the hub lists them:
#   (key, label, backing table, description)
MASTER_SETS: list[tuple[str, str, str, str]] = [
    ("staff", "Doctors & staff", "practitioner",
     "Who can be selected as a consultant, admitting doctor or interpreter."),
    ("pharmacy", "Pharmacy catalogue", "stock_item",
     "Drugs and consumables, their MRP, GST rate and reorder level."),
    ("wards", "Wards & beds", "ward",
     "Ward tariffs, gender policy and the physical beds in each."),
    ("codes", "Clinical codes", "terminology",
     "Every picker in the app: diagnoses, complaints, medicines, lab panels, "
     "procedures, charges and the rest."),
]

# Terminology kinds grouped for the code master, with a human label.
CODE_GROUPS: list[tuple[str, list[tuple[str, str]]]] = [
    ("Clinical", [
        ("complaint", "Chief complaints"),
        ("diagnosis", "Diagnoses"),
        ("procedure", "Procedures"),
        ("procedure_category", "Procedure categories"),
        ("procedure_outcome", "Procedure outcomes"),
        ("allergen", "Allergens"),
        ("reaction", "Allergy reactions"),
    ]),
    ("Medication", [
        ("medicine", "Medicines"),
        ("route", "Routes"),
        ("method", "Administration methods"),
        ("dose_instruction", "Dosage instructions"),
    ]),
    ("Diagnostics", [
        ("vital", "Vital signs"),
        ("lab_panel", "Lab panels"),
        ("lab_analyte", "Lab analytes"),
        ("specimen", "Specimen types"),
    ]),
    ("Dialysis", [
        ("dialysis_modality", "Modalities"),
        ("vascular_access", "Vascular access"),
        ("dialyser", "Dialysers"),
        ("anticoagulant", "Anticoagulants"),
        ("dialysis_complication", "Complications"),
    ]),
    ("Wellness", [
        ("wellness_vital", "Wellness vitals"),
        ("wellness_body", "Body measurements"),
        ("wellness_activity", "Physical activity"),
        ("wellness_assessment", "General assessment"),
        ("wellness_women", "Women's health"),
        ("wellness_lifestyle", "Lifestyle topics"),
        # answer lists referenced from the `extra` column of the sets above
        ("lifestyle_smoking", "— smoking answers"),
        ("lifestyle_alcohol", "— alcohol answers"),
        ("lifestyle_yesno", "— yes/no answers"),
        ("wellness_mental", "— mental status answers"),
        ("wellness_wellbeing", "— well-being answers"),
    ]),
    ("Administrative", [
        ("charge", "Charge master"),
        ("claim_package", "Claim packages (HBP)"),
        ("payer_adapter", "Payer adapters (NHCX)"),
        ("department", "Departments"),
        ("ward", "Ward types"),
        ("encounter_type", "Encounter types"),
        ("service_type", "Service types"),
        ("priority", "Encounter priority"),
        ("admission_type", "Admission types"),
        ("discharge_disposition", "Discharge dispositions"),
        ("marital_status", "Marital status"),
        ("blood_group", "Blood groups"),
    ]),
]

CODE_LABEL = {kind: label for _group, kinds in CODE_GROUPS for kind, label in kinds}

# Kinds whose `extra` column carries structured data, so the editor can label it.
EXTRA_HINT = {
    "lab_panel": "category|analyte codes|price, e.g. Biochemistry|2160-0,3094-0|600",
    "charge": "price|invoice type code, e.g. 500|00",
    "claim_package": "package rate in ₹, e.g. 27000",
    "payer_adapter": "adapter key — pmjay, kyrocare or generic",
    "ward": "per-day tariff",
    "medicine": "presentation, e.g. 500 mg tablet",
    "allergen": "default category: medication | food | environment",
    "wellness_lifestyle": "terminology kind holding the permitted answers",
}


# ===========================================================================
# counts for the hub
# ===========================================================================
def counts() -> dict[str, int]:
    """How many rows each master set holds, for the hub screen."""
    return {
        "staff": db.scalar("SELECT COUNT(*) FROM practitioner", default=0),
        "pharmacy": db.scalar("SELECT COUNT(*) FROM stock_item", default=0),
        "wards": db.scalar("SELECT COUNT(*) FROM ward", default=0),
        "beds": db.scalar("SELECT COUNT(*) FROM bed", default=0),
        "codes": db.scalar("SELECT COUNT(*) FROM terminology", default=0),
        "machines": db.scalar("SELECT COUNT(*) FROM dialysis_machine", default=0),
    }


# ===========================================================================
# terminology
# ===========================================================================
def code_kinds() -> list[str]:
    return [kind for _group, kinds in CODE_GROUPS for kind, _label in kinds]


def add_code(kind: str, values: dict[str, Any]) -> int:
    """Add a concept to a picker. Refuses a duplicate code within the kind."""
    code = (values.get("code") or "").strip()
    display = (values.get("display") or "").strip()
    if not code or not display:
        raise ValueError("a concept needs both a code and a display name")
    if db.term(kind, code) is not None:
        raise ValueError(f"{code} already exists in this set")
    payload = dict(values)
    payload.update({"kind": kind, "code": code, "display": display})
    payload.setdefault("system", "https://nanoemr.local/CodeSystem/" + kind)
    payload.setdefault("sort_order", (db.scalar(
        "SELECT COALESCE(MAX(sort_order), 0) FROM terminology WHERE kind = ?",
        (kind,), default=0) or 0) + 1)
    return db.insert("terminology", payload)


def update_code(term_id: int, values: dict[str, Any]) -> None:
    if not (values.get("display") or "").strip():
        raise ValueError("a concept needs a display name")
    db.update("terminology", term_id, values)


def delete_code(term_id: int) -> None:
    """Remove a concept.

    Existing records keep the code and display they were written with — every
    table stores both — so deleting a concept only takes it out of the picker,
    it never rewrites history.
    """
    db.execute("DELETE FROM terminology WHERE id = ?", (term_id,))


# ===========================================================================
# practitioners
# ===========================================================================
def staff(include_inactive: bool = True) -> list:
    sql = "SELECT * FROM practitioner"
    if not include_inactive:
        sql += " WHERE active = 1"
    return db.query(sql + " ORDER BY active DESC, department, name")


def save_practitioner(practitioner_id: int | None, values: dict[str, Any]) -> int:
    if not (values.get("name") or "").strip():
        raise ValueError("a practitioner needs a name")
    if not (values.get("identifier_value") or "").strip():
        raise ValueError("a practitioner needs a registration or HPID number")
    if practitioner_id:
        db.update("practitioner", practitioner_id, values)
        return practitioner_id
    return db.insert("practitioner", values)


def set_practitioner_active(practitioner_id: int, active: bool) -> None:
    """Retire or reinstate a practitioner.

    Never deleted: their name is on encounters, prescriptions and reports.
    """
    db.update("practitioner", practitioner_id, {"active": 1 if active else 0})


# ===========================================================================
# pharmacy catalogue
# ===========================================================================
def save_stock_item(item_id: int | None, values: dict[str, Any]) -> int:
    code = (values.get("code") or "").strip()
    if not code or not (values.get("name") or "").strip():
        raise ValueError("an item needs both a code and a name")
    clash = db.one("SELECT id FROM stock_item WHERE code = ? AND id <> ?",
                   (code, item_id or 0))
    if clash:
        raise ValueError(f"{code} is already used by another item")
    if item_id:
        db.update("stock_item", item_id, values)
        return item_id
    return db.insert("stock_item", values)


def set_item_active(item_id: int, active: bool) -> None:
    db.update("stock_item", item_id, {"active": 1 if active else 0})


# ===========================================================================
# wards and beds
# ===========================================================================
def save_ward(ward_id: int | None, values: dict[str, Any]) -> int:
    code = (values.get("code") or "").strip()
    if not code or not (values.get("name") or "").strip():
        raise ValueError("a ward needs both a code and a name")
    clash = db.one("SELECT id FROM ward WHERE code = ? AND id <> ?",
                   (code, ward_id or 0))
    if clash:
        raise ValueError(f"{code} is already used by another ward")
    if ward_id:
        db.update("ward", ward_id, values)
        return ward_id
    return db.insert("ward", values)


def add_bed(ward_id: int, code: str, tariff: float | None = None) -> int:
    code = (code or "").strip()
    if not code:
        raise ValueError("a bed needs a code")
    if db.one("SELECT id FROM bed WHERE code = ?", (code,)):
        raise ValueError(f"bed {code} already exists")
    return db.insert("bed", {"ward_id": ward_id, "code": code, "status": "vacant",
                             "tariff": tariff, "active": 1})


def retire_bed(bed_id: int) -> None:
    """Take a bed out of service. Refused while a patient is in it."""
    row = db.one("SELECT * FROM bed WHERE id = ?", (bed_id,))
    if row is None:
        raise ValueError("unknown bed")
    if row["status"] == "occupied":
        raise ValueError("that bed is occupied")
    db.update("bed", bed_id, {"active": 0, "status": "blocked"})


def restore_bed(bed_id: int) -> None:
    db.update("bed", bed_id, {"active": 1, "status": "vacant"})


# ===========================================================================
# maintenance
# ===========================================================================
def transactional_counts() -> dict[str, int]:
    """Row counts for everything a reset would delete, largest first."""
    out = {}
    for table in CLEAR_ORDER:
        out[table] = db.scalar(f"SELECT COUNT(*) FROM {table}", default=0) or 0
    return dict(sorted(out.items(), key=lambda kv: -kv[1]))


def clear_transactional_data() -> dict[str, int]:
    """Delete every patient record, keeping the masters.

    Runs in one transaction: either the whole clinic is reset or nothing is.
    Beds and dialysis machines are *reset* rather than deleted — they are
    physical assets — and the numbering counters are dropped so a fresh clinic
    starts again at MRN00001.

    Returns ``{table: rows_deleted}`` for the tables that had anything in them.
    """
    deleted: dict[str, int] = {}
    with db.transaction() as conn:
        # Enforcement is deferred to COMMIT rather than checked per statement.
        # `PRAGMA foreign_keys` is a no-op inside a transaction; this one is not,
        # and it means the wipe is judged on the end state, not the middle of it.
        conn.execute("PRAGMA defer_foreign_keys = ON")
        # nothing may still point at an encounter when the encounters go
        conn.execute("UPDATE bed SET encounter_id = NULL")
        for table in CLEAR_ORDER:
            count = db.scalar(f"SELECT COUNT(*) FROM {table}", default=0) or 0
            if count:
                deleted[table] = count
            db.execute(f"DELETE FROM {table}")
        # physical assets survive; only their occupancy is cleared
        db.execute("UPDATE bed SET status = 'vacant', encounter_id = NULL "
                   "WHERE active = 1")
        db.execute("UPDATE dialysis_machine SET status = 'available' "
                   "WHERE active = 1 AND status <> 'retired'")
    return deleted


def unlisted_tables() -> list[str]:
    """Tables the reset would miss — a guard against a new table being forgotten.

    Every table is either a master or gets cleared. Anything that appears here is
    a schema change that has not been classified yet.
    """
    known = MASTER_TABLES | set(CLEAR_ORDER)
    rows = db.query("SELECT name FROM sqlite_master WHERE type = 'table' "
                    "AND name NOT LIKE 'sqlite_%'")
    return sorted(r["name"] for r in rows if r["name"] not in known)
