"""NHCX claims — policy search and coverage eligibility via a local hcxkit.

The EMR never talks to the NHCX gateway directly. A co-deployed hcxkit
instance does the JWE encryption, participant registry lookups and dispatch;
this module only calls its plain-JSON HTTP API:

  POST /internal/policies/search   — beneficiary policy discovery (BIS)
  POST /internal/mappings/map      — build the CoverageEligibilityRequest bundle
  POST /fhir/out/v1/coverageeligibility/check — queue the bundle for the payer
  POST /internal/txn/related|fhir|dispatch    — poll for the payer's on_check

The exchange is asynchronous: the check call only acknowledges queueing, the
payer's verdict arrives later on the same correlation id. It is picked up two
ways, both landing in :func:`apply_response`: hcxkit pushes each inbound
envelope to the callback URL on its participant profile (:func:`receive`,
mounted at ``POST /nhcx/callback``), and opening a claim still polls
(:func:`poll_response`) so a missed or misconfigured callback only costs
immediacy, never the verdict.
"""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
from typing import Any

from . import db

# Base URL of the hcxkit gateway this installation pairs with.
HCXKIT_URL = os.environ.get("NANOEMR_HCXKIT_URL", "http://localhost:8080").rstrip("/")

# NHCX participant code of the payer claims are sent to (PMJAY by default).
PAYER_CODE = os.environ.get("NANOEMR_PAYER_CODE", "1518@hcx")
PAYER_NAME = os.environ.get("NANOEMR_PAYER_NAME", "Nhcx Pmjay")

# Optional shared secret on the callback URL. The hcxkit worker cannot hold a
# session, so when this is set it rides on the URL as ``?token=…`` instead.
CALLBACK_TOKEN = os.environ.get("NANOEMR_CALLBACK_TOKEN", "")

# Identifier kinds the BIS policy search accepts.
ID_TYPES = {
    "MobileNo": "Mobile number",
    "AbhaNumber": "ABHA number",
    "MemberId": "Member ID",
}

PURPOSES = {
    "validation": "Validation — is the policy in force?",
    "discovery": "Discovery — find active coverage",
}

STATUS = {
    "draft": ("Draft", "warning"),
    "checking": ("Awaiting payer", "info"),
    "eligible": ("Eligible", "success"),
    "not-eligible": ("Not eligible", "danger"),
    "error": ("Error", "danger"),
}


# ------------------------------------------------------------------ transport
class GatewayError(ValueError):
    """An hcxkit call that failed, carrying the HTTP status when there was one.

    A subclass of ``ValueError`` so every existing ``except ValueError`` still
    catches it; the status lets :func:`poll_response` tell "the gateway has no
    such transaction" (404, permanent) from a transient failure.
    """

    def __init__(self, message: str, status: int = 0):
        super().__init__(message)
        self.status = status


def _api(path: str, payload: dict[str, Any] | None = None,
         timeout: float = 25, method: str = "POST") -> Any:
    """Call hcxkit and return the decoded JSON reply.

    Network problems and non-2xx replies surface as :class:`GatewayError` (a
    ``ValueError``) so routes can turn them into a red flash instead of a 500.
    """
    request = urllib.request.Request(
        HCXKIT_URL + path,
        data=None if payload is None else json.dumps(payload).encode(),
        headers={"Content-Type": "application/json"},
        method=method)
    try:
        with urllib.request.urlopen(request, timeout=timeout) as reply:
            body = reply.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as error:
        detail = error.read().decode("utf-8", "replace")
        raise GatewayError(f"hcxkit {path} returned {error.code}: "
                           f"{_error_text(detail)}", error.code) from error
    except (urllib.error.URLError, OSError, TimeoutError) as error:
        raise GatewayError(
            f"hcxkit gateway unreachable at {HCXKIT_URL} — {error}") from error
    try:
        return json.loads(body)
    except json.JSONDecodeError as error:
        raise GatewayError(f"hcxkit {path} returned non-JSON output") from error


