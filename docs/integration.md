# NHCX integration

How NanoEMR raises insurance claims over the National Health Claims Exchange
(NHCX). This is a working draft; scope so far is **policy search**, **coverage
eligibility (validation / discovery)**, **linking the admitted patient** and
**capturing the preauth dossier**. Insurance plan fetch, auth-requirements and
the preauth submission itself come next.

## Architecture

NanoEMR never speaks NHCX protocol itself. All registry lookups, FHIR bundle
building, JWE encryption (RSA-OAEP-256 / A256GCM), participant certificate
handling and gateway dispatch are delegated to a co-deployed **hcxkit**
gateway, which exposes a plain-JSON HTTP API on `http://localhost:8080`.

```
NanoEMR (emr/claims.py)          hcxkit                        ABDM / NHCX sandbox
─────────────────────────        ───────────────────────       ─────────────────────
policy search ──────────────────▶ /internal/policies/search ──▶ BIS get/policies
build bundle ───────────────────▶ /internal/mappings/map        (pmjay Lua template)
send check ─────────────────────▶ /fhir/out/v1/coverage-  ────▶ JWE ▶ /hcx/v1/
                                  eligibility/check              coverageeligibility/check
poll verdict ◀──────────────────  /internal/txn/related|fhir ◀─ payer on_check (JWE)
```

The exchange is **asynchronous**: sending a check only returns a queue
acknowledgement (`txn_id` + `correlation_id`). The payer's reply arrives later
at hcxkit's inbound endpoint on the same correlation id; NanoEMR picks it up
by polling the hcxkit transaction ledger.

Division of labour:

- **NanoEMR** owns the operator workflow, the claim ledger (`claim` table) and
  the flattened verdict; it stores the raw policy row and the full `on_check`
  bundle for audit.
- **hcxkit** owns participant identity (keys, certs, ABDM token refresh),
  bundle templating, encryption, dispatch/retry and the transaction ledger.

## Configuration

| What | Where | Current value |
|---|---|---|
| hcxkit base URL | env `NANOEMR_HCXKIT_URL` | `http://localhost:8080` |
| Default payer participant code | env `NANOEMR_PAYER_CODE` | `1518@hcx` (PMJAY) |
| Default payer name | env `NANOEMR_PAYER_NAME` | `Nhcx Pmjay` |
| Facility HFR / facility ID | `organization.identifier_value` (Settings) | `IN2710000123` |
| Facility NHCX participant code (sender) | `organization.participant_code` (Settings) | `1000003463@hcx` |

A coverage check refuses to run until both organization fields are set — the
operator is pointed at Settings rather than producing a malformed bundle.

hcxkit side: `config.json` holds the participant keys, the ABDM sandbox URLs
and the `fhirMap` group `pmjay: ["1518@hcx"]`. Its worker refreshes the ABDM
bearer token every 15 minutes; if that token is stale the policy search
returns 401 upstream.

## Step 1 — policy search

Screen: **Claims → New claim** (`/claims/new`).

The operator picks an identifier type and value; NanoEMR calls:

```
POST {hcxkit}/internal/policies/search
{"identifiertype": "MobileNo" | "AbhaNumber" | "MemberId",
 "identifiervalue": "..."}
```

hcxkit relays this to the ABDM Beneficiary Identification System
(`participant/get/policies`) with its bearer token and passes the reply
through verbatim. Observed sandbox reply is a top-level array:

```json
[{"abhanumber": "91703412374240", "memberid": "MD5SLS4X5",
  "mobilenumber": "", "payerid": "1518@hcx", "processingid": "1518@hcx",
  "productid": "PMJAY/HP/S/G", "productname": "PMJAY for Himachal",
  "sno": "200013271"}]
```

Field names upstream are inconsistent across environments, so
`claims._normalise_policy()` maps several spellings onto one vocabulary
(`member_id`, `policy_code`, `payer_id`, …). Notes from live testing:

- The sandbox carries the plan identifier in `productid` — that becomes the
  `policy_code` fallback when no dedicated policy-number field is present.
- "Nothing linked to this identifier" comes back as **HTTP 400, code
  NHCX-1016**; NanoEMR treats that as an empty result, not an error.
- The sandbox returns no beneficiary name and no photo at this stage.

Selecting a policy opens a claim (`CLM-nnnnn`) that snapshots the normalised
fields plus the raw row (`claim.policy_json`).

## Step 2 — coverage eligibility check

Screen: claim detail (`/claims/<id>`), "Coverage eligibility check" card.
Purpose is `validation` (is this policy in force?) or `discovery` (find
active coverage). Validation requires a policy code; both require a member ID.

**Build.** NanoEMR asks hcxkit to render its own pmjay template — the
productised form of `template/bundles/pmjay/coverage_validation/request.json`
— so what goes out is exactly what the kit's parsers expect back:

```
POST {hcxkit}/internal/mappings/map
{"group": "pmjay", "name": "coverage/request/check", "flow": "request",
 "input": {"purpose": "validation", "policyNumber": "PMJAY/HP/S/G",
           "pmjayId": "MD5SLS4X5", "subscriberId": "MD5SLS4X5",
           "providerId": "<HFR>", "providerName": "<facility name>",
           "payerId": "1518", "payerName": "Nhcx Pmjay",
           "abhaNumber": "…", "servicedDate": "YYYY-MM-DD"}}
```

The resulting collection Bundle contains:

| Resource | Populated with |
|---|---|
| `CoverageEligibilityRequest` | purpose, serviced date, references below |
| `Patient` | PMJAY member ID, ABHA, phone |
| `Organization` (prov) | **HFR ID** as NPI identifier + facility name |
| `Organization` (pay) | payer NIIP (`1518`) + payer name |
| `Coverage` | **policy code** as NH identifier, `subscriberId` = member ID |

**Send.** The bundle must be wrapped under `"fhir"`; only three JWE headers
are the caller's to set (the rest — api_call_id, request_id, correlation_id,
timestamp, status — are generated by hcxkit):

```
POST {hcxkit}/fhir/out/v1/coverageeligibility/check
{"jwe_headers": {"x-hcx-sender_code": "1000003463@hcx",
                 "x-hcx-recipient_code": "1518@hcx",
                 "x-hcx-workflow_id": "CLM-00002"},
 "fhir": { ...bundle... }}

→ 200 {"message": "Payload queued successfully",
       "txn_id": "01M07CJ…", "correlation_id": "<uuid>", …}
```

The claim number rides as `x-hcx-workflow_id`, so every leg of the episode is
groupable on both sides. hcxkit's worker then fetches the recipient's
encryption cert, JWE-encrypts, and POSTs to
`{nhcx}/v1/coverageeligibility/check`. Verified live: the sandbox gateway
answers **202 / `request.queued`**.

**Poll.** The claim sits in "Awaiting payer" and its page refreshes every 5 s.
Each refresh does one ledger pass:

1. `POST /internal/txn/related {"txnId": …}` — when a row with
   `direction: "in"` appears, that is the payer's `on_check`.
2. `POST /internal/txn/fhir {"txnId": <that row's id>}` — returns the stored
   envelope with the response bundle.
3. Gateway rejections arrive as a **plain-JSON `ProtocolResponse`**, not an
   encrypted on_check — e.g. `PAYR-1008` when the bundle's HFR ID doesn't
   match the registry id NHCX holds for the sender participant. hcxkit cannot
   read protocol headers off that payload, so its ledger row gets a *fresh*
   correlation id and `txn/related` never links it; NanoEMR therefore also
   scans recent inbound coverage rows and joins on the
   `x-hcx-correlation_id` *inside* the stored body
   (`claims._protocol_error()`), settling the claim as an error with the
   NHCX code and message.
4. If no reply yet, `POST /internal/txn/dispatch` is checked so a
   `dispatch_failed` / dead-letter (e.g. `CERT_NOT_FOUND` for an unregistered
   recipient) surfaces as a claim error instead of waiting forever.
5. A **404 "transaction not found"** from `txn/related` is terminal, not a
   hiccup: hcxkit's ledger no longer has the transaction (typically its
   database was reset after the check went out), so no reply can ever be
   matched to that txn id. The claim settles as an error telling the operator
   to send the check again (`claims.GatewayError` carries the HTTP status so
   `poll_response` can tell this apart from a transient failure, which still
   just shows the "could not poll" note and retries).

Polling is now the *fallback*, not the only route. hcxkit also pushes each
inbound envelope to the `participant.callbackUrl` on its profile, which points
at `POST /nhcx/callback` (`claims.receive()`): the verdict lands the moment it
arrives instead of waiting for an operator to open the claim. Notes:

- Every message type is delivered to that one URL, with `X-Hcxkit-Type` /
  `X-Hcxkit-Flow` saying which — anything that is not a coverage `on_request`
  is acknowledged and ignored.
- The worker reads the reply as a delivery outcome: 2xx delivered, 4xx
  dead-lettered, 5xx retried with backoff. So an unmatched correlation id
  answers 200 (retrying cannot help), an unreadable body 400, and only a
  genuine internal fault 500.
- Redelivery is safe: a claim that is no longer `checking` is left alone.
- The route is unauthenticated because the worker cannot hold a session. Point
  the callback at `127.0.0.1` so it is not reachable from outside the box, and
  set `NANOEMR_CALLBACK_TOKEN` to additionally require `?token=…` on the URL.
- Both halves converge on `apply_response()`, so a missed or misconfigured
  callback costs immediacy, never the verdict — opening the claim still polls.

**Verdict.** `claims.parse_validation_bundle()` flattens the reply. The
response bundle repeats the request's resources and appends the payer's own
`CoverageEligibilityResponse`, enriched `Patient` and `Coverage` — where a
resource type repeats, the *last* one is the payer's copy.

