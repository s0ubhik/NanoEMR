"""Builders that turn EMR rows into NRCES-profiled FHIR R4 resources.

Every builder stamps `meta.profile` with the NRCES canonical URL and only emits
elements the profile actually allows, because ABDM gateways reject bundles that
fail profile validation.
"""

from __future__ import annotations

import uuid
from typing import Any

from .. import db
from ..terminology import (
    ALLERGY_CLINICAL,
    DISCHARGE_DISPOSITION,
    NDHM_IDENTIFIER_TYPE,
    V2_0203,
    COND_CATEGORY,
    COND_CLINICAL,
    COND_VERIFICATION,
    ICD10,
    INTERPRETATION,
    LOINC,
    NDHM_PRICE_COMPONENTS,
    NDHM_BILLING_CODES,
    OBS_CATEGORY,
    ORG_TYPE,
    PRICE_COMPONENT,
    PROFILE,
    SNOMED,
    UCUM,
    V3_ACT_CODE,
    V3_MARITAL,
)

# Stable namespace so re-exporting the same encounter yields the same urn:uuids.
NS = uuid.UUID("6f0a4b6e-6b25-5f27-9d1c-9b3a2c7e1d40")


def urn(kind: str, local_id: Any) -> str:
    """A stable ``urn:uuid:`` for a resource, derived from its type and id."""
    return "urn:uuid:" + str(uuid.uuid5(NS, f"{kind}/{local_id}"))


def cc(system: str | None, code: str | None, display: str | None,
       text: str | None = None) -> dict | None:
    """CodeableConcept, omitting the coding when the code is missing."""
    out: dict[str, Any] = {}
    if system and code:
        coding = {"system": system, "code": code}
        if display:
            coding["display"] = display
        out["coding"] = [coding]
    if text:
        out["text"] = text
    elif display and "coding" in out:
        out["text"] = display
    return out or None


def prune(obj: Any) -> Any:
    """Drop None / empty members — FHIR forbids empty elements."""
    if isinstance(obj, dict):
        cleaned = {k: prune(v) for k, v in obj.items()}
        return {k: v for k, v in cleaned.items()
                if v is not None and v != [] and v != {}}
    if isinstance(obj, list):
        items = [prune(v) for v in obj]
        return [v for v in items if v is not None and v != [] and v != {}]
    return obj


class Bag:
    """Collects resources for one document bundle, deduplicating by fullUrl."""

    def __init__(self) -> None:
        self._entries: dict[str, dict] = {}
        self.order: list[str] = []

    def add(self, full_url: str, resource: dict) -> str:
        if full_url not in self._entries:
            self._entries[full_url] = prune(resource)
            self.order.append(full_url)
        return full_url

    def has(self, full_url: str) -> bool:
        return full_url in self._entries

    def entries(self) -> list[dict]:
        return [{"fullUrl": u, "resource": self._entries[u]} for u in self.order]

    def __len__(self) -> int:
        return len(self._entries)


def ref(full_url: str, display: str | None = None) -> dict:
    """A FHIR Reference to a bundle entry."""
    out = {"reference": full_url}
    if display:
        out["display"] = display
    return out


# ---------------------------------------------------------------------------
# Administrative resources
# ---------------------------------------------------------------------------
def organization(row) -> tuple[str, dict]:
    full = urn("Organization", row["id"])
    res = {
        "resourceType": "Organization",
        "id": str(uuid.uuid5(NS, f"Organization/{row['id']}")),
        "meta": {"profile": [PROFILE["Organization"]]},
        "identifier": [{
            "type": cc(row["identifier_type_system"], row["identifier_type_code"],
                       row["identifier_type_display"]),
            "system": row["identifier_system"],
            "value": row["identifier_value"],
        }],
        "type": [cc(ORG_TYPE, row["type_code"], row["type_display"])],
        "name": row["name"],
        "telecom": _telecom(row["phone"], row["email"]),
        "address": _address(row),
    }
    return full, res