def _error_text(body: str) -> str:
    """Dig the human-readable message out of a gateway error reply.

    hcxkit relays upstream failures as ``{"error": "...", "details": "<the
    raw ABDM body>"}`` where details is itself JSON with a nested
    ``error.message`` — that innermost message is the one worth showing.
    """
    message = body[:500]
    try:
        reply = json.loads(body)
        message = reply.get("error") or message
        details = reply.get("details")
        if isinstance(details, str):
            details = json.loads(details)
        if isinstance(details, dict):
            nested = details.get("error")
            if isinstance(nested, dict) and nested.get("message"):
                message = nested["message"]
    except (json.JSONDecodeError, AttributeError, TypeError):
        pass
    return message


def _first(source: dict[str, Any], *keys: str) -> Any:
    for key in keys:
        value = source.get(key)
        if value not in (None, ""):
            return value
    return None


# -------------------------------------------------------------- policy search
def search_policies(id_type: str, id_value: str) -> list[dict[str, Any]]:
    """Look up a beneficiary's policies on the BIS via hcxkit.

    Returns normalised policy dicts; the raw upstream row rides along under
    ``raw`` so nothing the payer sent is lost when a policy is selected.
    """
    if id_type not in ID_TYPES:
        raise ValueError("Choose what kind of identifier you are searching with.")
    id_value = (id_value or "").strip()
    if not id_value:
        raise ValueError("Enter an identifier value to search for.")
    try:
        reply = _api("/internal/policies/search",
                     {"identifiertype": id_type, "identifiervalue": id_value})
    except ValueError as error:
        # The BIS answers "nothing linked to this identifier" as an HTTP
        # error (NHCX-1016); to an operator that is an empty result, not a
        # failure.
        if "No policies found" in str(error):
            return []
        raise
    if isinstance(reply, list):
        rows = reply
    elif isinstance(reply, dict):
        rows = _first(reply, "policies", "result", "data") or []
    else:
        rows = []
    if not isinstance(rows, list):
        rows = []
    return [_normalise_policy(row) for row in rows if isinstance(row, dict)]


def _normalise_policy(raw: dict[str, Any]) -> dict[str, Any]:
    """Map the (inconsistently named) BIS fields onto one vocabulary."""
    return {
        "member_id": _first(raw, "memberid", "member_id", "memberId", "pmjayid"),
        "name": _first(raw, "name", "membername", "patient_name", "patientName",
                       "beneficiaryname", "beneficiaryName"),
        # The sandbox BIS carries the plan identifier in `productid`; a
        # dedicated policy-number field wins when one is present.
        "policy_code": _first(raw, "policy_number", "policyNumber", "policyno",
                              "policyNo", "policycode", "productid"),
        "payer_id": _first(raw, "payerid", "payerId", "insurer_code"),
        "payer_name": _first(raw, "payerName", "payername", "insurer_name"),
        "product_id": _first(raw, "productid", "productId"),
        "product_name": _first(raw, "productname", "productName"),
        "abha_number": _first(raw, "abhanumber", "abhaNumber", "abha_number"),
        "mobile_number": _first(raw, "mobilenumber", "mobileNumber", "mobile"),
        "photo": _first(raw, "photo", "photoUrl", "beneficiaryphoto",
                        "memberphoto"),
        "raw": raw,
    }


