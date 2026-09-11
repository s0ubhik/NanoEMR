# NHCX integration

How NanoEMR raises insurance claims over the National Health Claims Exchange
(NHCX): **policy search**, **coverage eligibility (validation / discovery /
auth-requirements)**, **the payer's package master (InsurancePlan)**,
**linking the admitted patient**, **the preauth dossier**, a
**predetermination quote**, the **pre-authorisation** (with enhancements and
cancellation), the payer's **queries**, the **claim** (normal, LAMA/DAMA and
death discharges), **status** and **reprocess** asks, and the **payment
notices**. Together they cover every step of the Dummy IRDAI Payer's
integration checklist; `scripts/e2e/nanoemr_payer_flow.py` plays them all.

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
fetch package master ───────────▶ /fhir/out/v1/insurance- ───▶ JWE ▶ /hcx/v1/
                                  plan/request                   insuranceplan/request
poll / callback ◀───────────────  /internal/txn/related|fhir ◀─ payer on_check / on_request
```

The exchange is **asynchronous**: sending a check only returns a queue
acknowledgement (`txn_id` + `correlation_id`). The payer's reply arrives later
at hcxkit's inbound endpoint on the same correlation id; NanoEMR picks it up
by polling the hcxkit transaction ledger.

Division of labour:

- **NanoEMR** owns the operator workflow, the claim ledger (`claim` table),
  the flattened verdict and the payer's package master (`claim_plan`); it
  stores the raw policy row and the full `on_check` / `on_request` bundles for
  audit. The InsurancePlan request bundle is the one NanoEMR builds itself —
  hcxkit ships no `pmjay/insurance` template to render it, and the payload is
  a two-resource lookup with no clinical content.
- **hcxkit** owns participant identity (keys, certs, ABDM token refresh),
  bundle templating, encryption, dispatch/retry and the transaction ledger.

The claim screen (`/claims/<id>`) walks the episode in the order it happens:
**Eligibility → Insurance plan → Line items → Validate → Pre-authorisation →
Communication → Claim → Payments**. Insurance plan is the payer's package
master; Line items are the lines quoted from it (the tab says so until the
plan has been fetched; quantities are whole numbers, and a package the plan
prices at zero takes the hospital's own price); Validate sends that procedure
set to the payer for its ruling; the Pre-authorisation tab appears once the
payer has ruled (at once for a payer that does not rule) and holds the
dossier (stay,
diagnoses, team, forms, documents) and the button that sends it;
Communication collects the payer's questions on
either leg and the answers.

## Configuration

| What | Where | Current value |
|---|---|---|
| hcxkit base URL | env `NANOEMR_HCXKIT_URL` | `http://localhost:8080` |
| Default payer participant code | env `NANOEMR_PAYER_CODE` | `1518@hcx` (PMJAY) |
| Default payer name | env `NANOEMR_PAYER_NAME` | `Nhcx Pmjay` |
| Facility HFR / facility ID | `organization.identifier_value` (Settings) | `IN2710000123` |
| Facility NHCX participant code (sender) | `organization.participant_code` (Settings) | `1000003463@hcx` |
| Callback base hcxkit pushes to | hcxkit `participant.callbackUrl` | `http://127.0.0.1:8765/callback` |
| Callback shared secret (optional) | env `NANOEMR_CALLBACK_TOKEN` | unset |

A coverage check refuses to run until both organization fields are set — the
operator is pointed at Settings rather than producing a malformed bundle.

### Payer adapters

NHCX standardises the envelope, not the contents. What a payer actually puts in
a bundle is scheme-specific — PMJAY quotes package codes under its own
`payer.pmjay.nha.gov.in` system, stamps every claim line `AB-PMJAY`, answers an
`auth-requirements` check at all, and hides the *stage* of a required document
in a free-text `Type: pre` string. None of that is in the spec; all of it is in
the samples. So it lives behind one lookup in `emr/payers.py`, and the exchange
code asks the adapter rather than hard-coding a scheme.

Which adapter a claim uses is **configuration, not code** — the `payer_adapter`
terminology kind, editable under **Masters → Clinical codes → Payer adapters**,
one row per payer:

| Column | Holds | Seeded values |
|---|---|---|
| `code` | the payer's NHCX participant code | `1518@hcx`, `1000004805@hcx` |
| `display` | what an operator calls them | PMJAY / Ayushman Bharat, Dummy IRDAI Payer |
| `extra` | the adapter key | `pmjay`, `kyrocare` |

Matching is on the numeric part, so `1518`, `1518@hcx` and `1518@HCX` all reach
the same row. An unmapped payer — or one whose row names no key — falls back to
`generic`, which sends a plain NRCES bundle with none of PMJAY's extras and
declines the checks PMJAY-specific. That is the right default for a payer
nobody has characterised yet, and a visible one: the pre-authorisation screen
names the adapter in use.

| Adapter carries | `pmjay` | `kyrocare` | `generic` |
|---|---|---|---|
| `payer_system` | `https://payer.pmjay.nha.gov.in` | `https://kyro.care/fhir` | `https://nhcx.abdm.gov.in` |
| `program_code` | `AB-PMJAY` on every claim line | none | none |
| `auth_requirements` | yes | yes | no |
| `preauth_stages` | `pre` | `pre` | `pre` |

`kyrocare` is the IRDAI payer portal in this repository (`apps/irdai-payer`).
It publishes its package master the PMJAY way — a `Procedure` cost per package
under `plan.specificCost`, the conditions and document requirements on the
benefit, a treatment-guideline questionnaire beside it — and answers the
`auth-requirements` check per item with the documents due at each stage in
PMJAY's spelling (`Type: pre` / `Type: post`, `fullUrl:` for the form), so
the one reader serves both. Its packages carry no rate until an underwriter
prices them on the portal's procedure registry; an unpriced package shows
here with no rate and quotes at zero, which the portal's plan preview reports
as an issue on its side.

Reading the payer's free text is the adapter's job too:
`payers.supporting_entry()` turns one `authorizationSupporting` entry into
kind, code, stage, form url and the procedure it was asked for.

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
inbound envelope to the `participant.callbackUrl` on its profile
(`claims.receive()`): the verdict lands the moment it arrives instead of
waiting for an operator to open the claim. Notes:

- The kit appends the message's own inbound route (its `inMap` url for that
  type/flow) to the callback base, so a profile configured as
  `http://127.0.0.1:8765/callback` delivers the payer's coverage on_check to
  **`POST http://127.0.0.1:8765/callback/v1/coverageeligibility/on_check`**.
  NanoEMR answers every path under `/callback`, plus the flat
  `/nhcx/callback` for a profile still pointed at the old URL — a route the
  kit spells differently (its `inMap` currently carries
  `v1/coverageeligibility/check` for coverage `on_request`) therefore still
  lands instead of dead-lettering as a 404.
- Every message type is delivered to that same base, with `X-Hcxkit-Type` /
  `X-Hcxkit-Flow` saying which — the headers decide, not the path, and
  anything that is not a coverage `on_request` is acknowledged and ignored.
- The worker reads the reply as a delivery outcome: 2xx delivered, 4xx
  dead-lettered, 5xx retried with backoff. So an unmatched correlation id
  answers 200 (retrying cannot help), an unreadable body 400, and only a
  genuine internal fault 500.