def practitioner(row) -> tuple[str, dict]:
    full = urn("Practitioner", row["id"])
    qualification = None
    if row["qualification_display"]:
        qualification = [{
            "code": cc(row["qualification_system"], row["qualification_code"],
                       row["qualification_display"], text=row["qualification_display"]),
        }]
    res = {
        "resourceType": "Practitioner",
        "id": str(uuid.uuid5(NS, f"Practitioner/{row['id']}")),
        "meta": {"profile": [PROFILE["Practitioner"]]},
        "identifier": [{
            "type": cc(row["identifier_type_system"], row["identifier_type_code"],
                       row["identifier_type_display"]),
            "system": row["identifier_system"],
            "value": row["identifier_value"],
        }],
        "name": [{"text": row["name"]}],
        "telecom": _telecom(row["phone"], row["email"]),
        "gender": row["gender"] or None,
        "qualification": qualification,
    }
    return full, res


def patient(row, org_full: str | None) -> tuple[str, dict]:
    full = urn("Patient", row["id"])
    identifiers = [{
        "type": cc(V2_0203, "MR", "Medical record number"),
        "system": "https://healthid.abdm.gov.in/mrn",
        "value": row["mrn"],
    }]
    if row["abha_number"]:
        identifiers.append({
            "type": cc(NDHM_IDENTIFIER_TYPE, "ABHA",
                       "Ayushman Bharat Health Account (ABHA) ID"),
            "system": "https://healthid.abdm.gov.in",
            "value": row["abha_number"],
        })
    if row["abha_address"]:
        identifiers.append({
            "type": cc(NDHM_IDENTIFIER_TYPE, "HIN", "Health ID issued by NDHM"),
            "system": "https://healthid.abdm.gov.in",
            "value": row["abha_address"],
        })

    contact = None
    if row["contact_name"]:
        contact = [{
            "relationship": [{"text": row["contact_relation"] or "Emergency contact"}],
            "name": {"text": row["contact_name"]},
            "telecom": _telecom(row["contact_phone"], None),
        }]

    name: dict[str, Any] = {"text": row["name"]}
    if row["family_name"]:
        name["family"] = row["family_name"]
    if row["given_name"]:
        name["given"] = [row["given_name"]]

    res = {
        "resourceType": "Patient",
        "id": str(uuid.uuid5(NS, f"Patient/{row['id']}")),
        "meta": {"profile": [PROFILE["Patient"]]},
        "identifier": identifiers,
        "name": [name],
        "telecom": _telecom(row["phone"], row["email"]),
        "gender": row["gender"],
        "birthDate": row["birth_date"] or None,
        "deceasedBoolean": True if row["deceased"] else None,
        "address": _address(row),
        "maritalStatus": cc(V3_MARITAL, row["marital_status_code"],
                            row["marital_status_display"]),
        "contact": contact,
        "managingOrganization": ref(org_full) if org_full else None,
    }
    return full, res


def encounter(row, patient_full: str, practitioner_full: str | None,
              diagnosis_refs: list[dict] | None = None) -> tuple[str, dict]:
    full = urn("Encounter", row["id"])
    participant = None
    if practitioner_full:
        ptype = ("ADM", "admitter") if row["kind"] == "IPD" else ("PPRF", "primary performer")
        participant = [{
            "type": [cc("http://terminology.hl7.org/CodeSystem/v3-ParticipationType",
                        ptype[0], ptype[1])],
            "individual": ref(practitioner_full),
        }]

    hospitalization = None
    if row["kind"] == "IPD" and row["discharge_disposition_code"]:
        hospitalization = {
            "dischargeDisposition": cc(
                DISCHARGE_DISPOSITION, row["discharge_disposition_code"],
                row["discharge_disposition_display"]),
        }

    period = {"start": db.to_instant(row["period_start"])}
    if row["period_end"]:
        period["end"] = db.to_instant(row["period_end"])

    res = {
        "resourceType": "Encounter",
        "id": str(uuid.uuid5(NS, f"Encounter/{row['id']}")),
        "meta": {"profile": [PROFILE["Encounter"]]},
        "identifier": [{"system": "https://nanoemr.local/encounter",
                        "value": row["encounter_no"]}],
        "status": row["status"],
        "class": {"system": V3_ACT_CODE, "code": row["class_code"],
                  "display": row["class_display"]},
        "type": [cc(row["type_system"] or SNOMED, row["type_code"], row["type_display"])],
        "serviceType": cc(row["service_type_system"] or SNOMED,
                          row["service_type_code"], row["service_type_display"]),
        "priority": cc(row["priority_system"], row["priority_code"],
                       row["priority_display"]),
        "subject": ref(patient_full),
        "participant": participant,
        "period": period,
        "reasonCode": [cc(SNOMED, row["reason_code"], row["reason_display"])]
        if row["reason_code"] else None,
        "diagnosis": diagnosis_refs or None,
        "hospitalization": hospitalization,
        "location": _location(row),
    }
    return full, res


