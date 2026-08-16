"""Assembly of NRCES clinical artifacts into FHIR R4 document bundles.

Each artifact is a `Composition` conforming to one of the NRCES record profiles,
wrapped in a `DocumentBundle` (Bundle.type = document) whose first entry is that
Composition. Section codes and the Composition.type coding are fixed by the
profiles and are read from :mod:`emr.terminology`.
"""

from __future__ import annotations

import html
import json
import uuid
from typing import Any, Callable

from .. import db
from ..terminology import (
    COMPOSITION_TITLE,
    COMPOSITION_TYPE,
    DIAGNOSTIC_SECTION,
    DS_SECTIONS,
    INVOICE_SECTION,
    OP_SECTIONS,
    PROFILE,
    SNOMED,
)
from . import resources as R
from .validator import validate_bundle

ARTIFACTS = {
    "OPConsultRecord": "OP Consultation Record",
    "DischargeSummaryRecord": "Discharge Summary",
    "DiagnosticReportRecord": "Diagnostic Report",
    "InvoiceRecord": "Invoice Record",
    "WellnessRecord": "Wellness Record",
}


# ---------------------------------------------------------------------------
# narrative helpers
# ---------------------------------------------------------------------------
def _div(inner: str) -> dict:
    return {"status": "generated",
            "div": f'<div xmlns="http://www.w3.org/1999/xhtml">{inner}</div>'}


def _para(*parts: str | None) -> str:
    return "".join(f"<p>{html.escape(p)}</p>" for p in parts if p)


def _section(key: str, table: dict, entries: list[str],
             narrative: str | None = None) -> dict | None:
    """Build one Composition.section from the profile's fixed section table."""
    if not entries and not narrative:
        return None
    title, code, display = table[key]
    section: dict[str, Any] = {
        "title": title,
        "code": {"coding": [{"system": SNOMED, "code": code, "display": display}],
                 "text": display},
    }
    if narrative:
        section["text"] = _div(narrative)
    if entries:
        section["entry"] = [R.ref(u) for u in entries]
    return section


# ---------------------------------------------------------------------------
# shared context
# ---------------------------------------------------------------------------
class Ctx:
    """Loads the actors every artifact needs and parks them in the bag."""

    def __init__(self, patient_id: int, practitioner_id: int | None):
        self.bag = R.Bag()
        self.org_row = db.default_org()
        self.org = None
        if self.org_row:
            self.org = self.bag.add(*R.organization(self.org_row))

        self.patient_row = db.one("SELECT * FROM patient WHERE id = ?", (patient_id,))
        if self.patient_row is None:
            raise ValueError(f"patient {patient_id} not found")
        self.patient = self.bag.add(*R.patient(self.patient_row, self.org))

        self.practitioner_row = None
        self.practitioner = None
        if practitioner_id:
            self.practitioner_row = db.one(
                "SELECT * FROM practitioner WHERE id = ?", (practitioner_id,))
            if self.practitioner_row is not None:
                self.practitioner = self.bag.add(*R.practitioner(self.practitioner_row))

    def add_practitioner(self, practitioner_id: int | None) -> str | None:
        if not practitioner_id:
            return None
        row = db.one("SELECT * FROM practitioner WHERE id = ?", (practitioner_id,))
        if row is None:
            return None
        return self.bag.add(*R.practitioner(row))


def _compose(artifact: str, ctx: Ctx, *, subject: str, encounter: str | None,
             author: list[str], date: str, sections: list[dict],
             identifier_value: str, narrative: str,
             type_override: dict | None = None) -> dict:
    ctype = type_override or {
        "coding": [COMPOSITION_TYPE[artifact]],
        "text": COMPOSITION_TYPE[artifact]["display"],
    }
    comp = {
        "resourceType": "Composition",
        "id": str(uuid.uuid5(R.NS, f"Composition/{artifact}/{identifier_value}")),
        "meta": {
            "versionId": "1",
            "lastUpdated": db.now_iso(),
            "profile": [PROFILE[artifact]],
        },
        "language": "en-IN",
        "text": _div(narrative),
        "identifier": {"system": "https://nanoemr.local/composition",
                       "value": identifier_value},
        "status": "final",
        "type": ctype,
        "subject": R.ref(subject),
        "encounter": R.ref(encounter) if encounter else None,
        "date": date,
        "author": [R.ref(a) for a in author],
        "title": COMPOSITION_TITLE[artifact],
        "custodian": R.ref(ctx.org) if ctx.org else None,
        "section": sections,
    }
    return R.prune(comp)