| Shown on the claim | Taken from |
|---|---|
| Eligible / not eligible | `insurance[0].inforce`, `outcome` |
| Disposition ("Policy is currently in-force") | `disposition` |
| Sum insured / utilised / **wallet balance** | benefit `allowedMoney` / `usedMoney`; balance = allowed − used |
| Pre-authorisation required | `item[].authorizationRequired` |
| Profile: name, gender, DOB, full address, ABHA | payer `Patient` |
| Photo | payer `Patient.photo` when present (sandbox sends none; the UI falls back to initials) |
| Plan name, period, relationship | payer `Coverage` (`class[0].name`, `period`, `relationship`) |

## Step 3 — link the admitted patient

Screen: claim detail, **Pre-authorisation tab**. Opens once the payer's
verdict is *eligible*.

The payer's `on_check` reply carries the beneficiary's ABHA number; NanoEMR
lists every **current IPD stay** (kind `IPD`, status not `finished`) of a
patient registered with that ABHA and the operator links one
(`claims.linkable_admissions` / `link_admission`). Notes:

- ABHA is compared **digits-only** — the payer writes `91-7034-…` while the
  front desk may have registered the patient without the dashes.
- Linking stores `claim.patient_id` + `claim.encounter_id` and defaults the
  preauth's admission date from the encounter's `period_start`.
- Linking is refused before an eligible verdict, and for any stay that is not
  a current IPD admission of the matching patient. Unlink keeps the preauth
  draft.

## Step 4 — preauth capture

Same tab, once linked. `claims.save_preauth()` validates and stores the
dossier in one transaction:

| Captured | Where | Rules |
|---|---|---|
| Admission / provisional discharge date | `claim.admission_date`, `claim.expected_discharge_date` | admission required; discharge ≥ admission |
| ICD-10 diagnoses | `claim_diagnosis` | ≥ 1; picked from the `diagnosis` master, stored with both the SNOMED and the ICD-10 coding |
| Care team | `claim_care_team` | ≥ 1 active practitioner + role (`claims.CARE_ROLES`) |
| Package case | `claim.package_code/-name` | rate read from the `claim_package` terminology master (HBP list, editable under Masters → Clinical codes) |
| Non-package case | `claim_item` | charge-master items at their **fixed** master price; only the quantity is the operator's; `amount = price × qty` |
| Estimated amount | `claim.preauth_total` | package rate, or the sum of items — always recomputed server-side, never taken from the form |
| Documents | `claim_document` | PDF / JPEG / PNG / WebP, ≤ 10 MB each, stored inline (BLOB); served back at `/claims/<id>/documents/<id>` |

Uploads ride on `multipart/form-data`, parsed by
`emr/web/router.parse_multipart()` (the stdlib lost `cgi`; this is a small
parser for what browsers actually send).

## Data model

One row per claim episode in `claim` (see `emr/schema.sql`): search inputs,
the selected policy snapshot, exchange bookkeeping (`txn_id`,
`correlation_id`, `purpose`, `checked_at`, `error_message`), the flattened
verdict, the admission link (`patient_id`, `encounter_id`) and the preauth
scalars, plus `policy_json` / `response_json` raw payloads for audit. The
preauth's repeating parts live in `claim_diagnosis`, `claim_care_team`,
`claim_item` and `claim_document` (all `ON DELETE CASCADE`).
Status machine: `draft → checking → eligible | not-eligible | error`, with
"check again" allowed from any settled state.

## Testing

- `selftest.py` section **"NHCX claims"** (10 checks, offline): claim
  creation/refusals, the settings guard, verdict parsing against a reduced
  `on_check` bundle, and status settlement. No network is touched — the guard
  refusals fire before any HTTP call.
- `selftest.py` section **"NHCX preauth"** (22 checks, offline): ABHA
  digits-only matching, link guards, every preauth validation refusal,
  price/total computation from the masters, child-row replacement on re-save,
  document type/size guards, and the multipart parser itself.
- Live sandbox identifiers that work end-to-end: MemberId `MD5SLS4X5`
  (beneficiary PALLVI, policy `PMJAY/HP/S/G`, "PMJAY for Himachal",
  ₹5,00,000 sum insured).

## Roadmap

1. ~~Policy search~~ (done)
2. ~~Coverage eligibility validation / discovery~~ (done)
3. ~~Link the admitted patient (ABHA ↔ current IPD stay)~~ (done)
4. ~~Preauth capture — dates, ICD-10, care team, package / items, documents~~
   (done)
5. Insurance plan fetch — hcxkit already routes `v1/insuranceplan/request`
6. Coverage eligibility **auth-requirements** purpose (needs `items[]` with
   package/procedure codes)
7. Raise preauth — `v1/preauth/submit` from the captured dossier (Task-based
   bundle, see `hcxkit/pmay_bundle/coverage_auth_request.json`; hcxkit's
   `pmjay/preauth` template is still a skeleton and needs the diagnosis /
   item / attachment mappings before this can go out)