def _location(row) -> list[dict] | None:
    if row["kind"] != "IPD" or not row["ward"]:
        return None
    label = row["ward"] + (f" / Bed {row['bed']}" if row["bed"] else "")
    return [{"location": {"display": label}}]


# ---------------------------------------------------------------------------
# Clinical resources
# ---------------------------------------------------------------------------
CONDITION_CATEGORY = {
    "chief-complaint": ("problem-list-item", "Problem List Item"),
    "diagnosis": ("encounter-diagnosis", "Encounter Diagnosis"),
    "medical-history": ("problem-list-item", "Problem List Item"),
}


def condition(row, patient_full: str, encounter_full: str | None,
              asserter_full: str | None = None) -> tuple[str, dict]:
    full = urn("Condition", row["id"])
    codings = []
    if row["icd10_code"]:
        codings.append({"system": ICD10, "code": row["icd10_code"],
                        "display": row["icd10_display"] or row["text"]})
    if row["snomed_code"]:
        codings.append({"system": SNOMED, "code": row["snomed_code"],
                        "display": row["snomed_display"] or row["text"]})
    code: dict[str, Any] = {"text": row["text"]}
    if codings:
        code["coding"] = codings

    cat_code, cat_display = CONDITION_CATEGORY.get(
        row["category"], ("problem-list-item", "Problem List Item"))

    res = {
        "resourceType": "Condition",
        "id": str(uuid.uuid5(NS, f"Condition/{row['id']}")),
        "meta": {"profile": [PROFILE["Condition"]]},
        "clinicalStatus": cc(COND_CLINICAL, row["clinical_status"],
                             row["clinical_status"].title()),
        "verificationStatus": cc(COND_VERIFICATION, row["verification_status"],
                                 row["verification_status"].title()),
        "category": [cc(COND_CATEGORY, cat_code, cat_display)],
        "severity": cc(SNOMED, row["severity_code"], row["severity_display"]),
        "code": code,
        "subject": ref(patient_full),
        "encounter": ref(encounter_full) if encounter_full else None,
        "onsetDateTime": db.to_instant(row["onset"]) if row["onset"] else None,
        "recordedDate": db.to_instant(row["recorded_at"]),
        "asserter": ref(asserter_full) if asserter_full else None,
        "note": [{"text": row["note"]}] if row["note"] else None,
    }
    return full, res


OBS_CATEGORY_CODE = {
    "vital-signs": ("vital-signs", "Vital Signs"),
    "exam": ("exam", "Exam"),
    "laboratory": ("laboratory", "Laboratory"),
}


