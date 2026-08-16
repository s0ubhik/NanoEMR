"""A lightweight structural validator for the NRCES document bundles.

This is deliberately *not* a full FHIR validator — it encodes the mandatory
elements and fixed codings that the NRCES StructureDefinitions declare, which is
what actually trips up an ABDM submission. Run the official HL7 validator with
the NRCES package for a conformance sign-off; use this as the fast in-app gate.
"""

from __future__ import annotations

from typing import Any, Iterable

from ..terminology import (
    COMPOSITION_TYPE,
    DS_SECTIONS,
    ICD10,
    LOINC,
    OP_SECTIONS,
    PROFILE,
    SNOMED,
    WELLNESS_SECTION_TITLES,
)

ERROR = "error"
WARNING = "warning"


def _walk(obj: Any, path: str) -> list[Any]:
    """Resolve a dotted FHIR path, flattening lists as FHIRPath does."""
    values: list[Any] = [obj]
    for part in path.split("."):
        nxt: list[Any] = []
        for value in values:
            if isinstance(value, list):
                for item in value:
                    if isinstance(item, dict) and part in item:
                        nxt.append(item[part])
            elif isinstance(value, dict) and part in value:
                nxt.append(value[part])
        values = nxt
        if not values:
            return []
    out: list[Any] = []
    for value in values:
        if isinstance(value, list):
            out.extend(value)
        else:
            out.append(value)
    return out


def _present(obj: Any, path: str) -> bool:
    return any(v not in (None, "", [], {}) for v in _walk(obj, path))


