# Features, screen by screen

Everything below is driven by the sidebar's six groups. Every "New …" screen is
reached from a primary button on the list page it belongs to — the sidebar lists
destinations, not actions.

## Front desk

**Patient directory** (`/patients`) — search by name, MRN, phone or ABHA;
register with identity, contact & optional ABHA number/address, demographics,
address and emergency contact. MRNs are allocated automatically (`MRN00001…`).

**Patient chart** (`/patients/<id>`) — five tabs, each of which captures as
well as displays; deep-link with `?tab=overview|encounters|problems|lab|billing`.

* **Overview** — demographics plus **vitals**: a snapshot of the last recording
  with high/low flags, a capture form, and history grouped by recording. Nine
  LOINC-coded signs; BMI derives from height and weight when not typed. A
  recording attached to an encounter enters that encounter's FHIR document;
  left unattached it stays on the chart.
* **Problems & allergies** — the problem list (SNOMED + ICD-10 dual-coded) and
  the allergy register (SNOMED allergens, category, criticality, coded or
  free-text reaction). A high-risk allergy raises a banner. Chart-level
  problems reach the *Medical history* section of every document generated for
  the patient.
* **Encounters / Laboratory / Billing** — the patient's history in each area,
  with shortcuts to start the next thing.

**Appointments** (`/appointments`) — booking issues a per-doctor token for the
day; the queue moves booked → arrived → seen, and *Start visit* creates the OPD
encounter carrying the doctor, department and fee across.

## Clinical

**OPD** (`/opd`) — registration then the consultation note: chief complaints,
vitals and examination, diagnoses (dual-coded), prescription with SNOMED
medicines/routes/instructions, investigation advice (orders lab panels),
referral and follow-up. Repeating rows are add/remove blocks.

**Ward board** (`/beds`) — every bed, its status
(vacant/occupied/cleaning/blocked) and occupant, with cleaning and blocking
controls. Admission picks a *vacant bed* honouring each ward's gender policy.
Every stay writes a `bed_movement` ledger; **room charges bill from that
ledger**, so a patient stepped down from ICU bills at each ward's own tariff.
Transfers mid-stay; discharge releases the bed to cleaning.

**IPD** (`/ipd`) — admission, the in-patient clinical record (complaints,
vitals, diagnoses, procedures, medication), the stay/room-charges card, and a
structured discharge summary (course in hospital, condition at discharge,
instructions, diet, care plan, follow-up).

**Wellness records** (`/wellness`) — the periodic, largely self-reported
picture ABDM expects in a PHR: vitals, body measurement, physical activity,
general assessment, women's health and lifestyle. Captured section by section
as a draft, then signed; only signed records export. Every concept offered is a
member of the ValueSet its NRCES profile binds to. One wellness record is also
**generated automatically per completed dialysis run** — see
[fhir.md](fhir.md#wellnessrecord).

## Dialysis

A **course** is the standing prescription: modality, vascular access and site,
sessions per week, duration, dry weight, dialyser, Qb/Qd, anticoagulation, the
dialysate bath (Na/K/Ca/HCO₃) and the per-session tariff. Courses are tabbed
*Active / Closed & completed / All* and can be closed and reopened.

A **session** is one run against a course. It inherits the prescription, takes
a named machine (which no other patient can take mid-run, and which cannot be
sent for service while running), and records what was actually delivered — Qb,
Qd, dialysate temperature and conductivity, dialyser and reuse count, heparin,
UF goal, Kt/V, URR, complications. *Create session* starts from the active
course list, so a run can never be orphaned from a prescription, and can start
the run in the same step.

The **flowsheet** captures pre- and post-dialysis vitals as ordinary LOINC
observations (range-flagged, exportable); ultrafiltration derives from
pre-minus-post weight unless typed. The **intra-dialytic chart** is the
half-hourly BP, pulse, Qb, arterial/venous/TMP pressures and cumulative UF that
nursing records — kept as a plain table because a dozen rows per run would
swamp an exported bundle.

Completing a run writes a SNOMED `Procedure` against the encounter (how it
reaches the discharge summary and the bill), frees the machine, and generates
the wellness record. A managed complication downgrades the outcome to
*partially successful*; a session bills once from the course tariff, never
twice as a theatre procedure.

The **machine register** (`/dialysis/machines`) tracks model, serial, location,
status, current occupant and service dates.

## Diagnostics

**Laboratory** (`/lab`) — order a LOINC-coded panel and it expands into its
analytes with units and reference ranges. Result entry flags each value
high/low automatically; an abnormal set drives the SNOMED conclusion code.
Preliminary vs final; only final reports export. `resultsInterpreter` is
mandatory at ordering because the NRCES profile requires it.

**Pharmacy** (`/pharmacy`) — drugs and consumables with MRP, purchase price,
GST rate, HSN and reorder level. Stock arrives as batches with expiry; issues
consume **first-expiry-first-out** and refuse to go negative. Issues against an
encounter are picked up automatically when it is billed, GST split into
CGST/SGST. Reorder and 90-day expiry alerts surface here and on the dashboard.

## Finance

**Billing** (`/billing`) — invoices with line items, discount and CGST/SGST.
IPD/OPD bills pre-fill from what the encounter consumed: bed ledger, nursing,
consultations, procedures, lab orders, unbilled pharmacy issues, unbilled
dialysis sessions. Part payments each get a numbered receipt with mode and
reference; overpayment is refused; an invoice flips to `balanced` when settled.

**Reports** (`/reports`) — date-ranged MIS: OPD footfall, admissions,
discharges, registrations, live bed occupancy, billed vs collected, collections
by mode, top diagnoses, lab workload, pharmacy consumption, stock alerts.
`/reports/dues` lists every unpaid invoice with the patient's phone number.

## Facility

**FHIR export centre** (`/fhir`) — deliberately **the only place FHIR
appears**. Build any of the five artifacts from its source record, inspect the
bundle resource by resource, read the validation report, download the JSON.
See [fhir.md](fhir.md).

**Masters** (`/masters`) — doctors & staff, the pharmacy catalogue, wards &
beds, and all 41 clinical code pickers in one editor, plus the
clear-patient-data reset. See [operations.md](operations.md).

**Settings** (`/settings`) — facility name, HFR ID and identifier type, NHCX
participant ID, address, contact, GSTIN. These travel in every exported bundle
as the document custodian.