def _bundle(artifact: str, composition: dict, ctx: Ctx,
            identifier_value: str) -> dict:
    comp_full = R.urn("Composition", f"{artifact}/{identifier_value}")
    entries = [{"fullUrl": comp_full, "resource": composition}] + ctx.bag.entries()
    return R.prune({
        "resourceType": "Bundle",
        "id": str(uuid.uuid5(R.NS, f"Bundle/{artifact}/{identifier_value}")),
        "meta": {
            "versionId": "1",
            "lastUpdated": db.now_iso(),
            "profile": [PROFILE["DocumentBundle"]],
        },
        "identifier": {"system": "https://nanoemr.local/bundle",
                       "value": identifier_value},
        "type": "document",
        "timestamp": db.now_iso(),
        "entry": entries,
    })


# ---------------------------------------------------------------------------
# OP Consult Record
# ---------------------------------------------------------------------------
def build_opconsult(encounter_id: int) -> dict:
    """Build an OPConsultRecord bundle from an OPD encounter."""
    enc = db.one("SELECT * FROM encounter WHERE id = ?", (encounter_id,))
    if enc is None:
        raise ValueError("encounter not found")
    ctx = Ctx(enc["patient_id"], enc["practitioner_id"])
    note = db.one(
        "SELECT * FROM clinical_note WHERE encounter_id = ? AND kind = 'OPD_NOTE'",
        (encounter_id,))

    enc_full = ctx.bag.add(*R.encounter(enc, ctx.patient, ctx.practitioner))
    author = [ctx.practitioner] if ctx.practitioner else ([ctx.org] if ctx.org else [])

    sections: list[dict] = []

    complaints = _conditions(ctx, enc, "chief-complaint")
    sections.append(_section("ChiefComplaints", OP_SECTIONS, complaints,
                             _para(note["history_text"] if note else None)))

    exam = _observations(ctx, enc, ("vital-signs", "exam"))
    sections.append(_section("PhysicalExamination", OP_SECTIONS, exam,
                             _para(note["examination_text"] if note else None)))

    sections.append(_section("Allergies", OP_SECTIONS, _allergies(ctx, enc)))

    # The OP Consult profile has no dedicated diagnosis slice, so confirmed
    # diagnoses ride along in the medical history section next to past history.
    history = _conditions(ctx, enc, "medical-history") + _conditions(ctx, enc, "diagnosis")
    sections.append(_section("MedicalHistory", OP_SECTIONS, history))

    sections.append(_section("InvestigationAdvice", OP_SECTIONS,
                             _service_requests(ctx, enc, "investigation")))
    sections.append(_section("Medications", OP_SECTIONS, _medications(ctx, enc)))
    sections.append(_section("Procedure", OP_SECTIONS, _procedures(ctx, enc)))
    sections.append(_section("Referral", OP_SECTIONS,
                             _service_requests(ctx, enc, "referral")))

    follow_up: list[str] = []
    if note and note["follow_up_date"]:
        follow_up.append(ctx.bag.add(*R.appointment(note, ctx.patient, ctx.practitioner)))
    sections.append(_section("FollowUp", OP_SECTIONS, follow_up,
                             _para(note["follow_up_note"] if note else None)))

    if note and note["advice_text"]:
        sections.append(_section("OtherObservations", OP_SECTIONS, [],
                                 _para("Advice: " + note["advice_text"])))

    sections = [s for s in sections if s]
    narrative = _para(
        f"OP consultation for {ctx.patient_row['name']} "
        f"({ctx.patient_row['mrn']}) on {enc['period_start'][:10]}.",
        f"Attending: {ctx.practitioner_row['name']}" if ctx.practitioner_row else None,
    )
    ident = enc["encounter_no"]
    comp = _compose("OPConsultRecord", ctx, subject=ctx.patient, encounter=enc_full,
                    author=author, date=db.to_instant(
                        note["updated_at"] if note else enc["period_start"]),
                    sections=sections, identifier_value=ident, narrative=narrative)
    return _bundle("OPConsultRecord", comp, ctx, ident)


