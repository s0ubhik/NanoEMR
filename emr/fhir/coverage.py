"""The CoverageEligibilityRequest bundle, built here rather than by a gateway.

This is a port of hcxkit's ``pmjay/coverage/request/check`` template. nanoemr
used to POST the field values to the kit's ``/internal/mappings/map`` and send
back whatever came out, which made an eligibility check impossible without
that particular gateway running. The bundle is ours to build, so it is built
here and any NHCX gateway can carry it.

The shape is AB-PMJAY's, and several of its oddities are deliberate:

* **Absolute payer URLs, not ``urn:uuid``.** PMJAY resolves entries by the URL
  under ``BASE``, and every entry repeats it in both ``id`` and ``fullUrl``.
* **IST, not UTC.** PMJAY stamps ``+05:30`` timestamps.
* **Blank strings, not omitted fields.** A discovery goes out with the
  demographics empty — the payer fills them in on the response — so those
  fields are present and empty rather than absent.
* **``item`` only on auth-requirements.** Discovery and validation carry no
  items at all, and an empty list is not the same as no list.
"""

from __future__ import annotations

import time
from typing import Any

PROFILE = "https://nrces.in/ndhm/fhir/r4/StructureDefinition/"
PAYER_SYSTEM = "https://payer.pmjay.nha.gov.in"
BIS = "https://bis.pmjay.gov.in"
FACILITY = "https://facility.abdm.gov.in"
V2_0203 = "http://terminology.hl7.org/CodeSystem/v2-0203"
NDHM_IDTYPE = "https://nrces.in/ndhm/fhir/r4/CodeSystem/ndhm-identifier-type-code"
ORG_TYPE = "http://terminology.hl7.org/CodeSystem/organization-type"
PROCESS_PRIORITY = "http://terminology.hl7.org/CodeSystem/processpriority"
PROGRAM_CODE = "https://nrces.in/ndhm/fhir/r4/CodeSystem/ndhm-program-code"

BASE = ("https://payer.nha.gov.in/coverageeligibility/v1/coverageeligibility"
        "/check/coverageeligibilityrequest")

PURPOSES = ("discovery", "validation", "auth-requirements")

IST_OFFSET = 19800  # +05:30 in seconds


def _cc(system: str, code: str, display: str | None = None) -> dict:
    coding: dict[str, Any] = {"system": system, "code": code}
    if display:
        coding["display"] = display
    return {"coding": [coding]}


def _ref(url: str) -> dict:
    return {"reference": url}


def _ist(fmt: str) -> str:
    return time.strftime(fmt, time.gmtime(time.time() + IST_OFFSET))


def _midnight(day: str) -> str:
    return day + "T00:00:00+05:30"


