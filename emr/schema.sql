-- NanoEMR — SQLite schema
-- Column names are deliberately aligned with the NRCES / ABDM FHIR R4 profiles
-- (https://www.nrces.in/ndhm/fhir/r4/profiles.html) so that the export layer is a
-- near mechanical mapping rather than a guessing game.

PRAGMA foreign_keys = ON;

-- ---------------------------------------------------------------- terminology
-- Small local master of the SNOMED CT / LOINC / ICD-10 codes the app needs.
-- Rows are grouped by `kind`, one kind per picker (diagnosis, medicine,
-- lab_panel, dialysis_modality, wellness_vital, …). The authoritative list
-- lives in code, where it cannot drift: emr/seed.py defines the kinds and
-- masters.CODE_GROUPS labels every one of them for the /masters/codes editor.
CREATE TABLE IF NOT EXISTS terminology (
    id          INTEGER PRIMARY KEY,
    kind        TEXT NOT NULL,
    system      TEXT NOT NULL,
    code        TEXT NOT NULL,
    display     TEXT NOT NULL,
    -- optional secondary coding (e.g. ICD-10 next to SNOMED for a diagnosis)
    alt_system  TEXT,
    alt_code    TEXT,
    alt_display TEXT,
    unit        TEXT,
    ref_low     REAL,
    ref_high    REAL,
    extra       TEXT,
    sort_order  INTEGER NOT NULL DEFAULT 100,
    UNIQUE (kind, system, code)
);
CREATE INDEX IF NOT EXISTS ix_terminology_kind ON terminology (kind, sort_order);

-- --------------------------------------------------------------- organization
CREATE TABLE IF NOT EXISTS organization (
    id                    INTEGER PRIMARY KEY,
    name                  TEXT NOT NULL,
    -- Organization.identifier (1..*) with a typed identifier (ndhm-identifier-type-code)
    identifier_type_system  TEXT NOT NULL,
    identifier_type_code    TEXT NOT NULL,
    identifier_type_display TEXT NOT NULL,
    identifier_system     TEXT NOT NULL,
    identifier_value      TEXT NOT NULL,        -- the HFR / facility ID
    participant_code      TEXT,                 -- NHCX / HCX participant ID
    -- Organization.type.coding fixed to hl7 organization-type
    type_code             TEXT NOT NULL DEFAULT 'prov',
    type_display          TEXT NOT NULL DEFAULT 'Healthcare Provider',
    phone                 TEXT,
    email                 TEXT,
    address_line          TEXT,
    city                  TEXT,
    district              TEXT,
    state                 TEXT,
    postal_code           TEXT,
    country               TEXT NOT NULL DEFAULT 'India',
    gstin                 TEXT,
    is_default            INTEGER NOT NULL DEFAULT 0
);

-- --------------------------------------------------------------- practitioner
CREATE TABLE IF NOT EXISTS practitioner (
    id                      INTEGER PRIMARY KEY,
    name                    TEXT NOT NULL,           -- Practitioner.name.text (1..1 when name present)
    prefix                  TEXT,
    gender                  TEXT,
    identifier_type_system  TEXT NOT NULL,
    identifier_type_code    TEXT NOT NULL,           -- e.g. HPID / MD (registration no.)
    identifier_type_display TEXT NOT NULL,
    identifier_system       TEXT NOT NULL,
    identifier_value        TEXT NOT NULL,
    qualification_code      TEXT,
    qualification_display   TEXT,
    qualification_system    TEXT,
    -- PractitionerRole.code / .specialty are SNOMED-fixed in the NRCES profile
    role_code               TEXT,
    role_display            TEXT,
    specialty_code          TEXT,
    specialty_display       TEXT,
    department              TEXT,
    phone                   TEXT,
    email                   TEXT,
    consultation_fee        REAL NOT NULL DEFAULT 0,
    active                  INTEGER NOT NULL DEFAULT 1
);

-- -------------------------------------------------------------------- patient
CREATE TABLE IF NOT EXISTS patient (
    id                    INTEGER PRIMARY KEY,
    mrn                   TEXT NOT NULL UNIQUE,      -- Patient.identifier (type MR)
    abha_number           TEXT,                      -- Patient.identifier (type ABHA)
    abha_address          TEXT,
    name                  TEXT NOT NULL,             -- Patient.name.text
    given_name            TEXT,
    family_name           TEXT,
    gender                TEXT NOT NULL,             -- male | female | other | unknown
    birth_date            TEXT,                      -- YYYY-MM-DD
    age_years             INTEGER,                   -- captured when DOB unknown
    phone                 TEXT NOT NULL,             -- Patient.telecom.value (1..1 when telecom present)
    email                 TEXT,
    marital_status_code   TEXT,
    marital_status_display TEXT,
    blood_group           TEXT,
    address_line          TEXT,
    city                  TEXT,
    district              TEXT,
    state                 TEXT,
    postal_code           TEXT,
    country               TEXT NOT NULL DEFAULT 'India',
    contact_name          TEXT,
    contact_relation      TEXT,
    contact_phone         TEXT,
    managing_org_id       INTEGER REFERENCES organization (id),
    deceased              INTEGER NOT NULL DEFAULT 0,
    created_at            TEXT NOT NULL,
    updated_at            TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_patient_name ON patient (name);
CREATE INDEX IF NOT EXISTS ix_patient_phone ON patient (phone);

-- ------------------------------------------------------------------ encounter
-- Carries both the OPD visit and the IPD admission. kind separates them.
CREATE TABLE IF NOT EXISTS encounter (
    id                        INTEGER PRIMARY KEY,
    encounter_no              TEXT NOT NULL UNIQUE,
    patient_id                INTEGER NOT NULL REFERENCES patient (id),
    kind                      TEXT NOT NULL,          -- OPD | IPD
    status                    TEXT NOT NULL,          -- planned|arrived|in-progress|finished|cancelled
    class_code                TEXT NOT NULL,          -- AMB | IMP | EMER  (v3-ActCode)
    class_display             TEXT NOT NULL,
    type_code                 TEXT,                   -- Encounter.type.coding (SNOMED)
    type_display              TEXT,
    type_system               TEXT,
    service_type_code         TEXT,
    service_type_display      TEXT,
    service_type_system       TEXT,
    priority_code             TEXT,
    priority_display          TEXT,
    priority_system           TEXT,
    practitioner_id           INTEGER REFERENCES practitioner (id),
    department                TEXT,
    period_start              TEXT NOT NULL,
    period_end                TEXT,
    reason_code               TEXT,                   -- SNOMED (fixed system in profile)
    reason_display            TEXT,
    -- IPD only
    admission_type            TEXT,
    bed_id                    INTEGER REFERENCES bed (id),
    ward                      TEXT,                   -- denormalised for Encounter.location
    bed                       TEXT,
    bed_rate                  REAL NOT NULL DEFAULT 0,
    discharge_disposition_code    TEXT,
    discharge_disposition_display TEXT,
    discharge_ts              TEXT,
    -- OPD only
    consultation_fee          REAL NOT NULL DEFAULT 0,
    appointment_slot          TEXT,
    created_at                TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_encounter_patient ON encounter (patient_id, period_start DESC);
CREATE INDEX IF NOT EXISTS ix_encounter_kind ON encounter (kind, status);

-- ------------------------------------------------------------------ condition
CREATE TABLE IF NOT EXISTS condition (
    id                  INTEGER PRIMARY KEY,
    patient_id          INTEGER NOT NULL REFERENCES patient (id),
    encounter_id        INTEGER REFERENCES encounter (id),
    category            TEXT NOT NULL,      -- chief-complaint | diagnosis | medical-history
    clinical_status     TEXT NOT NULL DEFAULT 'active',
    verification_status TEXT NOT NULL DEFAULT 'confirmed',
    snomed_code         TEXT,               -- Condition.code.coding:SNOMEDCT
    snomed_display      TEXT,
    icd10_code          TEXT,               -- Condition.code.coding:ICD-10
    icd10_display       TEXT,
    text                TEXT NOT NULL,
    onset               TEXT,
    severity_code       TEXT,
    severity_display    TEXT,
    note                TEXT,
    recorded_at         TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_condition_enc ON condition (encounter_id, category);

-- ---------------------------------------------------------------- observation
-- Vitals / physical examination findings and (via lab_order_id) lab analytes.
CREATE TABLE IF NOT EXISTS observation (
    id                 INTEGER PRIMARY KEY,
    patient_id         INTEGER NOT NULL REFERENCES patient (id),
    encounter_id       INTEGER REFERENCES encounter (id),
    lab_order_id       INTEGER REFERENCES lab_order (id) ON DELETE CASCADE,
    -- pre/post dialysis vitals reuse this table so they stay LOINC-coded,
    -- range-flagged and exportable like every other observation
    dialysis_session_id INTEGER REFERENCES dialysis_session (id) ON DELETE CASCADE,
    phase              TEXT,                -- pre | post (dialysis vitals)
    wellness_record_id INTEGER REFERENCES wellness_record (id) ON DELETE CASCADE,
    -- Observation.valueCodeableConcept; the NRCES Lifestyle profile allows only
    -- a coded value, and General Assessment allows Quantity or CodeableConcept
    value_system       TEXT,
    value_code         TEXT,
    value_display      TEXT,
    -- Observation.code.text for concepts with no LOINC/SNOMED code. The NRCES
    -- Observation profile closes code.coding to those two systems, so a local
    -- coding would be a violation — a text-only CodeableConcept is the legal way.
    code_text          TEXT,
    category           TEXT NOT NULL,       -- vital-signs | exam | laboratory
    status             TEXT NOT NULL DEFAULT 'final',
    loinc_code         TEXT,                -- Observation.code.coding:LOINC
    loinc_display      TEXT,
    snomed_code        TEXT,                -- Observation.code.coding:SNOMEDCT
    snomed_display     TEXT,
    value_quantity     REAL,
    value_unit         TEXT,
    value_string       TEXT,
    ref_low            REAL,
    ref_high           REAL,
    interpretation     TEXT,                -- N | H | L | A
    body_site_code     TEXT,
    body_site_display  TEXT,
    note               TEXT,
    effective_ts       TEXT NOT NULL,
    sort_order         INTEGER NOT NULL DEFAULT 100
);
CREATE INDEX IF NOT EXISTS ix_obs_enc ON observation (encounter_id, category);
CREATE INDEX IF NOT EXISTS ix_obs_lab ON observation (lab_order_id);

-- ----------------------------------------------------------- allergy (OP/DS)
CREATE TABLE IF NOT EXISTS allergy (
    id              INTEGER PRIMARY KEY,
    patient_id      INTEGER NOT NULL REFERENCES patient (id),
    encounter_id    INTEGER REFERENCES encounter (id),
    snomed_code     TEXT,
    snomed_display  TEXT,
    text            TEXT NOT NULL,
    category        TEXT,                   -- food | medication | environment
    criticality     TEXT,                   -- low | high | unable-to-assess
    clinical_status TEXT NOT NULL DEFAULT 'active',
    reaction        TEXT,
    recorded_at     TEXT NOT NULL
);

-- --------------------------------------------------------- medication request
CREATE TABLE IF NOT EXISTS medication_request (
    id                      INTEGER PRIMARY KEY,
    patient_id              INTEGER NOT NULL REFERENCES patient (id),
    encounter_id            INTEGER REFERENCES encounter (id),
    status                  TEXT NOT NULL DEFAULT 'active',
    intent                  TEXT NOT NULL DEFAULT 'order',
    snomed_code             TEXT NOT NULL,   -- MedicationRequest.medicationCodeableConcept (SNOMED fixed)
    snomed_display          TEXT NOT NULL,
    dose_quantity           REAL,
    dose_unit               TEXT,
    route_code              TEXT,
    route_display           TEXT,
    method_code             TEXT,
    method_display          TEXT,
    frequency               INTEGER,         -- times per period
    period                  REAL,
    period_unit             TEXT,            -- d | h
    duration_days           INTEGER,
    timing_text             TEXT,            -- "1-0-1 for 5 days"
    additional_code         TEXT,            -- dosageInstruction.additionalInstruction (SNOMED)
    additional_display      TEXT,
    reason_code             TEXT,
    reason_display          TEXT,
    note                    TEXT,
    authored_on             TEXT NOT NULL,
    requester_id            INTEGER REFERENCES practitioner (id),
    sort_order              INTEGER NOT NULL DEFAULT 100
);
CREATE INDEX IF NOT EXISTS ix_medreq_enc ON medication_request (encounter_id);

-- ------------------------------------------------------------------ procedure
CREATE TABLE IF NOT EXISTS procedure (
    id                 INTEGER PRIMARY KEY,
    patient_id         INTEGER NOT NULL REFERENCES patient (id),
    encounter_id       INTEGER REFERENCES encounter (id),
    -- set when the row was written by a completed dialysis run, so the generic
    -- procedure charge does not bill on top of the per-session dialysis charge
    dialysis_session_id INTEGER,
    status             TEXT NOT NULL DEFAULT 'completed',
    snomed_code        TEXT NOT NULL,        -- Procedure.code.coding (SNOMED fixed)
    snomed_display     TEXT NOT NULL,
    category_code      TEXT,
    category_display   TEXT,
    body_site_code     TEXT,
    body_site_display  TEXT,
    outcome_code       TEXT,
    outcome_display    TEXT,
    performed_ts       TEXT,
    performer_id       INTEGER REFERENCES practitioner (id),
    note               TEXT
);
CREATE INDEX IF NOT EXISTS ix_proc_enc ON procedure (encounter_id);

-- ------------------------------------------------------------ service request
-- "Investigation advice" and "Referral" sections of the OP Consult Record.
CREATE TABLE IF NOT EXISTS service_request (
    id             INTEGER PRIMARY KEY,
    patient_id     INTEGER NOT NULL REFERENCES patient (id),
    encounter_id   INTEGER REFERENCES encounter (id),
    purpose        TEXT NOT NULL,            -- investigation | referral
    status         TEXT NOT NULL DEFAULT 'active',
    intent         TEXT NOT NULL DEFAULT 'order',
    code_system    TEXT NOT NULL,
    code           TEXT NOT NULL,
    display        TEXT NOT NULL,
    note           TEXT,
    authored_on    TEXT NOT NULL,
    requester_id   INTEGER REFERENCES practitioner (id)
);
CREATE INDEX IF NOT EXISTS ix_svcreq_enc ON service_request (encounter_id, purpose);

-- ----------------------------------------------------------- clinical notes
-- Narrative payload for the OPD note and the discharge summary. One row per
-- encounter per kind; the structured children live in the tables above.
CREATE TABLE IF NOT EXISTS clinical_note (
    id                      INTEGER PRIMARY KEY,
    encounter_id            INTEGER NOT NULL REFERENCES encounter (id),
    patient_id              INTEGER NOT NULL REFERENCES patient (id),
    kind                    TEXT NOT NULL,     -- OPD_NOTE | DISCHARGE_SUMMARY
    status                  TEXT NOT NULL DEFAULT 'final',
    author_id               INTEGER REFERENCES practitioner (id),
    history_text            TEXT,
    examination_text        TEXT,
    advice_text             TEXT,
    follow_up_date          TEXT,
    follow_up_note          TEXT,
    -- discharge summary specific
    admission_reason        TEXT,
    course_in_hospital      TEXT,
    condition_at_discharge  TEXT,
    discharge_instructions  TEXT,
    diet_advice             TEXT,
    care_plan_text          TEXT,
    created_at              TEXT NOT NULL,
    updated_at              TEXT NOT NULL,
    UNIQUE (encounter_id, kind)
);

-- ------------------------------------------------------------------ lab order
CREATE TABLE IF NOT EXISTS lab_order (
    id                     INTEGER PRIMARY KEY,
    order_no               TEXT NOT NULL UNIQUE,
    patient_id             INTEGER NOT NULL REFERENCES patient (id),
    encounter_id           INTEGER REFERENCES encounter (id),
    status                 TEXT NOT NULL DEFAULT 'registered',  -- registered|preliminary|final
    -- DiagnosticReport.category.coding (SNOMED, fixed system)
    category_code          TEXT NOT NULL,
    category_display       TEXT NOT NULL,
    -- DiagnosticReport.code.coding (LOINC, fixed system)
    panel_code             TEXT NOT NULL,
    panel_display          TEXT NOT NULL,
    -- Specimen profile
    specimen_code          TEXT,
    specimen_display       TEXT,
    specimen_collected_ts  TEXT,
    specimen_received_ts   TEXT,
    performer_org_id       INTEGER REFERENCES organization (id),
    interpreter_id         INTEGER REFERENCES practitioner (id),  -- resultsInterpreter (1..*)
    conclusion             TEXT,                                  -- conclusion (1..1 required)
    conclusion_code        TEXT,
    conclusion_display     TEXT,
    effective_ts           TEXT,
    issued_ts              TEXT,
    ordered_at             TEXT NOT NULL,
    price                  REAL NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS ix_lab_patient ON lab_order (patient_id, ordered_at DESC);

-- -------------------------------------------------------------------- invoice
CREATE TABLE IF NOT EXISTS invoice (
    id             INTEGER PRIMARY KEY,
    invoice_no     TEXT NOT NULL UNIQUE,      -- Invoice.identifier (1..1)
    patient_id     INTEGER NOT NULL REFERENCES patient (id),
    encounter_id   INTEGER REFERENCES encounter (id),
    status         TEXT NOT NULL DEFAULT 'issued',   -- draft|issued|balanced|cancelled
    type_code      TEXT NOT NULL,             -- ndhm-billing-codes: 00 03 02 01 99
    type_display   TEXT NOT NULL,
    date           TEXT NOT NULL,             -- Invoice.date (1..1)
    issuer_org_id  INTEGER REFERENCES organization (id),
    participant_id INTEGER REFERENCES practitioner (id),
    total_net      REAL NOT NULL DEFAULT 0,   -- Invoice.totalNet (1..1)
    total_gross    REAL NOT NULL DEFAULT 0,   -- Invoice.totalGross (1..1)
    amount_paid    REAL NOT NULL DEFAULT 0,
    payment_mode   TEXT,
    note           TEXT,
    created_at     TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_invoice_patient ON invoice (patient_id, date DESC);

CREATE TABLE IF NOT EXISTS invoice_line (
    id             INTEGER PRIMARY KEY,
    invoice_id     INTEGER NOT NULL REFERENCES invoice (id) ON DELETE CASCADE,
    seq            INTEGER NOT NULL,
    description    TEXT NOT NULL,
    charge_system  TEXT NOT NULL,
    charge_code    TEXT NOT NULL,
    charge_display TEXT NOT NULL,
    quantity       REAL NOT NULL DEFAULT 1,
    unit_price     REAL NOT NULL DEFAULT 0,   -- price component 01 "Rate"
    discount_pct   REAL NOT NULL DEFAULT 0,   -- price component 02 "Discount"
    cgst_pct       REAL NOT NULL DEFAULT 0,   -- price component 03 "CGST"
    sgst_pct       REAL NOT NULL DEFAULT 0,   -- price component 04 "SGST"
    line_net       REAL NOT NULL DEFAULT 0,
    line_gross     REAL NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS ix_invline ON invoice_line (invoice_id, seq);

-- -------------------------------------------------------------- fhir exports
CREATE TABLE IF NOT EXISTS fhir_export (
    id                INTEGER PRIMARY KEY,
    artifact          TEXT NOT NULL,        -- OPConsultRecord | DischargeSummaryRecord | ...
    patient_id        INTEGER NOT NULL REFERENCES patient (id),
    encounter_id      INTEGER REFERENCES encounter (id),
    source_kind       TEXT NOT NULL,        -- encounter | lab_order | invoice
    source_id         INTEGER NOT NULL,
    bundle_identifier TEXT NOT NULL,
    bundle_version    INTEGER NOT NULL DEFAULT 1,
    resource_count    INTEGER NOT NULL DEFAULT 0,
    valid             INTEGER NOT NULL DEFAULT 0,
    issues_json       TEXT NOT NULL DEFAULT '[]',
    bundle_json       TEXT NOT NULL,
    created_at        TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_export_src ON fhir_export (source_kind, source_id);
CREATE INDEX IF NOT EXISTS ix_export_created ON fhir_export (created_at DESC);

-- =========================================================================
-- Ward and bed management — the operational core of a 10–20 bed hospital
-- =========================================================================
CREATE TABLE IF NOT EXISTS ward (
    id            INTEGER PRIMARY KEY,
    code          TEXT NOT NULL UNIQUE,
    name          TEXT NOT NULL,
    class         TEXT NOT NULL,        -- general | semi-private | private | icu | hdu | maternity
    tariff        REAL NOT NULL DEFAULT 0,   -- per day
    nursing_rate  REAL NOT NULL DEFAULT 0,   -- per day
    gender_policy TEXT NOT NULL DEFAULT 'any',  -- any | male | female
    active        INTEGER NOT NULL DEFAULT 1,
    sort_order    INTEGER NOT NULL DEFAULT 100
);

CREATE TABLE IF NOT EXISTS bed (
    id            INTEGER PRIMARY KEY,
    ward_id       INTEGER NOT NULL REFERENCES ward (id),
    code          TEXT NOT NULL UNIQUE,
    status        TEXT NOT NULL DEFAULT 'vacant',  -- vacant|occupied|cleaning|blocked
    encounter_id  INTEGER REFERENCES encounter (id),
    tariff        REAL,                 -- overrides the ward tariff when set
    note          TEXT,
    active        INTEGER NOT NULL DEFAULT 1
);
CREATE INDEX IF NOT EXISTS ix_bed_ward ON bed (ward_id, code);

-- One row per stay in a bed; a transfer closes one row and opens the next.
-- Bed and nursing charges are billed from this ledger, not from a flat rate.
CREATE TABLE IF NOT EXISTS bed_movement (
    id            INTEGER PRIMARY KEY,
    encounter_id  INTEGER NOT NULL REFERENCES encounter (id),
    bed_id        INTEGER NOT NULL REFERENCES bed (id),
    from_ts       TEXT NOT NULL,
    to_ts         TEXT,
    tariff        REAL NOT NULL DEFAULT 0,
    nursing_rate  REAL NOT NULL DEFAULT 0,
    reason        TEXT,
    billed        INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS ix_bedmove_enc ON bed_movement (encounter_id, from_ts);

-- =========================================================================
-- Pharmacy and consumables
-- =========================================================================
CREATE TABLE IF NOT EXISTS stock_item (
    id             INTEGER PRIMARY KEY,
    code           TEXT NOT NULL UNIQUE,
    name           TEXT NOT NULL,
    kind           TEXT NOT NULL DEFAULT 'drug',   -- drug | consumable
    form           TEXT,                            -- tablet, injection, …
    strength       TEXT,
    unit           TEXT NOT NULL DEFAULT 'unit',    -- tablet, vial, piece …
    snomed_code    TEXT,                            -- Medication.code (SNOMED)
    snomed_display TEXT,
    hsn_code       TEXT,                            -- Medication.identifier type HSN
    mrp            REAL NOT NULL DEFAULT 0,
    purchase_price REAL NOT NULL DEFAULT 0,
    gst_pct        REAL NOT NULL DEFAULT 0,
    reorder_level  REAL NOT NULL DEFAULT 0,
    active         INTEGER NOT NULL DEFAULT 1
);
CREATE INDEX IF NOT EXISTS ix_stock_item_name ON stock_item (name);

CREATE TABLE IF NOT EXISTS stock_batch (
    id          INTEGER PRIMARY KEY,
    item_id     INTEGER NOT NULL REFERENCES stock_item (id),
    batch_no    TEXT NOT NULL,
    expiry_date TEXT,                    -- YYYY-MM-DD
    quantity    REAL NOT NULL DEFAULT 0, -- remaining
    cost_price  REAL NOT NULL DEFAULT 0,
    supplier    TEXT,
    received_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_batch_item ON stock_batch (item_id, expiry_date);

CREATE TABLE IF NOT EXISTS stock_txn (
    id           INTEGER PRIMARY KEY,
    item_id      INTEGER NOT NULL REFERENCES stock_item (id),
    batch_id     INTEGER REFERENCES stock_batch (id),
    kind         TEXT NOT NULL,          -- receipt | issue | return | adjust
    quantity     REAL NOT NULL,          -- signed: + into stock, - out of stock
    rate         REAL NOT NULL DEFAULT 0,
    patient_id   INTEGER REFERENCES patient (id),
    encounter_id INTEGER REFERENCES encounter (id),
    billed       INTEGER NOT NULL DEFAULT 0,
    note         TEXT,
    created_at   TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_stock_txn ON stock_txn (created_at DESC);
CREATE INDEX IF NOT EXISTS ix_stock_txn_enc ON stock_txn (encounter_id, kind);

-- =========================================================================
-- Money in: receipts against invoices
-- =========================================================================
CREATE TABLE IF NOT EXISTS payment (
    id          INTEGER PRIMARY KEY,
    receipt_no  TEXT NOT NULL UNIQUE,
    invoice_id  INTEGER NOT NULL REFERENCES invoice (id) ON DELETE CASCADE,
    patient_id  INTEGER NOT NULL REFERENCES patient (id),
    amount      REAL NOT NULL,
    mode        TEXT NOT NULL DEFAULT 'cash',  -- cash|card|upi|bank|insurance|other
    reference   TEXT,
    note        TEXT,
    received_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_payment_invoice ON payment (invoice_id);
CREATE INDEX IF NOT EXISTS ix_payment_date ON payment (received_at DESC);

-- =========================================================================
-- Appointments / OPD queue
-- =========================================================================
CREATE TABLE IF NOT EXISTS appointment (
    id              INTEGER PRIMARY KEY,
    appointment_no  TEXT NOT NULL UNIQUE,
    patient_id      INTEGER NOT NULL REFERENCES patient (id),
    practitioner_id INTEGER REFERENCES practitioner (id),
    department      TEXT,
    slot_date       TEXT NOT NULL,      -- YYYY-MM-DD
    slot_time       TEXT,               -- HH:MM
    token           INTEGER NOT NULL DEFAULT 0,
    status          TEXT NOT NULL DEFAULT 'booked',
                    -- booked | arrived | fulfilled | cancelled | no-show
    reason          TEXT,
    note            TEXT,
    encounter_id    INTEGER REFERENCES encounter (id),
    created_at      TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_appt_slot ON appointment (slot_date, token);

-- =========================================================================
-- Dialysis unit
-- =========================================================================
CREATE TABLE IF NOT EXISTS dialysis_machine (
    id            INTEGER PRIMARY KEY,
    code          TEXT NOT NULL UNIQUE,        -- HD-01
    model         TEXT,
    manufacturer  TEXT,
    serial_no     TEXT,
    location      TEXT,
    status        TEXT NOT NULL DEFAULT 'available',
                  -- available | in-use | maintenance | retired
    last_service  TEXT,                        -- YYYY-MM-DD
    note          TEXT,
    active        INTEGER NOT NULL DEFAULT 1
);

-- A course is the standing prescription: "maintenance HD, 3 sessions a week,
-- 4 hours each, via the left radiocephalic fistula". Sessions hang off it.
CREATE TABLE IF NOT EXISTS dialysis_course (
    id                  INTEGER PRIMARY KEY,
    course_no           TEXT NOT NULL UNIQUE,
    patient_id          INTEGER NOT NULL REFERENCES patient (id),
    encounter_id        INTEGER REFERENCES encounter (id),
    practitioner_id     INTEGER REFERENCES practitioner (id),
    status              TEXT NOT NULL DEFAULT 'active',  -- active|completed|cancelled
    modality_code       TEXT NOT NULL,          -- SNOMED, e.g. 302497006 Hemodialysis
    modality_display    TEXT NOT NULL,
    indication_code     TEXT,                   -- SNOMED diagnosis
    indication_display  TEXT,
    access_code         TEXT,                   -- vascular access type
    access_display      TEXT,
    access_site         TEXT,                   -- "Left radiocephalic"
    sessions_per_week   REAL NOT NULL DEFAULT 3,
    duration_minutes    INTEGER NOT NULL DEFAULT 240,
    dry_weight_kg       REAL,
    dialyser            TEXT,
    anticoagulant_code  TEXT,
    anticoagulant_display TEXT,
    heparin_bolus_units   REAL,
    heparin_hourly_units  REAL,
    blood_flow_rate     REAL,                   -- prescribed Qb, mL/min
    dialysate_flow_rate REAL,                   -- prescribed Qd, mL/min
    dialysate_na        REAL,                   -- mmol/L
    dialysate_k         REAL,
    dialysate_ca        REAL,
    dialysate_bicarb    REAL,
    session_charge      REAL NOT NULL DEFAULT 0,
    started_on          TEXT NOT NULL,
    ended_on            TEXT,
    note                TEXT,
    created_at          TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_dialysis_course ON dialysis_course (patient_id, status);

CREATE TABLE IF NOT EXISTS dialysis_session (
    id                  INTEGER PRIMARY KEY,
    session_no          TEXT NOT NULL UNIQUE,
    course_id           INTEGER NOT NULL REFERENCES dialysis_course (id),
    patient_id          INTEGER NOT NULL REFERENCES patient (id),
    encounter_id        INTEGER REFERENCES encounter (id),
    machine_id          INTEGER REFERENCES dialysis_machine (id),
    practitioner_id     INTEGER REFERENCES practitioner (id),
    seq                 INTEGER NOT NULL DEFAULT 1,   -- nth session of the course
    status              TEXT NOT NULL DEFAULT 'planned',
                        -- planned | in-progress | completed | abandoned
    scheduled_at        TEXT,
    started_at          TEXT,
    ended_at            TEXT,
    duration_minutes    INTEGER,
    -- prescription actually delivered
    access_code         TEXT,
    access_display      TEXT,
    dialyser            TEXT,
    dialyser_reuse      INTEGER,
    blood_flow_rate     REAL,                   -- Qb  mL/min
    dialysate_flow_rate REAL,                   -- Qd  mL/min
    dialysate_temp      REAL,                   -- degC
    conductivity        REAL,                   -- mS/cm
    dialysate_na        REAL,
    dialysate_k         REAL,
    dialysate_ca        REAL,
    dialysate_bicarb    REAL,
    anticoagulant_code  TEXT,
    anticoagulant_display TEXT,
    heparin_bolus_units   REAL,
    heparin_hourly_units  REAL,
    -- fluid management
    dry_weight_kg       REAL,
    pre_weight_kg       REAL,
    post_weight_kg      REAL,
    uf_goal_ml          REAL,
    uf_achieved_ml      REAL,
    -- adequacy
    ktv                 REAL,
    urr_pct             REAL,
    -- outcome
    complication_code   TEXT,
    complication_display TEXT,
    complication_note   TEXT,
    note                TEXT,
    billed              INTEGER NOT NULL DEFAULT 0,
    created_at          TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_dialysis_session ON dialysis_session (course_id, seq);
CREATE INDEX IF NOT EXISTS ix_dialysis_session_day
    ON dialysis_session (scheduled_at, status);

-- The half-hourly monitoring chart. Deliberately a plain table rather than
-- FHIR Observations: it is nursing surveillance, not diagnostic data, and one
-- session produces a dozen rows that would swamp an exported bundle.
CREATE TABLE IF NOT EXISTS dialysis_reading (
    id                INTEGER PRIMARY KEY,
    session_id        INTEGER NOT NULL REFERENCES dialysis_session (id) ON DELETE CASCADE,
    elapsed_minutes   INTEGER NOT NULL DEFAULT 0,
    recorded_at       TEXT NOT NULL,
    bp_systolic       REAL,
    bp_diastolic      REAL,
    pulse             REAL,
    blood_flow_rate   REAL,
    arterial_pressure REAL,                   -- mmHg
    venous_pressure   REAL,                   -- mmHg
    tmp               REAL,                   -- transmembrane pressure, mmHg
    uf_volume_ml      REAL,                   -- cumulative
    note              TEXT
);
CREATE INDEX IF NOT EXISTS ix_dialysis_reading ON dialysis_reading (session_id,
                                                                   elapsed_minutes);

-- =========================================================================
-- Wellness record (NRCES WellnessRecord) — PHR-style periodic health data
-- =========================================================================
CREATE TABLE IF NOT EXISTS wellness_record (
    id              INTEGER PRIMARY KEY,
    record_no       TEXT NOT NULL UNIQUE,
    patient_id      INTEGER NOT NULL REFERENCES patient (id),
    encounter_id    INTEGER REFERENCES encounter (id),
    practitioner_id INTEGER REFERENCES practitioner (id),
    status          TEXT NOT NULL DEFAULT 'draft',   -- draft | final
    -- set when the record was generated from a completed dialysis run
    dialysis_session_id INTEGER REFERENCES dialysis_session (id),
    recorded_on     TEXT NOT NULL,                   -- YYYY-MM-DD
    source          TEXT,                            -- clinic | health camp | PHR app
    note            TEXT,
    created_at      TEXT NOT NULL,
    updated_at      TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_wellness ON wellness_record (patient_id,
                                                           recorded_on DESC);

-- =========================================================================
-- NHCX claims — policy search, coverage eligibility and (later) preauth.
-- One row per claim episode; the FHIR exchange itself is delegated to a
-- local hcxkit gateway, this table keeps the EMR-side ledger and verdict.
-- =========================================================================
CREATE TABLE IF NOT EXISTS claim (
    id               INTEGER PRIMARY KEY,
    claim_no         TEXT NOT NULL UNIQUE,
    created_at       TEXT NOT NULL,
    status           TEXT NOT NULL DEFAULT 'draft',  -- draft | checking | eligible | not-eligible | error
    -- how the beneficiary was found (inputs to the policy search)
    search_id_type   TEXT,                           -- MobileNo | AbhaNumber | MemberId
    search_id_value  TEXT,
    -- the policy the operator selected from the search result
    member_id        TEXT NOT NULL,                  -- PMJAY beneficiary / member ID
    policy_code      TEXT,                           -- national health plan identifier, e.g. PMJAY/HP/S/G
    beneficiary_name TEXT,
    abha_number      TEXT,
    mobile_number    TEXT,
    payer_id         TEXT,                           -- NIIP, e.g. 1518
    payer_name       TEXT,
    product_id       TEXT,
    product_name     TEXT,
    policy_json      TEXT,                           -- raw policy row as returned by the search
    -- coverage eligibility exchange bookkeeping (async via hcxkit)
    purpose          TEXT,                           -- validation | discovery
    txn_id           TEXT,                           -- hcxkit ledger ULID of the outbound check
    correlation_id   TEXT,                           -- x-hcx-correlation_id tying request to on_check
    checked_at       TEXT,
    error_message    TEXT,
    -- payer verdict (flattened from the CoverageEligibilityResponse bundle)
    inforce          INTEGER,                        -- 1 policy in force, 0 not
    outcome          TEXT,                           -- complete | error | partial
    disposition      TEXT,
    auth_required    INTEGER,
    allowed_amount   REAL,                           -- benefit allowedMoney (sum insured)
    used_amount      REAL,                           -- benefit usedMoney
    plan_name        TEXT,
    plan_period_start TEXT,
    plan_period_end  TEXT,
    relationship     TEXT,                           -- subscriber relationship (self, child, …)
    patient_gender   TEXT,
    patient_dob      TEXT,
    patient_address  TEXT,
    patient_photo    TEXT,                           -- base64 or URL when the payer returns one
    response_json    TEXT,                           -- full on_check bundle for audit
    -- link to the admitted patient (same ABHA, current IPD stay)
    patient_id       INTEGER REFERENCES patient (id),
    encounter_id     INTEGER REFERENCES encounter (id),
    -- preauth draft (children in claim_diagnosis / claim_care_team / claim_item)
    admission_date   TEXT,                           -- YYYY-MM-DD
    expected_discharge_date TEXT,                    -- provisional, YYYY-MM-DD
    case_type        TEXT,                           -- package | nonpackage
    package_code     TEXT,                           -- HBP package (case_type = package)
    package_name     TEXT,
    preauth_total    REAL,                           -- package rate or sum of items
    preauth_saved_at TEXT
);
CREATE INDEX IF NOT EXISTS ix_claim_status ON claim (status, id DESC);

-- The payer's package master for this claim's policy-provider pair, fetched
-- over NHCX with an InsurancePlan discovery Task (v1/insuranceplan/request).
-- One row per claim: refetching replaces it, and the benefits cascade with it.
CREATE TABLE IF NOT EXISTS claim_plan (
    id              INTEGER PRIMARY KEY,
    claim_id        INTEGER NOT NULL UNIQUE REFERENCES claim (id) ON DELETE CASCADE,
    status          TEXT NOT NULL DEFAULT 'fetching',  -- fetching | ready | empty | error
    txn_id          TEXT,                          -- hcxkit ledger ULID of the request
    correlation_id  TEXT,                          -- ties the request to the on_request
    requested_at    TEXT,
    fetched_at      TEXT,
    error_message   TEXT,
    -- what the discovery Task asked with (Task.input)
    policy_code     TEXT,                          -- policyNumber
    provider_id     TEXT,                          -- providerId, the facility's HFR ID
    -- flattened from the payer's InsurancePlan resource
    plan_identifier TEXT,
    plan_title      TEXT,                          -- InsurancePlan.name
    plan_type       TEXT,                          -- plan[0].type coding
    sum_insured     REAL,                          -- plan[0].generalCost[0].cost
    policy_documents TEXT,                         -- JSON: what every claim under this policy needs
    response_json   TEXT                           -- full on_request bundle for audit
);
CREATE INDEX IF NOT EXISTS ix_claim_plan_corr ON claim_plan (correlation_id);

-- One row per package (Approach 1: specificCost → category → benefit) or per
-- covered benefit (Approach 2: coverage → benefit → limit); the payer bundle
-- follows one shape or the other and both flatten to this.
CREATE TABLE IF NOT EXISTS claim_plan_benefit (
    id               INTEGER PRIMARY KEY,
    plan_id          INTEGER NOT NULL REFERENCES claim_plan (id) ON DELETE CASCADE,
    seq              INTEGER NOT NULL DEFAULT 1,
    category_code    TEXT,                         -- speciality / coverage type
    category_display TEXT,
    code             TEXT NOT NULL,                -- package / benefit code
    display          TEXT,
    kind             TEXT,                         -- Procedure | Implant — what the code names
    rate             REAL,                         -- package rate; 0 = bundled, not free
    currency         TEXT,
    cost_type        TEXT,                         -- package rate | included | conditional …
    requirement      TEXT,                         -- Approach 2 benefit.requirement
    conditions       TEXT,                         -- JSON: claim-condition code → value
    extras           TEXT,                         -- JSON: stratification / implant tiers paid over the rate
    supporting_info  TEXT                          -- JSON: documents the payer requires for this benefit
);
CREATE INDEX IF NOT EXISTS ix_claim_plan_benefit ON claim_plan_benefit (plan_id, seq);

-- The questionnaires the payer ships with the plan — the dynamic forms a
-- document requirement points at (STG checklists, discharge information …).
-- Referenced by url, which is how `documentationUrl` names them; the payer
-- sends the same form once per benefit that needs it, hence the unique index.
CREATE TABLE IF NOT EXISTS claim_plan_form (
    id       INTEGER PRIMARY KEY,
    plan_id  INTEGER NOT NULL REFERENCES claim_plan (id) ON DELETE CASCADE,
    url      TEXT NOT NULL,                        -- Questionnaire.url
    form_id  TEXT,                                 -- Questionnaire.id
    title    TEXT,
    kind     TEXT,                                 -- questionnaire | stgquestionnaire
    items    TEXT NOT NULL                         -- JSON: the questions and their options
);
CREATE UNIQUE INDEX IF NOT EXISTS ux_claim_plan_form ON claim_plan_form (plan_id, url);

-- The payer's verdict on a *procedure set*: a coverage eligibility check with
-- purpose `auth-requirements`, sent once the lines are chosen. It answers
-- per line — is authorisation required, is it excluded, what is allowed — and
-- names the documents and questionnaires that set needs.
CREATE TABLE IF NOT EXISTS claim_auth (
    id             INTEGER PRIMARY KEY,
    claim_id       INTEGER NOT NULL UNIQUE REFERENCES claim (id) ON DELETE CASCADE,
    status         TEXT NOT NULL DEFAULT 'checking',  -- checking | ready | error
    txn_id         TEXT,
    correlation_id TEXT,
    requested_at   TEXT,
    settled_at     TEXT,
    error_message  TEXT,
    outcome        TEXT,
    disposition    TEXT,
    inforce        INTEGER,
    response_json  TEXT
);
CREATE INDEX IF NOT EXISTS ix_claim_auth_corr ON claim_auth (correlation_id);

-- One row per line the payer answered on.
CREATE TABLE IF NOT EXISTS claim_auth_item (
    id             INTEGER PRIMARY KEY,
    auth_id        INTEGER NOT NULL REFERENCES claim_auth (id) ON DELETE CASCADE,
    seq            INTEGER NOT NULL DEFAULT 1,
    code           TEXT NOT NULL,
    display        TEXT,
    category_code  TEXT,
    auth_required  INTEGER,
    excluded       INTEGER,
    benefit_type   TEXT,                          -- Procedure | Implant | …
    allowed_amount REAL
);
CREATE INDEX IF NOT EXISTS ix_claim_auth_item ON claim_auth_item (auth_id, seq);

-- What that procedure set has to be accompanied by. `stage` is the payer's
-- own word for when it is wanted; `at_preauth` is the adapter's reading of it,
-- so the pre-authorisation screen asks for those and leaves the rest to the
-- claim.
CREATE TABLE IF NOT EXISTS claim_auth_requirement (
    id         INTEGER PRIMARY KEY,
    auth_id    INTEGER NOT NULL REFERENCES claim_auth (id) ON DELETE CASCADE,
    seq        INTEGER NOT NULL DEFAULT 1,
    kind       TEXT NOT NULL,                     -- document | form
    code       TEXT,
    display    TEXT,
    form_url   TEXT,
    stage      TEXT,
    for_code   TEXT,                              -- the line it was asked for
    at_preauth INTEGER NOT NULL DEFAULT 1
);
CREATE INDEX IF NOT EXISTS ix_claim_auth_req ON claim_auth_requirement (auth_id, seq);

-- The preauth's line items, chosen from the payer's package master: the
-- procedures (with quantity), the implants approved for them and the ward /
-- ICU stratification tiers. Prices come from the plan, never from the form.
CREATE TABLE IF NOT EXISTS claim_line (
    id               INTEGER PRIMARY KEY,
    claim_id         INTEGER NOT NULL REFERENCES claim (id) ON DELETE CASCADE,
    seq              INTEGER NOT NULL DEFAULT 1,
    kind             TEXT NOT NULL,                -- Procedure | Implant | Stratification
    code             TEXT NOT NULL,
    display          TEXT,
    category_code    TEXT,                         -- speciality, for Claim.item.category
    category_display TEXT,
    unit_price       REAL NOT NULL DEFAULT 0,
    quantity         REAL NOT NULL DEFAULT 1,
    amount           REAL NOT NULL DEFAULT 0
);
CREATE UNIQUE INDEX IF NOT EXISTS ux_claim_line ON claim_line (claim_id, kind, code);

-- Answers to the questionnaires the payer's package master demands. One row
-- per question; they become the QuestionnaireResponse resources on the wire.
CREATE TABLE IF NOT EXISTS claim_form_answer (
    id       INTEGER PRIMARY KEY,
    claim_id INTEGER NOT NULL REFERENCES claim (id) ON DELETE CASCADE,
    form_url TEXT NOT NULL,
    link_id  TEXT NOT NULL,
    answer   TEXT
);
CREATE UNIQUE INDEX IF NOT EXISTS ux_claim_form_answer
    ON claim_form_answer (claim_id, form_url, link_id);

-- The preauth submission exchange and the payer's verdict on it. Separate
-- from the draft on `claim`, exactly as `claim_plan` is separate from the
-- coverage check: a draft can be re-submitted, and the ledger is per attempt.
CREATE TABLE IF NOT EXISTS claim_preauth (
    id              INTEGER PRIMARY KEY,
    claim_id        INTEGER NOT NULL UNIQUE REFERENCES claim (id) ON DELETE CASCADE,
    status          TEXT NOT NULL DEFAULT 'submitting',  -- submitting | approved | rejected | queried | error
    txn_id          TEXT,
    correlation_id  TEXT,
    submitted_at    TEXT,
    settled_at      TEXT,
    error_message   TEXT,
    claim_ref       TEXT,                          -- Claim.identifier sent
    preauth_ref     TEXT,                          -- ClaimResponse.preAuthRef
    api_call_id     TEXT,                          -- the last reply applied, so a redelivery is not
    adjudication    TEXT,                          -- the reason code that IS the verdict
    query_note      TEXT,                          -- the payer's query trail, verbatim
    outcome         TEXT,                          -- complete | error | partial | queued
    disposition     TEXT,
    approved_amount REAL,
    requested_amount REAL,
    request_json    TEXT,                          -- the bundle as sent, for audit
    response_json   TEXT,
    -- withdrawing it again: a Task the payer acts on, its own exchange
    cancel_txn_id         TEXT,
    cancel_correlation_id TEXT,
    cancel_requested_at   TEXT,
    cancel_reason         TEXT,                   -- claims.CANCEL_REASONS key
    cancel_note           TEXT
);
CREATE INDEX IF NOT EXISTS ix_claim_preauth_corr ON claim_preauth (correlation_id);

-- A predetermination: the pre-authorisation's own bundle sent with
-- `use: predetermination` before anything is committed. The payer prices it
-- at once and answers with a ClaimResponse that binds nobody and opens no
-- case, so it is its own row per ask rather than a state of the pre-auth.
CREATE TABLE IF NOT EXISTS claim_predetermination (
    id              INTEGER PRIMARY KEY,
    claim_id        INTEGER NOT NULL REFERENCES claim (id) ON DELETE CASCADE,
    status          TEXT NOT NULL DEFAULT 'asking',  -- asking | answered | error
    txn_id          TEXT,
    correlation_id  TEXT,
    requested_at    TEXT NOT NULL,
    answered_at     TEXT,
    error_message   TEXT,
    outcome         TEXT,                          -- complete | error | partial
    adjudication    TEXT,                          -- the reason code beside it
    disposition     TEXT,
    allowed_amount  REAL,                          -- total[benefit]
    requested_amount REAL,
    request_json    TEXT,
    response_json   TEXT
);
CREATE INDEX IF NOT EXISTS ix_claim_predetermination_corr ON claim_predetermination (correlation_id);
CREATE INDEX IF NOT EXISTS ix_claim_predetermination_claim ON claim_predetermination (claim_id, id);

-- The claim itself: everything the preauth carried, plus how the stay ended.
-- Separate from `claim_preauth` because it is a separate exchange with its own
-- ledger — and because a claim can go in after a preauth was queried.
CREATE TABLE IF NOT EXISTS claim_submission (
    id               INTEGER PRIMARY KEY,
    claim_id         INTEGER NOT NULL UNIQUE REFERENCES claim (id) ON DELETE CASCADE,
    status           TEXT NOT NULL DEFAULT 'draft',  -- draft | submitting | approved | queried | rejected | error
    -- how the stay ended: PMJAY prices a completed episode, so this changes
    -- what can be claimed as much as what is attached
    discharge_mode   TEXT,                          -- normal | lama | dama | death
    discharge_stage  TEXT,                          -- Before/During/After Surgery
    discharge_date   TEXT,
    surgery_date     TEXT,
    death_date       TEXT,                          -- mode = death only
    -- exchange bookkeeping
    txn_id           TEXT,
    correlation_id   TEXT,
    submitted_at     TEXT,
    settled_at       TEXT,
    error_message    TEXT,
    claim_ref        TEXT,
    -- the payer's verdict
    preauth_ref      TEXT,
    api_call_id      TEXT,
    adjudication     TEXT,
    query_note       TEXT,
    outcome          TEXT,
    disposition      TEXT,
    approved_amount  REAL,
    requested_amount REAL,
    request_json     TEXT,
    response_json    TEXT
);
CREATE INDEX IF NOT EXISTS ix_claim_submission_corr
    ON claim_submission (correlation_id);

-- Payment notices. This is the one leg the *payer* starts: it posts a notice
-- when money moves, matched to a claim by the CLN identifier inside it rather
-- than by a correlation id of ours. Several arrive over a claim's life —
-- initiated, then cleared — so they are rows, not columns.
CREATE TABLE IF NOT EXISTS claim_payment (
    id             INTEGER PRIMARY KEY,
    claim_id       INTEGER NOT NULL REFERENCES claim (id) ON DELETE CASCADE,
    claim_ref      TEXT,                           -- the claim number it named
    correlation_id TEXT,                           -- the payer's, deduping redeliveries
    -- who sent it: the acknowledgement goes back to *them*, which is not
    -- always the payer the claim was raised with
    sender_code    TEXT,
    workflow_id    TEXT,
    received_at    TEXT NOT NULL,
    -- what the notice says
    disposition    TEXT,                           -- "Payment initiated", …
    payment_status TEXT,                           -- PaymentNotice.paymentStatus
    payment_date   TEXT,
    amount         REAL,
    currency       TEXT,
    utr            TEXT,                           -- Unique Transaction Reference
    notice_json    TEXT,
    -- the acknowledgement this EMR sends straight back
    ack_status     TEXT NOT NULL DEFAULT 'pending',  -- pending | sent | error
    ack_txn_id     TEXT,
    ack_correlation_id TEXT,
    acknowledged_at TEXT,
    ack_error      TEXT
);
CREATE INDEX IF NOT EXISTS ix_claim_payment ON claim_payment (claim_id, id DESC);
CREATE UNIQUE INDEX IF NOT EXISTS ux_claim_payment_corr
    ON claim_payment (correlation_id);

-- A payer's query on a leg, as a CommunicationRequest on a thread of its own.
-- PMJAY queries inside the ClaimResponse and is answered by resubmitting the
-- pre-authorisation; an IRDAI payer asks on `communication/request` and is
-- answered with a Communication carrying the request's correlation id back.
CREATE TABLE IF NOT EXISTS claim_query (
    id              INTEGER PRIMARY KEY,
    claim_id        INTEGER NOT NULL REFERENCES claim (id) ON DELETE CASCADE,
    stage           TEXT NOT NULL DEFAULT 'preauth',  -- the leg queried: preauth | claim
    correlation_id  TEXT,                          -- the request's own thread
    request_id      TEXT,                          -- CommunicationRequest.id
    claim_ref       TEXT,                          -- the claim number it named
    sender_code     TEXT,
    workflow_id     TEXT,                          -- the queried submission's thread
    received_at     TEXT NOT NULL,
    status          TEXT NOT NULL DEFAULT 'open',  -- open | answered | error
    remarks         TEXT,                          -- what the payer wrote, joined
    questions       TEXT,                          -- JSON: one entry per payload line
    request_json    TEXT,
    -- the reply this EMR sends
    reply_text      TEXT,
    reply_documents TEXT,                          -- JSON: claim_document ids attached
    reply_txn_id    TEXT,
    reply_correlation_id TEXT,
    answered_at     TEXT,
    error_message   TEXT,
    reply_json      TEXT
);
CREATE INDEX IF NOT EXISTS ix_claim_query ON claim_query (claim_id, id DESC);
CREATE UNIQUE INDEX IF NOT EXISTS ux_claim_query_corr
    ON claim_query (correlation_id);

-- The reconciliation's breakdown: what was paid, what was withheld.
CREATE TABLE IF NOT EXISTS claim_payment_detail (
    id         INTEGER PRIMARY KEY,
    payment_id INTEGER NOT NULL REFERENCES claim_payment (id) ON DELETE CASCADE,
    seq        INTEGER NOT NULL DEFAULT 1,
    reference  TEXT,
    type_code  TEXT,                               -- Payment | RF | …
    type_display TEXT,
    date       TEXT,
    amount     REAL
);
CREATE INDEX IF NOT EXISTS ix_claim_payment_detail
    ON claim_payment_detail (payment_id, seq);

-- Decisions taken from the PMJAY adjudicator screen. NHCX carries no record
-- of them — they go to the payer service, outside the exchange — so this is
-- the only trace of who decided what from here, and why.
CREATE TABLE IF NOT EXISTS claim_adjudication (
    id             INTEGER PRIMARY KEY,
    claim_id       INTEGER NOT NULL REFERENCES claim (id) ON DELETE CASCADE,
    stage          TEXT NOT NULL,                 -- preauth | claim
    case_number    TEXT,
    role           TEXT,
    action         TEXT,
    usecase        TEXT,
    correlation_id TEXT,
    remarks        TEXT,
    success        INTEGER NOT NULL DEFAULT 0,
    http_status    INTEGER,
    response_json  TEXT,
    taken_at       TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_claim_adjudication
    ON claim_adjudication (claim_id, stage, id DESC);

-- ICD-10 diagnoses quoted on the preauth (picked from the diagnosis master,
-- which carries ICD-10 as the secondary coding next to SNOMED).
CREATE TABLE IF NOT EXISTS claim_diagnosis (
    id             INTEGER PRIMARY KEY,
    claim_id       INTEGER NOT NULL REFERENCES claim (id) ON DELETE CASCADE,
    seq            INTEGER NOT NULL DEFAULT 1,
    snomed_code    TEXT,
    snomed_display TEXT,
    icd10_code     TEXT NOT NULL,
    icd10_display  TEXT
);
CREATE INDEX IF NOT EXISTS ix_claim_dx ON claim_diagnosis (claim_id, seq);

-- The doctors responsible for the admission, quoted on the preauth.
CREATE TABLE IF NOT EXISTS claim_care_team (
    id              INTEGER PRIMARY KEY,
    claim_id        INTEGER NOT NULL REFERENCES claim (id) ON DELETE CASCADE,
    seq             INTEGER NOT NULL DEFAULT 1,
    practitioner_id INTEGER NOT NULL REFERENCES practitioner (id),
    role            TEXT NOT NULL                    -- claims.CARE_ROLES key
);
CREATE INDEX IF NOT EXISTS ix_claim_team ON claim_care_team (claim_id, seq);

-- Non-package case: charge-master items at their fixed price, quantity chosen
-- by the operator. amount = unit_price * quantity, computed at save time.
CREATE TABLE IF NOT EXISTS claim_item (
    id         INTEGER PRIMARY KEY,
    claim_id   INTEGER NOT NULL REFERENCES claim (id) ON DELETE CASCADE,
    seq        INTEGER NOT NULL DEFAULT 1,
    code       TEXT NOT NULL,                        -- charge master code
    display    TEXT NOT NULL,
    unit_price REAL NOT NULL DEFAULT 0,              -- fixed, from the master
    quantity   REAL NOT NULL DEFAULT 1,
    amount     REAL NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS ix_claim_item ON claim_item (claim_id, seq);

-- Supporting documents (PDF / images) attached to the preauth, stored inline —
-- a preauth carries a handful of files, well within SQLite's comfort zone.
CREATE TABLE IF NOT EXISTS claim_document (
    id           INTEGER PRIMARY KEY,
    claim_id     INTEGER NOT NULL REFERENCES claim (id) ON DELETE CASCADE,
    filename     TEXT NOT NULL,
    content_type TEXT NOT NULL,
    label        TEXT,
    -- which of the payer's requirements this file answers, so the preauth
    -- quotes its own code back rather than a generic "other document"
    code         TEXT,
    category     TEXT,
    -- which leg of the exchange it was attached for: a claim-stage document
    -- must not ride on the pre-authorisation, and vice versa
    stage        TEXT NOT NULL DEFAULT 'preauth',
    size         INTEGER NOT NULL DEFAULT 0,
    data         BLOB NOT NULL,
    uploaded_at  TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_claim_doc ON claim_document (claim_id, id);

-- ---------------------------------------------------------------- sequences
CREATE TABLE IF NOT EXISTS counter (
    name   TEXT PRIMARY KEY,
    value  INTEGER NOT NULL DEFAULT 0
);


-- The small exchanges a claim may start beside the main legs: a status
-- enquiry ("where does this stand?") and a reprocess request (an appeal
-- against a verdict). One row per ask, each on its own correlation id.
CREATE TABLE IF NOT EXISTS claim_enquiry (
    id             INTEGER PRIMARY KEY,
    claim_id       INTEGER NOT NULL REFERENCES claim (id) ON DELETE CASCADE,
    kind           TEXT NOT NULL,                  -- status | reprocess
    stage          TEXT,                           -- preauth | claim, for status / reprocess
    txn_id         TEXT,
    correlation_id TEXT,
    requested_at   TEXT NOT NULL,
    status         TEXT NOT NULL DEFAULT 'asking', -- asking | answered | error
    answer         TEXT,                           -- entity_status / verdict / task status
    detail         TEXT,                           -- what the payer said, in words
    amount         REAL,                           -- an amount the answer carried
    reason         TEXT,                           -- why a reprocess was asked for
    error_message  TEXT,
    request_json   TEXT,
    response_json  TEXT,
    answered_at    TEXT
);
CREATE INDEX IF NOT EXISTS ix_claim_enquiry_corr ON claim_enquiry (correlation_id);
CREATE INDEX IF NOT EXISTS ix_claim_enquiry_claim ON claim_enquiry (claim_id, id);