# ------------------------------------------------------------------ the claim
def create_claim(policy: dict[str, Any], id_type: str = "",
                 id_value: str = "") -> int:
    """Open a claim episode around one selected policy."""
    member_id = (policy.get("member_id") or "").strip()
    if not member_id:
        raise ValueError("That policy has no member ID; a claim cannot be "
                         "raised without one.")
    return db.insert("claim", {
        "claim_no": db.next_number("claim", "CLM-"),
        "created_at": db.now_iso(),
        "status": "draft",
        "search_id_type": id_type or None,
        "search_id_value": id_value or None,
        "member_id": member_id,
        "policy_code": policy.get("policy_code"),
        "beneficiary_name": policy.get("name"),
        "abha_number": policy.get("abha_number"),
        "mobile_number": policy.get("mobile_number"),
        "payer_id": policy.get("payer_id"),
        "payer_name": policy.get("payer_name") or PAYER_NAME,
        "product_id": policy.get("product_id"),
        "product_name": policy.get("product_name"),
        "patient_photo": policy.get("photo"),
        "policy_json": json.dumps(policy.get("raw") or {}, ensure_ascii=False),
    })


def claims_list(status: str = "") -> list:
    if status:
        return db.query("SELECT * FROM claim WHERE status = ? ORDER BY id DESC",
                        (status,))
    return db.query("SELECT * FROM claim ORDER BY id DESC")


def claim(claim_id: int):
    return db.one("SELECT * FROM claim WHERE id = ?", (claim_id,))


# ------------------------------------------------- coverage eligibility check
def run_check(claim_id: int, purpose: str, policy_code: str,
              member_id: str) -> None:
    """Build the CoverageEligibilityRequest bundle and queue it to the payer.

    The bundle is built by hcxkit's own pmjay template (mappings/map), so what
    goes out is exactly what the rest of the kit expects to parse back.
    """
    row = claim(claim_id)
    if row is None:
        raise ValueError("Claim not found.")
    if purpose not in PURPOSES:
        raise ValueError("Choose whether this check is a validation or a "
                         "discovery.")
    policy_code = (policy_code or "").strip()
    member_id = (member_id or "").strip()
    if not member_id:
        raise ValueError("Member ID is required for an eligibility check.")
    if purpose == "validation" and not policy_code:
        raise ValueError("Policy code is required for a validation check.")

    org = db.default_org()
    if org is None or not org["identifier_value"]:
        raise ValueError("Set the facility's HFR ID under Settings before "
                         "raising claims.")
    if not org["participant_code"]:
        raise ValueError("Set the facility's NHCX participant code under "
                         "Settings before raising claims.")

    bundle_input = {
        "purpose": purpose,
        "policyNumber": policy_code or None,
        "pmjayId": member_id,
        "subscriberId": member_id,
        "patientName": row["beneficiary_name"],
        "abhaNumber": row["abha_number"],
        "patientPhone": row["mobile_number"],
        "providerId": org["identifier_value"],
        "providerName": org["name"],
        "payerId": (row["payer_id"] or PAYER_CODE).split("@")[0],
        "payerName": row["payer_name"] or PAYER_NAME,
        "servicedDate": db.today_iso(),
    }
    bundle = _api("/internal/mappings/map", {
        "group": "pmjay", "name": "coverage/request/check", "flow": "request",
        "input": {k: v for k, v in bundle_input.items() if v}})
    if isinstance(bundle, dict) and bundle.get("resourceType") != "Bundle":
        bundle = _first(bundle, "output", "result", "fhir", "bundle") or bundle
    if not isinstance(bundle, dict) or bundle.get("resourceType") != "Bundle":
        raise ValueError("hcxkit did not return a FHIR bundle for the check.")

    recipient = row["payer_id"] or PAYER_CODE
    ack = _api("/fhir/out/v1/coverageeligibility/check", {
        "jwe_headers": {
            "x-hcx-sender_code": org["participant_code"],
            "x-hcx-recipient_code": recipient,
            "x-hcx-workflow_id": row["claim_no"],
        },
        "fhir": bundle})
    db.update("claim", claim_id, {
        "status": "checking",
        "purpose": purpose,
        "policy_code": policy_code or row["policy_code"],
        "member_id": member_id,
        "txn_id": ack.get("txn_id"),
        "correlation_id": ack.get("correlation_id"),
        "checked_at": db.now_iso(),
        "error_message": None,
    })


