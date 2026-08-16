"""Code systems and canonical URLs used by the ABDM / NRCES FHIR R4 profiles.

Everything here was taken from the published StructureDefinitions at
https://www.nrces.in/ndhm/fhir/r4/profiles.html — the `fixedUri` values in those
profiles are not negotiable, so they live in one place.
"""

from __future__ import annotations

# ------------------------------------------------------------ canonical bases
NRCES_SD = "https://nrces.in/ndhm/fhir/r4/StructureDefinition"
NRCES_CS = "https://nrces.in/ndhm/fhir/r4/CodeSystem"
NRCES_VS = "https://nrces.in/ndhm/fhir/r4/ValueSet"

# ------------------------------------------------------------------- systems
SNOMED = "http://snomed.info/sct"
LOINC = "http://loinc.org"
ICD10 = "http://hl7.org/fhir/sid/icd-10"
UCUM = "http://unitsofmeasure.org"
V2_0203 = "http://terminology.hl7.org/CodeSystem/v2-0203"
V3_ACT_CODE = "http://terminology.hl7.org/CodeSystem/v3-ActCode"
V3_MARITAL = "http://terminology.hl7.org/CodeSystem/v3-MaritalStatus"
V3_ROLE_CODE = "http://terminology.hl7.org/CodeSystem/v3-RoleCode"
ORG_TYPE = "http://terminology.hl7.org/CodeSystem/organization-type"
OBS_CATEGORY = "http://terminology.hl7.org/CodeSystem/observation-category"
COND_CLINICAL = "http://terminology.hl7.org/CodeSystem/condition-clinical"
COND_VERIFICATION = "http://terminology.hl7.org/CodeSystem/condition-ver-status"
COND_CATEGORY = "http://terminology.hl7.org/CodeSystem/condition-category"
ALLERGY_CLINICAL = "http://terminology.hl7.org/CodeSystem/allergyintolerance-clinical"
DIAGNOSTIC_SERVICE = "http://terminology.hl7.org/CodeSystem/v2-0074"
INTERPRETATION = "http://terminology.hl7.org/CodeSystem/v3-ObservationInterpretation"
DISCHARGE_DISPOSITION = "http://terminology.hl7.org/CodeSystem/discharge-disposition"
ENCOUNTER_DIAGNOSIS_ROLE = (
    "http://terminology.hl7.org/CodeSystem/diagnosis-role"
)

# NRCES-specific code systems
NDHM_IDENTIFIER_TYPE = f"{NRCES_CS}/ndhm-identifier-type-code"
NDHM_PRICE_COMPONENTS = f"{NRCES_CS}/ndhm-price-components"
NDHM_BILLING_CODES = f"{NRCES_CS}/ndhm-billing-codes"

# ------------------------------------------------------------------ profiles
PROFILE = {
    "Patient": f"{NRCES_SD}/Patient",
    "Practitioner": f"{NRCES_SD}/Practitioner",
    "PractitionerRole": f"{NRCES_SD}/PractitionerRole",
    "Organization": f"{NRCES_SD}/Organization",
    "Encounter": f"{NRCES_SD}/Encounter",
    "Condition": f"{NRCES_SD}/Condition",
    "Observation": f"{NRCES_SD}/Observation",
    "ObservationVitalSigns": f"{NRCES_SD}/ObservationVitalSigns",
    "ObservationBodyMeasurement": f"{NRCES_SD}/ObservationBodyMeasurement",
    "ObservationPhysicalActivity": f"{NRCES_SD}/ObservationPhysicalActivity",
    "ObservationGeneralAssessment": f"{NRCES_SD}/ObservationGeneralAssessment",
    "ObservationWomenHealth": f"{NRCES_SD}/ObservationWomenHealth",
    "ObservationLifestyle": f"{NRCES_SD}/ObservationLifestyle",
    "AllergyIntolerance": f"{NRCES_SD}/AllergyIntolerance",
    "MedicationRequest": f"{NRCES_SD}/MedicationRequest",
    "Procedure": f"{NRCES_SD}/Procedure",
    "ServiceRequest": f"{NRCES_SD}/ServiceRequest",
    "Specimen": f"{NRCES_SD}/Specimen",
    "DiagnosticReportLab": f"{NRCES_SD}/DiagnosticReportLab",
    "Invoice": f"{NRCES_SD}/Invoice",
    "ChargeItem": f"{NRCES_SD}/ChargeItem",
    "CarePlan": f"{NRCES_SD}/CarePlan",
    "DocumentReference": f"{NRCES_SD}/DocumentReference",
    "Appointment": f"{NRCES_SD}/Appointment",
    "DocumentBundle": f"{NRCES_SD}/DocumentBundle",
    # Composition-based clinical artifacts
    "OPConsultRecord": f"{NRCES_SD}/OPConsultRecord",
    "DischargeSummaryRecord": f"{NRCES_SD}/DischargeSummaryRecord",
    "DiagnosticReportRecord": f"{NRCES_SD}/DiagnosticReportRecord",
    "InvoiceRecord": f"{NRCES_SD}/InvoiceRecord",
    "PrescriptionRecord": f"{NRCES_SD}/PrescriptionRecord",
    "HealthDocumentRecord": f"{NRCES_SD}/HealthDocumentRecord",
    "WellnessRecord": f"{NRCES_SD}/WellnessRecord",
}

