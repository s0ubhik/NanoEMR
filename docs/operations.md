# Operations

Configuring a facility, resetting one, and what to fix before production.

## Facility settings (`/settings`)

The facility's own record: name, HFR/facility ID and its identifier type
(NHRR / ROHINI / PMJAY / provider number / other), identifier system, NHCX
participant ID, address with a state picker, phone, email, GSTIN.

This is not cosmetic — the organization row is the `custodian` of every FHIR
document and the `Organization` resource inside it, so changing the name or HFR
ID here changes what every subsequent export carries. The current facility name
shows beside the product name in the header.

## Master data (`/masters`)

Where a clinic is configured, as opposed to where patients are treated.

| Set | What it drives |
|---|---|
| **Doctors & staff** | Every consultant, admitting-doctor and interpreter picker. Retiring removes someone from pickers but keeps their name on the records they signed — never deleted. |
| **Pharmacy catalogue** | Items, MRP, GST, reorder level, dispensing SNOMED code. A new item is in the catalogue immediately but not issuable until stock is received. |
| **Wards & beds** | Tariffs, nursing rates, gender policy, the physical beds. A bed cannot be retired while occupied. |
| **Clinical codes** | All 41 pickers in one editor — diagnoses, complaints, medicines, lab panels/analytes, procedures, dialysis and wellness sets, the charge master, administrative lists. A lab panel added here is orderable immediately, analyte expansion and price included. |
| Dialysis machines | On `/dialysis/machines` — model, serial, location, service log. |
| Facility | On `/settings`. |

Deleting a concept only removes it from its picker; records keep the code and
display they were written with.

Some code sets use the `extra` column for structured data — the editor shows a
per-set hint. Notables: a lab panel's `extra` is
`category|comma-separated analyte codes|price`; a charge's is
`price|invoice-type code`.

## Clearing patient data (`/masters/maintenance`)

Deletes every patient record while keeping all masters — for handing over a
configured system, or wiping a trial run. The screen shows exactly what will go
and what will stay, requires the word `CLEAR` typed in, and runs in a single
transaction with `PRAGMA defer_foreign_keys`, so the wipe is judged on its end
state: either the whole clinic resets or nothing does.

* Beds and dialysis machines are **reset, not deleted** — physical assets keep
  their configuration, occupancy and status are cleared.
* Numbering counters drop, so the next patient is `MRN00001` again.
* The split is an allow-list: anything not in `masters.MASTER_TABLES` is
  cleared. A warning banner (and a failing test) appears if a schema change
  adds a table that is neither classified nor cleared.

## Running it

```bash
python3 run.py                      # http://127.0.0.1:8765
python3 run.py --host 0.0.0.0 --port 80    # LAN — read the caveats first
NANOEMR_DB=/srv/emr/clinic.db python3 run.py
```

SQLite runs in WAL mode with a thread-local connection per request thread; a
single-clinic load is comfortably inside its envelope. Back up by copying
`emr.db` (plus `-wal`/`-shm` if hot, or after a clean stop).

Static assets (logo, favicon) are served from an in-memory allow-list with a
week-long cache and content ETags — replacing a file requires a restart to be
picked up.

## Before production — the honest list

1. **Authentication, roles, audit.** There are none. Login, per-role access
   (front desk / nurse / doctor / pharmacy / accounts) and a change log are the
   first work before real patient data.
2. **Terminology.** The 324-concept seed is a demonstration subset. License
   SNOMED CT (free for Indian affiliates via NRCES), load the proper ValueSet
   expansions, and re-verify binding strengths — e.g. the vital-signs set the
   wellness profile binds is only five concepts.
3. **Official validation.** Run the HL7 validator with the NRCES IG package
   over representative bundles before any ABDM submission.
4. **Rate card.** Seeded tariffs, MRPs and GST rates are placeholders.
5. **TLS and backups** appropriate to wherever it is deployed.