# resourceType -> [(path, severity, message), ...]
RESOURCE_RULES: dict[str, list[tuple[str, str, str]]] = {
    "Patient": [
        ("identifier", ERROR, "Patient.identifier is 1..* in the NRCES profile"),
        ("identifier.type.coding.code", ERROR, "Patient.identifier.type.coding.code is mandatory"),
        ("identifier.value", ERROR, "Patient.identifier.value is mandatory"),
        ("name.text", ERROR, "Patient.name.text is mandatory when a name is supplied"),
        ("telecom.value", WARNING, "Patient.telecom.value is mandatory when telecom is supplied"),
        ("gender", WARNING, "Patient.gender is expected by ABDM consumers"),
    ],
    "Practitioner": [
        ("identifier", ERROR, "Practitioner.identifier is 1..*"),
        ("identifier.type.coding.code", ERROR, "Practitioner.identifier.type.coding.code is mandatory"),
        ("identifier.value", ERROR, "Practitioner.identifier.value is mandatory"),
        ("name.text", ERROR, "Practitioner.name.text is mandatory"),
    ],
    "Organization": [
        ("identifier", ERROR, "Organization.identifier is 1..*"),
        ("identifier.system", ERROR, "Organization.identifier.system is mandatory"),
        ("identifier.value", ERROR, "Organization.identifier.value is mandatory"),
        ("identifier.type.coding.code", ERROR, "Organization.identifier.type.coding.code is mandatory"),
        ("type.coding.code", ERROR, "Organization.type.coding.code is mandatory"),
        ("name", ERROR, "Organization.name is mandatory"),
    ],
    "Encounter": [
        ("status", ERROR, "Encounter.status is mandatory"),
        ("class", ERROR, "Encounter.class is mandatory"),
        ("subject", ERROR, "Encounter.subject is mandatory"),
    ],
    "Condition": [
        ("code", ERROR, "Condition.code is 1..1 in the NRCES profile"),
        ("subject", ERROR, "Condition.subject is mandatory"),
    ],
    "Observation": [
        ("status", ERROR, "Observation.status is mandatory"),
        ("code", ERROR, "Observation.code is mandatory"),
        ("subject", ERROR, "Observation.subject is mandatory"),
    ],
    "DiagnosticReport": [
        ("status", ERROR, "DiagnosticReport.status is mandatory"),
        ("code.coding.code", ERROR, "DiagnosticReport.code.coding.code is mandatory"),
        ("category.coding.code", ERROR, "DiagnosticReport.category.coding.code is mandatory"),
        ("subject", ERROR, "DiagnosticReport.subject is mandatory"),
        ("result", ERROR, "DiagnosticReportLab.result is 1..*"),
        ("resultsInterpreter", ERROR, "DiagnosticReportLab.resultsInterpreter is 1..*"),
        ("conclusion", ERROR, "DiagnosticReportLab.conclusion is 1..1"),
    ],
    "Specimen": [
        ("type.coding.code", ERROR, "Specimen.type.coding.code is mandatory"),
        ("receivedTime", ERROR, "Specimen.receivedTime is 1..1"),
        ("collection.collectedDateTime", ERROR, "Specimen.collection.collected[x] is 1..1"),
    ],
    "MedicationRequest": [
        ("status", ERROR, "MedicationRequest.status is mandatory"),
        ("intent", ERROR, "MedicationRequest.intent is mandatory"),
        ("medicationCodeableConcept.coding.code", ERROR,
         "MedicationRequest.medication[x].coding.code is mandatory"),
        ("subject", ERROR, "MedicationRequest.subject is mandatory"),
        ("authoredOn", ERROR, "MedicationRequest.authoredOn is 1..1"),
        ("requester", ERROR, "MedicationRequest.requester is 1..1"),
        ("dosageInstruction", ERROR, "MedicationRequest.dosageInstruction is 1..*"),
    ],
    "Procedure": [
        ("status", ERROR, "Procedure.status is mandatory"),
        ("code.coding.code", ERROR, "Procedure.code.coding.code is mandatory"),
        ("subject", ERROR, "Procedure.subject is mandatory"),
    ],
    "ServiceRequest": [
        ("status", ERROR, "ServiceRequest.status is mandatory"),
        ("intent", ERROR, "ServiceRequest.intent is mandatory"),
        ("subject", ERROR, "ServiceRequest.subject is mandatory"),
    ],
    "AllergyIntolerance": [
        ("patient", ERROR, "AllergyIntolerance.patient is mandatory"),
        ("code", ERROR, "AllergyIntolerance.code is 1..1 in the NRCES profile"),
    ],
    "CarePlan": [
        ("status", ERROR, "CarePlan.status is mandatory"),
        ("intent", ERROR, "CarePlan.intent is mandatory"),
        ("subject", ERROR, "CarePlan.subject is mandatory"),
    ],
    "Appointment": [
        ("status", ERROR, "Appointment.status is mandatory"),
        ("participant", ERROR, "Appointment.participant is 1..*"),
    ],
    "Invoice": [
        ("identifier", ERROR, "Invoice.identifier is 1..1"),
        ("identifier.value", ERROR, "Invoice.identifier.value is mandatory"),
        ("status", ERROR, "Invoice.status is mandatory"),
        ("type.coding.code", ERROR, "Invoice.type.coding.code is mandatory"),
        ("subject", ERROR, "Invoice.subject is 1..1"),
        ("date", ERROR, "Invoice.date is 1..1"),
        ("lineItem", ERROR, "Invoice.lineItem is 1..*"),
        ("lineItem.priceComponent.code.coding.code", ERROR,
         "Invoice.lineItem.priceComponent.code.coding.code is mandatory"),
        ("lineItem.priceComponent.amount", ERROR,
         "Invoice.lineItem.priceComponent.amount is 1..1"),
        ("totalNet", ERROR, "Invoice.totalNet is 1..1"),
        ("totalGross", ERROR, "Invoice.totalGross is 1..1"),
    ],
}

# Fixed systems that the profiles pin with `fixedUri`.
FIXED_SYSTEMS: list[tuple[str, str, str, str]] = [
    ("DiagnosticReport", "category.coding.system", SNOMED,
     "DiagnosticReportLab.category.coding.system is fixed to SNOMED CT"),
    ("DiagnosticReport", "code.coding.system", LOINC,
     "DiagnosticReportLab.code.coding.system is fixed to LOINC"),
    ("MedicationRequest", "medicationCodeableConcept.coding.system", SNOMED,
     "MedicationRequest.medication[x].coding.system is fixed to SNOMED CT"),
    ("Procedure", "code.coding.system", SNOMED,
     "Procedure.code.coding.system is fixed to SNOMED CT"),
    ("Specimen", "type.coding.system", SNOMED,
     "Specimen.type.coding.system is fixed to SNOMED CT"),
    ("AllergyIntolerance", "code.coding.system", SNOMED,
     "AllergyIntolerance.code.coding.system is fixed to SNOMED CT"),
]