def poll_response(claim_id: int) -> bool:
    """One look at the hcxkit ledger for the payer's on_check reply.

    Returns True when the claim row changed (verdict landed or dispatch
    failed), False while the exchange is still pending.
    """
    row = claim(claim_id)
    if row is None or row["status"] != "checking" or not row["txn_id"]:
        return False
    try:
        related = _api("/internal/txn/related", {"txnId": row["txn_id"]})
    except GatewayError as error:
        # 404 is not a hiccup: hcxkit's ledger has no such transaction any
        # more (typically its database was reset after the check went out).
        # No reply can ever be matched to this txn id, so settle the claim
        # instead of re-polling forever — "Check again" re-sends cleanly.
        if error.status == 404:
            db.update("claim", claim_id, {
                "status": "error",
                "error_message": "The hcxkit gateway no longer has this "
                                 "transaction — its ledger was reset after "
                                 "the check was sent. Send the eligibility "
                                 "check again.",
            })
            return True
        raise
    inbound = [r for r in related or []
               if isinstance(r, dict) and r.get("direction") == "in"]
    if inbound:
        envelope = _api("/internal/txn/fhir", {"txnId": inbound[-1]["id"]})
        bundle = _first(envelope, "fhir", "payload") if isinstance(envelope, dict) else None
        if not isinstance(bundle, dict):
            raise ValueError("Payer reply arrived but hcxkit returned no "
                             "bundle for it.")
        apply_response(claim_id, parse_validation_bundle(bundle), bundle)
        return True
    error = _protocol_error(row)
    if error:
        db.update("claim", claim_id, {"status": "error",
                                      "error_message": error})
        return True
    dispatch = _api("/internal/txn/dispatch", {"txnId": row["txn_id"]})
    if isinstance(dispatch, dict) and dispatch.get("status") in (
            "dispatch_failed", "dead", "failed"):
        db.update("claim", claim_id, {
            "status": "error",
            "error_message": dispatch.get("errorMessage")
            or dispatch.get("errorCode") or "Dispatch to NHCX failed.",
        })
        return True
    return False


def receive(envelope: dict[str, Any], message_type: str = "", flow: str = "",
            payload_kind: str = "") -> str:
    """Settle a claim from an envelope hcxkit pushed to the callback URL.

    hcxkit delivers *every* inbound message to the one callback URL, saying
    which this is in the ``X-Hcxkit-*`` headers, and reads the reply as a
    delivery outcome — so anything not addressed to a claim of ours is
    ``"ignored"`` rather than an error, and only a body we genuinely cannot
    read raises.

    Returns what happened: ``"settled"``, ``"unmatched"`` (no claim waiting on
    this correlation id) or ``"ignored"``.
    """
    if payload_kind == "data" or message_type not in ("", "coverage") \
            or flow not in ("", "on_request"):
        return "ignored"

    headers = envelope.get("jwe_headers")
    correlation_id = (headers or {}).get("x-hcx-correlation_id") \
        if isinstance(headers, dict) else None
    if not correlation_id:
        return "unmatched"

    row = db.one("SELECT * FROM claim WHERE correlation_id = ? "
                 "ORDER BY id DESC", (correlation_id,))
    if row is None:
        return "unmatched"
    # A redelivery of something already settled must not reopen it: the worker
    # retries on anything but a 2xx, so this has to be safe to receive twice.
    if row["status"] != "checking":
        return "ignored"

    body = envelope.get("fhir")
    if not isinstance(body, dict):
        raise ValueError("The callback carried no readable payload.")

    # A gateway or payer rejection arrives as a plain ProtocolResponse in place
    # of the on_check bundle — the same shape _protocol_error digs out of the
    # ledger when polling.
    if body.get("type") == "ProtocolResponse":
        details = body.get("x-hcx-error_details") or {}
        db.update("claim", row["id"], {
            "status": "error",
            "error_message": (f'{details.get("code") or "NHCX"}: '
                              f'{details.get("message") or "The gateway rejected the request."}'),
        })
        return "settled"

    apply_response(row["id"], parse_validation_bundle(body), body)
    return "settled"