def observation(row, patient_full: str, encounter_full: str | None,
                performer_full: str | None = None,
                specimen_full: str | None = None,
                profile: str | None = None) -> tuple[str, dict]:
    full = urn("Observation", row["id"])
    keys = row.keys() if hasattr(row, "keys") else ()
    codings = []
    if row["loinc_code"]:
        codings.append({"system": LOINC, "code": row["loinc_code"],
                        "display": row["loinc_display"]})
    if row["snomed_code"]:
        codings.append({"system": SNOMED, "code": row["snomed_code"],
                        "display": row["snomed_display"]})
    code: dict[str, Any] = {}
    if codings:
        code["coding"] = codings
    code["text"] = (
        (row["code_text"] if "code_text" in keys else None)
        or row["loinc_display"] or row["snomed_display"] or "Observation")

    value_coded = None
    if "value_code" in keys and row["value_code"]:
        value_coded = cc(row["value_system"], row["value_code"], row["value_display"])

    value_quantity = None
    if row["value_quantity"] is not None:
        value_quantity = {"value": row["value_quantity"]}
        if row["value_unit"]:
            value_quantity.update({"unit": row["value_unit"], "system": UCUM,
                                   "code": row["value_unit"]})

    ref_range = None
    if row["ref_low"] is not None or row["ref_high"] is not None:
        rr: dict[str, Any] = {}
        if row["ref_low"] is not None:
            rr["low"] = {"value": row["ref_low"], "unit": row["value_unit"],
                         "system": UCUM, "code": row["value_unit"]}
        if row["ref_high"] is not None:
            rr["high"] = {"value": row["ref_high"], "unit": row["value_unit"],
                          "system": UCUM, "code": row["value_unit"]}
        ref_range = [rr]

    interp = None
    if row["interpretation"]:
        interp = [cc(INTERPRETATION, row["interpretation"],
                     {"N": "Normal", "H": "High", "L": "Low",
                      "A": "Abnormal"}.get(row["interpretation"], row["interpretation"]))]

    cat_code, cat_display = OBS_CATEGORY_CODE.get(row["category"], ("exam", "Exam"))

    res = {
        "resourceType": "Observation",
        "id": str(uuid.uuid5(NS, f"Observation/{row['id']}")),
        "meta": {"profile": [profile or PROFILE["Observation"]]},
        "status": row["status"],
        "category": [cc(OBS_CATEGORY, cat_code, cat_display)],
        "code": code,
        "subject": ref(patient_full),
        "encounter": ref(encounter_full) if encounter_full else None,
        "effectiveDateTime": db.to_instant(row["effective_ts"]),
        "performer": [ref(performer_full)] if performer_full else None,
        "valueQuantity": value_quantity if value_coded is None else None,
        "valueCodeableConcept": value_coded,
        "valueString": (row["value_string"]
                        if value_quantity is None and value_coded is None else None),
        "interpretation": interp,
        "bodySite": cc(SNOMED, row["body_site_code"], row["body_site_display"]),
        "specimen": ref(specimen_full) if specimen_full else None,
        "referenceRange": ref_range,
        "note": [{"text": row["note"]}] if row["note"] else None,
    }
    return full, res


def allergy(row, patient_full: str, encounter_full: str | None,
            recorder_full: str | None = None) -> tuple[str, dict]:
    full = urn("AllergyIntolerance", row["id"])
    reaction = None
    if row["reaction"]:
        reaction = [{"manifestation": [{"text": row["reaction"]}]}]
    res = {
        "resourceType": "AllergyIntolerance",
        "id": str(uuid.uuid5(NS, f"AllergyIntolerance/{row['id']}")),
        "meta": {"profile": [PROFILE["AllergyIntolerance"]]},
        "clinicalStatus": cc(ALLERGY_CLINICAL, row["clinical_status"],
                             row["clinical_status"].title()),
        "category": [row["category"]] if row["category"] else None,
        "criticality": row["criticality"] or None,
        "code": cc(SNOMED, row["snomed_code"], row["snomed_display"], text=row["text"]),
        "patient": ref(patient_full),
        "encounter": ref(encounter_full) if encounter_full else None,
        "recordedDate": db.to_instant(row["recorded_at"]),
        "recorder": ref(recorder_full) if recorder_full else None,
        "reaction": reaction,
    }
    return full, res