# ---------------------------------------------------------------------------
# Discharge Summary Record
# ---------------------------------------------------------------------------
def build_discharge_summary(encounter_id: int) -> dict:
    """Build a DischargeSummaryRecord bundle from a discharged admission."""
    enc = db.one("SELECT * FROM encounter WHERE id = ?", (encounter_id,))
    if enc is None:
        raise ValueError("encounter not found")
    ctx = Ctx(enc["patient_id"], enc["practitioner_id"])
    note = db.one(
        "SELECT * FROM clinical_note WHERE encounter_id = ? AND kind = 'DISCHARGE_SUMMARY'",
        (encounter_id,))

    enc_full = ctx.bag.add(*R.encounter(enc, ctx.patient, ctx.practitioner))
    author = [ctx.practitioner] if ctx.practitioner else ([ctx.org] if ctx.org else [])

    sections: list[dict] = []
    sections.append(_section("ChiefComplaints", DS_SECTIONS,
                             _conditions(ctx, enc, "chief-complaint"),
                             _para(note["admission_reason"] if note else None)))
    sections.append(_section("PhysicalExamination", DS_SECTIONS,
                             _observations(ctx, enc, ("vital-signs", "exam")),
                             _para(note["examination_text"] if note else None)))
    sections.append(_section("Allergies", DS_SECTIONS, _allergies(ctx, enc)))

    history = _conditions(ctx, enc, "medical-history") + _conditions(ctx, enc, "diagnosis")
    sections.append(_section("MedicalHistory", DS_SECTIONS, history,
                             _para(note["history_text"] if note else None)))

    sections.append(_section("Investigations", DS_SECTIONS, _lab_reports(ctx, enc)))
    sections.append(_section("Medications", DS_SECTIONS, _medications(ctx, enc)))
    sections.append(_section("Procedures", DS_SECTIONS, _procedures(ctx, enc)))

    care_plan_refs: list[str] = []
    if note:
        care_plan_refs.append(ctx.bag.add(*R.care_plan(
            note, ctx.patient, enc_full, ctx.practitioner)))
    sections.append(_section("CarePlan", DS_SECTIONS, care_plan_refs, _para(
        ("Course in hospital: " + note["course_in_hospital"])
        if note and note["course_in_hospital"] else None,
        ("Condition at discharge: " + note["condition_at_discharge"])
        if note and note["condition_at_discharge"] else None,
        ("Discharge instructions: " + note["discharge_instructions"])
        if note and note["discharge_instructions"] else None,
    )))

    sections = [s for s in sections if s]
    narrative = _para(
        f"Discharge summary for {ctx.patient_row['name']} ({ctx.patient_row['mrn']}).",
        f"Admitted {enc['period_start'][:16].replace('T', ' ')}"
        + (f", discharged {enc['discharge_ts'][:16].replace('T', ' ')}"
           if enc["discharge_ts"] else ""),
        f"Ward: {enc['ward']} / Bed {enc['bed']}" if enc["ward"] else None,
    )
    ident = enc["encounter_no"]
    comp = _compose("DischargeSummaryRecord", ctx, subject=ctx.patient,
                    encounter=enc_full, author=author,
                    date=db.to_instant(enc["discharge_ts"] or db.now_iso()),
                    sections=sections, identifier_value=ident, narrative=narrative)
    return _bundle("DischargeSummaryRecord", comp, ctx, ident)