SECTION_TABLES = {
    "OPConsultRecord": OP_SECTIONS,
    "DischargeSummaryRecord": DS_SECTIONS,
}


def validate_bundle(artifact: str, bundle: dict) -> list[dict]:
    """Check one document bundle against the NRCES profile rules.

    Returns a list of ``{severity, path, message}``; an empty list means the
    bundle satisfies every mandatory element and fixed coding this module knows
    about. Not a substitute for the official HL7 validator.
    """
    issues: list[dict] = []

    def add(sev: str, path: str, message: str) -> None:
        issues.append({"severity": sev, "path": path, "message": message})

    # ------------------------------------------------------------- Bundle
    if bundle.get("resourceType") != "Bundle":
        add(ERROR, "Bundle", "root resource must be a Bundle")
        return issues
    if PROFILE["DocumentBundle"] not in _walk(bundle, "meta.profile"):
        add(ERROR, "Bundle.meta.profile",
            "must declare the NRCES DocumentBundle profile")
    if not _present(bundle, "meta.versionId"):
        add(ERROR, "Bundle.meta.versionId", "Bundle.meta.versionId is 1..1")
    if not _present(bundle, "identifier.system") or not _present(bundle, "identifier.value"):
        add(ERROR, "Bundle.identifier",
            "Bundle.identifier.system and .value are mandatory")
    if bundle.get("type") != "document":
        add(ERROR, "Bundle.type", "Bundle.type is fixed to 'document'")
    if not bundle.get("timestamp"):
        add(ERROR, "Bundle.timestamp", "Bundle.timestamp is 1..1")

    entries = bundle.get("entry") or []
    if not entries:
        add(ERROR, "Bundle.entry", "a document bundle needs at least the Composition")
        return issues

    composition = entries[0].get("resource", {})
    if composition.get("resourceType") != "Composition":
        add(ERROR, "Bundle.entry[0]",
            "the first entry of a document bundle must be the Composition")

    # ---------------------------------------------------------- Composition
    issues.extend(_validate_composition(artifact, composition))

    # ------------------------------------------------------------ resources
    seen_urls = {e.get("fullUrl") for e in entries}
    for index, entry in enumerate(entries):
        resource = entry.get("resource", {})
        rtype = resource.get("resourceType", "?")
        if not entry.get("fullUrl"):
            add(ERROR, f"Bundle.entry[{index}].fullUrl",
                "every entry of a document bundle needs a fullUrl")
        for path, severity, message in RESOURCE_RULES.get(rtype, []):
            if not _present(resource, path):
                add(severity, f"{rtype}.{path}", message)
        for target, path, system, message in FIXED_SYSTEMS:
            if rtype != target:
                continue
            for value in _walk(resource, path):
                if value != system:
                    add(ERROR, f"{rtype}.{path}", f"{message} (found {value})")
        if rtype == "Condition":
            systems = set(_walk(resource, "code.coding.system"))
            if systems and not systems <= {ICD10, SNOMED}:
                add(ERROR, "Condition.code.coding.system",
                    "only the ICD-10 and SNOMED CT slices are allowed "
                    f"(found {sorted(systems - {ICD10, SNOMED})})")
        if rtype == "Observation":
            systems = set(_walk(resource, "code.coding.system"))
            if systems and not systems <= {LOINC, SNOMED}:
                add(ERROR, "Observation.code.coding.system",
                    "only the LOINC and SNOMED CT slices are allowed "
                    f"(found {sorted(systems - {LOINC, SNOMED})})")

    # ------------------------------------------------- reference resolution
    for missing in sorted(_dangling_references(bundle, seen_urls)):
        add(ERROR, "Bundle.entry.resource.reference",
            f"reference {missing} does not resolve inside the document bundle")

    return issues