def build_coverage_request(data: dict[str, Any]) -> dict:
    """Build the bundle from the same field vocabulary the template took."""
    now = _ist("%Y-%m-%dT%H:%M:%S") + "+05:30"
    today = _ist("%Y-%m-%d")

    def s(key: str, default: str = "") -> str:
        value = data.get(key)
        return default if value is None else str(value)

    purpose = s("purpose", "discovery") or "discovery"
    policy_number = s("policyNumber")
    pmjay_id = s("pmjayId")
    serviced_date = s("servicedDate") or today
    serviced_end = s("servicedEnd") or serviced_date

    patient = {
        "resourceType": "Patient",
        "id": "1",
        "meta": {"versionId": "1", "lastUpdated": now,
                 "profile": [PROFILE + "Patient"]},
        "identifier": [
            {"type": _cc(NDHM_IDTYPE, "PMJAY",
                         "Pradhan Mantri Jan Aarogya Yojana (PMJAY) ID"),
             "system": BIS, "value": pmjay_id},
            {"type": _cc(V2_0203, "JHN", "Jurisdictional health number"),
             "system": BIS, "value": s("abhaNumber")},
            {"type": _cc(V2_0203, "PI", "Patient internal identifier"),
             "system": "https://provider.pmjay.gov.in", "value": s("patientRefId")},
        ],
        # PMJAY writes name.text as "<given> <family>" — a beneficiary with no
        # family name really does go out with a trailing space.
        "name": [{"text": s("patientName") + " " + s("patientFamily"),
                  "family": s("patientFamily"),
                  "given": [s("patientName")]}],
        "telecom": [{"system": "phone", "value": s("patientPhone")}],
        "gender": s("patientGender"),
        "birthDate": s("patientDob"),
        "address": [{"district": s("districtCode"), "state": s("stateCode")}],
    }

    provider = {
        "resourceType": "Organization",
        "id": "1",
        "meta": {"profile": [PROFILE + "Organization"]},
        "identifier": [{"type": _cc(V2_0203, "NPI", "National provider identifier"),
                        "system": FACILITY, "value": s("providerId")}],
        "active": True,
        "type": [_cc(ORG_TYPE, "prov", "Healthcare Provider")],
        "name": s("providerName", "Provider") or "Provider",
    }

    payer = {
        "resourceType": "Organization",
        "id": "2",
        "meta": {"profile": [PROFILE + "Organization"]},
        "identifier": [{"type": _cc(V2_0203, "NIIP",
                                    "National Insurance Payor Identifier (Payor)"),
                        "system": FACILITY, "value": s("payerId")}],
        "active": True,
        "type": [_cc(ORG_TYPE, "pay", "Payer")],
        "name": s("payerName", "Nhcx Pmjay") or "Nhcx Pmjay",
    }

    coverage = {
        "resourceType": "Coverage",
        "id": "1",
        "meta": {"profile": [PROFILE + "Coverage"]},
        "identifier": [{"type": _cc(V2_0203, "NH", "National Health Plan Identifier"),
                        "system": "https://payer.nha.gov.in", "value": policy_number}],
        "subscriberId": s("subscriberId") or pmjay_id,
        "status": "active",
        "beneficiary": _ref(BASE + "/patient"),
        "period": {"start": _midnight(serviced_date), "end": _midnight(serviced_end)},
        "payor": [_ref(BASE + "/organization/pay")],
    }

    request: dict[str, Any] = {
        "resourceType": "CoverageEligibilityRequest",
        "id": policy_number,
        "meta": {"versionId": "1", "lastUpdated": now,
                 "profile": [PROFILE + "CoverageEligibilityRequest"]},
        "identifier": [{"system": "https://hcx.pmjay.gov.in/v1/coverageeligibility/check",
                        "value": policy_number}],
        "status": "active",
        "purpose": [purpose],
        "patient": _ref(BASE + "/patient"),
        "servicedDate": serviced_date,
        "created": s("created") or now,
        "insurer": _ref(BASE + "/organization/pay"),
        "enterer": _ref(BASE + "/practitioner/1"),
        "provider": _ref(BASE + "/organization/prov"),
        "priority": _cc(PROCESS_PRIORITY, s("priority", "normal") or "normal", "Normal"),
        "facility": {"identifier": {"system": "https://nhcx.pmjay.gov.in",
                                    "value": data.get("facilityId")}},
        "insurance": [{"coverage": _ref(BASE + "/coverage")}],
    }

    items = data.get("items")
    if isinstance(items, list) and items:
        built = []
        for i, it in enumerate(items, start=1):
            if not isinstance(it, dict):
                continue
            entry: dict[str, Any] = {
                "id": f"Item/{i}",
                "sequence": i,
                "productOrService": _cc("http://snomed.info/sct",
                                        str(it.get("code") or ""),
                                        it.get("display")),
                "programCode": [_cc(PROGRAM_CODE, "AB-PMJAY",
                                    "Ayushman Bharat Pradhan Mantri Jan Arogya "
                                    "Yojana (AB-PMJAY)")],
                "servicedPeriod": {"start": it.get("start") or serviced_date,
                                   "end": it.get("end") or serviced_end},
                "quantity": {"value": _number(it.get("quantity"), 1)},
                "unitPrice": {"value": _number(it.get("unitPrice"), 0)},
                "net": {"value": _number(it.get("net"),
                                         _number(it.get("unitPrice"), 0))},
            }
            if it.get("categoryCode"):
                entry["category"] = _cc(PAYER_SYSTEM, str(it["categoryCode"]),
                                        it.get("categoryName"))
            if it.get("factor") is not None:
                entry["factor"] = _number(it.get("factor"), 1)
            if it.get("modifierCode"):
                entry["modifier"] = [_cc(PAYER_SYSTEM, str(it["modifierCode"]),
                                         it.get("modifierName"))]
            built.append(entry)
        if built:
            request["item"] = built

    def entry_of(url: str, resource: dict) -> dict:
        return {"id": url, "fullUrl": url, "resource": resource}

    return {
        "resourceType": "Bundle",
        "id": "COVERAGE_REQUEST",
        "identifier": {"system": PAYER_SYSTEM, "value": s("caseId")},
        "meta": {"lastUpdated": now},
        "type": "collection",
        "timestamp": now,
        "entry": [
            entry_of(BASE, request),
            entry_of(BASE + "/patient", patient),
            entry_of(BASE + "/organization/prov", provider),
            entry_of(BASE + "/organization/pay", payer),
            entry_of(BASE + "/coverage", coverage),
        ],
    }


def _number(value: Any, default: float) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default