def _protocol_error(row) -> str | None:
    """Find an NHCX ProtocolResponse rejection addressed to this claim.

    A payer-side rejection (e.g. PAYR-1008 for a wrong HFR ID) arrives as a
    plain-JSON ProtocolResponse, not an encrypted on_check. hcxkit cannot read
    protocol headers off that payload, so its ledger row gets a fresh
    correlation id and txn/related never links it — the only reliable join is
    the ``x-hcx-correlation_id`` inside the stored body.
    """
    try:
        ledger = _api("/internal/txn/list", method="GET")
    except ValueError:
        return None
    since = row["checked_at"] or ""
    candidates = [r for r in ledger or [] if isinstance(r, dict)
                  and r.get("direction") == "in"
                  and r.get("type") == "coverage"
                  and (r.get("created") or "") >= since][:20]
    for entry in candidates:
        try:
            envelope = _api("/internal/txn/fhir", {"txnId": entry["id"]})
        except ValueError:
            continue
        body = envelope.get("fhir") if isinstance(envelope, dict) else None
        if (isinstance(body, dict)
                and body.get("type") == "ProtocolResponse"
                and body.get("x-hcx-correlation_id") == row["correlation_id"]
                and body.get("x-hcx-status") == "response.error"):
            details = body.get("x-hcx-error_details") or {}
            return (f'{details.get("code") or "NHCX"}: '
                    f'{details.get("message") or "The gateway rejected the request."}')
    return None


# ------------------------------------------------------------ verdict parsing
def parse_validation_bundle(bundle: dict[str, Any]) -> dict[str, Any]:
    """Flatten the on_check bundle into the columns the claim row stores.

    The reply carries the request's resources followed by the payer's own
    Patient/Coverage, so where a resource type repeats the *last* one is the
    payer-enriched copy.
    """
    resources = [e.get("resource", {}) for e in bundle.get("entry", [])
                 if isinstance(e, dict)]
    verdict = next((r for r in resources
                    if r.get("resourceType") == "CoverageEligibilityResponse"),
                   None)
    if verdict is None:
        raise ValueError("The payer reply carries no "
                         "CoverageEligibilityResponse.")

    insurance = (verdict.get("insurance") or [{}])[0]
    allowed = used = None
    auth_required = None
    for item in insurance.get("item") or []:
        if item.get("authorizationRequired") is not None:
            auth_required = 1 if item["authorizationRequired"] else 0
        for benefit in item.get("benefit") or []:
            money = (benefit.get("allowedMoney") or {}).get("value")
            if money is None:
                continue
            if allowed is None or money > allowed:
                allowed = money
                used = (benefit.get("usedMoney") or {}).get("value")

    values: dict[str, Any] = {
        "inforce": (1 if insurance.get("inforce") else 0)
        if insurance.get("inforce") is not None else None,
        "outcome": verdict.get("outcome"),
        "disposition": verdict.get("disposition"),
        "auth_required": auth_required,
        "allowed_amount": allowed,
        "used_amount": used,
    }

    patients = [r for r in resources if r.get("resourceType") == "Patient"]
    if patients:
        patient = patients[-1]
        name = (patient.get("name") or [{}])[0]
        values["beneficiary_name"] = (name.get("text")
                                      or " ".join(name.get("given") or [])
                                      or name.get("family"))
        values["patient_gender"] = patient.get("gender")
        values["patient_dob"] = patient.get("birthDate")
        address = (patient.get("address") or [{}])[0]
        values["patient_address"] = ", ".join(
            str(part) for part in ((address.get("line") or [])
                                   + [address.get("district"),
                                      address.get("state"),
                                      address.get("postalCode")]) if part)
        for identifier in patient.get("identifier") or []:
            codings = (identifier.get("type") or {}).get("coding") or [{}]
            if codings[0].get("code") == "ABHA":
                values["abha_number"] = identifier.get("value")
        photo = (patient.get("photo") or [{}])[0]
        if photo.get("data") or photo.get("url"):
            values["patient_photo"] = photo.get("data") or photo.get("url")

    coverages = [r for r in resources if r.get("resourceType") == "Coverage"]
    if coverages:
        coverage = coverages[-1]
        plan_class = (coverage.get("class") or [{}])[0]
        values["plan_name"] = plan_class.get("name")
        period = coverage.get("period") or {}
        values["plan_period_start"] = period.get("start")
        values["plan_period_end"] = period.get("end")
        relation = ((coverage.get("relationship") or {}).get("coding") or [{}])[0]
        values["relationship"] = relation.get("display") or relation.get("code")

    return {k: v for k, v in values.items() if v not in (None, "")}