- Redelivery is safe: a claim that is no longer `checking` is left alone.
- The routes are unauthenticated because the worker cannot hold a session.
  Point the callback at `127.0.0.1` so it is not reachable from outside the
  box, and set `NANOEMR_CALLBACK_TOKEN` to additionally require `?token=…` on
  the URL.
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

## Step 3 — the payer's package master (InsurancePlan)

Screen: claim detail (`/claims/<id>`), **Insurance plan** tab.

The InsurancePlan exchange is the only NHCX transaction whose request carries
no clinical content at all: the provider names a policy and itself, and the
payer answers with the machine-readable contract governing every later preauth
for that policy–provider pair. It is *provider-specific* (only what the MoU
empanels), *policy-specific*, and cacheable but worth refreshing.

**Build.** NanoEMR builds this bundle itself (`claims.build_plan_request()`),
modelled on hcxkit's PMJAY sample `pmay_bundle/insurance_request.json` — a
collection Bundle carrying a `Task` and the provider `Organization` it points
at:

| Element | Value |
|---|---|
| `Bundle.identifier` | the claim number, under `https://payer.pmjay.nha.gov.in` |
| `Task.status` / `Task.intent` | `requested` / `plan` |
| `Task.code` | `poll` — a fetch, not a create or update |
| `Task.input[]` | `policyNumber` (the claim's policy code) and `providerId` (the facility's HFR ID) |
| `Task.requester` | the provider Organization, by the sample set's absolute anchor URL |
| `Organization` (prov) | HFR ID as NPI identifier, facility name and phone |

Two deliberate readings, both flagged rather than silently chosen:

- **`Task.intent`.** The sample sends `plan`; the published Insurance Plan IG's
  element table says `order`. Both are legal FHIR Task intents. The sample
  wins because it is the shape this payer has been exercised with —
  `claims.PLAN_TASK_INTENT` is the single place to flip it.
- **`Task.code`.** The sample omits it; the IG marks it `1..1` with code
  `poll`. It is sent, since it is additive and hcxkit's own inbound parser
  (`template/insurance/in/request.lua`) never reads it.
- **At least one `Task.input` is mandatory.** Both go when both are known,
  which is the more precise lookup; a request with neither is refused before
  any HTTP call.

**Send.** Same envelope discipline as the check — three caller-owned headers,
the rest generated by hcxkit:

```
POST {hcxkit}/fhir/out/v1/insuranceplan/request
{"jwe_headers": {"x-hcx-sender_code": "1000003463@hcx",
                 "x-hcx-recipient_code": "1518@hcx",
                 "x-hcx-workflow_id": "CLM-00002"},
 "fhir": { ...Task bundle... }}
```

That route is registered from hcxkit's `outMap` (tag `insuranceIN`, url
`v1/insuranceplan/request`) and dispatches to `{nhcx}/v1/insuranceplan/request`
— the target path *is* the route path, so the `flow: on_request` label on that
outMap entry is cosmetic and does not misroute the request.

**Reply.** The payer answers on `v1/insuranceplan/on_request`, picked up the
same two ways as the verdict: hcxkit pushes it to
`POST /callback/v1/insuranceplan/on_request`, and opening the claim polls
(`claims.poll_plan()`) — a 404 from `txn/related`, a `ProtocolResponse`
rejection and a dead-lettered dispatch all settle the plan as an error rather
than spinning.

> **The flow label is not the router.** hcxkit's `inMap` spells the coverage
> and insurance directions the opposite way round, so the *same* direction of
> travel arrives labelled `X-Hcxkit-Flow: on_request` for coverage and
> `request` for insurance. `claims.receive()` therefore routes on
> `X-Hcxkit-Type` and matches the correlation id; flow is not a filter.

**Parse.** `claims.parse_plan_bundle()` flattens the InsurancePlan into one
`claim_plan` row plus one `claim_plan_benefit` per package. Both published
shapes are read, because the payer picks:

| Shape | Tree | Flattens to |
|---|---|---|
| Package master (PMJAY) | `plan → specificCost → category → benefit → cost` | speciality, package code/name, package rate |
| Indemnity | `coverage → benefit → limit` | coverage type, benefit code/name, limit value |

The rules below were **corrected against a live sandbox bundle** — 1442
entries, 1053 distinct packages across 7 specialities, ₹5,00,000 sum insured —
after a first pass built from the guides alone got three of them wrong:

- **The payer sends both shapes, for the same packages.** 975 codes under
  `specificCost` and a 1053-code superset under `coverage`, overlapping
  completely. They are merged on the package code, `specificCost` winning
  where both describe one (its costs are explicitly typed) and `coverage`
  contributing the 78 it alone carries. Concatenating them — the first
  reading — doubled every package.