# --------------------------------------------------- Composition.type codings
# Fixed by the profiles — see StructureDefinition-<artifact>.json differentials.
COMPOSITION_TYPE = {
    "OPConsultRecord": {
        "system": SNOMED,
        "code": "371530004",
        "display": "Clinical consultation report",
    },
    "DischargeSummaryRecord": {
        "system": SNOMED,
        "code": "373942005",
        "display": "Discharge summary",
    },
    "DiagnosticReportRecord": {
        "system": SNOMED,
        "code": "721981007",
        "display": "Diagnostic studies report",
    },
    "InvoiceRecord": {
        "system": SNOMED,
        "code": "721981007",
        "display": "Diagnostic studies report",
    },
    "PrescriptionRecord": {
        "system": SNOMED,
        "code": "440545006",
        "display": "Prescription record",
    },
}

COMPOSITION_TITLE = {
    "OPConsultRecord": "OP Consultation Record",
    "DischargeSummaryRecord": "Discharge Summary",
    "DiagnosticReportRecord": "Diagnostic Report",
    "InvoiceRecord": "Invoice Record",
    "PrescriptionRecord": "Prescription Record",
    "WellnessRecord": "Wellness Record",
}

# -------------------------------------------------- Composition.section codes
# key -> (title, {system, code, display}) exactly as fixed in the differentials.
OP_SECTIONS = {
    "ChiefComplaints": ("Chief complaints", "422843007", "Chief complaint section"),
    "PhysicalExamination": ("Physical examination", "425044008", "Physical exam section"),
    "Allergies": ("Allergies", "722446000", "Allergy record"),
    "MedicalHistory": ("Medical history", "371529009", "History and physical report"),
    "FamilyHistory": ("Family history", "422432008", "Family history section"),
    "InvestigationAdvice": ("Investigation advice", "721963009", "Order document"),
    "Medications": ("Medications", "721912009", "Medication summary document"),
    "FollowUp": ("Follow up", "390906007", "Follow-up encounter"),
    "Procedure": ("Procedure", "371525003", "Clinical procedure report"),
    "Referral": ("Referral", "306206005", "Referral to service"),
    "OtherObservations": ("Other observations", "404684003", "Clinical finding"),
    "DocumentReference": ("Document reference", "371530004", "Clinical consultation report"),
}

DS_SECTIONS = {
    "ChiefComplaints": ("Chief complaints", "422843007", "Chief complaint section"),
    "PhysicalExamination": ("Physical examination", "425044008", "Physical exam section"),
    "Allergies": ("Allergies", "722446000", "Allergy record"),
    "MedicalHistory": ("Medical history", "1003642006", "Past medical history section"),
    "FamilyHistory": ("Family history", "422432008", "Family history section"),
    "Investigations": ("Investigations", "721981007", "Diagnostic studies report"),
    "Medications": ("Medications", "1003606003", "Medication history section"),
    "Procedures": ("Procedures", "1003640003", "History of past procedure section"),
    "CarePlan": ("Care plan", "734163000", "Care plan"),
    "DocumentReference": ("Document reference", "373942005", "Discharge summary"),
}

# WellnessRecord slices its sections by a fixed title string and constrains no
# section code — the one record profile in the guide that works that way.
WELLNESS_SECTION_TITLES = [
    "Vital Signs", "Body Measurement", "Physical Activity", "General Assessment",
    "Women Health", "Lifestyle", "Other Observations", "Document Reference",
]

DIAGNOSTIC_SECTION = ("Diagnostic Report", "721981007", "Diagnostic studies report")
INVOICE_SECTION = ("Invoice", "721981007", "Diagnostic studies report")

# ------------------------------------------------------ ndhm-price-components
PRICE_COMPONENT = {
    "mrp": ("00", "MRP"),
    "rate": ("01", "Rate"),
    "discount": ("02", "Discount"),
    "cgst": ("03", "CGST"),
    "sgst": ("04", "SGST"),
}

# --------------------------------------- ndhm-invoice-types (billing artifact)
INVOICE_TYPE = {
    "00": "Consultation",
    "01": "Pharmacy",
    "02": "IPD",
    "03": "OPD",
    "99": "Others",
}

# ------------------------------------------------ identifier type conveniences
IDENTIFIER_TYPE = {
    "MR": (V2_0203, "MR", "Medical record number"),
    "PRN": (V2_0203, "PRN", "Provider number"),
    "MD": (V2_0203, "MD", "Medical License number"),
    "ABHA": (NDHM_IDENTIFIER_TYPE, "ABHA", "Ayushman Bharat Health Account (ABHA) ID"),
    "HIN": (NDHM_IDENTIFIER_TYPE, "HIN", "Health ID issued by NDHM"),
    "HPID": (NDHM_IDENTIFIER_TYPE, "HPID", "Healthcare Professional ID (HPID)"),
    "NHRR": (NDHM_IDENTIFIER_TYPE, "NHRR", "National Health Resource Repository (NHRR) ID"),
    "ROHINI": (NDHM_IDENTIFIER_TYPE, "ROHINI", "Registry of Hospitals in Network of Insurance (ROHINI) ID"),
    "OIN": (NDHM_IDENTIFIER_TYPE, "OIN", "Other identifier"),
}

# ------------------------------------------------------------- encounter class
ENCOUNTER_CLASS = {
    "AMB": "ambulatory",
    "IMP": "inpatient encounter",
    "EMER": "emergency",
}