def medication_request(row, patient_full: str, encounter_full: str | None,
                       requester_full: str | None) -> tuple[str, dict]:
    full = urn("MedicationRequest", row["id"])
    timing = None
    if row["frequency"] and row["period"]:
        timing = {"repeat": {"frequency": row["frequency"],
                             "period": row["period"],
                             "periodUnit": row["period_unit"] or "d"}}
        if row["duration_days"]:
            timing["repeat"]["boundsDuration"] = {
                "value": row["duration_days"], "unit": "days",
                "system": UCUM, "code": "d"}

    dose: dict[str, Any] = {"text": row["timing_text"] or row["snomed_display"]}
    if timing:
        dose["timing"] = timing
    if row["additional_code"]:
        dose["additionalInstruction"] = [
            cc(SNOMED, row["additional_code"], row["additional_display"])]
    if row["route_code"]:
        dose["route"] = cc(SNOMED, row["route_code"], row["route_display"])
    if row["method_code"]:
        dose["method"] = cc(SNOMED, row["method_code"], row["method_display"])
    if row["dose_quantity"]:
        dose["doseAndRate"] = [{"doseQuantity": {
            "value": row["dose_quantity"], "unit": row["dose_unit"] or "1",
            "system": UCUM, "code": row["dose_unit"] or "1"}}]

    res = {
        "resourceType": "MedicationRequest",
        "id": str(uuid.uuid5(NS, f"MedicationRequest/{row['id']}")),
        "meta": {"profile": [PROFILE["MedicationRequest"]]},
        "status": row["status"],
        "intent": row["intent"],
        "medicationCodeableConcept": cc(SNOMED, row["snomed_code"], row["snomed_display"]),
        "subject": ref(patient_full),
        "encounter": ref(encounter_full) if encounter_full else None,
        "authoredOn": db.to_instant(row["authored_on"]),
        "requester": ref(requester_full) if requester_full else None,
        "reasonCode": [cc(SNOMED, row["reason_code"], row["reason_display"])]
        if row["reason_code"] else None,
        "dosageInstruction": [dose],
        "note": [{"text": row["note"]}] if row["note"] else None,
    }
    return full, res


def procedure(row, patient_full: str, encounter_full: str | None,
              performer_full: str | None) -> tuple[str, dict]:
    full = urn("Procedure", row["id"])
    performer = None
    if performer_full:
        performer = [{"actor": ref(performer_full)}]
    res = {
        "resourceType": "Procedure",
        "id": str(uuid.uuid5(NS, f"Procedure/{row['id']}")),
        "meta": {"profile": [PROFILE["Procedure"]]},
        "status": row["status"],
        "category": cc(SNOMED, row["category_code"], row["category_display"]),
        "code": cc(SNOMED, row["snomed_code"], row["snomed_display"]),
        "subject": ref(patient_full),
        "encounter": ref(encounter_full) if encounter_full else None,
        "performedDateTime": db.to_instant(row["performed_ts"]) if row["performed_ts"] else None,
        "performer": performer,
        "bodySite": [cc(SNOMED, row["body_site_code"], row["body_site_display"])]
        if row["body_site_code"] else None,
        "outcome": cc(SNOMED, row["outcome_code"], row["outcome_display"]),
        "note": [{"text": row["note"]}] if row["note"] else None,
    }
    return full, res


def service_request(row, patient_full: str, encounter_full: str | None,
                    requester_full: str | None) -> tuple[str, dict]:
    full = urn("ServiceRequest", row["id"])
    res = {
        "resourceType": "ServiceRequest",
        "id": str(uuid.uuid5(NS, f"ServiceRequest/{row['id']}")),
        "meta": {"profile": [PROFILE["ServiceRequest"]]},
        "status": row["status"],
        "intent": row["intent"],
        "code": cc(row["code_system"], row["code"], row["display"]),
        "subject": ref(patient_full),
        "encounter": ref(encounter_full) if encounter_full else None,
        "authoredOn": db.to_instant(row["authored_on"]),
        "requester": ref(requester_full) if requester_full else None,
        "note": [{"text": row["note"]}] if row["note"] else None,
    }
    return full, res