- **The cost coded `Procedure` is the package rate.** The other cost codes
  seen live are `Stratification` (the Routine Ward / HDU / ICU / ICU-with-
  ventilator per-day tiers, named by the `qualifiers` on that cost) and
  `Implant`. Those are money paid *over and above* the rate, so they are kept
  in `claim_plan_benefit.extras` rather than mistaken for it — matching on the
  cost `type.coding.code`, never on its display, which is a whole sentence
  ("Selected treatment or service or proudct is a type of procedure or
  package", payer's spelling) and contains the word *package* on every cost.
  A `Procedure` value of **0 is real**: 166 packages are priced entirely by
  their ward tiers, with no base fee.
- **Conditions and document requirements are different things** and live under
  different url families — `Claim-Condition` and
  `Claim-SupportingInfoRequirement` (the guides write them `claimCondition` /
  `claimSupportingInfoRequirement`; `claims._family()` matches either
  spelling). A condition's own child url *names* it
  (`…/Claim-Condition/IsDayCare` → `IsDayCare: "Y"`), and all 20 published
  PMJAY condition codes come through that way; a requirement's children are a
  `category` and a `code` CodeableConcept naming the document (`MAND0409`,
  "any investigations done"), stored separately in `supporting_info`. Folding
  both into one bag — the first reading — filled the Conditions column with
  `MAND…` codes and stray `documentationUrl` keys.
- `plan.generalCost` is the *overall* sum insured; the per-package rates are
  in `specificCost`.
- **A requirement can point at a form.** Its `documentationUrl` child is a
  reference to a `Questionnaire` shipped in the same bundle —
  `/policy/questionnaire/<id>` for a policy form, `/policy/stgquestionnaire/<id>`
  for a standard-treatment-guideline checklist. The live plan carries 2865
  such references over 991 distinct forms, and **all 991 resolve in-bundle**.
  The payer repeats a form once per benefit that needs it (1440 resources for
  those 991), so they are collected by url into `claim_plan_form`.
- **Questions live on `item.prefix`, not `item.text`** — 7423 against 81 in
  the live plan — so both are read, prefix first. Item types seen:
  `attachment`, `choice`, `dateTime`; a `choice` carries its options as
  `answerOption[].valueString` ("Yes" / "No"), not as codings.
- **Requirements on the InsurancePlan resource itself are policy-wide** —
  proof of identity, proof of address — and apply to every claim whatever the
  package. They land in `claim_plan.policy_documents`. Their children use a
  second spelling (`SupportInfoCategory` / `SupportInfoCode` beside
  `category` / `code`); both are read. They are parsed and stored but shown
  nowhere at present — they had a card on the plan tab and it was taken off.
- **An empty plan is a legitimate answer**, not a transport failure: it means
  no coverage matches this policy–provider pair. The plan settles as `empty`
  and the preauth falls back to the local HBP master.

Not yet used: the ward/ICU tiers are shown on the package row (`+4 tier(s), up
to ₹4,500`) but do not feed the preauth estimate, which still quotes the flat
package rate. Stratification-aware estimates belong with the preauth
submission work.

**Browse.** A payer master is a thousand packages, so the tab is a search, not
a scroll: one box matching the procedure **name or code** (substring, either
case), narrowed by speciality and by what the code names — `Procedure` or
`Implant`. That type is derived, not published: every `specificCost` benefit
carries a `Procedure` cost, and the 78 codes `coverage` adds are exactly the
codes that appear as qualifiers on `Implant` costs elsewhere in the bundle,
so they are typed as implants rather than passed off as procedures.

Each row is code, name, speciality, type and a **View** control opening the
package in full: rate, **the implants approved for it**, the ward and ICU
tiers, all its claim conditions, every document the payer will want, and **the
forms those documents point at** — rendered as the questions they are:
question text, answer type and, for a choice, its options.

An implant is named twice in the plan — as a qualifier on the `Implant` cost
of each package that may use it, and as an entry of its own carrying its rate,
conditions and documents — so the item view joins the two and links them. A
package shows the implants it allows (with `ImplantApplicable`,
`MultipleImplantsAllowed` and `MaximumImplantsAllowed` beside them); an implant
shows the packages that allow it, the same relation read backwards. On the live
plan 112 packages approve implants, all 78 implants are used by at least one,
and **every implant tier rate equals that implant's own rate** — the tier's
figure is still what is shown, because that is what this package was quoted. Above the table sits what every claim under the policy
needs regardless of package, and an **All forms** page lists the plan's
questionnaires (991 on the live plan) with their own search. A requirement
whose form the payer referenced but did not ship says so rather than going
quiet. That view is a real route
(`/claims/<id>/plan/<benefit>`), so it is a page of its own with JavaScript
off; the tab asks the same route for `?fragment=1` and shows it in a modal.
One modal shell is filled on demand — a dialog per row would dwarf the page it
sits on.

**Use.** Once a plan is `ready`, `claims.packages(claim_id)` returns the
payer's packages instead of the local `claim_package` terminology, so the
preauth's package picker — and the server-side rate it recomputes — come from
the payer's own master for this policy and this facility. Refetching replaces
the master wholesale; a stale package never survives it.

## Step 4 — link the admitted patient

Screen: claim detail, **Pre-authorisation** tab. Opens once the payer's
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

## Step 5 — preauth capture

Same tab, once linked. `claims.save_preauth()` validates and stores the
dossier in one transaction:

| Captured | Where | Rules |
|---|---|---|
| Admission / provisional discharge date | `claim.admission_date`, `claim.expected_discharge_date` | admission required; discharge ≥ admission |
| ICD-10 diagnoses | `claim_diagnosis` | ≥ 1; **read off the linked admission** (`condition` rows of category `diagnosis`, coded from the same `diagnosis` master) and stored with both the SNOMED and the ICD-10 coding; the form only offers a picker when the admission has recorded none |
| Treating doctor | `claim_care_team` | the admission's consultant (`encounter.practitioner_id`) as `treating`; the form only offers a picker when the admission names none (`claims.admission_dossier`) |
| Package case | `claim.package_code/-name` | quoted line by line off the payer's plan (Step 6) once one is fetched; without a plan, one code picked from the local `claim_package` terminology (HBP list, editable under Masters → Clinical codes) |
| Non-package case | `claim_item` | charge-master items at their **fixed** master price; only the quantity is the operator's; `amount = price × qty` |
| Estimated amount | `claim.preauth_total` | package rate, or the sum of items — always recomputed server-side, never taken from the form |
| Documents | `claim_document` | PDF / JPEG / PNG / WebP, ≤ 10 MB each, stored inline (BLOB); served back at `/claims/<id>/documents/<id>`. Each row records the payer requirement `code` it answers, if any |

The **package / non-package toggle** routes the two: *non-package* stays the
itemised charge-master repeater, while *package* hands over to
[Step 6](#step-6--quoting-the-preauth-from-the-payers-own-master) — it shows
what is quoted with its running total and a way through to the line-items
screen, and saving refuses a package case with no procedure quoted. A single
"pick one package" dropdown survives only for a claim whose plan has not been
fetched, where the local HBP list is all there is.

Uploads ride on `multipart/form-data`, parsed by
`emr/web/router.parse_multipart()` (the stdlib lost `cgi`; this is a small
parser for what browsers actually send).

## Step 6 — quoting the preauth from the payer's own master

Screen: **Claim → Line items → Choose line items** (`/claims/<id>/lines`).

A PMJAY preauth does not quote a hospital's price list. It quotes three things
off the payer's package master, and the page is built around exactly that:

| Line kind | Where its price comes from |
|---|---|
| **Procedure** | the benefit's `Procedure` cost — the package rate — times a quantity |
| **Implant** | the implant's own entry in the plan |
| **Stratification** | the ward / HDU / ICU tier on *that procedure's* costs — different procedures price the same ward differently, so a tier is always added through the procedure that offers it |

Three parts to the screen: what is already quoted (with editable quantities
and a running total), **what the payer says goes with it**, and the whole
master to search when something else is needed. The middle part is the point
of having fetched the plan at all — pick `SV015B` and the page offers Arch
Graft, Coselli Graft and the complex grafts, because the payer's own bundle
says those three are the implants approved for it; pick `MG004A` and it offers
Routine Ward / HDU / ICU / ICU-with-ventilator. Anything already quoted drops
out of the suggestions.

Prices are read from the plan at add time and the amount is always recomputed
server-side as `rate × quantity` — the form supplies the quantity and nothing
else. A code the plan does not carry, a tier the named procedure does not
offer, a duplicate line and a zero quantity are all refused. Quoting anything
at all is refused until the plan has been fetched.

The chosen lines then pull in the **questionnaires** the payer attached to
them (`claim_plan_form`, reached through each benefit's `supporting_info`),
and those render as forms on the preauth tab. **Each question is answered in
the shape it declares**, because the payer means the type it published — the
live plan's 7502 questions are `choice` (7461), `attachment` (21), `dateTime`
(12) and `string` (10):

| Question type | Control | On the wire |
|---|---|---|
| `choice` | a select of its `answerOption` values, defaulting to the one the payer marked `initialSelected` | `valueString` |
| `dateTime` / `date` / `time` | the matching picker | `valueDateTime` / `valueTime`, stamped `+05:30` |
| `attachment` | a file input; the file joins the claim's documents and the answer records which one | `valueAttachment` with contentType and base64 |
| `boolean` | Yes / no | `valueBoolean` |
| `integer` / `decimal` | a number box, falling back to text if what was typed is not one | `valueInteger` / `valueDecimal` |
| `string` / anything else | a text box | `valueString` |

The payer's own sample only ever shows `choice` answers, so the non-string
value types follow FHIR rather than an example — a `dateTime` question answered
with a `valueString` would be the wrong resource whichever way it is read.

Answers are stored per question in `claim_form_answer`; a file answer stores
the id of the document it uploaded, so re-saving a form without touching its
file question leaves the file attached.

## Step 7 — validating the procedure set (auth-requirements)

Screen: claim detail → **Validate**, "Authorisation requirements".

The ruling is about the set it was asked about. Add or remove a line
afterwards and the card says so ("The line items have changed since this
ruling") and offers *Check again* — `claims.ruling_is_stale` compares the
ruled codes with the quoted ones (ward tiers excepted).

Choosing lines is the provider's opinion; this is the payer's. The quoted set
goes back as a **coverage eligibility check with `purpose: ["auth-requirements"]`**
— the same `/fhir/out/v1/coverageeligibility/check` route as the validation
check, carrying `item[]` in the *same shape the Claim will* (`productOrService`,
`category`, `programCode`, `servicedPeriod`, `quantity`/`unitPrice`/`net`), so
one helper builds the items for both.

The reply's `insurance[0].item[]` answers **per line**:

| Element | Read as |
|---|---|
| `authorizationRequired` | does this line need authorising |
| `excluded` | is it excluded from the policy |
| `benefit[].type` / `allowedMoney` | benefit kind (Procedure / Implant / …) and what is allowed |
| `authorizationSupporting[]` | what the *set* has to be accompanied by |

`authorizationSupporting` is where the interesting part is, and it is not
coded. Each entry's free-text `text` carries the two facts that decide what to
do with it:

```
"text": "Type: pre\n Procedure Code:SE020A"     → a document, wanted now
"text": "Type: post\n Procedure Code:SE020A"    → a document, wanted with the claim
"text": "fullUrl: https://payer.gov.in/policy/questionnaire/100008"
                                                → a questionnaire to answer
```

The adapter reads it (`payers.supporting_entry()`), and only the requirements
whose stage is in `preauth_stages` are asked for at pre-authorisation. The rest
are listed as *"needed later, with the claim"* and left alone. That is the
whole point of running the check: without it the screen has to ask for
everything the package master ever attached to those codes, which is a
superset — with it, `claims.required_forms()` narrows to the forms the payer
named for **this** set, and `claims.required_documents()` names the documents
to attach now.

The documents it names get **a file picker each**, the same way a
questionnaire's `attachment` question does: choose a PDF against `MAND0671`
and the file is filed under that code, shown back as a link, and quoted to the
payer as `MAND0671` rather than as an anonymous attachment. Choosing another
file for the same code replaces it — one file per requirement, so the payer is
never sent two against one code. Requirements the ruling deferred get no
picker here at all. A free-form upload remains for anything else, with the
named codes as a dropdown and "other document (ODN)" as the default.

The ruling lands like every other coverage reply, on
`POST /callback/v1/coverageeligibility/on_check`. Two coverage exchanges now
run per claim, so `claims.receive()` tries `claim` first and then `claim_auth`
on the correlation id.

Stored in `claim_auth` (the exchange and the disposition), `claim_auth_item`
(one row per line ruled on) and `claim_auth_requirement` (one per document or
form, with the payer's `stage` and the adapter's reading of it). Refused before
any HTTP call: an empty procedure set, a claim whose policy is not eligible,
and a payer whose adapter does not answer this check.

## Step 8 — submitting the preauth

**Build.** `claims.build_preauth_bundle()` assembles the Claim bundle
NanoEMR sends, modelled on hcxkit's sample
(`pmay_bundle/preauth_request.json`):

| Entry | Carries |
|---|---|
| `Claim` | `use: preauthorization`, the identifiers, the diagnoses, the care team, one `item` per quoted line, `supportingInfo`, and `total` |
| `Patient` | PMJAY member ID and ABHA (JHN), name, gender, birth date |
| `Organization` ×2 | provider (HFR as NPI) and payer (NIIP) |
| `Coverage` | policy code as NH identifier, the stay's period, the payer as payor |
| `Practitioner` ×n | one per care-team member, HPID from the staff master |
| `Procedure` ×n | one per **procedure** line — implants and tiers are items, not procedures |
| `QuestionnaireResponse` ×n | one per form that was actually answered |

Every intra-bundle reference is an absolute anchor under
`https://payer.nha.gov.in/preauthorization/v1/preauth/submit/claim/…`, as the
whole PMJAY sample set uses. `Claim.item` carries `careTeamSequence`,
`diagnosisSequence` and `procedureSequence` back to those arrays, the
speciality as `category`, `AB-PMJAY` as `programCode`, the stay as
`servicedPeriod`, and `quantity` / `unitPrice` / `net`.

`supportingInfo` is where three different things live, which the sample makes
plain only by example:

- **documents** — each attachment base64 in a `valueAttachment`, under the
  code it was filed against: a file uploaded against one of the payer's named
  requirements quotes that code (`MAND0671`), and only a file nobody asked for
  by name falls back to `ODN`, "other document";
- **scalars** — `EDT` (EncounterDateTime) and `ADDD` (admission–discharge),
  as `valueString`;
- **answers** — a `valueReference` to a `QuestionnaireResponse` in the same
  bundle, category `STG` for a treatment-guideline form and `INF`/`ODN`
  otherwise. A form nobody answered is left out rather than sent empty: to the
  payer a blank response is a different claim from no response.

**Send.** `POST {hcxkit}/fhir/out/v1/preauth/submit` with the same three
caller-owned headers, dispatching to `{nhcx}/v1/preauth/submit`. The claim
number rides as `x-hcx-workflow_id`.

**Reply.** The payer answers on `v1/preauth/on_submit`, which reaches NanoEMR
the same two ways as everything else — pushed to
`POST /callback/v1/preauth/on_submit` (`X-Hcxkit-Type: preauth`) and polled
from the ledger when the claim is opened. A 404 from `txn/related`, a
`ProtocolResponse` rejection and a dead-lettered dispatch all settle it as an
error rather than spinning.

Two things about that reply cost a live approval before they were understood.

**`outcome` alone never tells you the decision.** `complete` comes back for
both a full approval and an outright rejection; what separates them is
`adjudication[].reason`. The handbook's §8.5 / §9.5.1 mapping, which
`claims.verdict_status()` implements:

| `outcome` | reason | Status |
|---|---|---|
| `complete` | `approved` | **approved** |
| `partial` | `approved` | **partially approved** |
| `partial` | `queried` | **queried** — still live, answer with more |
| `complete` | `cancelled` | **rejected** — closed |
| `queued` | `submitted` | **not a decision** — acknowledged, wait |

That last row is the live sandbox's own: *"Request acknowledged and accepted
for further processing"* arrives as `outcome: queued` before any verdict.
Reading it as a decision settles the pre-authorisation on an acknowledgement.

**A pre-authorisation is answered more than once, on one correlation id.**
Observed live: workflow 20 acknowledging, then workflow 21 approving at
₹8,175. Refusing anything after the first reply — which a
"settled means done" guard does — threw the approval away and answered
`{"status": "ignored"}`. Every reply is now applied; redeliveries are told
apart by `x-hcx-api_call_id`, falling back to "says exactly what the row
already says" for a payer that omits it.

Totals are read by matching `total[n].category.coding.code` — never
positionally, since the handbook prints every category against `total[1]` as a
documentation artefact. The payer's query trail (a pipe-delimited
`USER~datetime~type~comment~trust` string on a `reason`-categorised item
adjudication, not a code) is kept verbatim in `query_note` and shown on the
claim.

Guards, all firing before any HTTP call: the policy must be eligible, an
admission linked, an admission date entered, at least one diagnosis, at least
one care-team member and at least one quoted line.

## Step 8b — a predetermination quote

*Ask for a quote* on the pre-authorisation tab sends the dossier as it stands
— the very bundle Step 8 would send — with `Claim.use` changed to
`predetermination` (`claims.build_predetermination_bundle`), on the same
`preauth/submit` route and workflow id, since that is the route the gateway
knows and `use` is what tells the payer what it is. The payer prices it at
once against its rules and answers on `preauth/on_submit` with a
`ClaimResponse` that binds nobody and opens no case: what the policy would
allow (`total[benefit]`) and, when it would cut or refuse, why
(`disposition`). Each ask is its own row in `claim_predetermination`
(`asking` → `answered` | `error`), read with the same parser as a verdict
(`claims.parse_claim_response`) by `claims.poll_predetermination` on
Refresh or through the callback when no pre-authorisation matches the
correlation id. Nothing on the pre-authorisation itself changes: a quote is
a question, and the submission in Step 8 is what commits the payer.

## Step 9 — cancelling a pre-authorisation

Screen: claim detail → **Pre-authorisation**, once one is out or granted.

**Enhancement.** Add a line item after the payer has decided and the
pre-authorisation card shows it as an enhancement pending; *Submit
enhancement* sends a `Claim` (`use: preauthorization`) carrying **only the
added lines**, with `related[]` (relationship `prior`) naming the
`preAuthRef` the payer gave, so the payer files the lines on the same case
and decides the addition rather than opening a second case. The row keeps
its `preauth_ref`, counts the round in `enhancement_no`, and the bundle it
stores merges what earlier rounds asked for, so the next addition is
measured against the whole (`claims.enhancement_lines`).

A pre-authorisation can be withdrawn while the payer still has it and after it
has been granted or queried — not once it has been refused, and not twice
(`claims.CANCELLABLE`). It is not a resubmission: a `Task` asks the payer to
do something further with a thing that already exists.

**Build.** `claims.build_cancel_bundle()`, modelled on hcxkit's sample
(`pmay_bundle/preauth_cancel_request.json`): a `Task` and the two
Organizations it runs between.

| Element | Value |
|---|---|
| `Task.status` / `intent` | `requested` / `order` |
| `Task.code` | `cancel`, from `…/CodeSystem/financialtaskcode` |
| `Task.reasonCode` | one of the seven Appendix B codes, under `…/ndhm-reason-code` |
| `Task.description` | the operator's note, or a sentence built from the reason |
| `Task.requester` / `owner` | the provider and payer Organizations in the bundle |
| `Task.input[]` | the claim number as `claimNumber`, `initimationNumber` **and** `intimationNumber` |

Two deliberate readings:

- **The intimation number is spelled two ways.** The handbook documents
  `intimationNumber` and notes that the real payload uses
  `initimationNumber`; the sample carries the misspelling. Both go out, since
  a payer reading either finds it and one reading neither is the only failure
  worth avoiding.
- **No `basedOn`.** The handbook insists a Task carries a `basedOn` reference
  to the entity being acted on, but the sample has no Claim entry to point at
  and identifies the target through `Task.input` instead. A reference dangling
  outside the bundle would be worse than none, so the sample wins — flag this
  if a payer rejects it.

Reasons (`claims.CANCEL_REASONS`): `treatmentplanchanged`, `patientrequest`,
`financialconstraints`, `alternativetreatment`, `duplicateclaim`,
`administrativeerror`, `other`. **`other` makes the note mandatory** — with no
code to read, the free text is the only justification the payer gets.

**Send.** `POST {hcxkit}/fhir/out/v1/task/submit`, under
`x-hcx-workflow_id` **11** (`claims.CANCEL_WORKFLOW_ID`) — PMJAY pins the
workflow per exchange, so it is a constant of the message, not of the claim.

**Reply.** The payer answers on `v1/task/on_submit` with a **Task bundle, not
a bare ClaimResponse**: `Task.status` is `completed` and the adjudication hangs
off `Task.output[].valueReference`, resolved against the bundle's own entries.
`claims.parse_task_response()` digs it out and hands it to the same
ClaimResponse parser the submission uses. It reaches NanoEMR as
`X-Hcxkit-Type: task`, matched on the cancellation's own correlation id — the
submission's ids are kept alongside, so an audit still shows both legs.

Settles `cancelling → cancelled`; a payer that does not accept it puts the
pre-authorisation back to `approved` with the reason recorded, and the claim
number untouched — the payer still holds the preauth under it.

**An accepted cancellation retires the claim number.** The payer holds
`CLM-00043` against the pre-authorisation it just withdrew, so anything sent
under that number again is a duplicate of a cancelled case. `apply_cancel()`
allocates a fresh `CLM-` for the episode in the same transaction, and the
withdrawn number stays on the preauth row as `claim_ref` — the claim screen
shows both, which number was withdrawn and which the episode carries now.

## Step 10 — the claim

Screen: claim detail → **Claim** tab, which opens once a pre-authorisation is
approved (or queried) and the patient has left.

The bundle is the pre-authorisation's, with three differences: `use: claim`,
a `billablePeriod` spanning the stay, and a rebuilt `supportingInfo` carrying
**how the stay ended**. Bundle id `CLAIM`; sent to
`POST {hcxkit}/fhir/out/v1/claim/submit` under `x-hcx-workflow_id` **15**
(`claims.CLAIM_WORKFLOW_ID`).

### Discharge

Four modes, each a `DIS` disposition code the payer reads:

| Mode | `DIS` code | Meaning |
|---|---|---|
| Normal discharge | `DTH` | discharged home |
| LAMA | `LAMA` | left against medical advice |
| DAMA | `DAMA` | discharged against medical advice |
| Death | `DTM` | died in hospital |

with a **stage** — *Before / During / After Surgery* — and the discharge,
surgery and (for a death) death dates. On the wire:

- the **discharge summary** is an `HDS` attachment whose *code is the mode in
  brackets* — `(Normal)`, `(LAMA)`, `(DAMA)`, `(Death)` — exactly as the
  payer's sample writes it;
- the **disposition** is a `DIS` category with the mode as its code and the
  stage as its `valueString`;
- **admission**, **surgery**, **discharge** and **death** dates each get their
  own category (`ADMD`, `SURD`, `DSCHD`, `ONS`), the last coded `DTM`.

**A LAMA or DAMA discharge before or during surgery collapses the claim.** The
payer accepts only procedure `LM100` for that case and *disqualifies every
item the pre-authorisation approved* (PAYR-1362), so `claims.claim_lines()`
substitutes it rather than leaving the operator to remember and the payer to
reject. The quoted lines are left untouched — only what the claim carries
changes — and the screen says so. (The other error text, PAYR-1270, says
"after/during" where PAYR-1362 says "before or during"; the claim-side rule is
the one followed.)

### Documents and forms

The claim asks for what the `auth-requirements` ruling **deferred** to this
stage — `at_preauth = 0` — plus the discharge summary, which is always wanted
and is not one of the payer's `MAND…` codes. Each gets its own file picker,
the same as at pre-authorisation. Attachments carry a `stage`, so a claim-stage
document never rides on the pre-authorisation and vice versa. The
questionnaires split the same way, and only answered ones become
`QuestionnaireResponse` resources.

### Reply

`v1/claim/on_submit`, reaching NanoEMR as `X-Hcxkit-Type: claim` and matched on
the claim submission's own correlation id. The same `ClaimResponse` parser and
the same outcome-plus-reason reading as the pre-authorisation applies, with
the same multiple-replies-per-correlation behaviour; `claim_submission` settles
`submitting → approved | partial | queried | rejected | error`.

Beyond the requirements, **any number of other documents** can be attached
for the claim from the same card ("Everything attached for the claim"): any
PDF or image, under the payer's code where the ruling named one, otherwise
`ODN`, with a label. They are filed at the claim stage and ride on the claim
bundle beside the ones asked for — the pre-authorisation tab's *Supporting
documents* card does the same for the pre-auth leg.

## Step 11 — payments

Screen: claim detail → **Payments** tab.

This is the one leg **the payer starts**. It posts a payment notice when money
moves, and the corpus's own trap applies: for payment notice the provider
*exposes* `/paymentnotice/request` and *calls* `/paymentnotice/on_request` —
the opposite way round from every other transaction.

**Matched by the claim number, not a correlation id.** Nothing of ours was
sent, so there is nothing to correlate against; the notice names the claim in
an identifier. Where to read it from is the whole difficulty, because the
published sample and the live payer disagree:

| | Published sample | Live sandbox |
|---|---|---|
| Identifier type | `CLN`, "Claim number" | **none at all** — only a `system` spelling the resource name |
| Bundle identifier | the claim number | a message **uuid** |

So the lookup tries, in order: a `CLN`-typed identifier on the
`PaymentNotice`, then the `PaymentReconciliation`, then the `Task`, then the
**first entry's own identifier** — taking an *untyped* identifier rather than
ignoring it, since being untyped is not the same as being absent. The bundle
identifier is never tried: live it is a uuid, and a wrong match is worse than
none. Reading only the typed `CLN` missed every real notice and answered
`{"status": "unmatched"}`.

`claims._claim_for_reference()` then looks that number up against
`claim.claim_no` and the historical `claim_ref` on each leg — a cancellation
retires a number, and the payer will still be using the one the claim went out
under. The correlation id the notice does carry is used for one thing:
deduping redeliveries, which answer `ignored`.

**Read from three resources.** The `Task` says what happened in prose
("Payment initiated"), the `PaymentNotice` carries the amount and
`paymentStatus`, and the `PaymentReconciliation` the payment date, the **UTR**
and a `detail[]` breakdown of what was paid against what was withheld
(`Payment` ₹2,160 alongside `RF` ₹540 in the sample).

**Several arrive per claim** — initiated, then cleared — so they are rows, and
the tab shows one card each with its status, amount, UTR and breakdown. A
payer that keeps the same `PaymentNotice.id` on both (the IRDAI sandbox
payer does) gets one row that moves on: the second notice updates the first
(`notice_id`), and is acknowledged again on its own thread. A notice whose
disposition says the payment is *initiated* and that carries no UTR is shown
as "Initiated — UTR awaited" and is not counted as money received.

> **Counting them would double the money.** PMJAY stamps its *initiated*
> notice `paid` already, carrying the full amount, so a later *cleared* notice
> for the same payment repeats it. `claims.paid_total()` counts once per UTR,
> newest notice winning — the UTR is what identifies a payment, not the notice.

**Acknowledged automatically, back to whoever sent it.** The moment a notice
lands it is answered with a `Task` (`status: completed`, `code: status`) whose
`output` carries `paymentack` and the claim number, sent to
`POST {hcxkit}/fhir/out/v1/paymentnotice/on_request`. The recipient is the
notice's own `x-hcx-sender_code`, **not** the payer the claim was raised with
— a scheme can pay through a different participant and the live sandbox does
(`1000003538@hcx` paying a `1518@hcx` claim). Its workflow and correlation
ids are echoed back too. That send is
best-effort: a notice that arrived but could not be acknowledged **is still
recorded** and the callback still answers 2xx, because rejecting it would only
have hcxkit deliver it again. The failure is kept on the row and the card
offers to re-send.

## Step 12 — answering a payer's query

Screen: claim detail → **Communication** tab, which collects the payer's
questions on both legs and their answers.

Two payers, two ways of asking for more:

| | PMJAY | IRDAI payer (`kyrocare`) |
|---|---|---|
| How the query arrives | inside the `ClaimResponse` on the submission's own thread: `outcome: partial`, adjudication `queried`, the trail in `query_note` | a `CommunicationRequest` on `communication/request`, on a **new thread** the payer's gateway mints; `x-hcx-workflow_id` is the queried submission's correlation id |
| How it is answered | the pre-authorisation (or claim) submitted again with what was missing — **Submit again** on the Pre-authorisation tab | a `Communication` on `communication/on_request` carrying the request's correlation id back |
| What the submission's thread sees meanwhile | the queried `ClaimResponse` itself | nothing — the pre-authorisation stays *Awaiting payer* until the decision on the reply arrives as the first `ClaimResponse` |

**Receiving.** `claims.receive()` takes `X-Hcxkit-Type: communication`. The
bundle's `CommunicationRequest` is matched by the claim numbers it names —
`about[]` (the payer's own number first, ours when it differs) and
`identifier[CLN]` — through `_claim_for_reference()`, which also searches the
numbers each leg went out under; failing that, by the workflow id against the
pre-auth's or the claim's correlation id, which also says which leg is being
asked about. Each `payload[].contentString` is one thing asked for (the case
remark first, then one line per queried item, as the IRDAI payer spells it);
`reasonCode[].text` is read only when the payload said nothing. The row is
`claim_query`, `open`, deduped on the request's correlation id. A bundle with
no `CommunicationRequest` — a `Communication`, which is somebody's answer — is
`ignored`; a request naming nothing here is `unmatched`.

**Answering.** The card lists what the payer asked and takes a reply: text,
whichever of the claim's documents should go with it, and any number of
**new files** chosen on the reply itself (a PDF or image each, under the
payer's code where the ruling named one, otherwise `ODN`, with an optional
label) — those are filed on the claim at the queried leg's stage and sent in
the same message. Each document travels inside the `Communication` as a
`contentAttachment` (base64,
its label as the title) with the code it was filed under on the payload's
extension (`<adapter payer_system>/StructureDefinition/document-type`,
`valueString`), which is how the IRDAI payer files it against the requirement
it was attached for. `basedOn` and `inResponseTo` name the request, `about`
the claim (the payer's number first), and the two Organizations ride along.
The envelope goes back to whoever asked (`x-hcx-sender_code` of the request),
with `x-hcx-correlation_id` = the request's thread and `x-hcx-workflow_id` =
the queried submission's. An empty reply — no text, no document — is refused
before any HTTP call, as is a document that is not on this claim. A failed
send is kept on the row (`error`, with the text) and the card offers to send
again; a sent one is `answered`, with the transaction id and the files it
carried.

The leg itself stays `queried` until the payer decides again on the
submission's own thread — that verdict lands through `_receive_preauth()` /
`_receive_claim()` like any other, told apart from the query reply by
`x-hcx-api_call_id`.

## Step 13 — the small exchanges: status and reprocess

Two asks a claim can make beside the main legs, each on a correlation id
of its own, kept in `claim_enquiry` (`kind` status | reprocess) and
shown on the card they belong to, newest first. Each is polled on Refresh
like everything else, and a payer's callback lands on the enquiry's thread
(`claims._receive_enquiry`) ahead of the main-leg receivers.

**Status enquiry** — *Ask where it stands* on the pre-authorisation and claim
cards (while awaiting, and after a decision). A `Task` coded `status` naming
the claim (`input claimNumber`), sent to `POST {hcxkit}/fhir/out/v1/task/submit`
(`claims.STATUS_PATH`) with the leg's correlation id as workflow id — the
task route, like the cancel and reprocess Tasks, because the NHCX sandbox
refuses `v1/status` outright with NHCX-1012 ("no records found with the
requested api caller id") whatever correlation id the call carries: a fresh
one, the original request's, or its api_call_id were all tried. The payer
answers on the callback that pairs with the route (`task/on_submit`; it
answers `on_status` for an ask that does reach it on `v1/status`): the
entity status (`preauth-pending`, `claim-approved`, `settled`, `not-found`,
…) is read from the protocol header `x-hcx-status_response` first and the
Task's `claimStatus` output second; a Task `rejected` reads as `not-found`.
An enquiry whose own dispatch NHCX refused settles as an error with the
gateway's code, rather than waiting for an answer that will never come.

**Reprocess** — on a decided, unpaid claim, *Send reprocess request* with a
reason. A `Task` coded `reprocess` (`description` = the reason, `input
claimNumber`) on `task/submit`, workflow id = the claim's correlation id.
The payer's `completed` Task answer means the claim is reopened for a
person, so the submission goes back to *Awaiting payer*: the new verdict
arrives on the claim's own thread and `poll_claim` / the callback apply it
as before (a new `api_call_id`, so it is not mistaken for a redelivery). A
`rejected` Task is shown as refused.

## The claim's state as JSON

`GET /claims/<id>/state` is everything the claim's tabs show, as one JSON
document, after the same gateway polls opening the page would run: `claim`,
`plan` and its `benefits`, the linkable `admissions`, `lines`, the `ruling`
with `required_documents` and `forms` split by stage, `documents`,
`preauth`, `predeterminations` and `enhancement_lines`, `queries` (with
their questions), `submission`, `payments` and `paid_total`, `enquiries`,
and any `poll_notes` (a gateway that could not be reached). It exists for
drivers and tests — `scripts/e2e/nanoemr_payer_flow.py` plays the payer's
whole checklist against this EMR and the payer over HTTP alone, reading its
progress here and acting through the screens' own form routes.

## The PMJAY adjudicator

Screen: **Finance → PMJAY adjudicator** (`/adjudicator`).

NHCX carries the message; the *decision* on a PMJAY case is taken in the NHCX
Payer Service, which sits **outside the exchange**. Without it a preauth or a
claim stays at `request.initiated` indefinitely, waiting on somebody at the
other end. hcxkit exposes that payer service behind three internal endpoints,
and this screen drives them **for the cases this EMR itself raised** — which
is the part a payer console cannot do, because it does not know which
correlation id belongs to which episode.

Everything goes through hcxkit; nothing here reaches the payer service
directly:

| Call | Used for |
|---|---|
| `GET /internal/adjudicator/workflow` | the published steps, roles and actions |
| `POST /internal/adjudicator/role` | which role is holding a case right now |
| `POST /internal/adjudicator/process` | take one of that role's actions |

**The table is every leg, not every claim.** A claim raises a
pre-authorisation and then a claim; each is its own exchange with its own
correlation id and is adjudicated separately. Each row shows the case number
that leg went out under (which a cancellation can retire), the stage, the
beneficiary, our own status, the amount and the `x-hcx-correlation_id`.

**The case decides the step, not the operator.** A case sits at exactly one
step and only the role holding it may act, so the screen reads the role first
and offers *that role's* actions and nothing else — until the lookup answers
there is no action surface at all. The role lookup is a live call, so it is
made when asked for rather than on every page view. `adjudicator.actions_for()`
matches case-insensitively but sends the spelling hcxkit's table uses:
CPD-Trust takes **`cpdApprove`**, not `Approve`, and the payer service answers
the wrong name with a generic failure that never says which it wanted.

**The correlation id is never asked for and never invented.** It is read off
the leg being decided — the one thing this EMR knows and the payer console
does not. An action for a role that is not in the published workflow, or one
that role may not take, is refused here before the payer service is troubled.

Decisions are recorded in `claim_adjudication` — case, role, action, usecase,
correlation, remarks, the HTTP status and the raw reply — because NHCX carries
no record of a decision taken outside it, so this is the only trace of who
decided what from here.

## Data model

The payer's package master lives beside the claim rather than on it: one
`claim_plan` row per claim (exchange bookkeeping, the flattened InsurancePlan
header and `response_json` for audit) with one `claim_plan_benefit` per
package or covered benefit — speciality, code, package rate, `kind`
(`Procedure` or `Implant`) and three JSON columns: `conditions` (the
claim-condition codes), `extras` (the stratification / implant tiers paid over
the rate) and `supporting_info` (the documents the payer will require, each
with the form it points at). `claim_plan_form` holds the questionnaires,
unique per plan and url. Refetching replaces all three tables. Status machine:
`fetching → ready | empty | error`.

One row per claim episode in `claim` (see `emr/schema.sql`): search inputs,
the selected policy snapshot, exchange bookkeeping (`txn_id`,
`correlation_id`, `purpose`, `checked_at`, `error_message`), the flattened
verdict, the admission link (`patient_id`, `encounter_id`) and the preauth
scalars, plus `policy_json` / `response_json` raw payloads for audit. The
preauth's repeating parts live in `claim_diagnosis`, `claim_care_team`,
`claim_item` and `claim_document` (all `ON DELETE CASCADE`).
Status machine: `draft → checking → eligible | not-eligible | error`, with
"check again" allowed from any settled state.

What the pre-authorisation quotes lives in `claim_line` (kind, code,
speciality, plan rate, quantity, amount — unique per claim, kind and code) and
the answers to the payer's questionnaires in `claim_form_answer`, one row per
question; a file answer stores the id of the document it uploaded.
`claim_document` rows record the payer requirement `code` they answer, so the
preauth quotes that code back instead of filing everything as `ODN`.
The asks of Step 13 are `claim_enquiry` rows — one per status enquiry or
reprocess request, with its own transaction and
correlation id, the answer, and the raw bundles.

The payer's ruling on the procedure set is `claim_auth`
(`checking → ready | error`), with `claim_auth_item` per line ruled on and
`claim_auth_requirement` per document or form — the payer's own `stage`
alongside `at_preauth`, the adapter's reading of it.

Payment notices are `claim_payment`, one row per notice with
`claim_payment_detail` for the reconciliation breakdown, deduped on the
payer's correlation id and carrying the acknowledgement's own state
(`pending → sent | error`).

The claim itself is `claim_submission`, one row per claim: the discharge
(mode, stage, dates), the exchange and the payer's verdict,
`draft → submitting → approved | queried | rejected | error`. `claim_document`
rows carry a `stage`, so each leg sends only its own attachments.

A payer's query on a leg is `claim_query`, one row per `CommunicationRequest`:
the leg (`stage`), the request's own thread and id, what was asked
(`questions`, JSON, one entry per payload line), and the reply this EMR sent —
its text, the `claim_document` ids that went with it, the transaction, and
`open → answered | error`. PMJAY's in-band queries never reach this table;
they live in the leg's `query_note`.

The pre-authorisation and its verdict are `claim_preauth`, one row per claim:
`submitting → approved | queried | rejected | error`, keeping both the bundle
as sent and the reply for audit. A withdrawal adds `cancelling → cancelled` on
that same row, its Task carrying its own txn and correlation id beside the
submission's so both legs stay auditable.

## Testing

- `selftest.py` section **"NHCX claims"** (22 checks, offline): claim
  creation/refusals, the settings guard, verdict parsing against a reduced
  `on_check` bundle, status settlement, and the callback door itself — the
  route hcxkit posts an on_check to, a differently spelled inMap route, and
  the flat `/nhcx/callback`. No network is touched — the guard refusals fire
  before any HTTP call.
- `selftest.py` section **"NHCX insurance plan"** (45 checks, offline): the
  discovery Task's shape and its input guard, the settings guard, the outbound
  route, package-master parsing (the `Procedure` rate versus the ward tiers,
  conditions versus document requirements, both condition spellings, the two
  shapes merging on the package code, speciality grouping), the indemnity
  shape, benefit typing, search by name and by code with the speciality and
  type filters, reading one package back in full, policy-wide requirements and
  both of their spellings, questionnaire collection by url with the question
  text falling back from prefix to text, form lookup from a requirement, form
  search, the implant relation in both directions with ward tiers kept apart
  from it, the empty plan, the callback
  arriving with the inverted flow label, redelivery, a `ProtocolResponse`
  rejection, and refetch clearing the stale master.
- `selftest.py` section **"NHCX payer adapters"** (11 checks, offline): the
  seeded mapping for both payers, suffix-insensitive matching, the generic
  fallback for an unconfigured or unkeyed payer, the Dummy IRDAI Payer adapter's
  claims, and the free-text reading of a supporting entry — document versus
  form, a stage the payer defers, and the IRDAI payer's spelling of both.
- `selftest.py` section **"NHCX preauth submission"** (76 checks, offline):
  line pricing from the plan and its refusals, the payer's implant/tier
  suggestions and their exhaustion, server-side quantity maths, the forms the
  lines pull in, answer storage and each answer type's wire shape, documents
  filed under the payer's own codes, the `auth-requirements` round trip with
  its stage split, the Claim bundle element by element (items, procedures,
  anchors, care team, the three shapes of `supportingInfo`, sequence
  numbering), the outbound route, the `ClaimResponse` verdict with redelivery,
  the cancellation Task and its nested reply, the claim number retiring on an
  accepted cancellation, and every guard.
- `selftest.py` section **"PMJAY adjudicator"** (11 checks, offline): the
  workflow read from hcxkit rather than a second copy, every sent leg listed
  as its own case with the number it went out under, a decision carrying the
  correlation id off our own record and being written down, and each refusal
  — wrong action for the role, unknown role, no role, unsent stage — firing
  before the payer service is troubled.
- `selftest.py` section **"NHCX communication (the query loop)"** (17 checks,
  offline): a `CommunicationRequest` filed against the claim it names, on the
  leg its workflow id names, with every question; redelivery ignored; an
  unknown claim unmatched; a match by the payer's `CLN` alone; a
  `Communication` on the route left alone; the empty-reply refusal; the reply
  on `communication/on_request` with the request's correlation id and the
  asker as recipient; the `Communication` element by element — `basedOn`,
  `about`, the text, the attachment under its document code in the payer's
  namespace; files added on the reply filed at the leg's stage and sent, a
  file the claim cannot hold refusing the reply; the reply recorded; a
  foreign document refused; and a failed send kept to send again.
- `selftest.py` section **"NHCX payments"** (20 checks, offline): matching a
  notice by its `CLN` including a retired claim number, flattening all three
  of its resources and the reconciliation breakdown, the automatic
  acknowledgement's Task shape, redelivery, counting one payment once by UTR
  while a genuinely separate one adds up, and a failed acknowledgement leaving
  the notice recorded and re-sendable, reading an untyped identifier while
  never mistaking the bundle's message uuid for a claim number, and routing
  the acknowledgement back to the participant that sent the notice.
- `selftest.py` section **"NHCX claim submission"** (21 checks, offline): the
  discharge refusals, the four modes and their codes, the LAMA/DAMA collapse
  to `LM100` and its stage boundary, the claim bundle's `use` /
  `billablePeriod` and every discharge entry in `supportingInfo`, stage
  separation of documents, the outbound route and workflow, and the verdict
  with redelivery.
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
5. ~~Insurance plan fetch — the payer's package master, feeding the preauth
   picker~~ (done; not yet exercised against the live sandbox)
6. ~~Line items — procedures with quantity, the implants the payer approves
   for them and the ward / ICU tiers, all quoted off its own master~~ (done)
7. ~~Coverage eligibility **auth-requirements** — the payer's ruling on the
   procedure set, and the documents and questionnaires that set needs~~ (done)
8. ~~Raise preauth — `v1/preauth/submit` from the captured dossier~~ (done;
   the bundle is built in NanoEMR, since hcxkit's `pmjay/preauth` template is
   still a skeleton)
9. ~~Payer adapters — the scheme-specific parts behind a participant-code
   lookup, configurable under Masters~~ (done)
10. ~~Cancel a pre-authorisation — a `Task` coded `cancel` at
    `v1/task/submit`, which retires the claim number~~ (done)
11. ~~Claim submission (`v1/claim/submit`) once a preauth is approved, with
    the discharge and the requirements the ruling deferred to that stage~~
    (done)
12. ~~Payment notices — the payer-initiated leg, matched by `CLN` and
    acknowledged automatically~~ (done)
13. ~~The query loop — answering a payer that responds `queried` rather than
    approving or rejecting: PMJAY by resubmitting the leg, the IRDAI payer
    with a `Communication` on the request's own thread~~ (done)

14. ~~Enhancement — a line added after the decision goes as a pre-auth
    naming the prior one, and the payer files it on the same case~~ (done)
15. ~~Status enquiry — a `Task` coded `status` on `v1/status`, answered with
    the entity status~~ (done)
16. ~~Reprocess — appealing a decided claim with a `Task` coded `reprocess`;
    the new verdict comes back on the claim's thread~~ (done)

Steps 3 and 5–9 are verified offline and against stored live payloads; only
policy search and the coverage eligibility check have been round-tripped
against the sandbox.