# ---------------------------------------------------------------------------
# Diagnostic Report Record
# ---------------------------------------------------------------------------
def build_diagnostic_report(lab_order_id: int) -> dict:
    """Build a DiagnosticReportRecord bundle from a finalised lab order."""
    order = db.one("SELECT * FROM lab_order WHERE id = ?", (lab_order_id,))
    if order is None:
        raise ValueError("lab order not found")
    ctx = Ctx(order["patient_id"], order["interpreter_id"])
    enc_full = None
    if order["encounter_id"]:
        enc = db.one("SELECT * FROM encounter WHERE id = ?", (order["encounter_id"],))
        if enc is not None:
            enc_practitioner = ctx.add_practitioner(enc["practitioner_id"])
            enc_full = ctx.bag.add(*R.encounter(enc, ctx.patient, enc_practitioner))

    report_full = _lab_report_resource(ctx, order, enc_full)
    title, code, display = DIAGNOSTIC_SECTION
    section = {
        "title": order["panel_display"],
        "code": {"coding": [{"system": SNOMED, "code": code, "display": display}],
                 "text": display},
        "entry": [{"reference": report_full, "type": "DiagnosticReport"}],
    }
    author = [ctx.practitioner] if ctx.practitioner else ([ctx.org] if ctx.org else [])
    narrative = _para(
        f"{order['panel_display']} for {ctx.patient_row['name']} "
        f"({ctx.patient_row['mrn']}), order {order['order_no']}.",
        order["conclusion"],
    )
    ident = order["order_no"]
    comp = _compose("DiagnosticReportRecord", ctx, subject=ctx.patient,
                    encounter=enc_full, author=author,
                    date=db.to_instant(order["issued_ts"] or order["ordered_at"]),
                    sections=[section], identifier_value=ident, narrative=narrative)
    return _bundle("DiagnosticReportRecord", comp, ctx, ident)


# ---------------------------------------------------------------------------
# Invoice Record
# ---------------------------------------------------------------------------
def build_invoice_record(invoice_id: int) -> dict:
    """Build an InvoiceRecord bundle from an invoice."""
    inv = db.one("SELECT * FROM invoice WHERE id = ?", (invoice_id,))
    if inv is None:
        raise ValueError("invoice not found")
    lines = db.query(
        "SELECT * FROM invoice_line WHERE invoice_id = ? ORDER BY seq", (invoice_id,))
    ctx = Ctx(inv["patient_id"], inv["participant_id"])

    enc_full = None
    if inv["encounter_id"]:
        enc = db.one("SELECT * FROM encounter WHERE id = ?", (inv["encounter_id"],))
        if enc is not None:
            enc_practitioner = ctx.add_practitioner(enc["practitioner_id"])
            enc_full = ctx.bag.add(*R.encounter(enc, ctx.patient, enc_practitioner))

    invoice_full = ctx.bag.add(*R.invoice(
        inv, lines, ctx.patient, enc_full, ctx.org, ctx.practitioner))

    _, code, display = INVOICE_SECTION
    section = {
        "title": "Invoice",
        "code": {"coding": [{"system": SNOMED, "code": code, "display": display}],
                 "text": display},
        "entry": [{"reference": invoice_full, "type": "Invoice"}],
    }
    author = [ctx.org] if ctx.org else ([ctx.practitioner] if ctx.practitioner else [])
    narrative = _para(
        f"Invoice {inv['invoice_no']} ({inv['type_display']}) for "
        f"{ctx.patient_row['name']} ({ctx.patient_row['mrn']}).",
        f"Total payable INR {inv['total_gross']:.2f}.",
    )
    ident = inv["invoice_no"]
    # InvoiceRecord fixes Composition.type.text to the literal "Invoice Record".
    comp = _compose("InvoiceRecord", ctx, subject=ctx.patient, encounter=enc_full,
                    author=author, date=db.to_instant(inv["date"]),
                    sections=[section], identifier_value=ident, narrative=narrative,
                    type_override={"text": "Invoice Record"})
    return _bundle("InvoiceRecord", comp, ctx, ident)


# ---------------------------------------------------------------------------
# Wellness Record
# ---------------------------------------------------------------------------
def build_wellness_record(record_id: int) -> dict:
    """WellnessRecord slices its sections by a fixed `title` and puts no `code`
    on them at all — unlike every other record profile in the guide."""
    from .. import wellness

    row = db.one("SELECT * FROM wellness_record WHERE id = ?", (record_id,))
    if row is None:
        raise ValueError("wellness record not found")
    ctx = Ctx(row["patient_id"], row["practitioner_id"])

    enc_full = None
    if row["encounter_id"]:
        enc = db.one("SELECT * FROM encounter WHERE id = ?", (row["encounter_id"],))
        if enc is not None:
            enc_practitioner = ctx.add_practitioner(enc["practitioner_id"])
            enc_full = ctx.bag.add(*R.encounter(enc, ctx.patient, enc_practitioner))

    sections: list[dict] = []
    grouped = wellness.grouped(record_id)
    for key in wellness.SECTION_ORDER:
        entries = grouped.get(key) or []
        if not entries:
            continue
        profile = wellness.section_profile(key)
        refs = [ctx.bag.add(*R.observation(obs, ctx.patient, enc_full,
                                           ctx.practitioner, profile=profile))
                for obs in entries]
        sections.append({
            "title": wellness.SECTIONS[key][0],
            "entry": [R.ref(u) for u in refs],
        })

    if not sections:
        raise ValueError("this wellness record has nothing in it yet")

    author = [ctx.practitioner] if ctx.practitioner else ([ctx.org] if ctx.org else [])
    narrative = _para(
        f"Wellness record for {ctx.patient_row['name']} "
        f"({ctx.patient_row['mrn']}) recorded on {row['recorded_on']}.",
        f"Source: {row['source']}" if row["source"] else None,
        row["note"])
    ident = row["record_no"]
    comp = _compose("WellnessRecord", ctx, subject=ctx.patient, encounter=enc_full,
                    author=author, date=db.to_instant(row["recorded_on"]),
                    sections=sections, identifier_value=ident, narrative=narrative,
                    type_override={"text": "Wellness Record"})
    return _bundle("WellnessRecord", comp, ctx, ident)