def specimen(order, patient_full: str) -> tuple[str, dict]:
    full = urn("Specimen", order["id"])
    res = {
        "resourceType": "Specimen",
        "id": str(uuid.uuid5(NS, f"Specimen/{order['id']}")),
        "meta": {"profile": [PROFILE["Specimen"]]},
        "status": "available",
        "type": cc(SNOMED, order["specimen_code"], order["specimen_display"]),
        "subject": ref(patient_full),
        # Specimen.receivedTime and collection.collected[x] are both 1..1 in the
        # NRCES profile, so fall back to the order timestamp.
        "receivedTime": db.to_instant(order["specimen_received_ts"] or order["ordered_at"]),
        "collection": {
            "collectedDateTime": db.to_instant(
                order["specimen_collected_ts"] or order["ordered_at"]),
        },
    }
    return full, res


def diagnostic_report_lab(order, result_fulls: list[str], patient_full: str,
                          encounter_full: str | None, performer_full: str | None,
                          interpreter_full: str | None,
                          specimen_full: str | None) -> tuple[str, dict]:
    full = urn("DiagnosticReport", order["id"])
    res = {
        "resourceType": "DiagnosticReport",
        "id": str(uuid.uuid5(NS, f"DiagnosticReport/{order['id']}")),
        "meta": {"profile": [PROFILE["DiagnosticReportLab"]]},
        "identifier": [{"system": "https://nanoemr.local/lab-order",
                        "value": order["order_no"]}],
        "status": "final" if order["status"] == "final" else "preliminary",
        "category": [cc(SNOMED, order["category_code"], order["category_display"])],
        "code": cc(LOINC, order["panel_code"], order["panel_display"]),
        "subject": ref(patient_full),
        "encounter": ref(encounter_full) if encounter_full else None,
        "effectiveDateTime": db.to_instant(order["effective_ts"] or order["ordered_at"]),
        "issued": db.to_instant(order["issued_ts"] or order["ordered_at"]),
        "performer": [ref(performer_full)] if performer_full else None,
        "resultsInterpreter": [ref(interpreter_full)] if interpreter_full else None,
        "specimen": [ref(specimen_full)] if specimen_full else None,
        "result": [ref(u) for u in result_fulls],
        "conclusion": order["conclusion"] or None,
        "conclusionCode": [cc(SNOMED, order["conclusion_code"],
                              order["conclusion_display"])]
        if order["conclusion_code"] else None,
    }
    return full, res


def care_plan(note, patient_full: str, encounter_full: str | None,
              author_full: str | None) -> tuple[str, dict]:
    full = urn("CarePlan", note["id"])
    detail = "\n".join(filter(None, [
        note["care_plan_text"], note["discharge_instructions"], note["diet_advice"]]))
    res = {
        "resourceType": "CarePlan",
        "id": str(uuid.uuid5(NS, f"CarePlan/{note['id']}")),
        "meta": {"profile": [PROFILE["CarePlan"]]},
        "status": "active",
        "intent": "plan",
        "title": "Discharge care plan",
        "description": detail or "Care plan on discharge",
        "subject": ref(patient_full),
        "encounter": ref(encounter_full) if encounter_full else None,
        "created": db.to_instant(note["updated_at"]),
        "author": ref(author_full) if author_full else None,
    }
    return full, res


def appointment(note, patient_full: str, practitioner_full: str | None) -> tuple[str, dict]:
    full = urn("Appointment", note["id"])
    start = db.to_instant(note["follow_up_date"])
    res = {
        "resourceType": "Appointment",
        "id": str(uuid.uuid5(NS, f"Appointment/{note['id']}")),
        "meta": {"profile": [PROFILE["Appointment"]]},
        "status": "booked",
        "appointmentType": cc(V3_ACT_CODE, "FOLLOWUP", "A follow up visit from a previous appointment"),
        "description": note["follow_up_note"] or "Follow up visit",
        "start": start,
        "participant": [
            p for p in [
                {"actor": ref(patient_full), "status": "accepted"},
                {"actor": ref(practitioner_full), "status": "accepted"}
                if practitioner_full else None,
            ] if p
        ],
    }
    return full, res


