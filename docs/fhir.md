# FHIR: the NRCES / ABDM conformance story

NanoEMR exports five clinical artifacts, each a `Bundle` of type `document`
carrying the NRCES `DocumentBundle` profile, whose first entry is the profiled
`Composition`.

| Artifact | Built from | Canonical profile |
|---|---|---|
| **OPConsultRecord** | an OPD encounter + its note | `https://nrces.in/ndhm/fhir/r4/StructureDefinition/OPConsultRecord` |
| **DischargeSummaryRecord** | a discharged IPD admission | `…/DischargeSummaryRecord` |
| **DiagnosticReportRecord** | a finalised lab order | `…/DiagnosticReportRecord` |
| **InvoiceRecord** | an invoice | `…/InvoiceRecord` |
| **WellnessRecord** | a signed wellness record | `…/WellnessRecord` |

Supporting resources are profiled too: `Patient`, `Practitioner`,
`Organization`, `Encounter`, `Condition`, `Observation` (plus its wellness
variants), `AllergyIntolerance`, `MedicationRequest`, `Procedure`,
`ServiceRequest`, `Specimen`, `DiagnosticReportLab`, `Invoice`, `CarePlan`,
`Appointment`.

## How conformance was established

The fixed values in `emr/terminology.py` were **extracted from the published
StructureDefinition JSON**, not guessed. The constraints the exporter honours
include:

* `Composition.type.coding` is fixed per artifact — `371530004` *Clinical
  consultation report* for the OP consult, `373942005` *Discharge summary* for
  the discharge summary. `InvoiceRecord` and `WellnessRecord` instead fix
  `Composition.type.text` to the literal strings `"Invoice Record"` and
  `"Wellness Record"`.
* Every OP-consult and discharge-summary **section slice has a fixed SNOMED
  code** — chief complaints `422843007`, physical examination `425044008`,
  medications `721912009` (OP) vs `1003606003` (discharge), investigations
  `721981007`, care plan `734163000`, and so on.
* `DiagnosticReportLab` pins `category.coding.system` to SNOMED CT and
  `code.coding.system` to LOINC, and makes `result`, `resultsInterpreter` and
  `conclusion` mandatory — all three are enforced in the UI, not just at
  export.
* `Specimen.receivedTime` and `Specimen.collection.collected[x]` are 1..1.
* `MedicationRequest` requires `authoredOn`, `requester`,
  `dosageInstruction`, and pins `medication[x].coding.system` to SNOMED CT.
* `Invoice` requires `identifier`, `type`, `subject`, `date`, `lineItem`,
  `totalNet`, `totalGross`; price components are coded from
  `ndhm-price-components` (`01` Rate, `02` Discount, `03` CGST, `04` SGST) and
  the invoice type from `ndhm-invoice-types`.
* `DocumentBundle` requires `meta.versionId`, `identifier.system` + `.value`,
  `type = document` and `timestamp`.
* `Condition.code` is sliced into ICD-10 and SNOMED CT codings;
  `Observation.code` into LOINC and SNOMED CT — and that slicing is **closed**,
  which drives a design decision described below.
* `Patient.identifier` is typed: MRN as `MR` (HL7 v2-0203), ABHA number as
  `ABHA` and ABHA address as `HIN` (NDHM identifier code system).

## WellnessRecord

The odd one out, in two ways the exporter and validator handle specially:

1. It slices its **sections by a fixed `title` string** ("Vital Signs",
   "Body Measurement", …) and constrains no section `code` at all — every other
   record profile pins SNOMED section codes.
2. Each section's observations carry that section's own profile
   (`ObservationVitalSigns`, `ObservationBodyMeasurement`,
   `ObservationPhysicalActivity`, `ObservationGeneralAssessment`,
   `ObservationWomenHealth`, `ObservationLifestyle`), and the value follows
   what each permits: a `Quantity` where there is a unit, a `string` where
   there is not, and for Lifestyle a `valueCodeableConcept` **only**.

Every concept the capture screens offer is a member of the ValueSet its
profile binds (`ndhm-vital-signs`, `ndhm-body-measurement`,
`ndhm-physical-activity`, `ndhm-general-assessment`, `ndhm-women-health`,
`ndhm-lifestyle`) — pulled from the published expansions, not hand-picked.

**Generated from dialysis.** Completing a dialysis run generates one signed
wellness record: post-dialysis vitals to *Vital Signs* (SpO₂ remapped
`59408-5 → 2708-6` to match the bound set; blood pressure as the `85354-9`
panel code), post weight (and derived BMI) to *Body Measurement*, and the
entire dialysis parameter set to *Other Observations*. That last placement is
forced by the profiles: `Observation.code.coding` is sliced **closed** to LOINC
and SNOMED CT, dialysis parameters (Qb, Qd, TMP, conductivity, Kt/V…) have no
verifiable concept in either, so they are carried as **text-only
`CodeableConcept`s** — legal, and honest about what is and is not coded — and
*Other Observations* is the one section whose target is the base `Observation`
profile that accepts them. Generation is atomic (one transaction, nothing
partial survives a failure), idempotent (re-completing never duplicates), and
never fatal to the completion itself; generated records are machine-written,
so they cannot be hand-edited — *Regenerate* rebuilds them from the run.

## Design decisions worth knowing

* **Deterministic identity.** Resource `fullUrl`s are `urn:uuid:` values
  derived (UUIDv5) from the record's type and id, so re-exporting the same
  encounter produces byte-stable references instead of churning every build.
* **Vitals use the base Observation profile** in encounter documents, because
  that is what the `PhysicalExamination` section slice targets.
* **An OP consult has no diagnosis section** in the profile, so confirmed
  diagnoses ride in *Medical history* alongside past history.
* **Wellness data stays out of encounter documents** even when a record is
  linked to a visit — it belongs to the WellnessRecord artifact, not the
  consultation note.
* **The intra-dialytic monitoring chart is not exported.** Nursing
  surveillance at a dozen rows per run would swamp a bundle for no clinical
  gain; the run's summary travels as a `Procedure` note instead.
* **FHIR is confined to `/fhir`.** No clinical or billing screen carries an
  export button or bundle list; whoever handles ABDM submission works in one
  place.

## The built-in validator

`emr/fhir/validator.py` encodes the mandatory elements and fixed codings above
and runs on every export; the report is stored with the bundle and rendered on
the export screen (severity, FHIR path, explanation per issue). The test suite
proves it is not vacuously passing by deliberately breaking bundles — dropped
timestamps, wrong types, foreign section codes, dangling references — and
asserting each is caught.

It is a fast in-app gate, **not** a replacement for the official validator:

```bash
java -jar validator_cli.jar bundle.json -version 4.0.1 -ig ndhm.fhir.r4#<version>
```