def apply_response(claim_id: int, parsed: dict[str, Any],
                   bundle: dict[str, Any]) -> None:
    """Store the payer's verdict and settle the claim status."""
    if parsed.get("outcome") == "error":
        status = "error"
    elif parsed.get("inforce"):
        status = "eligible"
    else:
        status = "not-eligible"
    db.update("claim", claim_id, dict(parsed) | {
        "status": status,
        "response_json": json.dumps(bundle, ensure_ascii=False),
    })


def balance(row) -> float | None:
    """Remaining wallet = allowed − used, when the payer reported both."""
    if row["allowed_amount"] is None:
        return None
    return row["allowed_amount"] - (row["used_amount"] or 0)


# ------------------------------------------------------- linking the admission
def _abha_digits(value: str | None) -> str:
    return "".join(ch for ch in (value or "") if ch.isdigit())


def linkable_admissions(row) -> list:
    """Current IPD stays of patients registered with the claim's ABHA number.

    ABHA is compared on digits only — the payer writes ``91-7034-…`` while the
    front desk may have registered the patient without the dashes.
    """
    digits = _abha_digits(row["abha_number"])
    if not digits:
        return []
    stays = db.query(
        "SELECT e.id AS encounter_id, e.encounter_no, e.period_start, e.ward, "
        "e.bed, e.department, p.id AS patient_id, p.name AS patient_name, "
        "p.mrn, p.abha_number, d.name AS doctor_name "
        "FROM encounter e JOIN patient p ON p.id = e.patient_id "
        "LEFT JOIN practitioner d ON d.id = e.practitioner_id "
        "WHERE e.kind = 'IPD' AND e.status <> 'finished' "
        "ORDER BY e.period_start DESC")
    return [s for s in stays if _abha_digits(s["abha_number"]) == digits]


def link_admission(claim_id: int, encounter_id: int) -> None:
    """Attach the claim to the admitted patient it belongs to."""
    row = claim(claim_id)
    if row is None:
        raise ValueError("Claim not found.")
    if row["status"] != "eligible":
        raise ValueError("Link an admission only after the payer has "
                         "confirmed the policy is eligible.")
    match = next((s for s in linkable_admissions(row)
                  if s["encounter_id"] == encounter_id), None)
    if match is None:
        raise ValueError("That admission is not a current IPD stay of a "
                         "patient with this claim's ABHA number.")
    db.update("claim", claim_id, {
        "patient_id": match["patient_id"],
        "encounter_id": match["encounter_id"],
        # sensible default for the preauth; the operator can still change it
        "admission_date": row["admission_date"]
        or (match["period_start"] or "")[:10] or None,
    })


def unlink_admission(claim_id: int) -> None:
    """Detach the claim from the admission (kept preauth data survives)."""
    db.update("claim", claim_id, {"patient_id": None, "encounter_id": None})