# ---------------------------------------------------------------------------
# section collectors
# ---------------------------------------------------------------------------
def _conditions(ctx: Ctx, enc, category: str) -> list[str]:
    rows = db.query(
        "SELECT * FROM condition WHERE encounter_id = ? AND category = ? ORDER BY id",
        (enc["id"], category))
    if category == "medical-history":
        # Chart-level history is not tied to a visit but belongs in the medical
        # history section of every document generated for the patient.
        rows = db.query(
            "SELECT * FROM condition WHERE patient_id = ? AND category = ? "
            "AND encounter_id IS NULL ORDER BY id", (enc["patient_id"], category)
        ) + list(rows)
    return [ctx.bag.add(*R.condition(r, ctx.patient, R.urn("Encounter", enc["id"]),
                                     ctx.practitioner)) for r in rows]


def _observations(ctx: Ctx, enc, categories: tuple[str, ...]) -> list[str]:
    placeholders = ",".join("?" for _ in categories)
    rows = db.query(
        f"SELECT * FROM observation WHERE encounter_id = ? AND lab_order_id IS NULL "
        f"AND wellness_record_id IS NULL "
        f"AND category IN ({placeholders}) ORDER BY category, sort_order, id",
        (enc["id"], *categories))
    return [ctx.bag.add(*R.observation(r, ctx.patient, R.urn("Encounter", enc["id"]),
                                       ctx.practitioner)) for r in rows]


def _allergies(ctx: Ctx, enc) -> list[str]:
    rows = db.query(
        "SELECT * FROM allergy WHERE patient_id = ? ORDER BY id", (enc["patient_id"],))
    return [ctx.bag.add(*R.allergy(r, ctx.patient, R.urn("Encounter", enc["id"]),
                                   ctx.practitioner)) for r in rows]


def _medications(ctx: Ctx, enc) -> list[str]:
    rows = db.query(
        "SELECT * FROM medication_request WHERE encounter_id = ? ORDER BY sort_order, id",
        (enc["id"],))
    out = []
    for r in rows:
        requester = ctx.add_practitioner(r["requester_id"]) or ctx.practitioner
        out.append(ctx.bag.add(*R.medication_request(
            r, ctx.patient, R.urn("Encounter", enc["id"]), requester)))
    return out


def _procedures(ctx: Ctx, enc) -> list[str]:
    rows = db.query("SELECT * FROM procedure WHERE encounter_id = ? ORDER BY id",
                    (enc["id"],))
    out = []
    for r in rows:
        performer = ctx.add_practitioner(r["performer_id"]) or ctx.practitioner
        out.append(ctx.bag.add(*R.procedure(
            r, ctx.patient, R.urn("Encounter", enc["id"]), performer)))
    return out


def _service_requests(ctx: Ctx, enc, purpose: str) -> list[str]:
    rows = db.query(
        "SELECT * FROM service_request WHERE encounter_id = ? AND purpose = ? ORDER BY id",
        (enc["id"], purpose))
    out = []
    for r in rows:
        requester = ctx.add_practitioner(r["requester_id"]) or ctx.practitioner
        out.append(ctx.bag.add(*R.service_request(
            r, ctx.patient, R.urn("Encounter", enc["id"]), requester)))
    return out


