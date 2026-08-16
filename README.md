# NanoEMR

A complete HMIS/EMR for a 10–20 bed Indian hospital, with every clinical
artifact exportable as an **NRCES / ABDM FHIR R4 document bundle**.

Appointments and the OPD queue · patient registration · OPD consultation notes ·
a live ward board with bed management · IPD admission and discharge summaries ·
a dialysis unit with per-session flowsheets · laboratory · pharmacy stock ·
wellness records · billing with receipts · daily MIS · master data management.

* **Backend** — Python 3.10+ **standard library only** (`http.server` +
  `sqlite3`). No pip install, no virtualenv, no build step.
* **Frontend** — server-rendered HTML on the [0build](https://0build.dev)
  design system (kit 0.5.4), loaded from jsDelivr. No Tailwind, no Bootstrap,
  no custom component CSS.
* **Interoperability** — document bundles conform to the profiles published at
  <https://www.nrces.in/ndhm/fhir/r4/profiles.html>, enforced by a built-in
  validator and pinned by the test suite.

## Quick start

```bash
python3 run.py --reset --demo      # clean database + demo data, then serve
open http://127.0.0.1:8765

python3 selftest.py                # 345 end-to-end checks, no server needed
```

`run.py` flags: `--port`, `--host`, `--demo` (seed two walked-through patient
episodes), `--reset` (delete the database first), `--no-serve` (set up and
exit). The database lives at `data/emr.db`; override with `NANOEMR_DB`.

## What is where

| Area | Routes | Notes |
|---|---|---|
| Front desk | `/patients`, `/appointments` | Registration, search, the day's token queue |
| Clinical | `/opd`, `/beds`, `/ipd`, `/wellness` | Consultation notes, ward board, admissions and discharge, wellness capture |
| Dialysis | `/dialysis`, `…/machines`, `…/courses`, `…/sessions` | Courses (standing prescription), per-run parameters, pre/post flowsheet, intra-dialytic chart |
| Diagnostics | `/lab`, `/pharmacy` | Panel ordering with analyte expansion; batch/expiry stock with FEFO issue |
| Finance | `/billing`, `/reports` | GST invoices, part-payments with receipts, daily MIS, outstanding dues |
| Facility | `/fhir`, `/masters`, `/settings` | FHIR export centre, master data, facility identity |

The full tour is in **[docs/features.md](docs/features.md)**.

## Documentation

| Document | Contents |
|---|---|
| [docs/features.md](docs/features.md) | Every module, screen by screen |
| [docs/fhir.md](docs/fhir.md) | The NRCES conformance story: artifacts, fixed codings, the validator, deliberate modelling choices |
| [docs/architecture.md](docs/architecture.md) | Layers, conventions, how to add a module, how the test suite works |
| [docs/operations.md](docs/operations.md) | Masters, clearing patient data, facility settings, deployment caveats |
| [docs/ui.md](docs/ui.md) | The 0build contract, the helpers, and the token traps that cost debugging time |

## Layout

```
run.py                    entry point
selftest.py               345 checks in named sections, one seeded database
data/emr.db               SQLite (created on first run)
docs/                     the documents above
emr/
├── schema.sql            30 tables, columns named after the FHIR elements they feed
├── db.py                 thread-local SQLite, transactions, counters, terminology
├── terminology.py        NRCES canonical URLs, fixed codings, section tables
├── seed.py               SNOMED/LOINC/ICD-10 demo subset, org, staff, wards, stock
├── services.py           patients, encounters, vitals, labs, invoice arithmetic
├── hospital.py           beds, pharmacy, payments, appointments
├── dialysis.py           courses, sessions, machines, flowsheet
├── wellness.py           WellnessRecord capture + generation from dialysis runs
├── masters.py            master CRUD + the clear-patient-data reset
├── demo.py               demo episodes (also the fastest FHIR smoke test)
├── fhir/                 row → profiled resource → document bundle → validation
└── web/
    ├── router.py         ~250-line routing layer over http.server
    ├── ui.py             HTML built from the 0build kit — no templates
    ├── static/           logo + favicon, served from an allow-list
    └── routes/           one module per area, each exposing register(app)
```

## Before production

Read these before this touches real patients:

* **No authentication, roles, or audit trail.** Every screen is open to whoever
  reaches the port; it binds to `127.0.0.1` and assumes a trusted operator.
  This is the first gap to close.
* **The terminology is a demonstration subset.** Real and correctly-systemed
  SNOMED CT / LOINC / ICD-10 codes, but hand-picked. Replace with a licensed
  SNOMED CT India expansion and the NRCES ValueSet expansions.
* **The built-in validator is a fast gate, not a certification.** Run the
  official HL7 validator with the NRCES package before an ABDM submission.
* **ABDM linkage is not included.** ABHA numbers are captured and exported as
  typed identifiers, but nothing talks to the ABDM gateway — care-context
  linking and consent are separate work.
* **Tariffs, MRPs and GST rates in the seed are placeholders** for a real rate
  card.