# ------------------------------------------------------------- preauth capture
CARE_ROLES = {
    "admitting": "Admitting physician",
    "treating": "Treating doctor",
    "surgeon": "Surgeon",
    "anaesthetist": "Anaesthetist",
    "nurse": "Nursing lead",
}

CASE_TYPES = {
    "package": "Package (HBP rate)",
    "nonpackage": "Non-package (itemised)",
}


def packages() -> list[dict[str, Any]]:
    """The HBP package master: ``claim_package`` terminology, rate in extra."""
    rows = []
    for term in db.terms("claim_package"):
        try:
            rate = float(term["extra"] or 0)
        except ValueError:
            rate = 0.0
        rows.append({"code": term["code"], "display": term["display"],
                     "rate": rate})
    return rows


def charge_items() -> list[dict[str, Any]]:
    """The charge master with its fixed prices (``extra`` = ``price|type``)."""
    rows = []
    for term in db.terms("charge"):
        try:
            price = float((term["extra"] or "0").split("|")[0] or 0)
        except ValueError:
            price = 0.0
        rows.append({"code": term["code"], "display": term["display"],
                     "price": price})
    return rows


def preauth_children(claim_id: int) -> dict[str, list]:
    """The saved diagnosis / care team / item rows of one claim."""
    return {
        "diagnoses": db.query(
            "SELECT * FROM claim_diagnosis WHERE claim_id = ? ORDER BY seq",
            (claim_id,)),
        "care_team": db.query(
            "SELECT t.*, p.name AS doctor_name FROM claim_care_team t "
            "JOIN practitioner p ON p.id = t.practitioner_id "
            "WHERE t.claim_id = ? ORDER BY t.seq", (claim_id,)),
        "items": db.query(
            "SELECT * FROM claim_item WHERE claim_id = ? ORDER BY seq",
            (claim_id,)),
    }


def save_preauth(claim_id: int, values: dict[str, Any],
                 diagnoses: list[dict[str, str]], team: list[dict[str, str]],
                 items: list[dict[str, str]]) -> None:
    """Validate and store the preauth draft — the claim row plus its children.

    Prices are never taken from the form: a package's rate and an item's unit
    price are re-read from the masters at save time, only quantities are the
    operator's to choose.
    """
    row = claim(claim_id)
    if row is None:
        raise ValueError("Claim not found.")
    if not row["encounter_id"]:
        raise ValueError("Link the admitted patient before drafting a "
                         "pre-authorisation.")

    admission = (values.get("admission_date") or "").strip()
    discharge = (values.get("expected_discharge_date") or "").strip()
    if not admission:
        raise ValueError("Enter the admission date.")
    if discharge and discharge < admission:
        raise ValueError("The provisional discharge date cannot be before "
                         "the admission date.")

    dx_rows = []
    for entry in diagnoses:
        term = db.term("diagnosis", (entry.get("code") or "").strip())
        if term is None:
            continue
        dx_rows.append({
            "snomed_code": term["code"], "snomed_display": term["display"],
            "icd10_code": term["alt_code"] or term["code"],
            "icd10_display": term["alt_display"] or term["display"],
        })
    if not dx_rows:
        raise ValueError("Pick at least one ICD-10 diagnosis.")

    team_rows = []
    for entry in team:
        try:
            practitioner_id = int(entry.get("doctor") or 0)
        except ValueError:
            practitioner_id = 0
        doctor = db.one("SELECT id FROM practitioner WHERE id = ? AND active = 1",
                        (practitioner_id,))
        if doctor is None:
            continue
        role = entry.get("role") or "treating"
        if role not in CARE_ROLES:
            raise ValueError("Choose a valid care team role.")
        team_rows.append({"practitioner_id": practitioner_id, "role": role})
    if not team_rows:
        raise ValueError("Add at least one doctor to the care team.")

    case_type = values.get("case_type") or ""
    if case_type not in CASE_TYPES:
        raise ValueError("Choose whether this is a package or a non-package "
                         "case.")

    package_code = package_name = None
    item_rows = []
    if case_type == "package":
        package = next((p for p in packages()
                        if p["code"] == (values.get("package_code") or "").strip()),
                       None)
        if package is None:
            raise ValueError("Select the package for this case.")
        package_code, package_name = package["code"], package["display"]
        total = package["rate"]
    else:
        prices = {c["code"]: c for c in charge_items()}
        for entry in items:
            master = prices.get((entry.get("code") or "").strip())
            if master is None:
                continue
            try:
                quantity = float(entry.get("qty") or 0)
            except ValueError:
                quantity = 0
            if quantity <= 0:
                raise ValueError(
                    f'Enter a quantity for "{master["display"]}".')
            item_rows.append({
                "code": master["code"], "display": master["display"],
                "unit_price": master["price"], "quantity": quantity,
                "amount": round(master["price"] * quantity, 2),
            })
        if not item_rows:
            raise ValueError("Add at least one item to a non-package case.")
        total = round(sum(i["amount"] for i in item_rows), 2)

    with db.transaction():
        db.update("claim", claim_id, {
            "admission_date": admission,
            "expected_discharge_date": discharge or None,
            "case_type": case_type,
            "package_code": package_code,
            "package_name": package_name,
            "preauth_total": total,
            "preauth_saved_at": db.now_iso(),
        })
        for table in ("claim_diagnosis", "claim_care_team", "claim_item"):
            db.execute(f"DELETE FROM {table} WHERE claim_id = ?", (claim_id,))
        for seq, entry in enumerate(dx_rows, 1):
            db.insert("claim_diagnosis",
                      dict(entry) | {"claim_id": claim_id, "seq": seq})
        for seq, entry in enumerate(team_rows, 1):
            db.insert("claim_care_team",
                      dict(entry) | {"claim_id": claim_id, "seq": seq})
        for seq, entry in enumerate(item_rows, 1):
            db.insert("claim_item",
                      dict(entry) | {"claim_id": claim_id, "seq": seq})