def _lab_reports(ctx: Ctx, enc) -> list[str]:
    orders = db.query(
        "SELECT * FROM lab_order WHERE encounter_id = ? AND status = 'final' ORDER BY id",
        (enc["id"],))
    return [_lab_report_resource(ctx, o, R.urn("Encounter", enc["id"])) for o in orders]


def _lab_report_resource(ctx: Ctx, order, enc_full: str | None) -> str:
    specimen_full = None
    if order["specimen_code"]:
        specimen_full = ctx.bag.add(*R.specimen(order, ctx.patient))
    interpreter = ctx.add_practitioner(order["interpreter_id"])
    results = db.query(
        "SELECT * FROM observation WHERE lab_order_id = ? ORDER BY sort_order, id",
        (order["id"],))
    result_fulls = [
        ctx.bag.add(*R.observation(r, ctx.patient, enc_full, interpreter, specimen_full))
        for r in results
    ]
    return ctx.bag.add(*R.diagnostic_report_lab(
        order, result_fulls, ctx.patient, enc_full, ctx.org, interpreter, specimen_full))


# ---------------------------------------------------------------------------
# public API
# ---------------------------------------------------------------------------
BUILDERS: dict[str, Callable[[int], dict]] = {
    "OPConsultRecord": build_opconsult,
    "DischargeSummaryRecord": build_discharge_summary,
    "DiagnosticReportRecord": build_diagnostic_report,
    "InvoiceRecord": build_invoice_record,
    "WellnessRecord": build_wellness_record,
}

SOURCE_KIND = {
    "OPConsultRecord": "encounter",
    "DischargeSummaryRecord": "encounter",
    "DiagnosticReportRecord": "lab_order",
    "InvoiceRecord": "invoice",
    "WellnessRecord": "wellness",
}


def build_bundle(artifact: str, source_id: int) -> dict:
    """Build any artifact by name from its source record id."""
    if artifact not in BUILDERS:
        raise ValueError(f"unknown artifact {artifact!r}")
    return BUILDERS[artifact](source_id)


def persist_bundle(artifact: str, source_id: int) -> dict:
    """Build, validate and store a bundle; returns the fhir_export row as a dict."""
    bundle = build_bundle(artifact, source_id)
    issues = validate_bundle(artifact, bundle)
    errors = [i for i in issues if i["severity"] == "error"]

    patient_id, encounter_id = _source_context(artifact, source_id)
    version = db.scalar(
        "SELECT COALESCE(MAX(bundle_version), 0) + 1 FROM fhir_export "
        "WHERE source_kind = ? AND source_id = ? AND artifact = ?",
        (SOURCE_KIND[artifact], source_id, artifact), default=1)

    export_id = db.insert("fhir_export", {
        "artifact": artifact,
        "patient_id": patient_id,
        "encounter_id": encounter_id,
        "source_kind": SOURCE_KIND[artifact],
        "source_id": source_id,
        "bundle_identifier": bundle.get("identifier", {}).get("value", ""),
        "bundle_version": version,
        "resource_count": len(bundle.get("entry", [])),
        "valid": 0 if errors else 1,
        "issues_json": json.dumps(issues),
        "bundle_json": json.dumps(bundle, indent=2, ensure_ascii=False),
        "created_at": db.now_iso(),
    })
    return dict(db.one("SELECT * FROM fhir_export WHERE id = ?", (export_id,)))


def _source_context(artifact: str, source_id: int) -> tuple[int, int | None]:
    kind = SOURCE_KIND[artifact]
    if kind == "encounter":
        row = db.one("SELECT patient_id, id FROM encounter WHERE id = ?", (source_id,))
        return row["patient_id"], row["id"]
    if kind == "lab_order":
        row = db.one("SELECT patient_id, encounter_id FROM lab_order WHERE id = ?",
                     (source_id,))
        return row["patient_id"], row["encounter_id"]
    if kind == "wellness":
        row = db.one("SELECT patient_id, encounter_id FROM wellness_record "
                     "WHERE id = ?", (source_id,))
        return row["patient_id"], row["encounter_id"]
    row = db.one("SELECT patient_id, encounter_id FROM invoice WHERE id = ?", (source_id,))
    return row["patient_id"], row["encounter_id"]