def _validate_composition(artifact: str, composition: dict) -> list[dict]:
    issues: list[dict] = []

    def add(sev: str, path: str, message: str) -> None:
        issues.append({"severity": sev, "path": path, "message": message})

    if PROFILE[artifact] not in _walk(composition, "meta.profile"):
        add(ERROR, "Composition.meta.profile",
            f"must declare the NRCES {artifact} profile")
    for path, message in [
        ("status", "Composition.status is 1..1"),
        ("type", "Composition.type is 1..1"),
        ("date", "Composition.date is 1..1"),
        ("title", "Composition.title is 1..1"),
        ("subject.reference", "Composition.subject.reference is mandatory"),
        ("author.reference", "Composition.author.reference is mandatory"),
        ("section", "Composition.section is 1..*"),
    ]:
        if not _present(composition, path):
            add(ERROR, f"Composition.{path}", message)

    if artifact in ("OPConsultRecord", "DischargeSummaryRecord"):
        if not _present(composition, "encounter"):
            add(ERROR, "Composition.encounter",
                f"{artifact}.Composition.encounter is 1..1")

    if artifact == "InvoiceRecord":
        if _walk(composition, "type.text") != ["Invoice Record"]:
            add(ERROR, "Composition.type.text",
                "InvoiceRecord fixes Composition.type.text to 'Invoice Record'")
    elif artifact == "WellnessRecord":
        if _walk(composition, "type.text") != ["Wellness Record"]:
            add(ERROR, "Composition.type.text",
                "WellnessRecord fixes Composition.type.text to 'Wellness Record'")
    elif artifact in COMPOSITION_TYPE:
        fixed = COMPOSITION_TYPE[artifact]
        codes = _walk(composition, "type.coding.code")
        if fixed["code"] not in codes:
            add(ERROR, "Composition.type.coding.code",
                f"{artifact} fixes Composition.type.coding.code to {fixed['code']} "
                f"(found {codes})")

    if artifact == "WellnessRecord":
        # Sections are sliced on a fixed title and carry no code at all.
        for section in composition.get("section", []):
            title = section.get("title")
            if title not in WELLNESS_SECTION_TITLES:
                add(ERROR, "Composition.section.title",
                    f"{title!r} is not one of the WellnessRecord section titles")
            if not section.get("entry"):
                add(ERROR, "Composition.section.entry",
                    f"the {title} section needs at least one entry")
            for ref in _walk(section, "entry.reference"):
                if not ref:
                    add(ERROR, "Composition.section.entry.reference",
                        "WellnessRecord section entries need a reference")

    table = SECTION_TABLES.get(artifact)
    if table:
        allowed = {code for _title, code, _display in table.values()}
        for section in composition.get("section", []):
            for code in _walk(section, "code.coding.code"):
                if code not in allowed:
                    add(WARNING, "Composition.section.code.coding.code",
                        f"section code {code} is not one of the {artifact} slices")
    if artifact in ("DiagnosticReportRecord", "InvoiceRecord"):
        sections = composition.get("section", [])
        if len(sections) != 1:
            add(ERROR, "Composition.section",
                f"{artifact}.Composition.section is 1..1 (found {len(sections)})")
        for section in sections:
            if not section.get("entry"):
                add(ERROR, "Composition.section.entry",
                    "Composition.section.entry is 1..*")
    return issues


def _dangling_references(node: Any, known: Iterable[str]) -> set[str]:
    known = set(known)
    missing: set[str] = set()

    def visit(value: Any) -> None:
        if isinstance(value, dict):
            target = value.get("reference")
            if isinstance(target, str) and target.startswith("urn:uuid:") \
                    and target not in known:
                missing.add(target)
            for child in value.values():
                visit(child)
        elif isinstance(value, list):
            for child in value:
                visit(child)

    visit(node)
    return missing