# ------------------------------------------------------- supporting documents
DOCUMENT_TYPES = {
    "application/pdf": "PDF",
    "image/jpeg": "JPEG image",
    "image/png": "PNG image",
    "image/webp": "WebP image",
}
MAX_DOCUMENT_BYTES = 10 * 1024 * 1024


def add_document(claim_id: int, filename: str, content_type: str,
                 data: bytes, label: str = "") -> int:
    """Attach one uploaded PDF or image to the claim."""
    if claim(claim_id) is None:
        raise ValueError("Claim not found.")
    content_type = (content_type or "").split(";")[0].strip().lower()
    if content_type not in DOCUMENT_TYPES:
        allowed = ", ".join(sorted(DOCUMENT_TYPES.values()))
        raise ValueError(f"Only {allowed} files can be attached.")
    if not data:
        raise ValueError("That file is empty.")
    if len(data) > MAX_DOCUMENT_BYTES:
        raise ValueError("A document may be at most "
                         f"{MAX_DOCUMENT_BYTES // (1024 * 1024)} MB.")
    return db.insert("claim_document", {
        "claim_id": claim_id,
        "filename": (filename or "").strip() or "document",
        "content_type": content_type,
        "label": (label or "").strip() or None,
        "size": len(data),
        "data": data,
        "uploaded_at": db.now_iso(),
    })


def documents(claim_id: int) -> list:
    """The claim's attachments, without their payloads."""
    return db.query(
        "SELECT id, claim_id, filename, content_type, label, size, uploaded_at "
        "FROM claim_document WHERE claim_id = ? ORDER BY id", (claim_id,))


def document(doc_id: int):
    return db.one("SELECT * FROM claim_document WHERE id = ?", (doc_id,))


def delete_document(doc_id: int) -> None:
    db.execute("DELETE FROM claim_document WHERE id = ?", (doc_id,))