# ---------------------------------------------------------------------------
# Billing
# ---------------------------------------------------------------------------
# Invoice.lineItem.priceComponent.type is bound to the required FHIR
# InvoicePriceComponentType value set; the NRCES-specific meaning rides on .code.
_COMPONENT_TYPE = {"mrp": "base", "rate": "base", "discount": "discount",
                   "cgst": "tax", "sgst": "tax"}


def _price_component(kind: str, factor: float | None, amount: float) -> dict:
    code, display = PRICE_COMPONENT[kind]
    comp: dict[str, Any] = {
        "type": _COMPONENT_TYPE[kind],
        "code": {"coding": [{"system": NDHM_PRICE_COMPONENTS, "code": code,
                             "display": display}], "text": display},
        "amount": {"value": round(amount, 2), "currency": "INR"},
    }
    if factor:
        comp["factor"] = round(factor, 4)
    return comp


def invoice(inv, lines, patient_full: str, encounter_full: str | None,
            issuer_full: str | None, participant_full: str | None) -> tuple[str, dict]:
    full = urn("Invoice", inv["id"])
    line_items = []
    for line in lines:
        gross = line["unit_price"] * line["quantity"]
        discount = gross * (line["discount_pct"] or 0) / 100.0
        net = gross - discount
        cgst = net * (line["cgst_pct"] or 0) / 100.0
        sgst = net * (line["sgst_pct"] or 0) / 100.0
        components = [_price_component("rate", None, gross)]
        if discount:
            components.append(
                _price_component("discount", -(line["discount_pct"] or 0) / 100.0, -discount))
        if cgst:
            components.append(
                _price_component("cgst", (line["cgst_pct"] or 0) / 100.0, cgst))
        if sgst:
            components.append(
                _price_component("sgst", (line["sgst_pct"] or 0) / 100.0, sgst))
        line_items.append({
            "sequence": line["seq"],
            "chargeItemCodeableConcept": cc(line["charge_system"], line["charge_code"],
                                            line["charge_display"],
                                            text=line["description"]),
            "priceComponent": components,
        })

    res = {
        "resourceType": "Invoice",
        "id": str(uuid.uuid5(NS, f"Invoice/{inv['id']}")),
        "meta": {"profile": [PROFILE["Invoice"]]},
        "identifier": [{"system": "https://nanoemr.local/invoice",
                        "value": inv["invoice_no"]}],
        "status": inv["status"],
        "type": {"coding": [{"system": NDHM_BILLING_CODES, "code": inv["type_code"],
                             "display": inv["type_display"]}],
                 "text": inv["type_display"]},
        "subject": ref(patient_full),
        "recipient": ref(patient_full),
        "date": db.to_instant(inv["date"]),
        "participant": [{"actor": ref(participant_full)}] if participant_full else None,
        "issuer": ref(issuer_full) if issuer_full else None,
        "lineItem": line_items,
        "totalNet": {"value": round(inv["total_net"], 2), "currency": "INR"},
        "totalGross": {"value": round(inv["total_gross"], 2), "currency": "INR"},
        "note": [{"text": inv["note"]}] if inv["note"] else None,
    }
    return full, res


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------
def _telecom(phone: str | None, email: str | None) -> list[dict] | None:
    out = []
    if phone:
        out.append({"system": "phone", "value": phone, "use": "mobile"})
    if email:
        out.append({"system": "email", "value": email, "use": "work"})
    return out or None


def _address(row) -> list[dict] | None:
    keys = row.keys() if hasattr(row, "keys") else []
    if "address_line" not in keys or not row["address_line"]:
        return None
    text_parts = [row["address_line"], row["city"], row["district"],
                  row["state"], row["postal_code"]]
    return [{
        "use": "home",
        "type": "physical",
        "text": ", ".join(p for p in text_parts if p),
        "line": [row["address_line"]],
        "city": row["city"] or None,
        "district": row["district"] or None,
        "state": row["state"] or None,
        "postalCode": row["postal_code"] or None,
        "country": row["country"] or "India",
    }]
