"""NHCX claims — policy search and coverage eligibility via a local hcxkit.

The EMR never talks to the NHCX gateway directly. A co-deployed hcxkit
instance does the JWE encryption, participant registry lookups and dispatch;
this module only calls its plain-JSON HTTP API:

  POST /internal/policies/search   — beneficiary policy discovery (BIS)
  POST /fhir/out/v1/coverageeligibility/check — queue the bundle for the payer
  POST /fhir/out/v1/insuranceplan/request     — queue the package-master Task
  POST /internal/txn/related|fhir|dispatch    — poll for the payer's reply

The exchange is asynchronous: the check call only acknowledges queueing, the
payer's verdict arrives later on the same correlation id. It is picked up two
ways, both landing in :func:`apply_response`: hcxkit pushes each inbound
envelope to the callback URL on its participant profile (:func:`receive`,
mounted at ``POST /callback/v1/coverageeligibility/on_check`` and every other
path under ``/callback``), and opening a claim still polls
(:func:`poll_response`) so a missed or misconfigured callback only costs
immediacy, never the verdict.
"""

from __future__ import annotations

import base64
import json
import os
import urllib.error
import urllib.request
import uuid
from typing import Any

from . import db, payers
from .fhir.coverage import build_coverage_request

# Base URL of the hcxkit gateway this installation pairs with.
HCXKIT_URL = os.environ.get("NANOEMR_HCXKIT_URL", "http://localhost:8080").rstrip("/")
# Presented to the gateway as "Authorization: Bearer". hcxkit asks for none;
# nhcx-gateway does when its apiKey is set, and a gateway listening on more
# than loopback should have one.
HCXKIT_API_KEY = os.environ.get("NANOEMR_HCXKIT_API_KEY", "").strip()

# NHCX participant code of the payer claims are sent to (PMJAY by default).
PAYER_CODE = os.environ.get("NANOEMR_PAYER_CODE", "1518@hcx")
PAYER_NAME = os.environ.get("NANOEMR_PAYER_NAME", "Nhcx Pmjay")

# Optional shared secret on the callback URL. The hcxkit worker cannot hold a
# session, so when this is set it rides on the URL as ``?token=…`` instead.
CALLBACK_TOKEN = os.environ.get("NANOEMR_CALLBACK_TOKEN", "")

# Identifier kinds the BIS policy search accepts.
# The member id first: it is on the card the beneficiary hands over, and it
# is what the payer's own directory is keyed on.
ID_TYPES = {
    "MemberId": "Member ID",
    "MobileNo": "Mobile number",
    "AbhaNumber": "ABHA number",
}

# The document types a hospital attaches most, offered on every upload
# beside whatever the payer's ruling named. The codes are the ones the
# payer registries use, so a file goes across under a code the other side
# knows rather than as "other document".
COMMON_DOCUMENTS = [
    ("CLN", "Clinical document (case sheet, notes)"),
    ("HDS", "Hospital discharge summary"),
    ("DIA", "Diagnostic & laboratory reports"),
    ("RAD", "Radiology / scan reports"),
    ("PRE", "Doctor's prescription notes"),
    ("CER", "Medical certificate / referral"),
    ("EST", "Cost estimate"),
    ("MB", "Medical & pharmacy bills"),
    ("INV", "Final hospital invoice"),
    ("OTR", "Operation theatre notes"),
    ("ICU", "ICU chart"),
    ("IMP", "Implant invoice & sticker"),
    ("POI", "Proof of identity"),
    ("KYC", "KYC / bank proof"),
    ("FCF", "Filled claim form"),
    ("PAU", "Pre-authorisation approval letter"),
    ("ODN", "Other document"),
]
DEFAULT_DOCUMENT_CODE = "CLN"

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


def gateway(path: str, payload: dict[str, Any] | None = None,
            timeout: float = 25, method: str = "POST") -> Any:
    """Call the paired hcxkit — the one door out of this process.

    Public so the modules beside this one (:mod:`emr.adjudicator`) reach the
    gateway the same way, with the same error handling, rather than opening a
    second one.
    """
    return _api(path, payload, timeout, method)


def _api(path: str, payload: dict[str, Any] | None = None,
         timeout: float = 25, method: str = "POST") -> Any:
    """Call hcxkit and return the decoded JSON reply.

    Network problems and non-2xx replies surface as :class:`GatewayError` (a
    ``ValueError``) so routes can turn them into a red flash instead of a 500.
    """
    headers = {"Content-Type": "application/json"}
    if HCXKIT_API_KEY:
        headers["Authorization"] = "Bearer " + HCXKIT_API_KEY
    request = urllib.request.Request(
        HCXKIT_URL + path,
        data=None if payload is None else json.dumps(payload).encode(),
        headers=headers,
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

    The bundle is built here — see :mod:`emr.fhir.coverage`, a port of the
    pmjay template hcxkit used to build it from. Asking a gateway to assemble
    our own request tied the check to that one gateway; the shape is ours to
    own, and any NHCX gateway can carry it.
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
    bundle = build_coverage_request({k: v for k, v in bundle_input.items() if v})

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
    envelope, bundle = _latest_reply(related, "CoverageEligibilityResponse")
    if bundle is not None:
        apply_response(claim_id, parse_validation_bundle(bundle), bundle)
        return True
    error = _protocol_error(row["correlation_id"], row["checked_at"])
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

    hcxkit delivers *every* inbound message to the same callback base — the
    message's own route appended, so a coverage on_check arrives on
    ``/callback/v1/coverageeligibility/on_check`` — and says which this is in
    the ``X-Hcxkit-*`` headers. Those headers, not the path, decide: anything
    not addressed to a claim of ours is ``"ignored"`` rather than an error,
    and only a body we genuinely cannot read raises.

    ``X-Hcxkit-Type`` picks the exchange — ``coverage`` settles the eligibility
    verdict, ``insurance`` the package master, ``preauth`` the authorisation,
    ``task`` its cancellation and ``communication`` a payer's query on a leg.
    ``X-Hcxkit-Flow`` is
    deliberately *not* a filter: the kit labels a coverage reply ``on_request``
    but an insurance reply ``request``, because its inMap spells the two
    directions the opposite way round. The correlation id is the real
    discriminator — an inbound message nothing here is waiting for is simply
    unmatched.

    Returns what happened: ``"settled"``, ``"unmatched"`` (nothing waiting on
    this correlation id) or ``"ignored"``.
    """
    if payload_kind == "data" or message_type not in (
            "", "coverage", "insurance", "preauth", "task", "claim",
            "paymentnotice", "communication", "status"):
        return "ignored"

    headers = envelope.get("jwe_headers")
    correlation_id = (headers or {}).get("x-hcx-correlation_id") \
        if isinstance(headers, dict) else None

    # A payment notice is the payer's own message, not an answer to one of
    # ours, so it is matched by the claim number inside it rather than by a
    # correlation id — which it still carries, and which dedupes redeliveries.
    if message_type == "paymentnotice":
        return _receive_payment(envelope, correlation_id or "")
    # A query opens a thread of its own — the payer's gateway mints the
    # correlation id — so it too is matched by what it names, not by what
    # this EMR is waiting on.
    if message_type == "communication":
        return _receive_communication(envelope, correlation_id or "")

    if not correlation_id:
        return "unmatched"

    # The small exchanges — a status enquiry, an appeal — each wait on a
    # correlation id of their own, so they are looked for first; a Task on
    # one of those threads is theirs.
    if message_type in ("status", "task") and db.one(
            "SELECT id FROM claim_enquiry WHERE correlation_id = ?",
            (correlation_id,)):
        return _receive_enquiry(envelope, correlation_id)
    if message_type == "status":
        return "unmatched"

    if message_type == "insurance":
        return _receive_plan(envelope, correlation_id)
    if message_type == "preauth":
        return _receive_preauth(envelope, correlation_id)
    if message_type == "task":
        return _receive_cancel(envelope, correlation_id)
    if message_type == "claim":
        return _receive_claim(envelope, correlation_id)

    row = db.one("SELECT * FROM claim WHERE correlation_id = ? "
                 "ORDER BY id DESC", (correlation_id,))
    if row is None:
        # Two coverage exchanges run per claim — the eligibility check and the
        # auth-requirements ruling on a procedure set — so a coverage reply
        # nothing on `claim` is waiting for may still be one of ours.
        return _receive_auth(envelope, correlation_id)
    # A redelivery of something already settled must not reopen it: the worker
    # retries on anything but a 2xx, so this has to be safe to receive twice.
    if row["status"] != "checking":
        return "ignored"

    body = _callback_body(envelope)

    # A gateway or payer rejection arrives as a plain ProtocolResponse in place
    # of the on_check bundle — the same shape _protocol_error digs out of the
    # ledger when polling.
    if body.get("type") == "ProtocolResponse":
        db.update("claim", row["id"], {
            "status": "error",
            "error_message": _rejection(body),
        })
        return "settled"

    apply_response(row["id"], parse_validation_bundle(body), body)
    return "settled"


def _receive_plan(envelope: dict[str, Any], correlation_id: str) -> str:
    """The insurance half of :func:`receive` — settle a package master."""
    row = db.one("SELECT * FROM claim_plan WHERE correlation_id = ? "
                 "ORDER BY id DESC", (correlation_id,))
    if row is None:
        return "unmatched"
    if row["status"] != "fetching":
        return "ignored"

    body = _callback_body(envelope)
    if body.get("type") == "ProtocolResponse":
        db.update("claim_plan", row["id"], {"status": "error",
                                            "error_message": _rejection(body)})
        return "settled"

    apply_plan(row["id"], parse_plan_bundle(body), body)
    return "settled"


def _receive_auth(envelope: dict[str, Any], correlation_id: str) -> str:
    """The auth-requirements half of the coverage callback."""
    row = db.one("SELECT * FROM claim_auth WHERE correlation_id = ? "
                 "ORDER BY id DESC", (correlation_id,))
    if row is None:
        return "unmatched"
    if row["status"] != "checking":
        return "ignored"

    body = _callback_body(envelope)
    if body.get("type") == "ProtocolResponse":
        db.update("claim_auth", row["id"], {"status": "error",
                                            "error_message": _rejection(body)})
        return "settled"

    adapter = payers.for_claim(claim(row["claim_id"]))
    apply_auth(row["id"], parse_auth_bundle(body, adapter), body)
    return "settled"


def _receive_preauth(envelope: dict[str, Any], correlation_id: str) -> str:
    """The preauth half of :func:`receive` — apply a reply to a submission.

    A pre-authorisation is answered several times over its life on the *same*
    correlation id: an acknowledgement, then a query, then the decision.
    Refusing anything after the first reply — which is what a settled-status
    guard does — throws the actual approval away. So a reply is applied
    whenever it is one this row has not already seen.
    """
    row = db.one("SELECT * FROM claim_preauth WHERE correlation_id = ? "
                 "ORDER BY id DESC", (correlation_id,))
    if row is None:
        # A predetermination is answered on the same route.
        return _receive_predetermination(envelope, correlation_id)
    if row["status"] == "cancelled":
        return "ignored"

    body = _callback_body(envelope)
    if body.get("type") == "ProtocolResponse":
        db.update("claim_preauth", row["id"], {"status": "error",
                                               "error_message": _rejection(body)})
        return "settled"

    parsed = parse_claim_response(body)
    if row["status"] == "cancelling":
        # A payer that withdraws a pre-authorisation closes its thread too,
        # with a ClaimResponse adjudicated `cancelled`. That is the
        # withdrawal being confirmed, not a rejection — and anything else
        # on the thread while the cancel Task is out is the Task's to
        # settle (poll_cancel), not this.
        if (parsed.get("adjudication") or "").lower() == "cancelled":
            db.update("claim_preauth", row["id"], {
                "status": "cancelled", "settled_at": db.now_iso(),
                "error_message": None,
                "outcome": parsed.get("outcome"),
                "adjudication": parsed.get("adjudication"),
                "disposition": parsed.get("disposition"),
                "api_call_id": _api_call_id(envelope) or None,
                "response_json": json.dumps(body, ensure_ascii=False),
            })
            return "settled"
        return "ignored"
    api_call_id = _api_call_id(envelope)
    if _already_applied(row, api_call_id, parsed):
        return "ignored"
    apply_preauth(row["id"], parsed, body, api_call_id)
    return "settled"


def _receive_payment(envelope: dict[str, Any], correlation_id: str) -> str:
    """Record a payment notice and acknowledge it on the spot.

    The acknowledgement is best-effort: a notice that was received but could
    not be acknowledged is still received, and answering the callback with an
    error would only have hcxkit deliver it again. The failure is recorded on
    the row and the screen offers to re-send.
    """
    body = _callback_body(envelope)
    headers = envelope.get("jwe_headers")
    headers = headers if isinstance(headers, dict) else {}
    outcome, payment_id = record_payment(
        body, correlation_id,
        sender_code=headers.get("x-hcx-sender_code") or "",
        workflow_id=headers.get("x-hcx-workflow_id") or "")
    if payment_id is None:
        return outcome
    try:
        acknowledge_payment(payment_id)
    except ValueError:
        pass  # recorded on the row; the payments tab offers a re-send
    return "settled"


def _receive_communication(envelope: dict[str, Any],
                           correlation_id: str) -> str:
    """The communication half of the callback — a payer asking for more.

    The bundle carries a ``CommunicationRequest``; a ``Communication`` on this
    route is somebody's answer, not a question, and is left alone. The
    request is matched by the claim number it names and, failing that, by
    the queried submission's thread in ``x-hcx-workflow_id``. Nothing on the
    request's own correlation id is ever waiting: that thread starts here.
    """
    body = _callback_body(envelope)
    if body.get("type") == "ProtocolResponse":
        return "ignored"
    if not any(isinstance(e, dict)
               and (e.get("resource") or {}).get("resourceType")
               == "CommunicationRequest"
               for e in body.get("entry") or []):
        # A Communication, or nothing at all: not a question for this desk.
        return "ignored"
    headers = envelope.get("jwe_headers")
    headers = headers if isinstance(headers, dict) else {}
    outcome, _ = record_query(
        body, correlation_id,
        sender_code=headers.get("x-hcx-sender_code") or "",
        workflow_id=headers.get("x-hcx-workflow_id") or "")
    return outcome


def _receive_claim(envelope: dict[str, Any], correlation_id: str) -> str:
    """The claim half of the callback — the payer's verdict on the claim."""
    row = db.one("SELECT * FROM claim_submission WHERE correlation_id = ? "
                 "ORDER BY id DESC", (correlation_id,))
    if row is None:
        return "unmatched"

    body = _callback_body(envelope)
    if body.get("type") == "ProtocolResponse":
        db.update("claim_submission", row["id"], {
            "status": "error", "error_message": _rejection(body)})
        return "settled"

    parsed = parse_claim_response(body)
    api_call_id = _api_call_id(envelope)
    if _already_applied(row, api_call_id, parsed):
        return "ignored"
    apply_claim(row["id"], parsed, body, api_call_id)
    return "settled"


def _receive_cancel(envelope: dict[str, Any], correlation_id: str) -> str:
    """The Task half of the callback — the payer's answer to a cancellation."""
    row = db.one("SELECT * FROM claim_preauth WHERE cancel_correlation_id = ? "
                 "ORDER BY id DESC", (correlation_id,))
    if row is None:
        return "unmatched"
    if row["status"] != "cancelling":
        return "ignored"

    body = _callback_body(envelope)
    if body.get("type") == "ProtocolResponse":
        db.update("claim_preauth", row["id"], {
            "status": "error", "error_message": _rejection(body)})
        return "settled"

    apply_cancel(row["id"], parse_task_response(body), body)
    return "settled"


def _latest_reply(related, resource_type: str):
    """The newest inbound on a thread that actually carries the reply.

    hcxkit's ``related`` list is every row on the correlation, in both
    directions — and when one kit serves both participants (the sandbox
    loopback), the payer's inbound copy of *our own* request is on it too,
    as are NHCX's redeliveries of it. The reply is the newest inbound whose
    bundle carries the resource the reply is made of: a ClaimResponse for a
    pre-authorisation or a claim, a CoverageEligibilityResponse for a
    check, a Task for a cancellation.
    """
    # On that shared kit the payer's inbound copy of our own request is on
    # the thread with our participant code as its sender — and for a Task
    # it carries the very resource the reply is made of, so it would read
    # as an answer ("requested", with our own words as the description)
    # until the real one lands. What we sent is never what we are waiting
    # for.
    org = db.default_org()
    own = ((org["participant_code"] if org is not None else "") or "").strip().lower()
    inbound = [r for r in related or []
               if isinstance(r, dict) and r.get("direction") == "in"
               and not (own and (r.get("sender") or "").strip().lower() == own)]
    for entry in reversed(inbound):
        envelope = _api("/internal/txn/fhir", {"txnId": entry["id"]})
        bundle = (_first(envelope, "fhir", "payload")
                  if isinstance(envelope, dict) else None)
        if not isinstance(bundle, dict):
            continue
        for item in bundle.get("entry") or []:
            resource = item.get("resource") if isinstance(item, dict) else None
            if (isinstance(resource, dict)
                    and resource.get("resourceType") == resource_type):
                return envelope, bundle
    return None, None


def _peer_dispatch_error(related, own_txn_id: str) -> str | None:
    """On a shared gateway, what became of the other side's answer.

    When one hcxkit serves both participants, the payer's outbound answer
    is on our thread too, and if NHCX refused it — NHCX-1010, the thread
    closed because the request was redelivered three times without an
    acknowledgement it accepted — the reply will never arrive. Saying so
    beats waiting for it.
    """
    for entry in related or []:
        if (not isinstance(entry, dict) or entry.get("direction") != "out"
                or entry.get("id") == own_txn_id
                or entry.get("status") not in ("errored", "failed", "dead")):
            continue
        try:
            dispatch = _api("/internal/txn/dispatch", {"txnId": entry["id"]})
        except ValueError:
            dispatch = None
        detail = ""
        if isinstance(dispatch, dict):
            detail = (dispatch.get("errorMessage") or dispatch.get("errorCode")
                      or "")
        return ("The payer answered, but NHCX refused its reply"
                + (f": {detail[:300]}" if detail else ".")
                + " Nothing more will arrive on this thread — submit again "
                "to open a new one.")
    return None


def _api_call_id(envelope: dict[str, Any]) -> str:
    headers = envelope.get("jwe_headers")
    headers = headers if isinstance(headers, dict) else {}
    return str(headers.get("x-hcx-api_call_id") or "")


def _already_applied(row, api_call_id: str, parsed: dict[str, Any]) -> bool:
    """Has this exact reply already been taken in?

    The api_call_id identifies one message, so a redelivery of it is a
    redelivery. Without one — a payer that omits it — a reply saying exactly
    what the row already says is treated as the same message, which is the
    best that can be told apart.
    """
    if api_call_id:
        return row["api_call_id"] == api_call_id
    if row["status"] == "submitting":
        return False
    return (row["outcome"] == parsed.get("outcome")
            and row["adjudication"] == parsed.get("adjudication"))


def _callback_body(envelope: dict[str, Any]) -> dict[str, Any]:
    body = envelope.get("fhir")
    if not isinstance(body, dict):
        raise ValueError("The callback carried no readable payload.")
    return body


def _rejection(body: dict[str, Any]) -> str:
    """The operator-facing text of an NHCX ProtocolResponse rejection."""
    details = body.get("x-hcx-error_details") or {}
    return (f'{details.get("code") or "NHCX"}: '
            f'{details.get("message") or "The gateway rejected the request."}')


def _protocol_error(correlation_id: str | None, since: str | None,
                    kind: str = "coverage") -> str | None:
    """Find an NHCX ProtocolResponse rejection addressed to this exchange.

    A payer-side rejection (e.g. PAYR-1008 for a wrong HFR ID) arrives as a
    plain-JSON ProtocolResponse, not an encrypted on_check. hcxkit cannot read
    protocol headers off that payload, so its ledger row gets a fresh
    correlation id and txn/related never links it — the only reliable join is
    the ``x-hcx-correlation_id`` inside the stored body.
    """
    if not correlation_id:
        return None
    try:
        ledger = _api("/internal/txn/list", method="GET")
    except ValueError:
        return None
    since = since or ""
    candidates = [r for r in ledger or [] if isinstance(r, dict)
                  and r.get("direction") == "in"
                  and r.get("type") == kind
                  and (r.get("created") or "") >= since][:20]
    for entry in candidates:
        try:
            envelope = _api("/internal/txn/fhir", {"txnId": entry["id"]})
        except ValueError:
            continue
        body = envelope.get("fhir") if isinstance(envelope, dict) else None
        if (isinstance(body, dict)
                and body.get("type") == "ProtocolResponse"
                and body.get("x-hcx-correlation_id") == correlation_id
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


# ------------------------------------------- insurance plan (package master)
# The payer's digital policy for this policy-provider pair: every empanelled
# speciality, the packages under it and their rates. It is a lookup — the
# request carries no clinical content at all, only a policy number and the
# facility's HFR ID — and the answer arrives asynchronously on
# v1/insuranceplan/on_request, exactly like the coverage on_check.

PLAN_STATUS = {
    "fetching": ("Awaiting payer", "info"),
    "ready": ("Ready", "success"),
    "empty": ("No plan returned", "warning"),
    "error": ("Error", "danger"),
}

# Task.intent on the discovery request. hcxkit's PMJAY sample
# (pmay_bundle/insurance_request.json) sends "plan"; the published Insurance
# Plan IG's element table says "order". Both are legal FHIR Task intents — the
# sample is the shape this payer has been exercised with, so it wins, and this
# is the single place to flip if one is rejected.
PLAN_TASK_INTENT = "plan"

# Every bundle in the PMJAY sample set anchors the provider Organization on
# this one absolute URL instead of a urn:uuid, and dereferences it by exact
# string match — so it is carried verbatim, coverage path and all.
PROVIDER_ANCHOR = ("https://payer.nha.gov.in/coverageeligibility/v1/"
                   "coverageeligibility/check/coverageeligibilityrequest/"
                   "organization/prov")

TASK_INPUT_CS = ("https://nrces.in/ndhm/fhir/r4/CodeSystem/"
                 "ndhm-task-input-type-code")
NDHM_SD = "https://nrces.in/ndhm/fhir/r4/StructureDefinition"
PAYER_SYSTEM = "https://payer.pmjay.nha.gov.in"
FACILITY_SYSTEM = "https://facility.abdm.gov.in"
V2_0203 = "http://terminology.hl7.org/CodeSystem/v2-0203"
ORG_TYPE_CS = "http://terminology.hl7.org/CodeSystem/organization-type"


def _task_input(code: str, value: str) -> dict[str, Any]:
    return {"type": {"coding": [{"system": TASK_INPUT_CS, "code": code}]},
            "valueString": value}


def build_plan_request(case_id: str, policy_code: str | None,
                       provider_id: str, provider_name: str,
                       provider_phone: str | None = None) -> dict[str, Any]:
    """The InsurancePlan discovery Task bundle, shaped like the PMJAY sample.

    A Task (``status`` requested, ``code`` poll) naming what to look the plan
    up by, plus the provider Organization it refers to. At least one
    ``Task.input`` is mandatory — either the policy number or the provider id
    — and both go when both are known, which is the more precise lookup.
    """
    inputs = []
    if policy_code:
        inputs.append(_task_input("policyNumber", policy_code))
    if provider_id:
        inputs.append(_task_input("providerId", provider_id))
    if not inputs:
        raise ValueError("A plan request needs a policy code or the "
                         "facility's HFR ID.")

    stamp = db.now_iso()
    task_id = str(uuid.uuid4())
    task = {
        "resourceType": "Task",
        "id": task_id,
        "meta": {"profile": [f"{NDHM_SD}/Task"]},
        "status": "requested",
        "intent": PLAN_TASK_INTENT,
        # `poll` is the IG's fetch/discovery code — this is a lookup, not a
        # create or update. The sample omits it; it is 1..1 in the IG.
        "code": {"coding": [{"system": "https://nhcx.abdm.gov.in/api",
                             "code": "poll"}]},
        "authoredOn": stamp,
        "requester": {"reference": PROVIDER_ANCHOR, "display": "Organization"},
        "input": inputs,
    }
    organization = {
        "resourceType": "Organization",
        "id": "1",
        "meta": {"profile": [f"{NDHM_SD}/Organization"]},
        "identifier": [{
            "type": {"coding": [{"system": V2_0203, "code": "NPI",
                                 "display": "National provider identifier"}]},
            "system": FACILITY_SYSTEM,
            "value": provider_id,
        }],
        "active": True,
        "type": [{"coding": [{"system": ORG_TYPE_CS, "code": "prov",
                              "display": "Healthcare Provider"}]}],
        "name": provider_name,
    }
    if provider_phone:
        organization["contact"] = [
            {"telecom": [{"system": "phone", "value": provider_phone}]}]

    return {
        "id": "INSURANCE_REQUEST",
        "identifier": {"system": PAYER_SYSTEM, "value": case_id},
        "meta": {"lastUpdated": stamp},
        "type": "collection",
        "resourceType": "Bundle",
        "timestamp": stamp,
        "entry": [
            {"fullUrl": f"urn:uuid:{task_id}", "resource": task},
            {"id": PROVIDER_ANCHOR, "fullUrl": PROVIDER_ANCHOR,
             "resource": organization},
        ],
    }


def plan(claim_id: int):
    """The claim's package master row, if one has ever been requested."""
    return db.one("SELECT * FROM claim_plan WHERE claim_id = ?", (claim_id,))


def plan_benefits(plan_id: int, category: str = "", search: str = "",
                  kind: str = "") -> list:
    """The packages under a fetched plan, narrowed as the operator asked.

    A payer master runs to a thousand packages, so the picker is a search
    rather than a scroll: ``search`` matches the code *or* the name, either
    substring and either case, which is how an operator arrives — with half a
    procedure name, or with a code off a referral note.
    """
    sql = "SELECT * FROM claim_plan_benefit WHERE plan_id = ?"
    params: list[Any] = [plan_id]
    if category:
        sql += " AND category_code = ?"
        params.append(category)
    if kind:
        sql += " AND kind = ?"
        params.append(kind)
    term = (search or "").strip()
    if term:
        sql += " AND (code LIKE ? OR display LIKE ?)"
        params += [f"%{term}%", f"%{term}%"]
    return db.query(sql + " ORDER BY seq", params)


def benefit_implants(row) -> list[dict[str, Any]]:
    """The implants a package allows, resolved to their own plan entries.

    An implant is named twice in the plan — as a qualifier on the package's
    ``Implant`` cost, and as a benefit of its own — so the tier here can be
    joined back to the entry carrying that implant's conditions and documents.
    Every implant tier rate in the live plan equals its own entry's rate; the
    tier's figure still wins, because that is what this package was quoted.
    """
    tiers = [t for t in plan_extras(row) if t.get("type") == "Implant"]
    codes = [t["code"] for t in tiers if t.get("code")]
    entries = {b["code"]: b for b in _benefits_by_code(row["plan_id"], codes)}
    out = []
    for tier in tiers:
        entry = entries.get(tier.get("code"))
        out.append({**tier, "id": entry["id"] if entry else None,
                    "display": tier.get("label")
                    or (entry["display"] if entry else tier.get("code"))})
    return out


def benefit_procedures(row) -> list:
    """The packages that allow this implant — the relation read backwards."""
    if row["kind"] != "Implant":
        return []
    candidates = db.query(
        "SELECT * FROM claim_plan_benefit WHERE plan_id = ? AND id <> ? "
        "AND extras LIKE ? ORDER BY seq", (row["plan_id"], row["id"],
                                           f'%"{row["code"]}"%'))
    # LIKE only narrows the scan; the extras are JSON, so confirm properly.
    return [c for c in candidates
            if any(t.get("code") == row["code"] and t.get("type") == "Implant"
                   for t in plan_extras(c))]


def _benefits_by_code(plan_id: int, codes: list[str]) -> list:
    wanted = [c for c in dict.fromkeys(codes) if c]
    if not wanted:
        return []
    holes = ",".join("?" * len(wanted))
    return db.query(
        f"SELECT * FROM claim_plan_benefit WHERE plan_id = ? "
        f"AND code IN ({holes})", [plan_id, *wanted])


def plan_forms(plan_id: int, urls: list[str] | None = None) -> list:
    """The questionnaires shipped with a plan, or just the ones asked for."""
    if urls is None:
        return db.query("SELECT * FROM claim_plan_form WHERE plan_id = ? "
                        "ORDER BY title", (plan_id,))
    wanted = [u for u in dict.fromkeys(urls) if u]
    if not wanted:
        return []
    holes = ",".join("?" * len(wanted))
    return db.query(
        f"SELECT * FROM claim_plan_form WHERE plan_id = ? AND url IN ({holes}) "
        "ORDER BY title", [plan_id, *wanted])


def plan_form(form_id: int):
    """One questionnaire, with the claim it belongs to."""
    return db.one("SELECT f.*, p.claim_id FROM claim_plan_form f "
                  "JOIN claim_plan p ON p.id = f.plan_id WHERE f.id = ?",
                  (form_id,))


def search_forms(plan_id: int, search: str = "") -> list:
    """The plan's questionnaires, filtered by title or id."""
    term = (search or "").strip()
    if not term:
        return plan_forms(plan_id)
    return db.query(
        "SELECT * FROM claim_plan_form WHERE plan_id = ? "
        "AND (title LIKE ? OR form_id LIKE ?) ORDER BY title",
        (plan_id, f"%{term}%", f"%{term}%"))


def form_questions(row) -> list[dict[str, Any]]:
    """One questionnaire's questions, back from the stored JSON."""
    parsed = _stored_json(row, "items")
    return parsed if isinstance(parsed, list) else []


def policy_documents(plan_row) -> list[dict[str, Any]]:
    """What every claim under this policy needs, whatever the package."""
    parsed = _stored_json(plan_row, "policy_documents")
    return parsed if isinstance(parsed, list) else []


def plan_benefit(benefit_id: int):
    """One package, with the claim it belongs to, for the detail view."""
    return db.one(
        "SELECT b.*, p.claim_id, p.plan_title FROM claim_plan_benefit b "
        "JOIN claim_plan p ON p.id = b.plan_id WHERE b.id = ?", (benefit_id,))


def plan_categories(plan_id: int) -> list:
    """Speciality code/name pairs with a package count, for the filter."""
    return db.query(
        "SELECT category_code, category_display, COUNT(*) AS n "
        "FROM claim_plan_benefit WHERE plan_id = ? "
        "GROUP BY category_code, category_display ORDER BY category_display",
        (plan_id,))


def request_plan(claim_id: int) -> None:
    """Queue the InsurancePlan discovery Task for this claim's policy."""
    row = claim(claim_id)
    if row is None:
        raise ValueError("Claim not found.")

    org = db.default_org()
    if org is None or not org["identifier_value"]:
        raise ValueError("Set the facility's HFR ID under Settings before "
                         "fetching a package master.")
    if not org["participant_code"]:
        raise ValueError("Set the facility's NHCX participant code under "
                         "Settings before fetching a package master.")

    provider_id = org["identifier_value"]
    policy_code = (row["policy_code"] or "").strip()
    bundle = build_plan_request(row["claim_no"], policy_code, provider_id,
                                org["name"], org["phone"])

    ack = _api("/fhir/out/v1/insuranceplan/request", {
        "jwe_headers": {
            "x-hcx-sender_code": org["participant_code"],
            "x-hcx-recipient_code": row["payer_id"] or PAYER_CODE,
            "x-hcx-workflow_id": row["claim_no"],
        },
        "fhir": bundle})

    values = {
        "status": "fetching",
        "txn_id": ack.get("txn_id"),
        "correlation_id": ack.get("correlation_id"),
        "requested_at": db.now_iso(),
        "fetched_at": None,
        "error_message": None,
        "policy_code": policy_code or None,
        "provider_id": provider_id,
        "plan_identifier": None,
        "plan_title": None,
        "plan_type": None,
        "sum_insured": None,
        "response_json": None,
    }
    existing = plan(claim_id)
    with db.transaction():
        if existing is None:
            db.insert("claim_plan", dict(values) | {"claim_id": claim_id})
        else:
            # A refetch replaces the master wholesale — the payer's answer is
            # the authority, and a stale package must not survive it.
            db.execute("DELETE FROM claim_plan_benefit WHERE plan_id = ?",
                       (existing["id"],))
            db.update("claim_plan", existing["id"], values)


def poll_plan(claim_id: int) -> bool:
    """One look at the hcxkit ledger for the payer's on_request reply.

    The push callback normally beats this; polling is what makes a missed or
    misconfigured callback cost immediacy rather than the plan.
    """
    row = plan(claim_id)
    if row is None or row["status"] != "fetching" or not row["txn_id"]:
        return False
    try:
        related = _api("/internal/txn/related", {"txnId": row["txn_id"]})
    except GatewayError as error:
        if error.status == 404:
            db.update("claim_plan", row["id"], {
                "status": "error",
                "error_message": "The hcxkit gateway no longer has this "
                                 "transaction — its ledger was reset after "
                                 "the request was sent. Fetch again.",
            })
            return True
        raise
    envelope, bundle = _latest_reply(related, "InsurancePlan")
    if bundle is not None:
        apply_plan(row["id"], parse_plan_bundle(bundle), bundle)
        return True
    error = _protocol_error(row["correlation_id"], row["requested_at"],
                            "insurance")
    if error:
        db.update("claim_plan", row["id"], {"status": "error",
                                            "error_message": error})
        return True
    dispatch = _api("/internal/txn/dispatch", {"txnId": row["txn_id"]})
    if isinstance(dispatch, dict) and dispatch.get("status") in (
            "dispatch_failed", "dead", "failed"):
        db.update("claim_plan", row["id"], {
            "status": "error",
            "error_message": dispatch.get("errorMessage")
            or dispatch.get("errorCode") or "Dispatch to NHCX failed.",
        })
        return True
    return False


def _coding(concept: Any) -> tuple[str | None, str | None]:
    """First code/display of a CodeableConcept, tolerating text-only ones."""
    if not isinstance(concept, dict):
        return None, None
    coding = (concept.get("coding") or [{}])[0]
    if not isinstance(coding, dict):
        coding = {}
    return coding.get("code"), coding.get("display") or concept.get("text")


def _tail(url: Any) -> str:
    """The last path segment of an extension url — its local name."""
    return str(url or "").rstrip("/").rsplit("/", 1)[-1]


def _scalar(extension: dict[str, Any]) -> Any:
    """The value[x] of an extension, whatever type the payer chose."""
    for key, value in extension.items():
        if not key.startswith("value"):
            continue
        if isinstance(value, dict):
            code, display = _coding(value)
            return display or code or value.get("value")
        return value
    return None


# The url families PMJAY hangs off a plan element, and the cost type code that
# marks the package rate. Everything verified against a live sandbox bundle:
# 2711 benefits, 1285 of them priced, costs coded Procedure / Stratification /
# Implant.
# The guides write these urls as `claimCondition` /
# `claimSupportingInfoRequirement`; the live payload spells them
# `Claim-Condition` / `Claim-SupportingInfoRequirement`. Match on a squashed
# form so either reaches the right bag.
CONDITION_FAMILY = "claimcondition"
SUPPORTING_FAMILY = "claimsupportinginforequirement"
PACKAGE_COST_CODE = "Procedure"


def _family(url: str) -> str:
    """Which extension family a url belongs to, spelling-insensitively."""
    squashed = url.lower().replace("-", "").replace("_", "")
    if SUPPORTING_FAMILY in squashed:
        return "supporting"
    if CONDITION_FAMILY in squashed:
        return "condition"
    return ""


def _benefit_extensions(element: dict[str, Any]) -> tuple[dict[str, Any],
                                                          list[dict[str, Any]]]:
    """Split a plan element's extensions into conditions and document needs.

    Both are complex extensions under one url family each, and they mean
    different things — a condition constrains *whether* the benefit can be
    claimed, a supporting-info requirement names a document that must ride
    with the claim — so they must not land in the same bag.

    A condition's own child url *names* it
    (``…/Claim-Condition/IsDayCare`` → ``IsDayCare: "Y"``); a supporting-info
    requirement's children are a ``category`` and a ``code`` CodeableConcept
    (``MAND0409``, "any investigations done"). The guides publish the codes but
    not these urls, so a payer writing conditions as an explicit code/value
    pair is read too.
    """
    conditions: dict[str, Any] = {}
    documents: list[dict[str, Any]] = []
    for extension in element.get("extension") or []:
        if not isinstance(extension, dict):
            continue
        url = str(extension.get("url") or "")
        family = _family(url)
        children = [c for c in extension.get("extension") or []
                    if isinstance(c, dict)]
        if family == "supporting":
            entry = _supporting_entry(children)
            if entry.get("code"):
                documents.append(entry)
            continue
        if family != "condition":
            continue
        if not children:
            value = _scalar(extension)
            if value is not None:
                conditions[_tail(url)] = value
            continue
        code = value = None
        for child in children:
            name = _tail(child.get("url"))
            if name in ("code", "type", "condition"):
                code = _scalar(child)
            elif name == "value":
                value = _scalar(child)
            else:
                # The PMJAY shape: the child's url tail is the condition name.
                conditions[name] = _scalar(child)
        if code:
            conditions[str(code)] = value if value is not None else True
    return conditions, documents


BENEFIT_KINDS = {"Procedure": "Procedure", "Implant": "Implant"}


# The two spellings seen for a requirement's children, and the child that
# points at the form to fill in for it.
SUPPORT_CATEGORY = ("category", "supportinfocategory")
SUPPORT_CODE = ("code", "supportinfocode")


def _supporting_entry(children: list[dict[str, Any]]) -> dict[str, Any]:
    """One document requirement: what it is, and the form that goes with it."""
    entry: dict[str, Any] = {}
    for child in children:
        name = _tail(child.get("url")).lower()
        if name in SUPPORT_CATEGORY or name in SUPPORT_CODE:
            code, display = _coding(child.get("valueCodeableConcept"))
            if name in SUPPORT_CODE:
                entry["code"], entry["display"] = code, display
            else:
                entry["category"] = code
                entry["category_display"] = display
        elif name == "documentationurl":
            reference = child.get("valueReference") or {}
            entry["form"] = (reference.get("reference")
                             or child.get("valueUrl")
                             or child.get("valueString"))
    return entry


def _benefit_costs(costs: Any) -> tuple[Any, Any, list[dict[str, Any]]]:
    """Split a benefit's costs into the package rate and what rides on top.

    The cost coded ``Procedure`` **is** the package rate. Everything else —
    ``Stratification`` (the ICU / HDU / ventilator per-day tiers) and
    ``Implant`` — is money paid *over and above* it, named by the qualifier
    hanging off that cost. A benefit with no ``Procedure`` cost has no package
    rate, and its tiers must not be mistaken for one.
    """
    rate = currency = None
    extras: list[dict[str, Any]] = []
    for cost in costs or []:
        if not isinstance(cost, dict):
            continue
        code, _ = _coding(cost.get("type"))
        quantity = cost.get("value")
        quantity = quantity if isinstance(quantity, dict) else {}
        amount = quantity.get("value")
        unit = quantity.get("unit") or quantity.get("code")
        if code == PACKAGE_COST_CODE:
            if rate is None:
                rate, currency = amount, unit
            continue
        # The qualifier names the tier — "ICU - With Ventilator", "Mesh -
        # 15 X 15". Its code is kept too: it is how an implant offered as a
        # tier here is recognised as the same thing offered as a benefit of
        # its own elsewhere in the bundle.
        qualifier_code = label = None
        for qualifier in cost.get("qualifiers") or []:
            qualifier_code, label = _coding(qualifier)
            if qualifier_code or label:
                break
        extras.append({"type": code, "code": qualifier_code,
                       "label": label or qualifier_code, "rate": amount,
                       "currency": unit})
    return rate, currency, extras


def _json_or_none(value: Any) -> str | None:
    return json.dumps(value, ensure_ascii=False) if value else None


def _questionnaire(resource: dict[str, Any]) -> dict[str, Any]:
    """Flatten one Questionnaire into the form a document requirement needs.

    The payer writes the question on ``item.prefix`` far more often than on
    ``item.text`` (7423 against 81 in the live plan), so both are read, prefix
    first. Items nest, and the answer options are plain strings rather than
    codings.
    """
    def items(nodes: Any, depth: int = 0) -> list[dict[str, Any]]:
        out = []
        for node in nodes or []:
            if not isinstance(node, dict):
                continue
            options, initial = [], None
            for option in node.get("answerOption") or []:
                if not isinstance(option, dict):
                    continue
                label = option.get("valueString")
                if label is None:
                    label = _coding(option.get("valueCoding"))[1] \
                        or (option.get("valueCoding") or {}).get("code")
                if label is None:
                    continue
                options.append(str(label))
                # The payer pre-selects one of the options on some questions;
                # that is its default answer, not decoration.
                if option.get("initialSelected") and initial is None:
                    initial = str(label)
            out.append({
                "linkId": node.get("linkId"),
                "type": node.get("type"),
                "text": node.get("prefix") or node.get("text") or "",
                "required": bool(node.get("required")),
                "depth": depth,
                "options": options,
                "initial": initial,
            })
            out += items(node.get("item"), depth + 1)
        return out

    url = resource.get("url") or ""
    return {
        "url": url,
        "form_id": resource.get("id"),
        "title": resource.get("title") or resource.get("name") or url,
        # `/questionnaire/…` is a policy form, `/stgquestionnaire/…` a
        # standard-treatment-guideline checklist; the url is what says which.
        "kind": url.rstrip("/").rsplit("/", 2)[-2] if "/" in url else None,
        "items": json.dumps(items(resource.get("item")), ensure_ascii=False),
    }


def parse_plan_bundle(bundle: dict[str, Any]) -> dict[str, Any]:
    """Flatten an InsurancePlan bundle into the plan row and its benefits.

    Both published shapes are read, because a provider parser has to take
    whichever the payer sends: PMJAY's package master
    (``plan → specificCost → category → benefit → cost``) and the indemnity
    shape (``coverage → benefit → limit``). The live sandbox sends **both, for
    the same packages** — 975 codes under ``specificCost`` and a 1053-code
    superset under ``coverage`` — so they are merged on the package code
    rather than concatenated, which would double every package. ``specificCost``
    wins where both describe one: its costs are explicitly typed.

    An **empty plan is a legitimate answer** for a policy-provider pair with no
    matching coverage, not a parse failure, so it comes back as zero benefits
    rather than an exception.
    """
    resources = [e.get("resource", {}) for e in bundle.get("entry", [])
                 if isinstance(e, dict)]
    plans = [r for r in resources
             if isinstance(r, dict) and r.get("resourceType") == "InsurancePlan"]
    if not plans:
        raise ValueError("The payer reply carries no InsurancePlan.")
    resource = plans[0]

    identifier = (resource.get("identifier") or [{}])[0]
    # Requirements on the resource itself are policy-wide — proof of identity,
    # proof of address — and apply to every claim, not to one package.
    _, policy_documents = _benefit_extensions(resource)
    values: dict[str, Any] = {
        "plan_identifier": identifier.get("value") or resource.get("id"),
        "plan_title": resource.get("name"),
        "plan_type": _coding((resource.get("type") or [{}])[0])[1],
        "sum_insured": None,
        "policy_documents": _json_or_none(policy_documents),
    }

    # The payer ships the same questionnaire once per benefit that needs it —
    # 1440 resources for 991 distinct forms in the live plan — so they are
    # collected by url.
    forms: dict[str, dict[str, Any]] = {}
    for entry in resources:
        if isinstance(entry, dict) and entry.get("resourceType") == "Questionnaire":
            form = _questionnaire(entry)
            if form["url"]:
                forms.setdefault(form["url"], form)

    # Keyed by package code so the two shapes merge instead of doubling up.
    benefits: dict[str, dict[str, Any]] = {}
    # Implants are named twice: as the qualifier on a benefit's Implant cost,
    # and (in this payer's bundle) as 78 benefits of their own under coverage.
    # Collecting the qualifier codes here is what lets the second set be typed
    # as implants rather than passed off as procedures.
    implant_codes: set[str] = set()
    for offered in resource.get("plan") or []:
        if not isinstance(offered, dict):
            continue
        if values["plan_type"] is None:
            values["plan_type"] = _coding(offered.get("type"))[1]
        for general in offered.get("generalCost") or []:
            money = (general or {}).get("cost") or {}
            if money.get("value") is not None and values["sum_insured"] is None:
                values["sum_insured"] = money["value"]
        plan_conditions, plan_documents = _benefit_extensions(offered)
        for specific in offered.get("specificCost") or []:
            if not isinstance(specific, dict):
                continue
            cat_code, cat_display = _coding(specific.get("category"))
            for benefit in specific.get("benefit") or []:
                if not isinstance(benefit, dict):
                    continue
                code, display = _coding(benefit.get("type"))
                if not code:
                    continue
                if code in benefits:
                    continue
                rate, currency, extras = _benefit_costs(benefit.get("cost"))
                for extra in extras:
                    if extra["type"] == "Implant" and extra.get("code"):
                        implant_codes.add(str(extra["code"]))
                own_conditions, documents = _benefit_extensions(benefit)
                kind = "Procedure" if rate is not None else next(
                    (BENEFIT_KINDS[e["type"]] for e in extras
                     if e["type"] in BENEFIT_KINDS), None)
                benefits[code] = {
                    "category_code": cat_code,
                    "category_display": cat_display,
                    "code": code, "display": display, "kind": kind,
                    "rate": rate, "currency": currency,
                    "cost_type": PACKAGE_COST_CODE if rate is not None else None,
                    "requirement": None,
                    "conditions": _json_or_none(plan_conditions | own_conditions),
                    "extras": _json_or_none(extras),
                    "supporting_info": _json_or_none(plan_documents + documents),
                }

    # Approach 2 — coverage → benefit → limit. For an indemnity-style policy
    # this is the whole master; alongside a package master it contributes only
    # the packages specificCost did not carry.
    for coverage in resource.get("coverage") or []:
        if not isinstance(coverage, dict):
            continue
        cat_code, cat_display = _coding(coverage.get("type"))
        cover_conditions, cover_documents = _benefit_extensions(coverage)
        for benefit in coverage.get("benefit") or []:
            if not isinstance(benefit, dict):
                continue
            code, display = _coding(benefit.get("type"))
            if not code or code in benefits:
                continue
            # The limit that names the package itself is its rate; the rest
            # (STRAT…, implants) are tiers paid over the top, exactly as
            # specificCost's non-Procedure costs are.
            rate = currency = None
            extras: list[dict[str, Any]] = []
            for limit in benefit.get("limit") or []:
                if not isinstance(limit, dict):
                    continue
                limit_code, limit_display = _coding(limit.get("code"))
                quantity = limit.get("value")
                quantity = quantity if isinstance(quantity, dict) else {}
                amount = quantity.get("value")
                unit = quantity.get("unit") or quantity.get("code")
                if rate is None and limit_code in (None, code):
                    rate, currency = amount, unit
                    continue
                extras.append({"type": "Limit", "code": limit_code,
                               "label": limit_display or limit_code,
                               "rate": amount, "currency": unit})
            own_conditions, documents = _benefit_extensions(benefit)
            benefits[code] = {
                "category_code": cat_code, "category_display": cat_display,
                "code": code, "display": display,
                "kind": "Implant" if code in implant_codes else "Procedure",
                "rate": rate, "currency": currency,
                "cost_type": benefit.get("requirement") or None,
                "requirement": benefit.get("requirement"),
                "conditions": _json_or_none(cover_conditions | own_conditions),
                "extras": _json_or_none(extras),
                "supporting_info": _json_or_none(cover_documents + documents),
            }

    return {"values": values, "benefits": list(benefits.values()),
            "forms": list(forms.values())}


def apply_plan(plan_id: int, parsed: dict[str, Any],
               bundle: dict[str, Any]) -> None:
    """Store the payer's package master and settle the plan status."""
    benefits = parsed.get("benefits") or []
    with db.transaction():
        db.update("claim_plan", plan_id, dict(parsed.get("values") or {}) | {
            "status": "ready" if benefits else "empty",
            "fetched_at": db.now_iso(),
            "error_message": None,
            "response_json": json.dumps(bundle, ensure_ascii=False),
        })
        db.execute("DELETE FROM claim_plan_benefit WHERE plan_id = ?",
                   (plan_id,))
        db.execute("DELETE FROM claim_plan_form WHERE plan_id = ?", (plan_id,))
        for seq, benefit in enumerate(benefits, 1):
            db.insert("claim_plan_benefit",
                      dict(benefit) | {"plan_id": plan_id, "seq": seq})
        for form in parsed.get("forms") or []:
            db.insert("claim_plan_form", dict(form) | {"plan_id": plan_id})


def plan_condition_map(row) -> dict[str, Any]:
    """A benefit row's stored claim conditions, back as a dict."""
    parsed = _stored_json(row, "conditions")
    return parsed if isinstance(parsed, dict) else {}


def plan_extras(row) -> list[dict[str, Any]]:
    """The stratification / implant tiers paid over this benefit's rate."""
    parsed = _stored_json(row, "extras")
    return parsed if isinstance(parsed, list) else []


def plan_documents(row) -> list[dict[str, Any]]:
    """The documents the payer requires when claiming this benefit."""
    parsed = _stored_json(row, "supporting_info")
    return parsed if isinstance(parsed, list) else []


def _stored_json(row, column: str) -> Any:
    try:
        return json.loads(row[column] or "null")
    except (json.JSONDecodeError, TypeError, IndexError, KeyError):
        return None


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


def packages(claim_id: int | None = None) -> list[dict[str, Any]]:
    """What the package picker offers, payer master first.

    Once a claim's InsurancePlan has come back, the payer's own packages *are*
    the master for that policy-provider pair — provider-specific, policy-
    specific and rate-bearing — so they replace the local HBP list. Without a
    fetched plan (or for a plan the payer answered empty) the local
    ``claim_package`` terminology is what there is.
    """
    if claim_id is not None:
        row = plan(claim_id)
        if row is not None and row["status"] == "ready":
            return [{"code": b["code"],
                     "display": b["display"] or b["code"],
                     "rate": b["rate"] or 0.0,
                     "category": b["category_display"],
                     "source": "payer"}
                    for b in plan_benefits(row["id"])]
    rows = []
    for term in db.terms("claim_package"):
        try:
            rate = float(term["extra"] or 0)
        except ValueError:
            rate = 0.0
        rows.append({"code": term["code"], "display": term["display"],
                     "rate": rate, "category": None, "source": "local"})
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


def admission_dossier(encounter_id: int | None) -> dict[str, list]:
    """What the admission itself already says about the case.

    The diagnoses recorded on the IPD stay (category ``diagnosis``, coded
    from the same SNOMED/ICD-10 terminology the pre-authorisation quotes)
    and the consultant the stay is under. The pre-authorisation takes both
    from here rather than asking for them a second time; the form only asks
    when the admission has not recorded them.
    """
    if not encounter_id:
        return {"diagnoses": [], "team": []}
    diagnoses = []
    for condition in db.query(
            "SELECT * FROM condition WHERE encounter_id = ? "
            "AND category = 'diagnosis' AND clinical_status != 'resolved' "
            "ORDER BY id", (encounter_id,)):
        term = db.term("diagnosis", condition["snomed_code"] or "")
        if term is not None:
            diagnoses.append({"code": term["code"]})
        elif condition["icd10_code"]:
            diagnoses.append({"code": "", "icd10_code": condition["icd10_code"],
                              "icd10_display": condition["icd10_display"]
                              or condition["text"],
                              "snomed_code": condition["snomed_code"],
                              "snomed_display": condition["snomed_display"]
                              or condition["text"]})
    team = []
    encounter = db.one("SELECT practitioner_id FROM encounter WHERE id = ?",
                       (encounter_id,))
    if encounter is not None and encounter["practitioner_id"]:
        team.append({"doctor": str(encounter["practitioner_id"]),
                     "role": "treating"})
    return {"diagnoses": diagnoses, "team": team}


def save_preauth(claim_id: int, values: dict[str, Any],
                 diagnoses: list[dict[str, str]], team: list[dict[str, str]],
                 items: list[dict[str, str]]) -> None:
    """Validate and store the preauth draft — the claim row plus its children.

    Prices are never taken from the form: a package's rate and an item's unit
    price are re-read from the masters at save time, only quantities are the
    operator's to choose. Nor are the diagnoses and the treating doctor,
    when the admission has them: the IPD stay is the record, and the
    pre-authorisation quotes it. The form's own rows are used only for an
    admission that recorded neither.
    """
    row = claim(claim_id)
    if row is None:
        raise ValueError("Claim not found.")
    if not row["encounter_id"]:
        raise ValueError("Link the admitted patient before drafting a "
                         "pre-authorisation.")
    recorded = admission_dossier(row["encounter_id"])
    if recorded["diagnoses"]:
        diagnoses = recorded["diagnoses"]
    if recorded["team"]:
        team = recorded["team"]

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
            if entry.get("icd10_code"):
                # Coded on the admission outside the terminology: quoted
                # as recorded, since the ICD-10 is what the payer reads.
                dx_rows.append({
                    "snomed_code": entry.get("snomed_code") or "",
                    "snomed_display": entry.get("snomed_display") or "",
                    "icd10_code": entry["icd10_code"],
                    "icd10_display": entry.get("icd10_display") or "",
                })
            continue
        dx_rows.append({
            "snomed_code": term["code"], "snomed_display": term["display"],
            "icd10_code": term["alt_code"] or term["code"],
            "icd10_display": term["alt_display"] or term["display"],
        })
    if not dx_rows:
        raise ValueError("Record the diagnosis on the admission (or pick "
                         "one here) — the payer needs an ICD-10 code.")

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
        raise ValueError("Set the consultant on the admission (or add a "
                         "doctor here) — the payer needs a treating doctor.")

    case_type = values.get("case_type") or ""
    if case_type not in CASE_TYPES:
        raise ValueError("Choose whether this is a package or a non-package "
                         "case.")

    package_code = package_name = None
    item_rows = []
    if case_type == "package":
        plan_row = plan(claim_id)
        if plan_row is not None and plan_row["status"] == "ready":
            # With the payer's own master fetched, a package case is quoted
            # line by line — the procedure, the implants it approves and the
            # ward tier — not picked from one dropdown of a thousand codes.
            procedures = [r for r in lines(claim_id) if r["kind"] == "Procedure"]
            if not procedures:
                raise ValueError("Quote at least one procedure for this "
                                 "package case — use “Choose line items”.")
            package_code = procedures[0]["code"]
            package_name = procedures[0]["display"]
            total = lines_total(claim_id)
        else:
            package = next((p for p in packages(claim_id)
                            if p["code"]
                            == (values.get("package_code") or "").strip()),
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


# ------------------------------------------------------------- preauth lines
# What a PMJAY preauth actually quotes: the procedures being done (with a
# quantity), the implants approved for them, and the ward / ICU stratification
# tier the stay runs at. All three come from the payer's own package master —
# there is no local price list in this path.
LINE_KINDS = {
    "Procedure": "Procedure",
    "Implant": "Implant",
    "Stratification": "Ward / ICU tier",
}


def lines(claim_id: int) -> list:
    return db.query("SELECT * FROM claim_line WHERE claim_id = ? "
                    "ORDER BY kind, seq", (claim_id,))


def lines_total(claim_id: int) -> float:
    return round(db.scalar("SELECT SUM(amount) FROM claim_line "
                           "WHERE claim_id = ?", (claim_id,), 0) or 0, 2)


def _plan_for(claim_id: int):
    row = plan(claim_id)
    if row is None or row["status"] != "ready":
        raise ValueError("Fetch the payer's insurance plan first — the "
                         "preauth quotes its packages and its rates.")
    return row


def add_line(claim_id: int, kind: str, code: str, parent_code: str = "") -> None:
    """Put one package, implant or ward tier on the preauth.

    The price is read from the plan at add time and never from the form. A
    stratification tier is not a benefit of its own, so it is resolved through
    the procedure whose costs carry it — different procedures price the same
    ward differently.
    """
    if kind not in LINE_KINDS:
        raise ValueError("That is not something a preauth can quote.")
    code = (code or "").strip()
    if not code:
        raise ValueError("Nothing was selected.")
    plan_row = _plan_for(claim_id)

    if kind == "Stratification":
        parent = db.one("SELECT * FROM claim_plan_benefit WHERE plan_id = ? "
                        "AND code = ?", (plan_row["id"], parent_code))
        if parent is None:
            raise ValueError("Pick the procedure this ward tier belongs to.")
        tier = next((t for t in plan_extras(parent)
                     if t.get("code") == code
                     and t.get("type") == "Stratification"), None)
        if tier is None:
            raise ValueError("That ward tier is not offered for this package.")
        values = {"display": tier.get("label") or code,
                  "category_code": parent["category_code"],
                  "category_display": parent["category_display"],
                  "unit_price": tier.get("rate") or 0}
    else:
        benefit = db.one("SELECT * FROM claim_plan_benefit WHERE plan_id = ? "
                         "AND code = ? AND kind = ?",
                         (plan_row["id"], code, kind))
        if benefit is None:
            raise ValueError(f"{code} is not a {kind.lower()} in this plan.")
        values = {"display": benefit["display"] or code,
                  "category_code": benefit["category_code"],
                  "category_display": benefit["category_display"],
                  "unit_price": benefit["rate"] or 0}

    if db.one("SELECT id FROM claim_line WHERE claim_id = ? AND kind = ? "
              "AND code = ?", (claim_id, kind, code)):
        raise ValueError(f"{code} is already on this preauth.")
    seq = (db.scalar("SELECT MAX(seq) FROM claim_line WHERE claim_id = ?",
                     (claim_id,), 0) or 0) + 1
    db.insert("claim_line", values | {
        "claim_id": claim_id, "seq": seq, "kind": kind, "code": code,
        "quantity": 1, "amount": round(values["unit_price"], 2)})


def remove_line(claim_id: int, line_id: int) -> None:
    db.execute("DELETE FROM claim_line WHERE id = ? AND claim_id = ?",
               (line_id, claim_id))


def plan_rate(claim_id: int, line) -> float | None:
    """What the payer's plan prices this line at; None when it names none."""
    plan_row = plan(claim_id)
    if plan_row is None:
        return None
    if line["kind"] == "Stratification":
        for parent in db.query("SELECT * FROM claim_plan_benefit WHERE "
                               "plan_id = ?", (plan_row["id"],)):
            for tier in plan_extras(parent):
                if (tier.get("code") == line["code"]
                        and tier.get("type") == "Stratification"):
                    return tier.get("rate")
        return None
    benefit = db.one("SELECT rate FROM claim_plan_benefit WHERE plan_id = ? "
                     "AND code = ? AND kind = ?",
                     (plan_row["id"], line["code"], line["kind"]))
    return None if benefit is None else benefit["rate"]


def price_is_open(claim_id: int, line) -> bool:
    """May the hospital put its own price on this line?

    Only where the plan prices it at nothing: a package PMJAY lists at zero
    is billed at the hospital's rate, and there is nothing else to quote.
    A priced package is quoted at the plan's figure and nothing the form
    says changes that.
    """
    return not plan_rate(claim_id, line)


def save_quantities(claim_id: int, quantities: dict[int, Any],
                    prices: dict[int, Any] | None = None) -> None:
    """Re-quantify the lines; the amount is always recomputed.

    A quantity is a whole number — a package is done once, an implant is a
    count of implants. A price is taken only for a line the plan leaves
    unpriced; anywhere else the plan's rate stands.
    """
    prices = prices or {}
    with db.transaction():
        for row in lines(claim_id):
            unit_price = row["unit_price"] or 0
            if row["id"] in prices and price_is_open(claim_id, row):
                try:
                    unit_price = float(prices[row["id"]] or 0)
                except (TypeError, ValueError):
                    raise ValueError(f'Enter a price for {row["code"]}.')
                if unit_price < 0:
                    raise ValueError(f'The price of {row["code"]} cannot be '
                                     "negative.")
            if row["id"] in quantities:
                raw = str(quantities[row["id"]] or "").strip()
                try:
                    quantity = int(raw)
                except ValueError:
                    try:
                        as_float = float(raw)
                    except ValueError:
                        as_float = 0.5
                    if not as_float.is_integer():
                        raise ValueError(f'The quantity of {row["code"]} '
                                         "must be a whole number.")
                    quantity = int(as_float)
                if quantity <= 0:
                    raise ValueError(f'Enter a quantity for {row["code"]}.')
            else:
                quantity = int(row["quantity"] or 1)
            db.update("claim_line", row["id"], {
                "quantity": quantity,
                "unit_price": round(unit_price, 2),
                "amount": round(unit_price * quantity, 2)})


def line_suggestions(claim_id: int) -> dict[str, list]:
    """The implants and ward tiers the chosen procedures actually allow.

    This is the whole point of having fetched the package master: an operator
    should not be searching a thousand codes for the implant that goes with
    the procedure they just picked — the payer already said which ones do.
    """
    plan_row = plan(claim_id)
    if plan_row is None or plan_row["status"] != "ready":
        return {"implants": [], "tiers": []}
    chosen = {r["code"] for r in lines(claim_id)}
    procedures = [r for r in lines(claim_id) if r["kind"] == "Procedure"]
    implants: dict[str, dict[str, Any]] = {}
    tiers: dict[str, dict[str, Any]] = {}
    for line in procedures:
        benefit = db.one("SELECT * FROM claim_plan_benefit WHERE plan_id = ? "
                         "AND code = ?", (plan_row["id"], line["code"]))
        if benefit is None:
            continue
        for implant in benefit_implants(benefit):
            if implant.get("code") and implant["code"] not in chosen:
                implants.setdefault(implant["code"], dict(implant))
        for tier in plan_extras(benefit):
            if tier.get("type") != "Stratification" or not tier.get("code"):
                continue
            if tier["code"] in chosen:
                continue
            tiers.setdefault(tier["code"],
                             dict(tier) | {"parent": line["code"],
                                           "parent_display": line["display"]})
    return {"implants": list(implants.values()), "tiers": list(tiers.values())}


# -------------------------------------------------------------- payments
# The one leg the payer starts. It posts a PaymentNotice when money moves and
# expects an acknowledgement straight back, so there is no correlation id of
# ours to match on — the claim is named by the `CLN` identifier inside the
# bundle. Several notices arrive over a claim's life (initiated, then
# cleared), which is why they are rows rather than columns.
PAYMENT_STATUS = {
    "pending": ("Not acknowledged", "warning"),
    "sent": ("Acknowledged", "success"),
    "error": ("Acknowledgement failed", "danger"),
}

TASK_OUTPUT_TYPE_CS = "https://nrces.in/ndhm/fhir/r4/CodeSystem/ndhm-task-output-type"
TASK_OUTPUT_VALUE_CS = "https://nrces.in/ndhm/fhir/r4/CodeSystem/ndhm-task-output-value"


def payments(claim_id: int) -> list:
    return db.query("SELECT * FROM claim_payment WHERE claim_id = ? "
                    "ORDER BY id DESC", (claim_id,))


def payment(payment_id: int):
    return db.one("SELECT * FROM claim_payment WHERE id = ?", (payment_id,))


def payment_details(payment_id: int) -> list:
    return db.query("SELECT * FROM claim_payment_detail WHERE payment_id = ? "
                    "ORDER BY seq", (payment_id,))


SETTLED_PAYMENT_STATUS = ("paid", "cleared")


def paid_total(claim_id: int) -> float:
    """What the payer says it has actually paid.

    Successive notices describe the *same* money — PMJAY sends one when the
    payment is initiated and another when it clears, both already stamped
    `paid` and both carrying the full amount — so counting notices would
    double the total. They are counted once per UTR, newest notice winning,
    since the UTR is what identifies a payment.
    """
    seen: dict[str, float] = {}
    for row in db.query("SELECT * FROM claim_payment WHERE claim_id = ? "
                        "ORDER BY id", (claim_id,)):
        if (row["payment_status"] or "").lower() not in SETTLED_PAYMENT_STATUS:
            continue
        if payment_initiated_only(row):
            continue
        seen[row["utr"] or f'#{row["id"]}'] = row["amount"] or 0
    return round(sum(seen.values()), 2)


def _claim_for_reference(reference: str | None):
    """Find the claim a payer-initiated message names.

    The CLN is the claim number as it stood when the claim went out, which is
    not always the number the episode carries now — a cancellation retires one
    — so the historical references recorded on each leg are searched too.
    """
    reference = (reference or "").strip()
    if not reference:
        return None
    row = db.one("SELECT * FROM claim WHERE claim_no = ?", (reference,))
    if row is not None:
        return row
    for table in ("claim_submission", "claim_preauth"):
        found = db.one(f"SELECT claim_id FROM {table} WHERE claim_ref = ? "
                       "ORDER BY id DESC", (reference,))
        if found is not None:
            return claim(found["claim_id"])
    return None


def parse_payment_notice(bundle: dict[str, Any]) -> dict[str, Any]:
    """Flatten a payer's payment notice.

    Three resources carry the story between them: the `Task` says what
    happened in prose, the `PaymentNotice` the amount and status, and the
    `PaymentReconciliation` the date, the UTR and the breakdown of what was
    paid against what was withheld.
    """
    resources = [e.get("resource", {}) for e in bundle.get("entry", [])
                 if isinstance(e, dict)]

    def first(kind: str) -> dict[str, Any]:
        return next((r for r in resources if isinstance(r, dict)
                     and r.get("resourceType") == kind), {})

    notice = first("PaymentNotice")
    reconciliation = first("PaymentReconciliation")
    task = first("Task")
    if not notice and not reconciliation:
        raise ValueError("That is not a payment notice.")

    # Where the claim number hides, in the order worth trying. The published
    # sample types it `CLN` on the PaymentNotice; the live payer leaves it
    # untyped on every resource, so the first entry's own identifier is the
    # last thing tried before giving up. The *bundle* identifier is not tried
    # at all — live it is a message uuid, and a wrong match is worse than none.
    entries = [e for e in bundle.get("entry", []) if isinstance(e, dict)]
    first_entry = (entries[0].get("resource") if entries else {}) or {}
    reference = (_identifier_value(notice, "CLN")
                 or _identifier_value(reconciliation, "CLN")
                 or _identifier_value(task, "CLN")
                 or _identifier_value(first_entry, "CLN"))
    money = notice.get("amount") or reconciliation.get("paymentAmount") or {}

    details = []
    for index, line in enumerate(reconciliation.get("detail") or [], 1):
        if not isinstance(line, dict):
            continue
        code, display = _coding(line.get("type"))
        details.append({
            "seq": index,
            "reference": line.get("id")
            or (line.get("identifier") or {}).get("value"),
            "type_code": code, "type_display": display,
            "date": line.get("date"),
            "amount": (line.get("amount") or {}).get("value"),
        })

    status_code, status_display = _coding(notice.get("paymentStatus"))
    return {
        "claim_ref": reference,
        # The notice's own id names the payment it is about: the payer
        # sends one when the transfer is initiated and one when it clears,
        # and the second updates the first rather than sitting beside it.
        "notice_id": notice.get("id") or None,
        # The sample writes a sentence; the live payer writes nothing at all
        # and leaves the status to speak for the notice.
        "disposition": (reconciliation.get("disposition")
                        or task.get("description")
                        or status_display
                        or reconciliation.get("outcome")
                        or "Payment notice"),
        "payment_status": status_code,
        "payment_date": reconciliation.get("paymentDate")
        or (notice.get("created") or "")[:10] or None,
        "amount": money.get("value"),
        "currency": money.get("currency"),
        "utr": (reconciliation.get("paymentIdentifier") or {}).get("value"),
        "details": details,
    }


def _identifier_value(resource: dict[str, Any], type_code: str) -> str | None:
    """One identifier off a resource — the typed one if it says which.

    The published samples type the claim number `CLN`; the live payer sends
    the same value with no `type` at all, only a system that spells the
    resource name. So a typed match wins, and an untyped identifier is taken
    rather than ignored — being untyped is not the same as being absent.
    """
    identifiers = [i for i in (resource or {}).get("identifier") or []
                   if isinstance(i, dict) and i.get("value")]
    for identifier in identifiers:
        if _coding(identifier.get("type"))[0] == type_code:
            return identifier.get("value")
    for identifier in identifiers:
        if not identifier.get("type"):
            return identifier.get("value")
    return None


def record_payment(bundle: dict[str, Any], correlation_id: str = "",
                   sender_code: str = "",
                   workflow_id: str = "") -> tuple[str, int | None]:
    """Store one payment notice against the claim its number names.

    Returns the outcome and the row: ``("settled", id)`` for a new notice,
    ``("ignored", None)`` for a redelivery of one already held, and
    ``("unmatched", None)`` when no claim here answers to that number.
    """
    parsed = parse_payment_notice(bundle)
    if correlation_id and db.one(
            "SELECT id FROM claim_payment WHERE correlation_id = ?",
            (correlation_id,)):
        return "ignored", None
    row = _claim_for_reference(parsed["claim_ref"])
    if row is None:
        return "unmatched", None
    details = parsed.pop("details")
    earlier = None
    if parsed["notice_id"]:
        earlier = db.one("SELECT id FROM claim_payment WHERE claim_id = ? "
                         "AND notice_id = ?", (row["id"], parsed["notice_id"]))
    with db.transaction():
        values = parsed | {
            "claim_id": row["id"],
            "correlation_id": correlation_id or None,
            "sender_code": sender_code or None,
            "workflow_id": workflow_id or None,
            "received_at": db.now_iso(),
            "notice_json": json.dumps(bundle, ensure_ascii=False),
        }
        if earlier is not None:
            # The same payment, further along: the row moves on and is
            # acknowledged again, on the new thread.
            payment_id = earlier["id"]
            db.update("claim_payment", payment_id, values | {
                "ack_status": "pending", "ack_txn_id": None,
                "ack_correlation_id": None, "acknowledged_at": None,
                "ack_error": None})
            db.execute("DELETE FROM claim_payment_detail WHERE payment_id = ?",
                       (payment_id,))
        else:
            payment_id = db.insert("claim_payment", values)
        for detail in details:
            db.insert("claim_payment_detail",
                      dict(detail) | {"payment_id": payment_id})
    return "settled", payment_id


def payment_initiated_only(row) -> bool:
    """Is this notice about money on its way rather than money arrived?

    A payer says so in the disposition ("Payment initiated…") and leaves
    the UTR off, because the bank has not returned one yet.
    """
    return (not row["utr"]
            and "initiat" in (row["disposition"] or "").lower())


def build_payment_ack(payment_id: int) -> dict[str, Any]:
    """The Task that tells the payer the money was seen."""
    money = payment(payment_id)
    if money is None:
        raise ValueError("Payment notice not found.")
    row = claim(money["claim_id"])
    org = db.default_org()
    if org is None or not org["identifier_value"] or not org["participant_code"]:
        raise ValueError("Set the facility's HFR ID and NHCX participant code "
                         "under Settings before acknowledging payments.")
    adapter = payers.for_claim(row)
    stamp = db.now_iso()
    task_id = str(uuid.uuid4())
    reference = money["claim_ref"] or row["claim_no"]
    provider_anchor = PROVIDER_ANCHOR
    payer_anchor = PROVIDER_ANCHOR.replace("/organization/prov",
                                           "/organization/pay")
    return {
        "id": "PreAuth",
        "identifier": {"system": adapter["payer_system"], "value": reference},
        "meta": {"lastUpdated": stamp},
        "type": "collection",
        "resourceType": "Bundle",
        "timestamp": stamp,
        "entry": [
            {"fullUrl": f"urn:uuid:{task_id}", "resource": {
                "resourceType": "Task",
                "id": task_id,
                "meta": {"profile": [f"{NDHM_SD}/Task"]},
                "status": "completed",
                "intent": "order",
                "code": _cc(FINANCIAL_TASK_CS, "status", None),
                "description": f"Received the payment {reference}",
                "authoredOn": stamp,
                "requester": {"reference": provider_anchor,
                              "display": "Organization"},
                "owner": {"reference": payer_anchor,
                          "display": "Organization"},
                "output": [
                    {"type": _cc(TASK_OUTPUT_TYPE_CS, "status", None),
                     "valueCodeableConcept": _cc(TASK_OUTPUT_VALUE_CS,
                                                 "paymentack",
                                                 "Payment is acknowledged")},
                    {"type": _cc(TASK_INPUT_CS, "claimNumber", None),
                     "valueString": reference},
                ],
            }},
            {"fullUrl": provider_anchor, "resource": {
                "resourceType": "Organization", "id": "1",
                "meta": {"profile": [f"{NDHM_SD}/Organization"]},
                "identifier": [{"type": _cc(V2_0203, "NPI",
                                            "National provider identifier"),
                                "system": FACILITY_SYSTEM,
                                "value": org["identifier_value"]}],
                "active": True,
                "type": [_cc(ORG_TYPE_CS, "prov", "Healthcare Provider")],
                "name": org["name"],
            }},
            {"fullUrl": payer_anchor, "resource": {
                "resourceType": "Organization", "id": "2",
                "meta": {"profile": [f"{NDHM_SD}/Organization"]},
                "identifier": [{"type": _cc(V2_0203, "NIIP",
                                            "National Insurance Payor "
                                            "Identifier (Payor)"),
                                "system": FACILITY_SYSTEM,
                                "value": (row["payer_id"]
                                          or PAYER_CODE).split("@")[0]}],
                "active": True,
                "type": [_cc(ORG_TYPE_CS, "pay", "Payer")],
                "name": row["payer_name"] or PAYER_NAME,
            }},
        ],
    }


def acknowledge_payment(payment_id: int) -> None:
    """Tell the payer the money was seen.

    Sent automatically the moment a notice lands, and re-sendable by hand when
    that fails — a failed acknowledgement is this EMR's problem to retry, not
    a reason to reject the notice, which really did arrive.
    """
    money = payment(payment_id)
    if money is None:
        raise ValueError("Payment notice not found.")
    row = claim(money["claim_id"])
    bundle = build_payment_ack(payment_id)
    org = db.default_org()
    try:
        ack = _api("/fhir/out/v1/paymentnotice/on_request", {
            "jwe_headers": {
                "x-hcx-sender_code": org["participant_code"],
                # Back to whoever sent the notice. That is not always the
                # payer the claim was raised with — a scheme can pay through
                # a different participant, and the live sandbox does.
                "x-hcx-recipient_code": (money["sender_code"]
                                         or row["payer_id"] or PAYER_CODE),
                "x-hcx-workflow_id": (money["workflow_id"]
                                      or money["claim_ref"]
                                      or row["claim_no"]),
                "x-hcx-correlation_id": money["correlation_id"] or "",
            },
            "fhir": bundle})
    except ValueError as error:
        db.update("claim_payment", payment_id, {
            "ack_status": "error", "ack_error": str(error)})
        raise
    db.update("claim_payment", payment_id, {
        "ack_status": "sent",
        "ack_txn_id": ack.get("txn_id"),
        "ack_correlation_id": ack.get("correlation_id"),
        "acknowledged_at": db.now_iso(),
        "ack_error": None,
    })


# ------------------------------------------------------------ the query loop
# Two payers, two ways of asking for more. PMJAY writes its query inside the
# ClaimResponse (`queried`, the trail in `query_note`) and expects the
# pre-authorisation submitted again with what was missing — "Submit again" on
# the pre-auth card is that answer. An IRDAI payer sends a
# `CommunicationRequest` on `communication/request`, on a thread of its own,
# and expects a `Communication` back on `communication/on_request` carrying
# that thread's correlation id: the text of the answer in
# `payload[].contentString`, the files in `payload[].contentAttachment`.
QUERY_STATUS = {
    "open": ("Awaiting our reply", "warning"),
    "answered": ("Answered", "success"),
    "error": ("Reply failed", "danger"),
}


def queries(claim_id: int, stage: str = "") -> list:
    """The payer's queries on a claim, newest first — one leg or all."""
    if stage:
        return db.query("SELECT * FROM claim_query WHERE claim_id = ? AND "
                        "stage = ? ORDER BY id DESC", (claim_id, stage))
    return db.query("SELECT * FROM claim_query WHERE claim_id = ? "
                    "ORDER BY id DESC", (claim_id,))


def query(query_id: int):
    return db.one("SELECT * FROM claim_query WHERE id = ?", (query_id,))


def query_questions(row) -> list[str]:
    """What the payer asked, line by line."""
    parsed = _stored_json(row, "questions")
    return [str(q) for q in parsed] if isinstance(parsed, list) else []


def query_reply_documents(row) -> list:
    """The documents that went with the reply, as attached."""
    ids = _stored_json(row, "reply_documents")
    if not isinstance(ids, list) or not ids:
        return []
    holes = ",".join("?" * len(ids))
    return db.query(
        f"SELECT id, claim_id, filename, content_type, label, code, stage, "
        f"size, uploaded_at FROM claim_document WHERE id IN ({holes}) "
        "ORDER BY id", ids)


def parse_communication_request(bundle: dict[str, Any]) -> dict[str, Any]:
    """Flatten a payer's query.

    ``about[]`` names the claim — the payer's own number and, when it
    differs, ours; ``identifier[CLN]`` repeats the payer's. Every
    ``payload[].contentString`` is one thing asked for: the remark on the
    case first, then one line per queried item, which is how the IRDAI
    payer spells it. ``reasonCode[].text`` repeats the lines; it is read
    only when the payload said nothing.
    """
    resources = [e.get("resource", {}) for e in bundle.get("entry", [])
                 if isinstance(e, dict)]
    requests = [r for r in resources if isinstance(r, dict)
                and r.get("resourceType") == "CommunicationRequest"]
    if not requests:
        raise ValueError("The payer message carries no CommunicationRequest.")
    request = requests[0]

    references: list[str] = []

    def name(value: Any) -> None:
        text = str(value or "").strip()
        if not text:
            return
        references.append(text)
        tail = text.rsplit("/", 1)[-1]
        if tail != text:
            references.append(tail)

    for about in request.get("about") or []:
        if isinstance(about, dict):
            name(about.get("reference"))
            name(about.get("display"))
            identifier = about.get("identifier")
            if isinstance(identifier, dict):
                name(identifier.get("value"))
    for identifier in request.get("identifier") or []:
        if isinstance(identifier, dict):
            name(identifier.get("value"))

    questions = [str(p["contentString"]).strip()
                 for p in request.get("payload") or []
                 if isinstance(p, dict) and p.get("contentString")]
    if not questions:
        questions = [str(r.get("text")).strip()
                     for r in request.get("reasonCode") or []
                     if isinstance(r, dict) and r.get("text")]
    questions = [q for q in dict.fromkeys(questions) if q]

    return {
        "request_id": str(request.get("id") or "").strip() or None,
        "references": list(dict.fromkeys(references)),
        "questions": questions,
        "remarks": "\n".join(questions) or None,
        "authored_on": request.get("authoredOn"),
    }


def record_query(bundle: dict[str, Any], correlation_id: str = "",
                 sender_code: str = "",
                 workflow_id: str = "") -> tuple[str, int | None]:
    """Store one query against the leg it is about.

    Returns ``("settled", id)`` for a new query, ``("ignored", None)`` for a
    redelivery of one already held, and ``("unmatched", None)`` when nothing
    here answers to what it names.
    """
    parsed = parse_communication_request(bundle)
    if correlation_id and db.one(
            "SELECT id FROM claim_query WHERE correlation_id = ?",
            (correlation_id,)):
        return "ignored", None

    row = None
    for reference in parsed["references"]:
        row = _claim_for_reference(reference)
        if row is not None:
            break
    # The workflow id is the queried submission's thread, and the last
    # resort when the request named nothing this EMR knows.
    stage = None
    if workflow_id:
        for table, leg in (("claim_submission", "claim"),
                           ("claim_preauth", "preauth")):
            found = db.one(f"SELECT claim_id FROM {table} WHERE "
                           "correlation_id = ? ORDER BY id DESC",
                           (workflow_id,))
            if found is not None:
                stage = leg
                if row is None:
                    row = claim(found["claim_id"])
                break
    if row is None:
        return "unmatched", None
    if stage is None:
        # Whichever leg is out with the payer is the one being asked about;
        # a claim that has been filed is the later of the two.
        filed = submission(row["id"])
        stage = "claim" if filed is not None and filed["status"] not in (
            "draft", None) else "preauth"

    query_id = db.insert("claim_query", {
        "claim_id": row["id"],
        "stage": stage,
        "correlation_id": correlation_id or None,
        "request_id": parsed["request_id"],
        # The number the payer leads with is its own; ours, when it differs,
        # follows. The reply names the payer's first, as the payer reads it.
        "claim_ref": next((r for r in parsed["references"] if "/" not in r),
                          None),
        "sender_code": sender_code or None,
        "workflow_id": workflow_id or None,
        "received_at": db.now_iso(),
        "status": "open",
        "remarks": parsed["remarks"],
        "questions": json.dumps(parsed["questions"], ensure_ascii=False),
        "request_json": json.dumps(bundle, ensure_ascii=False),
    })
    return "settled", query_id


def build_communication_bundle(query_id: int, text: str,
                               document_ids: list[int]) -> dict[str, Any]:
    """The Communication that answers one query.

    Modelled on the shape the IRDAI payer documents for its door: the text in
    ``payload[].contentString``, each file as a ``contentAttachment`` with the
    document's code on the payload's extension so the payer files it under
    the requirement it was attached against, ``basedOn`` naming the request
    and ``about`` the claim. The two Organizations ride along so the
    references resolve inside the bundle.
    """
    asked = query(query_id)
    if asked is None:
        raise ValueError("Query not found.")
    row = claim(asked["claim_id"])
    org = db.default_org()
    if org is None or not org["identifier_value"] or not org["participant_code"]:
        raise ValueError("Set the facility's HFR ID and NHCX participant code "
                         "under Settings before answering a query.")
    adapter = payers.for_claim(row)
    text = (text or "").strip()
    if not text and not document_ids:
        raise ValueError("Write a reply or attach a document — an empty "
                         "answer tells the payer nothing.")

    stamp = db.now_iso()
    base = "https://provider.nhcx/communication"
    payload: list[dict[str, Any]] = []
    if text:
        payload.append({"contentString": text})
    for document_id in document_ids:
        attached = document(document_id)
        if attached is None or attached["claim_id"] != row["id"]:
            raise ValueError("One of the chosen documents is not on this "
                             "claim.")
        entry: dict[str, Any] = {"contentAttachment": {
            "contentType": attached["content_type"],
            "title": attached["label"] or attached["filename"],
            "creation": attached["uploaded_at"],
            "data": base64.b64encode(_document_bytes(attached)).decode()}}
        if attached["code"]:
            entry["extension"] = [{
                "url": f'{adapter["payer_system"]}/StructureDefinition/'
                       "document-type",
                "valueString": attached["code"]}]
        payload.append(entry)

    about = [{"reference": f'Claim/{asked["claim_ref"] or row["claim_no"]}',
              "display": row["claim_no"]}]
    if asked["claim_ref"] and asked["claim_ref"] != row["claim_no"]:
        about.append({"reference": f'Claim/{row["claim_no"]}',
                      "display": row["claim_no"]})

    communication = {
        "resourceType": "Communication",
        "id": str(uuid.uuid4()),
        "meta": {"profile": [f"{NDHM_SD}/Communication"]},
        "identifier": [{"system": f"{base}/reply",
                        "value": f'{row["claim_no"]}-Q{asked["id"]}'}],
        "status": "completed",
        "category": [_cc("http://terminology.hl7.org/CodeSystem/"
                         "communication-category", "instruction",
                         "Instruction")],
        "priority": "routine",
        "subject": {"display": row["beneficiary_name"] or ""},
        "about": about,
        "sent": stamp,
        "sender": {"reference": f"{base}/organization/prov"},
        "recipient": [{"reference": f"{base}/organization/pay"}],
        "payload": payload,
    }
    if asked["request_id"]:
        communication["basedOn"] = [
            {"reference": f'CommunicationRequest/{asked["request_id"]}'}]
        communication["inResponseTo"] = [
            {"reference": f'CommunicationRequest/{asked["request_id"]}'}]

    entries = [
        {"fullUrl": f"{base}/{communication['id']}",
         "resource": communication},
        {"fullUrl": f"{base}/organization/prov", "resource": {
            "resourceType": "Organization", "id": "1",
            "identifier": [{"type": _cc(V2_0203, "PRN", "Provider number"),
                            "system": FACILITY_SYSTEM,
                            "value": org["identifier_value"]}],
            "active": True,
            "type": [_cc(ORG_TYPE_CS, "prov", "Healthcare Provider")],
            "name": org["name"],
        }},
        {"fullUrl": f"{base}/organization/pay", "resource": {
            "resourceType": "Organization", "id": "2",
            "identifier": [{"type": _cc(V2_0203, "NIIP",
                                        "National Insurance Payor Identifier "
                                        "(Payor)"),
                            "system": FACILITY_SYSTEM,
                            "value": (row["payer_id"] or PAYER_CODE).split("@")[0]}],
            "active": True,
            "type": [_cc(ORG_TYPE_CS, "pay", "Payer")],
            "name": row["payer_name"] or PAYER_NAME,
        }},
    ]
    return {
        "resourceType": "Bundle",
        "id": str(uuid.uuid4()),
        "meta": {"lastUpdated": stamp, "profile": [f"{NDHM_SD}/Bundle"]},
        "identifier": {"system": adapter["payer_system"],
                       "value": f'{row["claim_no"]}-Q{asked["id"]}'},
        "type": "collection",
        "timestamp": stamp,
        "entry": entries,
    }


def answer_query(query_id: int, text: str,
                 document_ids: list[int] | None = None,
                 uploads: list[dict[str, Any]] | None = None) -> None:
    """Send the reply on the request's own thread.

    The correlation id the request arrived on goes back in the headers —
    that is how the payer ties the answer to the question — and the workflow
    id it carried rides along. A failed send is recorded on the row and the
    card offers to send again; the payer is never told twice about a reply
    it did receive, because the second send is the operator's decision.

    ``uploads`` are new files chosen on the reply itself — ``filename``,
    ``content_type``, ``data``, and optionally ``label`` and ``code``. They
    are filed on the claim like any other document, at the queried leg's
    stage, and travel with the reply beside the documents ticked from what
    was already attached. A file that cannot be filed refuses the whole
    reply before anything is sent.
    """
    asked = query(query_id)
    if asked is None:
        raise ValueError("Query not found.")
    row = claim(asked["claim_id"])
    document_ids = [int(d) for d in (document_ids or [])]
    for upload in uploads or []:
        document_ids.append(add_document(
            row["id"], upload.get("filename") or "", upload.get("content_type") or "",
            upload.get("data") or b"", upload.get("label") or "",
            code=upload.get("code") or "", stage=asked["stage"]))
    document_ids = list(dict.fromkeys(document_ids))
    bundle = build_communication_bundle(query_id, text, document_ids)
    org = db.default_org()
    leg = submission(row["id"]) if asked["stage"] == "claim" \
        else preauth(row["id"])
    workflow = (asked["workflow_id"]
                or (leg["correlation_id"] if leg is not None else None)
                or row["claim_no"])
    try:
        ack = _api("/fhir/out/v1/communication/on_request", {
            "jwe_headers": {
                "x-hcx-sender_code": org["participant_code"],
                "x-hcx-recipient_code": (asked["sender_code"]
                                         or row["payer_id"] or PAYER_CODE),
                "x-hcx-correlation_id": asked["correlation_id"] or "",
                "x-hcx-workflow_id": workflow,
            },
            "fhir": bundle})
    except ValueError as error:
        db.update("claim_query", query_id, {
            "status": "error",
            "error_message": str(error),
            "reply_text": (text or "").strip() or None,
            "reply_documents": json.dumps(document_ids),
        })
        raise
    db.update("claim_query", query_id, {
        "status": "answered",
        "error_message": None,
        "reply_text": (text or "").strip() or None,
        "reply_documents": json.dumps(document_ids),
        "reply_txn_id": ack.get("txn_id"),
        "reply_correlation_id": ack.get("correlation_id"),
        "answered_at": db.now_iso(),
        "reply_json": json.dumps(bundle, ensure_ascii=False),
    })


# ---------------------------------------------------------------- the claim
# How the stay ended. PMJAY prices a completed episode of care, so the mode is
# not paperwork — it decides what may be claimed at all.
DISCHARGE_MODES = {
    "normal": ("DTH", "Discharged home", "Normal discharge"),
    "lama": ("LAMA", "Left against medical advice", "LAMA"),
    "dama": ("DAMA", "Discharged against medical advice", "DAMA"),
    "death": ("DTM", "Died in hospital", "Death"),
}
DISCHARGE_STAGES = ("Before Surgery", "During Surgery", "After Surgery")

# For a LAMA/DAMA case discharged before or during surgery the payer accepts
# **only** this procedure and disqualifies everything the preauth approved
# (PAYR-1362). The other error text (PAYR-1270) says "after/during" instead;
# the claim-side rule is the one followed, and the conflict is flagged in the
# UI rather than guessed at silently.
LAMA_DAMA_CODE = "LM100"
LAMA_DAMA_STAGES = ("Before Surgery", "During Surgery")

CLAIM_STATUS = {
    "draft": ("Draft", "warning"),
    "submitting": ("Awaiting payer", "info"),
    "approved": ("Approved", "success"),
    "partial": ("Partially approved", "warning"),
    "queried": ("Query raised", "warning"),
    "rejected": ("Rejected", "danger"),
    "error": ("Error", "danger"),
}

CLAIM_WORKFLOW_ID = "15"


def submission(claim_id: int):
    return db.one("SELECT * FROM claim_submission WHERE claim_id = ?",
                  (claim_id,))


def _submission_row(claim_id: int):
    row = submission(claim_id)
    if row is None:
        db.insert("claim_submission", {"claim_id": claim_id, "status": "draft"})
        row = submission(claim_id)
    return row


def save_discharge(claim_id: int, values: dict[str, Any]) -> None:
    """Record how the stay ended, which is what makes a claim claimable."""
    row = _submission_row(claim_id)
    if row["status"] == "submitting":
        raise ValueError("The claim is with the payer; wait for its answer "
                         "before changing the discharge.")
    mode = (values.get("discharge_mode") or "").strip()
    if mode not in DISCHARGE_MODES:
        raise ValueError("Choose how the patient was discharged.")
    stage = (values.get("discharge_stage") or "").strip()
    if stage not in DISCHARGE_STAGES:
        raise ValueError("Choose whether that was before, during or after "
                         "surgery.")
    discharge = (values.get("discharge_date") or "").strip()
    if not discharge:
        raise ValueError("Enter the discharge date.")
    admission = claim(claim_id)["admission_date"]
    if admission and discharge < admission:
        raise ValueError("The discharge date cannot be before the admission "
                         "date.")
    surgery = (values.get("surgery_date") or "").strip()
    if surgery and admission and surgery < admission:
        raise ValueError("The surgery date cannot be before the admission "
                         "date.")
    death = (values.get("death_date") or "").strip()
    if mode == "death" and not death:
        raise ValueError("Enter the date and time of death.")
    if mode == "death" and len(death) == 10:
        raise ValueError("Enter the time of death as well as the date.")
    if mode != "death":
        death = ""

    db.update("claim_submission", row["id"], {
        "discharge_mode": mode,
        "discharge_stage": stage,
        "discharge_date": discharge,
        "surgery_date": surgery or None,
        "death_date": death or None,
        "status": "draft" if row["status"] in ("draft", "error") else row["status"],
    })


def lama_dama_only(row) -> bool:
    """Does this discharge collapse the claim to the LAMA/DAMA procedure?"""
    return (row is not None
            and row["discharge_mode"] in ("lama", "dama")
            and row["discharge_stage"] in LAMA_DAMA_STAGES)


def claim_lines(claim_id: int) -> list[dict[str, Any]]:
    """What the *claim* quotes, which is not always what the preauth did.

    A LAMA/DAMA case that ended before or during surgery is claimed as the
    single `LM100` procedure — the payer disqualifies every other approved
    item — so the substitution happens here rather than being left for the
    operator to remember and the payer to reject.
    """
    quoted = [dict(r) for r in lines(claim_id)]
    row = submission(claim_id)
    if not lama_dama_only(row):
        return quoted
    plan_row = plan(claim_id)
    benefit = None
    if plan_row is not None:
        benefit = db.one("SELECT * FROM claim_plan_benefit WHERE plan_id = ? "
                         "AND code = ?", (plan_row["id"], LAMA_DAMA_CODE))
    template = quoted[0] if quoted else {}
    return [{
        "kind": "Procedure",
        "code": LAMA_DAMA_CODE,
        "display": (benefit["display"] if benefit else "LAMA / DAMA procedure"),
        "category_code": (benefit["category_code"] if benefit
                          else template.get("category_code")),
        "category_display": (benefit["category_display"] if benefit
                             else template.get("category_display")),
        "unit_price": (benefit["rate"] if benefit else 0) or 0,
        "quantity": 1,
        "amount": (benefit["rate"] if benefit else 0) or 0,
    }]


def claim_total(claim_id: int) -> float:
    return round(sum(line["amount"] for line in claim_lines(claim_id)), 2)


# ------------------------------------------------------- forms and documents
def required_forms(claim_id: int, stage: str = "preauth") -> list:
    """The questionnaires one stage of the exchange has to carry.

    The payer's own ruling wins when there is one: an ``auth-requirements``
    check answers for *this* procedure set, and its requirements are split by
    the stage the payer asked for them at — the pre-authorisation gets its
    own, the claim gets the rest. Without a ruling the pre-authorisation falls
    back to every form the package master attached to the chosen lines, which
    is a superset; the claim asks for nothing it was not told to.
    """
    plan_row = plan(claim_id)
    if plan_row is None or plan_row["status"] != "ready":
        return []
    ruling = auth(claim_id)
    if ruling is not None and ruling["status"] == "ready":
        urls = [need["form_url"] for need in
                auth_requirements(ruling["id"], at_preauth=stage == "preauth")
                if need["kind"] == "form" and need["form_url"]]
        return plan_forms(plan_row["id"], urls)
    if stage != "preauth":
        return []
    codes = [r["code"] for r in lines(claim_id)]
    urls = []
    for benefit in _benefits_by_code(plan_row["id"], codes):
        urls += [d.get("form") for d in plan_documents(benefit) if d.get("form")]
    return plan_forms(plan_row["id"], urls)


def required_documents(claim_id: int, stage: str = "preauth") -> list:
    """The documents the payer named for one stage of the exchange."""
    ruling = auth(claim_id)
    if ruling is None or ruling["status"] != "ready":
        return []
    return [need for need in
            auth_requirements(ruling["id"], at_preauth=stage == "preauth")
            if need["kind"] == "document"]


def answers(claim_id: int, form_url: str = "") -> dict[str, str]:
    if form_url:
        rows = db.query("SELECT * FROM claim_form_answer WHERE claim_id = ? "
                        "AND form_url = ?", (claim_id, form_url))
    else:
        rows = db.query("SELECT * FROM claim_form_answer WHERE claim_id = ?",
                        (claim_id,))
    return {f'{r["form_url"]}|{r["link_id"]}': r["answer"] for r in rows}


def save_answers(claim_id: int, form_url: str,
                 given: dict[str, str]) -> None:
    """Store what was answered on one form; a blank answer clears the row."""
    with db.transaction():
        for link_id, answer in given.items():
            answer = (answer or "").strip()
            existing = db.one(
                "SELECT id FROM claim_form_answer WHERE claim_id = ? "
                "AND form_url = ? AND link_id = ?",
                (claim_id, form_url, link_id))
            if not answer:
                if existing:
                    db.execute("DELETE FROM claim_form_answer WHERE id = ?",
                               (existing["id"],))
                continue
            if existing:
                db.update("claim_form_answer", existing["id"],
                          {"answer": answer})
            else:
                db.insert("claim_form_answer", {
                    "claim_id": claim_id, "form_url": form_url,
                    "link_id": link_id, "answer": answer})


# --------------------------------------------- authorisation requirements
# Between choosing the lines and sending the preauth sits one more question:
# does the payer authorise *this procedure set*, and what does it want with
# it? That is a coverage eligibility check with purpose `auth-requirements`,
# carrying the same items the Claim will — so the answer is per line, and it
# names the documents and questionnaires the set needs.
AUTH_STATUS = {
    "checking": ("Awaiting payer", "info"),
    "ready": ("Validated", "success"),
    "error": ("Error", "danger"),
}


def auth(claim_id: int):
    return db.one("SELECT * FROM claim_auth WHERE claim_id = ?", (claim_id,))


def ruling_is_stale(claim_id: int) -> bool:
    """Has the procedure set changed since the payer ruled on it?

    The ruling is per line — which are authorised, what each needs — so a
    line added or removed since leaves it describing a set nobody is
    sending. The check is by code: quantities do not change what a line
    needs.
    """
    ruling = auth(claim_id)
    if ruling is None or ruling["status"] != "ready":
        return False
    ruled = {i["code"] for i in auth_items(ruling["id"]) if i["code"]}
    quoted = {r["code"] for r in lines(claim_id)
              if r["kind"] != "Stratification"}
    return bool(quoted) and quoted != ruled


def auth_items(auth_id: int) -> list:
    return db.query("SELECT * FROM claim_auth_item WHERE auth_id = ? "
                    "ORDER BY seq", (auth_id,))


def auth_requirements(auth_id: int, at_preauth: bool | None = None) -> list:
    sql = "SELECT * FROM claim_auth_requirement WHERE auth_id = ?"
    params: list[Any] = [auth_id]
    if at_preauth is not None:
        sql += " AND at_preauth = ?"
        params.append(1 if at_preauth else 0)
    return db.query(sql + " ORDER BY kind, seq", params)


def _claim_items(row, line_rows, adapter) -> list[dict[str, Any]]:
    """The `item[]` both the auth-requirements check and the Claim carry."""
    start = row["admission_date"] or db.today_iso()
    end = row["expected_discharge_date"] or start
    program = ([_cc(PROGRAM_CS, adapter["program_code"],
                    adapter["program_display"])]
               if adapter["program_code"] else [])
    items = []
    for index, line in enumerate(line_rows, 1):
        item = {
            "id": f"Item/{index}", "sequence": index,
            "category": _cc(adapter["payer_system"], line["category_code"],
                            line["category_display"]),
            "productOrService": _cc(SNOMED, line["code"], line["display"]),
            "servicedPeriod": {"start": start, "end": end},
            "quantity": {"value": line["quantity"]},
            "unitPrice": {"value": line["unit_price"]},
            "net": {"value": line["amount"]},
        }
        if program:
            item["programCode"] = program
        items.append(item)
    return items


def build_auth_bundle(claim_id: int) -> dict[str, Any]:
    """The purpose=auth-requirements CoverageEligibilityRequest bundle."""
    row = claim(claim_id)
    if row is None:
        raise ValueError("Claim not found.")
    adapter = payers.for_claim(row)
    if not adapter["auth_requirements"]:
        raise ValueError(f'{adapter["name"]} does not answer authorisation '
                         "requirement checks.")
    line_rows = lines(claim_id)
    if not line_rows:
        raise ValueError("Choose the line items first — this checks the "
                         "procedure set, so there has to be one.")
    org = db.default_org()
    if org is None or not org["identifier_value"] or not org["participant_code"]:
        raise ValueError("Set the facility's HFR ID and NHCX participant code "
                         "under Settings before checking requirements.")

    base = ("https://payer.nha.gov.in/coverageeligibility/v1/"
            "coverageeligibility/check/coverageeligibilityrequest")
    stamp = db.now_iso()

    def ref(path: str = "") -> dict:
        return {"reference": f"{base}/{path}" if path else base}

    entries = [
        {"fullUrl": base, "resource": {
            "resourceType": "CoverageEligibilityRequest",
            "id": row["policy_code"] or row["claim_no"],
            "meta": {"profile": [f"{NDHM_SD}/CoverageEligibilityRequest"]},
            "identifier": [{
                "system": "https://hcx.pmjay.gov.in/v1/coverageeligibility/check",
                "value": row["policy_code"] or row["claim_no"]}],
            "status": "active",
            "purpose": ["auth-requirements"],
            "patient": ref("patient"),
            "servicedDate": row["admission_date"] or db.today_iso(),
            "created": stamp,
            "insurer": ref("organization/pay"),
            "provider": ref("organization/prov"),
            "priority": _cc("http://terminology.hl7.org/CodeSystem/processpriority",
                            "normal", "Normal"),
            "facility": {"identifier": {"system": "https://nhcx.pmjay.gov.in",
                                        "value": org["identifier_value"]}},
            "insurance": [{"coverage": ref("coverage")}],
            "item": _claim_items(row, line_rows, adapter),
        }},
        {"fullUrl": f"{base}/patient", "resource": {
            "resourceType": "Patient", "id": "1",
            "identifier": [
                {"type": _cc(NDHM_ID_CS, "PMJAY",
                             "Pradhan Mantri Jan Aarogya Yojana (PMJAY) ID"),
                 "system": BIS_SYSTEM, "value": row["member_id"]},
                {"type": _cc(V2_0203, "JHN", "Jurisdictional health number"),
                 "system": BIS_SYSTEM, "value": row["abha_number"] or ""},
            ],
            "name": [{"text": row["beneficiary_name"] or ""}],
        }},
        {"fullUrl": f"{base}/organization/prov", "resource": {
            "resourceType": "Organization", "id": "1",
            "identifier": [{"type": _cc(V2_0203, "NPI",
                                        "National provider identifier"),
                            "system": FACILITY_SYSTEM,
                            "value": org["identifier_value"]}],
            "active": True,
            "type": [_cc(ORG_TYPE_CS, "prov", "Healthcare Provider")],
            "name": org["name"],
        }},
        {"fullUrl": f"{base}/organization/pay", "resource": {
            "resourceType": "Organization", "id": "2",
            "identifier": [{"type": _cc(V2_0203, "NIIP",
                                        "National Insurance Payor Identifier "
                                        "(Payor)"),
                            "system": FACILITY_SYSTEM,
                            "value": (row["payer_id"] or PAYER_CODE).split("@")[0]}],
            "active": True,
            "type": [_cc(ORG_TYPE_CS, "pay", "Payer")],
            "name": row["payer_name"] or PAYER_NAME,
        }},
        {"fullUrl": f"{base}/coverage", "resource": {
            "resourceType": "Coverage", "id": "1",
            "identifier": [{"type": _cc(V2_0203, "NH",
                                        "National Health Plan Identifier"),
                            "system": "https://payer.nha.gov.in",
                            "value": row["policy_code"] or ""}],
            "status": "active",
            "subscriberId": row["member_id"],
            "beneficiary": ref("patient"),
            "payor": [ref("organization/pay")],
        }},
    ]
    return {
        "id": "COVERAGE_AUTH_REQUEST",
        "identifier": {"system": adapter["payer_system"],
                       "value": row["claim_no"]},
        "meta": {"lastUpdated": stamp},
        "type": "collection",
        "resourceType": "Bundle",
        "timestamp": stamp,
        "entry": entries,
    }


def request_auth(claim_id: int) -> None:
    """Send the procedure set to the payer for an authorisation ruling."""
    row = claim(claim_id)
    if row is None:
        raise ValueError("Claim not found.")
    if row["status"] != "eligible":
        raise ValueError("Check the policy's eligibility before validating a "
                         "procedure set against it.")
    bundle = build_auth_bundle(claim_id)
    org = db.default_org()
    ack = _api("/fhir/out/v1/coverageeligibility/check", {
        "jwe_headers": {
            "x-hcx-sender_code": org["participant_code"],
            "x-hcx-recipient_code": row["payer_id"] or PAYER_CODE,
            "x-hcx-workflow_id": row["claim_no"],
        },
        "fhir": bundle})

    values = {
        "status": "checking",
        "txn_id": ack.get("txn_id"),
        "correlation_id": ack.get("correlation_id"),
        "requested_at": db.now_iso(),
        "settled_at": None,
        "error_message": None,
        "outcome": None, "disposition": None, "inforce": None,
        "response_json": None,
    }
    existing = auth(claim_id)
    with db.transaction():
        if existing is None:
            db.insert("claim_auth", values | {"claim_id": claim_id})
        else:
            db.execute("DELETE FROM claim_auth_item WHERE auth_id = ?",
                       (existing["id"],))
            db.execute("DELETE FROM claim_auth_requirement WHERE auth_id = ?",
                       (existing["id"],))
            db.update("claim_auth", existing["id"], values)


def parse_auth_bundle(bundle: dict[str, Any],
                      adapter: dict[str, Any]) -> dict[str, Any]:
    """Flatten the payer's auth-requirements answer.

    The reply repeats the request's resources and appends the payer's own
    CoverageEligibilityResponse, so the *last* one is theirs. Each
    ``insurance[].item`` answers one line; its ``authorizationSupporting``
    names what the set has to be accompanied by, and the adapter reads the
    payer's free-text ``text`` field for the kind, the stage and the form url.
    """
    resources = [e.get("resource", {}) for e in bundle.get("entry", [])
                 if isinstance(e, dict)]
    verdicts = [r for r in resources if isinstance(r, dict)
                and r.get("resourceType") == "CoverageEligibilityResponse"]
    if not verdicts:
        raise ValueError("The payer reply carries no "
                         "CoverageEligibilityResponse.")
    verdict = verdicts[-1]
    insurance = (verdict.get("insurance") or [{}])[0]

    items, requirements, seen = [], [], set()
    for entry in insurance.get("item") or []:
        if not isinstance(entry, dict):
            continue
        code, display = _coding(entry.get("productOrService"))
        benefit = (entry.get("benefit") or [{}])[0]
        benefit_type = _coding(benefit.get("type"))[0]
        items.append({
            "code": code or "",
            "display": display,
            "category_code": _coding(entry.get("category"))[0],
            "auth_required": None if entry.get("authorizationRequired") is None
            else (1 if entry["authorizationRequired"] else 0),
            "excluded": None if entry.get("excluded") is None
            else (1 if entry["excluded"] else 0),
            "benefit_type": benefit_type,
            "allowed_amount": (benefit.get("allowedMoney") or {}).get("value"),
        })
        for support in entry.get("authorizationSupporting") or []:
            if not isinstance(support, dict):
                continue
            parsed = payers.supporting_entry(adapter, support)
            key = (parsed["kind"], parsed["code"], parsed["form_url"])
            if key in seen:
                continue
            seen.add(key)
            requirements.append({
                "kind": parsed["kind"], "code": parsed["code"],
                "display": parsed["display"], "form_url": parsed["form_url"],
                "stage": parsed["stage"],
                "for_code": parsed["for_code"] or code,
                "at_preauth": 1 if parsed["at_preauth"] else 0,
            })

    return {
        "values": {
            "outcome": verdict.get("outcome"),
            "disposition": verdict.get("disposition"),
            "inforce": None if insurance.get("inforce") is None
            else (1 if insurance["inforce"] else 0),
        },
        "items": items,
        "requirements": requirements,
    }


def apply_auth(auth_id: int, parsed: dict[str, Any],
               bundle: dict[str, Any]) -> None:
    """Store the payer's ruling on the procedure set."""
    with db.transaction():
        db.update("claim_auth", auth_id, dict(parsed["values"]) | {
            "status": "ready",
            "settled_at": db.now_iso(),
            "error_message": None,
            "response_json": json.dumps(bundle, ensure_ascii=False),
        })
        db.execute("DELETE FROM claim_auth_item WHERE auth_id = ?", (auth_id,))
        db.execute("DELETE FROM claim_auth_requirement WHERE auth_id = ?",
                   (auth_id,))
        for seq, item in enumerate(parsed["items"], 1):
            db.insert("claim_auth_item",
                      dict(item) | {"auth_id": auth_id, "seq": seq})
        for seq, need in enumerate(parsed["requirements"], 1):
            db.insert("claim_auth_requirement",
                      dict(need) | {"auth_id": auth_id, "seq": seq})


def poll_auth(claim_id: int) -> bool:
    """One look at the ledger for the payer's ruling on the procedure set."""
    row = auth(claim_id)
    if row is None or row["status"] != "checking" or not row["txn_id"]:
        return False
    try:
        related = _api("/internal/txn/related", {"txnId": row["txn_id"]})
    except GatewayError as error:
        if error.status == 404:
            db.update("claim_auth", row["id"], {
                "status": "error",
                "error_message": "The hcxkit gateway no longer has this "
                                 "transaction — its ledger was reset after "
                                 "the check was sent. Check again.",
            })
            return True
        raise
    envelope, bundle = _latest_reply(related, "CoverageEligibilityResponse")
    if bundle is not None:
        apply_auth(row["id"],
                   parse_auth_bundle(bundle, payers.for_claim(claim(claim_id))),
                   bundle)
        return True
    error = _protocol_error(row["correlation_id"], row["requested_at"])
    if error:
        db.update("claim_auth", row["id"], {"status": "error",
                                            "error_message": error})
        return True
    dispatch = _api("/internal/txn/dispatch", {"txnId": row["txn_id"]})
    if isinstance(dispatch, dict) and dispatch.get("status") in (
            "dispatch_failed", "dead", "failed"):
        db.update("claim_auth", row["id"], {
            "status": "error",
            "error_message": dispatch.get("errorMessage")
            or dispatch.get("errorCode") or "Dispatch to NHCX failed.",
        })
        return True
    return False


# ---------------------------------------------------------- preauth submission
# The Claim bundle PMJAY answers to, modelled on hcxkit's sample
# (pmay_bundle/preauth_request.json): one Claim referring to a Patient, the
# provider and payer Organizations, a Coverage, a Practitioner per care-team
# member and a Procedure per procedure line — every reference an absolute
# anchor under the preauth path, as the whole PMJAY sample set does.
PREAUTH_BASE = "https://payer.nha.gov.in/preauthorization/v1/preauth/submit/claim"
NDHM_ID_CS = "https://nrces.in/ndhm/fhir/r4/CodeSystem/ndhm-identifier-type-code"
NDHM_SUPPORT_CS = "https://nrces.in/ndhm/fhir/r4/CodeSystem/ndhm-supportinginfo-code"
PROGRAM_CS = "https://nrces.in/ndhm/fhir/r4/CodeSystem/ndhm-program-code"
SNOMED = "http://snomed.info/sct"
BIS_SYSTEM = "https://bis.pmjay.gov.in"
HPR_SYSTEM = "https://hpr.abdm.gov.in"
QR_BASE = "https://payer.gov.in/policy/questionnaireResp"

PREAUTH_STATUS = {
    "submitting": ("Awaiting payer", "info"),
    "approved": ("Approved", "success"),
    "partial": ("Partially approved", "warning"),
    "queried": ("Query raised", "warning"),
    "rejected": ("Rejected", "danger"),
    "cancelling": ("Cancelling", "warning"),
    "cancelled": ("Cancelled", "danger"),
    "error": ("Error", "danger"),
}

# A pre-authorisation can be withdrawn while the payer still has it and after
# it has been granted — not once it has been refused, and not twice.
CANCELLABLE = ("submitting", "approved", "partial", "queried")

# Appendix B of the PMJAY handbook. `other` is the one that changes the
# implementation: with it the free-text note stops being decoration and
# becomes the only place the payer can read the justification.
CANCEL_REASONS = {
    "treatmentplanchanged": "Treatment plan changed during hospitalization",
    "patientrequest": "Patient requested cancellation",
    "financialconstraints": "Financial constraints",
    "alternativetreatment": "Alternative treatment chosen",
    "duplicateclaim": "Duplicate claim / preauth",
    "administrativeerror": "Administrative error",
    "other": "Other reason",
}

FINANCIAL_TASK_CS = "http://terminology.hl7.org/CodeSystem/financialtaskcode"
REASON_CS = "https://nrces.in/ndhm/fhir/r4/CodeSystem/ndhm-reason-code"

# The workflow this Task runs under. PMJAY pins these per exchange, so it is a
# constant of the message rather than the claim it is about.
CANCEL_WORKFLOW_ID = "11"


def _cc(system: str | None, code: str | None, display: str | None = None) -> dict:
    coding: dict[str, Any] = {}
    if system:
        coding["system"] = system
    if code is not None:
        coding["code"] = code
    if display:
        coding["display"] = display
    return {"coding": [coding]}


def _ref(path: str) -> dict:
    return {"reference": f"{PREAUTH_BASE}/{path}" if path else PREAUTH_BASE}


def preauth(claim_id: int):
    return db.one("SELECT * FROM claim_preauth WHERE claim_id = ?", (claim_id,))


def preauth_sent_lines(row) -> dict[str, float]:
    """What the last pre-authorisation actually asked for: code → quantity,
    read back off the bundle as it went, so "has anything been added since"
    is answered from the record and not from memory."""
    if row is None or not row["request_json"]:
        return {}
    try:
        bundle = json.loads(row["request_json"])
    except (TypeError, ValueError):
        return {}
    sent: dict[str, float] = {}
    for entry in bundle.get("entry") or []:
        resource = entry.get("resource") if isinstance(entry, dict) else None
        if not isinstance(resource, dict) or resource.get("resourceType") != "Claim":
            continue
        for item in resource.get("item") or []:
            code = _coding(item.get("productOrService"))[0]
            if code:
                sent[code] = (item.get("quantity") or {}).get("value") or 1
    return sent


def enhancement_lines(claim_id: int) -> list:
    """The lines quoted since the pre-authorisation was decided.

    An enhancement is the hospital coming back for more — a package or an
    implant the approved pre-authorisation did not ask for — so it carries
    only what is new. Empty when nothing has been added, or when no
    pre-authorisation has been decided yet.
    """
    sent = preauth(claim_id)
    if sent is None or sent["status"] not in ("approved", "partial"):
        return []
    already = preauth_sent_lines(sent)
    return [line for line in lines(claim_id) if line["code"] not in already]


def enhancement_pending(claim_id: int) -> bool:
    """Has a line been added since the pre-authorisation was decided?"""
    return bool(enhancement_lines(claim_id))


def build_preauth_bundle(claim_id: int, only_lines=None,
                         prior_ref: str = "") -> dict[str, Any]:
    """Assemble the preauth Claim bundle from the draft and the chosen lines.

    For an enhancement, ``only_lines`` narrows the items to what is new and
    ``prior_ref`` names the pre-authorisation being added to, as
    ``Claim.related`` with relationship ``prior`` — the payer files the
    lines on that case rather than opening another.
    """
    row = claim(claim_id)
    if row is None:
        raise ValueError("Claim not found.")
    org = db.default_org()
    if org is None or not org["identifier_value"] or not org["participant_code"]:
        raise ValueError("Set the facility's HFR ID and NHCX participant code "
                         "under Settings before submitting a preauth.")
    if not row["encounter_id"]:
        raise ValueError("Link the admitted patient before submitting.")
    if not row["admission_date"]:
        raise ValueError("Enter the admission date on the preauth draft.")
    children = preauth_children(claim_id)
    if not children["diagnoses"]:
        raise ValueError("Quote at least one ICD-10 diagnosis.")
    if not children["care_team"]:
        raise ValueError("Add at least one doctor to the care team.")
    line_rows = lines(claim_id) if only_lines is None else list(only_lines)
    if not line_rows:
        raise ValueError("Add at least one line — the procedure being done.")

    adapter = payers.for_claim(row)
    stamp = db.now_iso()
    start = row["admission_date"]
    end = row["expected_discharge_date"] or start
    patient = db.one("SELECT * FROM patient WHERE id = ?", (row["patient_id"],))

    entries: list[dict[str, Any]] = []

    # --- the people and parties -------------------------------------------
    name = (patient["name"] if patient else row["beneficiary_name"]) or ""
    entries.append({"fullUrl": f"{PREAUTH_BASE}/patient", "resource": {
        "resourceType": "Patient", "id": "1",
        "identifier": [
            {"type": _cc(NDHM_ID_CS, "PMJAY",
                         "Pradhan Mantri Jan Aarogya Yojana (PMJAY) ID"),
             "system": BIS_SYSTEM, "value": row["member_id"]},
            {"type": _cc(V2_0203, "JHN", "Jurisdictional health number"),
             "system": BIS_SYSTEM, "value": row["abha_number"] or ""},
        ],
        "name": [{"text": name, "given": [name]}],
        "telecom": [{"system": "phone", "value": row["mobile_number"] or ""}],
        "gender": (patient["gender"] if patient else row["patient_gender"]) or "",
        "birthDate": (patient["birth_date"] if patient
                      else row["patient_dob"]) or "",
    }})
    entries.append({"fullUrl": f"{PREAUTH_BASE}/organization/prov",
                    "resource": {
        "resourceType": "Organization", "id": "1",
        "identifier": [{"type": _cc(V2_0203, "NPI",
                                    "National provider identifier"),
                        "system": FACILITY_SYSTEM,
                        "value": org["identifier_value"]}],
        "active": True,
        "type": [_cc(ORG_TYPE_CS, "prov", "Healthcare Provider")],
        "name": org["name"],
        "contact": [{"telecom": [{"system": "phone",
                                  "value": org["phone"] or ""}]}],
    }})
    entries.append({"fullUrl": f"{PREAUTH_BASE}/organization/pay", "resource": {
        "resourceType": "Organization", "id": "2",
        "identifier": [{"type": _cc(V2_0203, "NIIP",
                                    "National Insurance Payor Identifier (Payor)"),
                        "system": FACILITY_SYSTEM,
                        "value": (row["payer_id"] or PAYER_CODE).split("@")[0]}],
        "active": True,
        "type": [_cc(ORG_TYPE_CS, "pay", "Payer")],
        "name": row["payer_name"] or PAYER_NAME,
    }})
    entries.append({"fullUrl": f"{PREAUTH_BASE}/coverage", "resource": {
        "resourceType": "Coverage", "id": "1",
        "identifier": [{"type": _cc(V2_0203, "NH",
                                    "National Health Plan Identifier"),
                        "system": "https://payer.nha.gov.in",
                        "value": row["policy_code"] or ""}],
        "status": "active",
        "beneficiary": _ref("patient"),
        "period": {"start": db.to_instant(start), "end": db.to_instant(end)},
        "payor": [_ref("organization/pay")],
    }})

    care_team = []
    for index, member in enumerate(children["care_team"], 1):
        doctor = db.one("SELECT * FROM practitioner WHERE id = ?",
                        (member["practitioner_id"],))
        entries.append({"fullUrl": f"{PREAUTH_BASE}/practitioner/{index}",
                        "resource": {
            "resourceType": "Practitioner", "id": str(index),
            "identifier": [{"type": _cc(NDHM_ID_CS, "HPIN",
                                        "Health Practitioner ID issued by NDHM"),
                            "system": HPR_SYSTEM,
                            "value": (doctor["identifier_value"]
                                      if doctor else "") or ""}],
            "active": True,
            "name": [{"text": doctor["name"] if doctor else "",
                      "given": [doctor["name"] if doctor else ""]}],
        }})
        care_team.append({"sequence": index,
                          "provider": _ref(f"practitioner/{index}")})

    # --- one Procedure per procedure line ---------------------------------
    procedures = []
    claim_procedures = []
    for index, line in enumerate(
            [r for r in line_rows if r["kind"] == "Procedure"], 1):
        entries.append({"fullUrl": f"{PREAUTH_BASE}/procedure/{index}",
                        "resource": {
            "resourceType": "Procedure", "id": str(index),
            "identifier": [{"type": _cc(V2_0203, "SNO", "Serial Number"),
                            "system": BIS_SYSTEM, "value": str(index)}],
            "status": "preparation",
            "category": _cc("http://hl7.org/fhir/ValueSet/procedure-category",
                            "Procedure", "Selected treatment or service or "
                                         "proudct is a type of procedure"),
            "code": _cc(adapter["payer_system"], line["code"], line["display"]),
            "subject": _ref("patient"),
        }})
        claim_procedures.append({
            "id": f"Procedure/{index}", "sequence": index,
            "procedureReference": _ref(f"procedure/{index}"),
        })
        procedures.append(index)
    procedure_sequence = procedures or [1]

    # --- the answered questionnaires --------------------------------------
    supporting: list[dict[str, Any]] = []
    sequence = 1
    for document in preauth_documents(claim_id):
        supporting.append({
            "id": f"SupportingInformation/{sequence}", "sequence": sequence,
            "category": _cc(NDHM_SUPPORT_CS, document["category"],
                            "Document Type - Investigation"
                            if document["category"] == "INV" else None),
            "code": _cc(adapter["payer_system"], document["code"],
                        document["label"]),
            "valueAttachment": {"contentType": document["content_type"],
                                "data": document["data"],
                                "title": document["label"]},
        })
        sequence += 1
    for key, value in (("EDT", db.to_instant(start)),
                       ("ADDD", db.to_instant(start))):
        supporting.append({
            "id": f"SupportingInformation/{sequence}", "sequence": sequence,
            "category": _cc(NDHM_SUPPORT_CS,
                            "OTH" if key == "EDT" else "ADMD",
                            "EncounterDateTime" if key == "EDT"
                            else "Admission Date - Discharge Date"),
            "code": _cc(adapter["payer_system"], key,
                        "EncounterDateTime" if key == "EDT"
                        else "Admission Date - Discharge Date"),
            "valueString": value,
        })
        sequence += 1
    for response in build_questionnaire_responses(claim_id):
        entries.append({"fullUrl": response["fullUrl"],
                        "resource": response["resource"]})
        supporting.append({
            "id": f"SupportingInformation/{sequence}", "sequence": sequence,
            "category": _cc(NDHM_SUPPORT_CS, response["category"],
                            response["category_display"]),
            "code": _cc(adapter["payer_system"], response["code"], None),
            "valueReference": {"reference": response["fullUrl"]},
        })
        sequence += 1

    # --- diagnoses and the money ------------------------------------------
    diagnoses = [{
        "sequence": index,
        "diagnosisCodeableConcept": _cc(adapter["payer_system"],
                                        dx["icd10_code"],
                                        dx["icd10_display"]),
        "type": [_cc(SNOMED, "148006", "Preliminary diagnosis")],
    } for index, dx in enumerate(children["diagnoses"], 1)]

    # The same item shape the auth-requirements check sent, plus the links
    # back into this Claim's own arrays.
    items = [item | {
        "careTeamSequence": [m["sequence"] for m in care_team],
        "diagnosisSequence": [d["sequence"] for d in diagnoses],
        "procedureSequence": procedure_sequence,
    } for item in _claim_items(row, line_rows, adapter)]

    total = (lines_total(claim_id) if only_lines is None
             else round(sum((r["amount"] or 0) for r in line_rows), 2))
    related = []
    if prior_ref:
        related = [{
            "id": "related-prior",
            "relationship": _cc("http://terminology.hl7.org/CodeSystem/ex-relatedclaimrelationship",
                                "prior", "Prior Claim"),
            "reference": {"system": adapter["payer_system"], "value": prior_ref},
        }]
    entries.insert(0, {"fullUrl": PREAUTH_BASE, "resource": {
        "resourceType": "Claim", "id": row["claim_no"],
        "meta": {"profile": [f"{NDHM_SD}/Claim"]},
        "identifier": [{"type": _cc(NDHM_ID_CS, "CLN", "Claim number"),
                        "system": "https://hcx.pmjay.gov.in/v1/preauthorization",
                        "value": row["claim_no"]}],
        "status": "active",
        "type": _cc("https://nrces.in/ndhm/fhir/r4/ValueSet/ndhm-claim-type",
                    "737481003", "Inpatient care management (procedure)"),
        "use": "preauthorization",
        "patient": _ref("patient"),
        "created": stamp,
        "insurer": _ref("organization/pay"),
        "provider": _ref("organization/prov"),
        "priority": _cc("http://terminology.hl7.org/CodeSystem/processpriority",
                        "normal", "Normal"),
        "careTeam": care_team,
        "diagnosis": diagnoses,
        "procedure": claim_procedures,
        "supportingInfo": supporting,
        "insurance": [{"sequence": 1, "focal": True,
                       "coverage": _ref("coverage")}],
        "item": items,
        "total": {"value": total},
    } | ({"related": related} if related else {})})

    return {
        "id": "PreAuth",
        "identifier": {"system": adapter["payer_system"],
                       "value": row["claim_no"]},
        "meta": {"lastUpdated": stamp},
        "type": "collection",
        "resourceType": "Bundle",
        "timestamp": stamp,
        "entry": entries,
    }


def build_questionnaire_responses(claim_id: int,
                                  stage: str = "preauth") -> list[dict[str, Any]]:
    """One QuestionnaireResponse per form that has been answered.

    An unanswered form is left out rather than sent empty — a blank response
    is a different claim to the payer than no response.
    """
    given = answers(claim_id)
    out = []
    for form in required_forms(claim_id, stage):
        items = []
        for question in form_questions(form):
            answer = given.get(f'{form["url"]}|{question.get("linkId")}')
            if not answer:
                continue
            items.append({"linkId": question.get("linkId"),
                          "text": question.get("text"),
                          "answer": [_answer_value(question, answer)]})
        if not items:
            continue
        stg = form["kind"] == "stgquestionnaire"
        out.append({
            "fullUrl": f'{QR_BASE}/{claim_id}/{form["form_id"]}',
            "category": "STG" if stg else "INF",
            "category_display": "Standard Treatment Guidelines" if stg
            else "Additional info related to claim",
            "code": "STG" if stg else "ODN",
            "resource": {
                "resourceType": "QuestionnaireResponse",
                "id": form["form_id"],
                "meta": {"profile": [f"{NDHM_SD}/QuestionnaireResponse"]},
                "questionnaire": form["url"],
                "status": "completed",
                "item": items,
            },
        })
    return out


# What an answer looks like on the wire, by the question's declared type. The
# payer's own sample only ever shows `choice` answers, so the rest follow FHIR
# rather than an example: a dateTime question answered with a string would be
# the wrong resource whichever way it is read.
def _answer_value(question: dict[str, Any], answer: str) -> dict[str, Any]:
    kind = (question.get("type") or "").lower()
    if kind == "attachment":
        document = _answer_document(answer)
        if document is None:
            return {"valueString": answer}
        return {"valueAttachment": {
            "contentType": document["content_type"],
            "title": document["label"] or document["filename"],
            "data": base64.b64encode(document["data"]).decode()}}
    if kind in ("datetime", "date", "instant"):
        return {"valueDateTime": db.to_instant(answer) or answer}
    if kind == "time":
        return {"valueTime": answer}
    if kind == "boolean":
        return {"valueBoolean": answer.strip().lower() in ("yes", "true", "1")}
    if kind == "integer":
        try:
            return {"valueInteger": int(float(answer))}
        except ValueError:
            return {"valueString": answer}
    if kind in ("decimal", "quantity"):
        try:
            return {"valueDecimal": float(answer)}
        except ValueError:
            return {"valueString": answer}
    return {"valueString": answer}


def _answer_document(answer: str):
    """An attachment answer stores the id of the document it uploaded."""
    try:
        return document(int(answer))
    except (TypeError, ValueError):
        return None


def answer_document(answer: str):
    """Public read of the document behind an attachment answer, if any."""
    return _answer_document(answer)


def save_answer_file(claim_id: int, form_url: str, link_id: str, label: str,
                     filename: str, content_type: str, data: bytes) -> int:
    """Attach a file as the answer to one question.

    The file joins the claim's documents like any other — a preauth's
    attachments all travel the same way — and the answer records which one, so
    the QuestionnaireResponse can carry it back as a `valueAttachment`.
    """
    document_id = add_document(claim_id, filename, content_type, data,
                               label or filename)
    save_answers(claim_id, form_url, {link_id: str(document_id)})
    return document_id


def preauth_documents(claim_id: int,
                      stage: str = "preauth") -> list[dict[str, Any]]:
    """One leg's attachments, base64 encoded for the wire.

    A document attached for the claim must not ride on the pre-authorisation
    and vice versa — the payer asked for each at its own stage.
    """
    out = []
    for row in db.query("SELECT * FROM claim_document WHERE claim_id = ? "
                        "AND stage = ? ORDER BY id", (claim_id, stage)):
        out.append({
            # `ODN` — "other document" — is the fallback for a file nobody
            # asked for by name, not the default for every attachment.
            "code": row["code"] or "ODN",
            "category": row["category"] or "INV",
            "label": row["label"] or row["filename"],
            "content_type": row["content_type"],
            "data": base64.b64encode(row["data"]).decode(),
        })
    return out


def submit_preauth(claim_id: int) -> None:
    """Send the preauth Claim bundle to the payer."""
    row = claim(claim_id)
    if row is None:
        raise ValueError("Claim not found.")
    if row["status"] != "eligible":
        raise ValueError("Submit a preauth only after the payer has confirmed "
                         "the policy is eligible.")
    org = db.default_org()
    existing = preauth(claim_id)
    added = enhancement_lines(claim_id)
    enhancement = existing is not None and bool(added)
    if enhancement:
        # Only what is new goes, against the pre-authorisation already
        # decided; the payer adds it to that case and decides the addition.
        bundle = build_preauth_bundle(
            claim_id, only_lines=added,
            prior_ref=existing["preauth_ref"] or existing["claim_ref"]
            or row["claim_no"])
        total = round(sum((r["amount"] or 0) for r in added), 2)
    else:
        bundle = build_preauth_bundle(claim_id)
        total = lines_total(claim_id)

    ack = _api("/fhir/out/v1/preauth/submit", {
        "jwe_headers": {
            "x-hcx-sender_code": org["participant_code"],
            "x-hcx-recipient_code": row["payer_id"] or PAYER_CODE,
            "x-hcx-workflow_id": "12", # dont change it
        },
        "fhir": bundle})

    values = {
        "status": "submitting",
        "txn_id": ack.get("txn_id"),
        "correlation_id": ack.get("correlation_id"),
        "submitted_at": db.now_iso(),
        "settled_at": None,
        "error_message": None,
        "claim_ref": row["claim_no"],
        "preauth_ref": None,
        "outcome": None,
        "disposition": None,
        "approved_amount": None,
        "requested_amount": total,
        "request_json": json.dumps(bundle, ensure_ascii=False),
        "response_json": None,
    }
    if enhancement:
        # The reference the payer gave stays: the enhancement is a round on
        # the same pre-authorisation, not a new one. What the earlier rounds
        # asked for is kept on the stored bundle (marked as already
        # authorised) so a further addition is measured against the whole.
        values["preauth_ref"] = existing["preauth_ref"]
        values["enhancement_no"] = (existing["enhancement_no"] or 0) + 1
        merged = json.loads(values["request_json"])
        claim_entry = next(e for e in merged["entry"]
                           if e["resource"]["resourceType"] == "Claim")
        adapter = payers.for_claim(row)
        for code, quantity in preauth_sent_lines(existing).items():
            claim_entry["resource"]["item"].append({
                "sequence": len(claim_entry["resource"]["item"]) + 1,
                "productOrService": _cc(SNOMED, code, None),
                "quantity": {"value": quantity},
                "extension": [{"url": f'{adapter["payer_system"]}/StructureDefinition/already-authorised',
                               "valueBoolean": True}],
            })
        values["request_json"] = json.dumps(merged, ensure_ascii=False)
    if existing is None:
        db.insert("claim_preauth", values | {"claim_id": claim_id})
    else:
        db.update("claim_preauth", existing["id"], values)


def poll_preauth(claim_id: int) -> bool:
    """One look at the ledger for the payer's on_submit reply."""
    row = preauth(claim_id)
    if row is None or row["status"] != "submitting" or not row["txn_id"]:
        return False
    try:
        related = _api("/internal/txn/related", {"txnId": row["txn_id"]})
    except GatewayError as error:
        if error.status == 404:
            db.update("claim_preauth", row["id"], {
                "status": "error",
                "error_message": "The hcxkit gateway no longer has this "
                                 "transaction — its ledger was reset after "
                                 "the preauth was sent. Submit again.",
            })
            return True
        raise
    envelope, bundle = _latest_reply(related, "ClaimResponse")
    if bundle is not None:
        apply_preauth(row["id"], parse_claim_response(bundle), bundle,
                      _api_call_id(envelope))
        return True
    error = (_peer_dispatch_error(related, row["txn_id"])
             or _protocol_error(row["correlation_id"], row["submitted_at"],
                            "preauth"))
    if error:
        db.update("claim_preauth", row["id"], {"status": "error",
                                               "error_message": error})
        return True
    dispatch = _api("/internal/txn/dispatch", {"txnId": row["txn_id"]})
    if isinstance(dispatch, dict) and dispatch.get("status") in (
            "dispatch_failed", "dead", "failed"):
        db.update("claim_preauth", row["id"], {
            "status": "error",
            "error_message": dispatch.get("errorMessage")
            or dispatch.get("errorCode") or "Dispatch to NHCX failed.",
        })
        return True
    return False


# ------------------------------------------------------------ predetermination
# "What would you pay for this?" — the pre-authorisation's own bundle sent
# with `use: predetermination` before anything is committed. The payer prices
# it at once and answers with a ClaimResponse that binds nobody and opens no
# case. It goes out on the preauth route, which is the route the gateway
# knows; `use` on the Claim says what it is, and the answer is read with the
# same parser as a verdict.
PREDETERMINATION_STATUS = {
    "asking": ("Awaiting payer", "info"),
    "answered": ("Quoted", "success"),
    "error": ("Error", "danger"),
}


def predeterminations(claim_id: int) -> list:
    """The quotes asked for on a claim, newest first."""
    return db.query("SELECT * FROM claim_predetermination WHERE claim_id = ? "
                    "ORDER BY id DESC", (claim_id,))


def predetermination(quote_id: int):
    return db.one("SELECT * FROM claim_predetermination WHERE id = ?",
                  (quote_id,))


def build_predetermination_bundle(claim_id: int) -> dict[str, Any]:
    """The pre-authorisation bundle as it would go, with `use` changed."""
    bundle = build_preauth_bundle(claim_id)
    for entry in bundle.get("entry") or []:
        resource = entry.get("resource") if isinstance(entry, dict) else None
        if isinstance(resource, dict) and resource.get("resourceType") == "Claim":
            resource["use"] = "predetermination"
    return bundle


def ask_predetermination(claim_id: int) -> int:
    """Send the dossier as a predetermination and record the ask."""
    row = claim(claim_id)
    if row is None:
        raise ValueError("Claim not found.")
    if row["status"] != "eligible":
        raise ValueError("Ask for a quote only after the payer has confirmed "
                         "the policy is eligible.")
    if any(q["status"] == "asking" for q in predeterminations(claim_id)):
        raise ValueError("A quote is already with the payer; wait for its "
                         "answer before asking again.")
    bundle = build_predetermination_bundle(claim_id)
    org = db.default_org()
    ack = _api("/fhir/out/v1/preauth/submit", {
        "jwe_headers": {
            "x-hcx-sender_code": org["participant_code"],
            "x-hcx-recipient_code": row["payer_id"] or PAYER_CODE,
            "x-hcx-workflow_id": "12", # the preauth route's; dont change it
        },
        "fhir": bundle})
    return db.insert("claim_predetermination", {
        "claim_id": claim_id, "status": "asking",
        "txn_id": ack.get("txn_id"), "correlation_id": ack.get("correlation_id"),
        "requested_at": db.now_iso(),
        "requested_amount": lines_total(claim_id),
        "request_json": json.dumps(bundle, ensure_ascii=False),
    })


def apply_predetermination(quote_id: int, parsed: dict[str, Any],
                           bundle: dict[str, Any]) -> None:
    """Store the payer's quote. It is an answer, not a status: whatever the
    outcome, the ask is answered."""
    db.update("claim_predetermination", quote_id, {
        "status": "answered", "answered_at": db.now_iso(), "error_message": None,
        "outcome": parsed.get("outcome"),
        "adjudication": parsed.get("adjudication"),
        "disposition": parsed.get("disposition"),
        "allowed_amount": parsed.get("approved_amount"),
        "response_json": json.dumps(bundle, ensure_ascii=False),
    })


def poll_predetermination(claim_id: int) -> bool:
    """One look at the ledger for every quote still awaited on a claim."""
    changed = False
    for row in predeterminations(claim_id):
        if row["status"] != "asking" or not row["txn_id"]:
            continue
        try:
            related = _api("/internal/txn/related", {"txnId": row["txn_id"]})
        except GatewayError as error:
            if error.status == 404:
                db.update("claim_predetermination", row["id"], {
                    "status": "error",
                    "error_message": "The hcxkit gateway no longer has this "
                                     "transaction. Ask again."})
                changed = True
                continue
            raise
        envelope, bundle = _latest_reply(related, "ClaimResponse")
        if bundle is not None:
            apply_predetermination(row["id"], parse_claim_response(bundle), bundle)
            changed = True
            continue
        error = (_peer_dispatch_error(related, row["txn_id"])
                 or _protocol_error(row["correlation_id"], row["requested_at"],
                                    "preauth"))
        if error:
            db.update("claim_predetermination", row["id"],
                      {"status": "error", "error_message": error})
            changed = True
    return changed


def _receive_predetermination(envelope: dict[str, Any], correlation_id: str) -> str:
    """The quote half of the preauth callback."""
    row = db.one("SELECT * FROM claim_predetermination WHERE correlation_id = ? "
                 "ORDER BY id DESC", (correlation_id,))
    if row is None:
        return "unmatched"
    if row["status"] != "asking":
        return "ignored"
    body = _callback_body(envelope)
    if body.get("type") == "ProtocolResponse":
        db.update("claim_predetermination", row["id"],
                  {"status": "error", "error_message": _rejection(body)})
        return "settled"
    apply_predetermination(row["id"], parse_claim_response(body), body)
    return "settled"


CLAIM_CATEGORY_CS = "https://nrces.in/ndhm/fhir/r4/CodeSystem/ndhm-supportinginfo-category"


def build_claim_bundle(claim_id: int) -> dict[str, Any]:
    """The Claim bundle sent once the patient has left.

    Structurally the pre-authorisation's bundle with `use: claim`, a
    `billablePeriod` and the discharge on `supportingInfo` — which is where
    PMJAY puts how the stay ended: the summary as an `HDS` attachment coded
    with the mode, the disposition as a `DIS` string, and the admission,
    surgery and discharge dates each under their own category.
    """
    row = claim(claim_id)
    if row is None:
        raise ValueError("Claim not found.")
    discharge = submission(claim_id)
    if discharge is None or not discharge["discharge_mode"]:
        raise ValueError("Record how the patient was discharged before "
                         "claiming.")
    granted = preauth(claim_id)
    if granted is None or granted["status"] not in ("approved", "queried"):
        raise ValueError("A claim goes in against an approved "
                         "pre-authorisation.")

    bundle = build_preauth_bundle(claim_id)
    adapter = payers.for_claim(row)
    stamp = db.now_iso()
    start = row["admission_date"]
    end = discharge["discharge_date"] or start

    resource = bundle["entry"][0]["resource"]
    resource["use"] = "claim"
    resource["billablePeriod"] = {"start": db.to_instant(start),
                                  "end": db.to_instant(end)}

    # The claim quotes what the *claim* may carry, which a LAMA/DAMA discharge
    # can collapse to one procedure.
    quoted = claim_lines(claim_id)
    care_team = resource["careTeam"]
    diagnoses = resource["diagnosis"]
    procedure_sequence = [p["sequence"] for p in resource["procedure"]] or [1]
    resource["item"] = [item | {
        "careTeamSequence": [m["sequence"] for m in care_team],
        "diagnosisSequence": [d["sequence"] for d in diagnoses],
        "procedureSequence": procedure_sequence,
    } for item in _claim_items(row, quoted, adapter)]
    resource["total"] = {"value": claim_total(claim_id)}

    # Rebuild supportingInfo for the claim leg: its own documents, the
    # discharge block, then the answered claim-stage questionnaires.
    bundle["entry"] = [e for e in bundle["entry"]
                       if e["resource"]["resourceType"] != "QuestionnaireResponse"]
    supporting: list[dict[str, Any]] = []
    sequence = 1

    def add(entry: dict[str, Any]) -> None:
        nonlocal sequence
        supporting.append({"id": f"SupportingInformation/{sequence}",
                           "sequence": sequence} | entry)
        sequence += 1

    for document in preauth_documents(claim_id, "claim"):
        # The discharge summary gets its own entry below, coded by discharge
        # type; it must not also ride as a plain attachment.
        if document["code"] == "HDS":
            continue
        add({"category": _cc(NDHM_SUPPORT_CS, document["category"],
                             "Document Type - Investigation"
                             if document["category"] == "INV" else None),
             "code": _cc(adapter["payer_system"], document["code"],
                         document["label"]),
             "valueAttachment": {"contentType": document["content_type"],
                                 "data": document["data"],
                                 "title": document["label"]}})

    mode_code, mode_display, mode_label = DISCHARGE_MODES[
        discharge["discharge_mode"]]
    summary = document_for(claim_id, "HDS")
    add({"category": _cc(NDHM_SUPPORT_CS, "HDS",
                         "Document Type - Hospital Discharge Summary"),
         # The mode rides as the code on the discharge summary, in brackets,
         # exactly as the payer's own sample writes it.
         "code": _cc(adapter["payer_system"], f"({mode_label})",
                     "Hospital Discharge Summary"),
         "valueAttachment": {
             "contentType": summary["content_type"] if summary else "application/pdf",
             "data": base64.b64encode(_document_bytes(summary)).decode()
             if summary else "",
             "title": "Hospital Discharge Summary"}})
    add({"category": _cc(CLAIM_CATEGORY_CS, "DIS",
                         "Discharge status and discharge to location detail"),
         "code": _cc(CLAIM_CATEGORY_CS, mode_code, mode_display),
         "valueString": discharge["discharge_stage"]})
    for category, label, value in (
            ("ADMD", "Admission Date", start),
            ("SURD", "Surgery Date", discharge["surgery_date"]),
            ("DSCHD", "Discharge Date", discharge["discharge_date"]),
            ("ONS", "Date and time of death", discharge["death_date"])):
        if not value:
            continue
        add({"category": _cc(CLAIM_CATEGORY_CS, category, label),
             "code": _cc(CLAIM_CATEGORY_CS,
                         "DTM" if category == "ONS" else "ADDD",
                         "Admission Date - Discharge Date"
                         if category == "ADMD" else label),
             "valueString": db.to_instant(value)})

    for response in build_questionnaire_responses(claim_id, "claim"):
        bundle["entry"].append({"fullUrl": response["fullUrl"],
                                "resource": response["resource"]})
        add({"category": _cc(NDHM_SUPPORT_CS, response["category"],
                             response["category_display"]),
             "code": _cc(adapter["payer_system"], response["code"], None),
             "valueReference": {"reference": response["fullUrl"]}})

    resource["supportingInfo"] = supporting
    bundle["id"] = "CLAIM"
    bundle["timestamp"] = stamp
    bundle["meta"] = {"lastUpdated": stamp}
    return bundle


def _document_bytes(row) -> bytes:
    full = document(row["id"]) if row is not None else None
    return full["data"] if full is not None else b""


def submit_claim(claim_id: int) -> None:
    """Send the claim to the payer."""
    row = claim(claim_id)
    bundle = build_claim_bundle(claim_id)
    org = db.default_org()
    ack = _api("/fhir/out/v1/claim/submit", {
        "jwe_headers": {
            "x-hcx-sender_code": org["participant_code"],
            "x-hcx-recipient_code": row["payer_id"] or PAYER_CODE,
            "x-hcx-workflow_id": CLAIM_WORKFLOW_ID,
        },
        "fhir": bundle})
    db.update("claim_submission", _submission_row(claim_id)["id"], {
        "status": "submitting",
        "txn_id": ack.get("txn_id"),
        "correlation_id": ack.get("correlation_id"),
        "submitted_at": db.now_iso(),
        "settled_at": None,
        "error_message": None,
        "claim_ref": row["claim_no"],
        "outcome": None, "disposition": None, "approved_amount": None,
        "requested_amount": claim_total(claim_id),
        "request_json": json.dumps(bundle, ensure_ascii=False),
        "response_json": None,
    })


def apply_claim(submission_id: int, parsed: dict[str, Any],
                bundle: dict[str, Any], api_call_id: str = "") -> None:
    """Store the payer's verdict on the claim — the same reading as a preauth."""
    status = verdict_status(parsed)
    settled = status != "submitting"
    db.update("claim_submission", submission_id, dict(parsed) | {
        "status": status,
        "api_call_id": api_call_id or None,
        "settled_at": db.now_iso() if settled else None,
        "error_message": None,
        "response_json": json.dumps(bundle, ensure_ascii=False),
    })


def poll_claim(claim_id: int) -> bool:
    """One look at the ledger for the payer's on_submit reply to the claim."""
    row = submission(claim_id)
    if row is None or row["status"] != "submitting" or not row["txn_id"]:
        return False
    try:
        related = _api("/internal/txn/related", {"txnId": row["txn_id"]})
    except GatewayError as error:
        if error.status == 404:
            db.update("claim_submission", row["id"], {
                "status": "error",
                "error_message": "The hcxkit gateway no longer has this "
                                 "transaction — its ledger was reset after "
                                 "the claim was sent. Submit again.",
            })
            return True
        raise
    envelope, body = _latest_reply(related, "ClaimResponse")
    if body is not None:
        parsed = parse_claim_response(body)
        # After a reprocess the submission waits again on the same thread,
        # where the verdict being appealed still is: what was already taken
        # in is not the answer being waited for.
        if _already_applied(row, _api_call_id(envelope), parsed):
            return False
        apply_claim(row["id"], parsed, body, _api_call_id(envelope))
        return True
    error = (_peer_dispatch_error(related, row["txn_id"])
             or _protocol_error(row["correlation_id"], row["submitted_at"], "claim"))
    if error:
        db.update("claim_submission", row["id"], {"status": "error",
                                                  "error_message": error})
        return True
    dispatch = _api("/internal/txn/dispatch", {"txnId": row["txn_id"]})
    if isinstance(dispatch, dict) and dispatch.get("status") in (
            "dispatch_failed", "dead", "failed"):
        db.update("claim_submission", row["id"], {
            "status": "error",
            "error_message": dispatch.get("errorMessage")
            or dispatch.get("errorCode") or "Dispatch to NHCX failed.",
        })
        return True
    return False


def build_cancel_bundle(claim_id: int, reason: str,
                        note: str = "") -> dict[str, Any]:
    """The Task bundle that asks the payer to withdraw a pre-authorisation.

    Modelled on hcxkit's sample (`pmay_bundle/preauth_cancel_request.json`):
    a `Task` coded `cancel` naming the claim, and the two Organizations it is
    between. The handbook additionally wants a `basedOn` reference to the
    entity being cancelled; the sample carries the claim number in
    ``Task.input`` instead and no Claim entry to point at, so that is what
    goes — a reference dangling outside the bundle would be worse than none.
    """
    row = claim(claim_id)
    if row is None:
        raise ValueError("Claim not found.")
    if reason not in CANCEL_REASONS:
        raise ValueError("Choose why the pre-authorisation is being "
                         "cancelled.")
    note = (note or "").strip()
    if reason == "other" and not note:
        raise ValueError("Describe the reason — with “Other reason” the note "
                         "is the only thing the payer can read.")
    org = db.default_org()
    if org is None or not org["identifier_value"] or not org["participant_code"]:
        raise ValueError("Set the facility's HFR ID and NHCX participant code "
                         "under Settings before cancelling.")

    adapter = payers.for_claim(row)
    stamp = db.now_iso()
    task_id = str(uuid.uuid4())
    provider_anchor = PROVIDER_ANCHOR
    payer_anchor = PROVIDER_ANCHOR.replace("/organization/prov",
                                           "/organization/pay")
    claim_ref = row["claim_no"]
    description = note or (f"Please cancel the preauth for claim {claim_ref} — "
                           f"{CANCEL_REASONS[reason].lower()}.")

    task = {
        "resourceType": "Task",
        "id": task_id,
        "meta": {"profile": [f"{NDHM_SD}/Task"]},
        "status": "requested",
        "intent": "order",
        "code": _cc(FINANCIAL_TASK_CS, "cancel", "Cancel"),
        "description": description,
        "authoredOn": stamp,
        "requester": {"reference": provider_anchor, "display": "Organization"},
        "owner": {"reference": payer_anchor, "display": "Organization"},
        "reasonCode": _cc(REASON_CS, reason, CANCEL_REASONS[reason]),
        # The handbook documents `intimationNumber` and notes that the real
        # payload misspells it `initimationNumber`. The misspelling is what
        # the payer reads, so both go.
        "input": [
            _task_input("claimNumber", claim_ref),
            _task_input("initimationNumber", claim_ref),
            _task_input("intimationNumber", claim_ref),
        ],
    }
    return _task_bundle(row, org, adapter, task, task_id, claim_ref, stamp)


def _task_bundle(row, org, adapter, task: dict[str, Any], task_id: str,
                 claim_ref: str, stamp: str) -> dict[str, Any]:
    """A Task and the two Organizations it is between, as one bundle — the
    shape every Task this EMR sends (cancel, status, reprocess) takes."""
    provider_anchor = PROVIDER_ANCHOR
    payer_anchor = PROVIDER_ANCHOR.replace("/organization/prov",
                                           "/organization/pay")
    return {
        "id": "PreAuth",
        "identifier": {"system": adapter["payer_system"], "value": claim_ref},
        "meta": {"lastUpdated": stamp},
        "type": "collection",
        "resourceType": "Bundle",
        "timestamp": stamp,
        "entry": [
            {"fullUrl": f"urn:uuid:{task_id}", "resource": task},
            {"fullUrl": provider_anchor, "resource": {
                "resourceType": "Organization", "id": "1",
                "meta": {"profile": [f"{NDHM_SD}/Organization"]},
                "identifier": [{"type": _cc(V2_0203, "NPI",
                                            "National provider identifier"),
                                "system": FACILITY_SYSTEM,
                                "value": org["identifier_value"]}],
                "active": True,
                "type": [_cc(ORG_TYPE_CS, "prov", "Healthcare Provider")],
                "name": org["name"],
                "contact": [{"telecom": [{"system": "phone",
                                          "value": org["phone"] or ""}]}],
            }},
            {"fullUrl": payer_anchor, "resource": {
                "resourceType": "Organization", "id": "2",
                "meta": {"profile": [f"{NDHM_SD}/Organization"]},
                "identifier": [{"type": _cc(V2_0203, "NIIP",
                                            "National Insurance Payor "
                                            "Identifier (Payor)"),
                                "system": FACILITY_SYSTEM,
                                "value": (row["payer_id"]
                                          or PAYER_CODE).split("@")[0]}],
                "active": True,
                "type": [_cc(ORG_TYPE_CS, "pay", "Payer")],
                "name": row["payer_name"] or PAYER_NAME,
            }},
        ],
    }


def cancel_preauth(claim_id: int, reason: str, note: str = "") -> None:
    """Ask the payer to withdraw the pre-authorisation."""
    sent = preauth(claim_id)
    if sent is None:
        raise ValueError("There is no pre-authorisation to cancel.")
    if sent["status"] not in CANCELLABLE:
        label = PREAUTH_STATUS[sent["status"]][0].lower()
        raise ValueError(f"A pre-authorisation that is {label} cannot be "
                         "cancelled.")
    row = claim(claim_id)
    bundle = build_cancel_bundle(claim_id, reason, note)
    org = db.default_org()

    ack = _api("/fhir/out/v1/task/submit", {
        "jwe_headers": {
            "x-hcx-sender_code": org["participant_code"],
            "x-hcx-recipient_code": row["payer_id"] or PAYER_CODE,
            "x-hcx-workflow_id": CANCEL_WORKFLOW_ID,
        },
        "fhir": bundle})

    db.update("claim_preauth", sent["id"], {
        "status": "cancelling",
        "cancel_txn_id": ack.get("txn_id"),
        "cancel_correlation_id": ack.get("correlation_id"),
        "cancel_requested_at": db.now_iso(),
        "cancel_reason": reason,
        "cancel_note": (note or "").strip() or None,
        "error_message": None,
    })


def parse_task_response(bundle: dict[str, Any]) -> dict[str, Any]:
    """Flatten the payer's answer to a Task.

    A task reply is a Task bundle, not a bare ClaimResponse: the adjudication
    hangs off ``Task.output[].valueReference`` and has to be resolved against
    the bundle's own entries. Everything downstream of that is the same
    ClaimResponse parser the preauth submission uses.
    """
    entries = [e for e in bundle.get("entry", []) if isinstance(e, dict)]
    resources = [e.get("resource", {}) for e in entries]
    task = next((r for r in resources if isinstance(r, dict)
                 and r.get("resourceType") == "Task"), None)
    if task is None:
        raise ValueError("The payer reply carries no Task.")

    parsed: dict[str, Any] = {"task_status": task.get("status")}
    for output in task.get("output") or []:
        reference = (output.get("valueReference") or {}).get("reference")
        if not reference:
            continue
        target = reference.split("urn:uuid:")[-1]
        for entry in entries:
            resource = entry.get("resource") or {}
            if (entry.get("fullUrl") == reference
                    or resource.get("id") == target):
                if resource.get("resourceType") == "ClaimResponse":
                    parsed |= parse_claim_response(
                        {"entry": [{"resource": resource}]})
                break
    return parsed


def apply_cancel(preauth_id: int, parsed: dict[str, Any],
                 bundle: dict[str, Any]) -> None:
    """Settle a cancellation from the payer's Task reply.

    An accepted cancellation **retires the claim number**. The payer holds
    that number against the pre-authorisation it just withdrew, so anything
    sent under it again is a duplicate of a cancelled case; the episode
    carries on under a fresh `CLM-` and the withdrawn number stays on the
    preauth row as `claim_ref`, which is where the audit needs it.
    """
    outcome = (parsed.get("outcome") or "").lower()
    accepted = (parsed.get("task_status") in (None, "completed", "accepted")
                and outcome != "error")
    row = db.one("SELECT * FROM claim_preauth WHERE id = ?", (preauth_id,))
    with db.transaction():
        db.update("claim_preauth", preauth_id, {
            "status": "cancelled" if accepted else "approved",
            "settled_at": db.now_iso(),
            "disposition": parsed.get("disposition"),
            "outcome": parsed.get("outcome"),
            "error_message": None if accepted
            else "The payer did not accept the cancellation.",
            "response_json": json.dumps(bundle, ensure_ascii=False),
        })
        if accepted and row is not None:
            db.update("claim", row["claim_id"],
                      {"claim_no": db.next_number("claim", "CLM-")})


def poll_cancel(claim_id: int) -> bool:
    """One look at the ledger for the payer's answer to the cancel Task."""
    row = preauth(claim_id)
    if row is None or row["status"] != "cancelling" or not row["cancel_txn_id"]:
        return False
    try:
        related = _api("/internal/txn/related", {"txnId": row["cancel_txn_id"]})
    except GatewayError as error:
        if error.status == 404:
            db.update("claim_preauth", row["id"], {
                "status": "error",
                "error_message": "The hcxkit gateway no longer has the cancel "
                                 "transaction — its ledger was reset after it "
                                 "was sent. Cancel again.",
            })
            return True
        raise
    envelope, bundle = _latest_reply(related, "Task")
    if bundle is not None:
        apply_cancel(row["id"], parse_task_response(bundle), bundle)
        return True
    error = _protocol_error(row["cancel_correlation_id"],
                            row["cancel_requested_at"], "task")
    if error:
        db.update("claim_preauth", row["id"], {"status": "error",
                                               "error_message": error})
        return True
    return False


# ------------------------------------------ the small exchanges: enquiries
ENQUIRY_KINDS = {
    "status": "Status enquiry",
    "reprocess": "Reprocess request",
}
STATUS_WORKFLOW_ID = "13"
# Where a status Task goes: the task route (see ask_status for why not v1/status).
STATUS_PATH = "/fhir/out/v1/task/submit"


def enquiries(claim_id: int, kind: str = "", stage: str = "") -> list:
    """The asks made on a claim, newest first."""
    sql = "SELECT * FROM claim_enquiry WHERE claim_id = ?"
    params: list[Any] = [claim_id]
    if kind:
        sql += " AND kind = ?"
        params.append(kind)
    if stage:
        sql += " AND stage = ?"
        params.append(stage)
    return db.query(sql + " ORDER BY id DESC", params)


def enquiry(enquiry_id: int):
    return db.one("SELECT * FROM claim_enquiry WHERE id = ?", (enquiry_id,))


def _task_for(claim_id: int, code: str, display: str, claim_ref: str,
              description: str = "") -> tuple[dict[str, Any], Any, Any]:
    """A Task coded `code` naming the claim, in its bundle."""
    row = claim(claim_id)
    if row is None:
        raise ValueError("Claim not found.")
    org = db.default_org()
    if org is None or not org["identifier_value"] or not org["participant_code"]:
        raise ValueError("Set the facility's HFR ID and NHCX participant code "
                         "under Settings first.")
    adapter = payers.for_claim(row)
    stamp = db.now_iso()
    task_id = str(uuid.uuid4())
    task = {
        "resourceType": "Task",
        "id": task_id,
        "meta": {"profile": [f"{NDHM_SD}/Task"]},
        "status": "requested",
        "intent": "order",
        "code": _cc(FINANCIAL_TASK_CS, code, display),
        "authoredOn": stamp,
        "requester": {"reference": PROVIDER_ANCHOR, "display": "Organization"},
        "owner": {"reference": PROVIDER_ANCHOR.replace("/organization/prov",
                                                       "/organization/pay"),
                  "display": "Organization"},
        "input": [_task_input("claimNumber", claim_ref)],
    }
    if description:
        task["description"] = description
    return _task_bundle(row, org, adapter, task, task_id, claim_ref, stamp), row, org


def _leg_reference(claim_id: int, stage: str) -> tuple[str, str]:
    """The claim number the payer knows a leg by, and the leg's thread."""
    if stage == "claim":
        state = submission(claim_id)
        if state is None or state["status"] == "draft":
            raise ValueError("The claim has not been submitted yet.")
        return state["claim_ref"] or claim(claim_id)["claim_no"], state["correlation_id"] or ""
    sent = preauth(claim_id)
    if sent is None:
        raise ValueError("No pre-authorisation has been sent yet.")
    return sent["claim_ref"] or claim(claim_id)["claim_no"], sent["correlation_id"] or ""


def _send_enquiry(claim_id: int, kind: str, stage: str, path: str,
                  bundle: dict[str, Any], row, org, workflow_id: str,
                  reason: str = "") -> int:
    ack = _api(path, {
        "jwe_headers": {
            "x-hcx-sender_code": org["participant_code"],
            "x-hcx-recipient_code": row["payer_id"] or PAYER_CODE,
            "x-hcx-workflow_id": workflow_id,
        },
        "fhir": bundle})
    return db.insert("claim_enquiry", {
        "claim_id": claim_id, "kind": kind, "stage": stage or None,
        "txn_id": ack.get("txn_id"), "correlation_id": ack.get("correlation_id"),
        "requested_at": db.now_iso(), "status": "asking",
        "reason": reason or None,
        "request_json": json.dumps(bundle, ensure_ascii=False),
    })


def ask_status(claim_id: int, stage: str = "preauth") -> int:
    """Ask the payer where a leg stands — a Task coded `status` naming the
    claim, answered with the entity status in the protocol header and a
    Task carrying `claimStatus`.

    It goes on `task/submit`, like the cancel and reprocess Tasks, because
    the NHCX sandbox refuses `v1/status` outright (NHCX-1012, "no records
    found with the requested api caller id") whatever correlation id the
    call carries — a fresh one, the original request's, or its api_call_id —
    so a status ask on the gateway's own status route never reaches the
    payer. `STATUS_PATH` is the one place to move it back.
    """
    if stage not in ("preauth", "claim"):
        raise ValueError("Ask about the pre-authorisation or the claim.")
    claim_ref, thread = _leg_reference(claim_id, stage)
    bundle, row, org = _task_for(claim_id, "status", "Status", claim_ref)
    return _send_enquiry(claim_id, "status", stage, STATUS_PATH,
                         bundle, row, org, thread or STATUS_WORKFLOW_ID)


def ask_reprocess(claim_id: int, reason: str) -> int:
    """Appeal a decided claim — a Task coded `reprocess` on `task/submit`.
    The payer reopens the claim for a person; its new verdict comes back on
    the claim's own thread, so the submission goes back to waiting."""
    reason = (reason or "").strip()
    if not reason:
        raise ValueError("Say why the claim should be looked at again.")
    state = submission(claim_id)
    if state is None or state["status"] not in ("rejected", "partial", "approved"):
        raise ValueError("Only a claim the payer has decided can be sent "
                         "back for reprocessing.")
    if paid_total(claim_id):
        raise ValueError("A claim that has been paid cannot be reprocessed.")
    claim_ref, thread = _leg_reference(claim_id, "claim")
    bundle, row, org = _task_for(claim_id, "reprocess", "Reprocess", claim_ref,
                                 reason)
    return _send_enquiry(claim_id, "reprocess", "claim",
                         "/fhir/out/v1/task/submit", bundle, row, org,
                         thread or CLAIM_WORKFLOW_ID, reason)


def _status_answer(envelope: dict[str, Any], bundle: dict[str, Any]) -> dict[str, Any]:
    """What a status reply says, from the protocol header first and the
    Task's `claimStatus` output second."""
    headers = envelope.get("jwe_headers") if isinstance(envelope, dict) else None
    headers = headers if isinstance(headers, dict) else {}
    response = headers.get("x-hcx-status_response")
    if isinstance(response, str):
        try:
            response = json.loads(response)
        except ValueError:
            response = {}
    response = response if isinstance(response, dict) else {}
    answer = response.get("entity_status") or ""
    detail = ""
    task = next((e.get("resource") for e in bundle.get("entry") or []
                 if isinstance(e, dict)
                 and (e.get("resource") or {}).get("resourceType") == "Task"), None)
    if task:
        for output in task.get("output") or []:
            if _coding(output.get("type"))[0] == "claimStatus" and output.get("valueString"):
                answer = answer or output["valueString"]
        if task.get("status") == "rejected":
            answer = answer or "not-found"
        detail = task.get("description") or ""
    if response:
        parts = [f'{k}: {response[k]}' for k in ("stage", "outcome", "total_approved", "total_paid")
                 if response.get(k) not in (None, "")]
        detail = "; ".join(p for p in [detail, "; ".join(parts)] if p)
    return {"answer": answer or "unknown", "detail": detail}


def apply_enquiry(enquiry_id: int, envelope: dict[str, Any],
                  bundle: dict[str, Any]) -> None:
    row = enquiry(enquiry_id)
    if row is None:
        return
    values: dict[str, Any] = {
        "status": "answered", "answered_at": db.now_iso(), "error_message": None,
        "response_json": json.dumps(bundle, ensure_ascii=False),
    }
    if row["kind"] == "status":
        values |= _status_answer(envelope, bundle)
    else:  # reprocess
        parsed = parse_task_response(bundle)
        accepted = parsed.get("task_status") in ("completed", "accepted", "in-progress")
        values |= {"answer": "reopened" if accepted else "refused",
                   "detail": parsed.get("disposition") or _task_description(bundle)}
        if accepted:
            state = submission(row["claim_id"])
            if state is not None:
                # The verdict is coming again, on the claim's own thread.
                db.update("claim_submission", state["id"], {
                    "status": "submitting", "settled_at": None,
                    "error_message": None})
    db.update("claim_enquiry", enquiry_id, values)


def _task_description(bundle: dict[str, Any]) -> str:
    for entry in bundle.get("entry") or []:
        resource = entry.get("resource") if isinstance(entry, dict) else None
        if isinstance(resource, dict) and resource.get("resourceType") == "Task":
            return resource.get("description") or ""
    return ""


def _receive_enquiry(envelope: dict[str, Any], correlation_id: str) -> str:
    row = db.one("SELECT * FROM claim_enquiry WHERE correlation_id = ? "
                 "ORDER BY id DESC", (correlation_id,))
    if row is None:
        return "unmatched"
    if row["status"] != "asking":
        return "ignored"
    body = _callback_body(envelope)
    if body.get("type") == "ProtocolResponse":
        db.update("claim_enquiry", row["id"], {
            "status": "error", "error_message": _rejection(body)})
        return "settled"
    apply_enquiry(row["id"], envelope, body)
    return "settled"


def poll_enquiries(claim_id: int) -> bool:
    """One look at the ledger for every ask still open on a claim."""
    changed = False
    for row in enquiries(claim_id):
        if row["status"] != "asking" or not row["txn_id"]:
            continue
        try:
            related = _api("/internal/txn/related", {"txnId": row["txn_id"]})
        except GatewayError as error:
            if error.status == 404:
                db.update("claim_enquiry", row["id"], {
                    "status": "error",
                    "error_message": "The hcxkit gateway no longer has this "
                                     "transaction. Ask again."})
                changed = True
                continue
            raise
        envelope, bundle = _latest_reply(related, "Task")
        if bundle is not None:
            apply_enquiry(row["id"], envelope, bundle)
            changed = True
            continue
        kind = "status" if (row["kind"] == "status"
                            and STATUS_PATH.endswith("/status")) else "task"
        error = (_peer_dispatch_error(related, row["txn_id"])
                 or _protocol_error(row["correlation_id"], row["requested_at"], kind)
                 or _own_dispatch_error(row["txn_id"]))
        if error:
            db.update("claim_enquiry", row["id"],
                      {"status": "error", "error_message": error})
            changed = True
    return changed


def _own_dispatch_error(txn_id: str) -> str | None:
    """What became of our own transaction at the gateway: a dispatch NHCX
    refused will never be answered, and saying so beats waiting for it."""
    try:
        dispatch = _api("/internal/txn/dispatch", {"txnId": txn_id})
    except ValueError:
        return None
    if not isinstance(dispatch, dict) or dispatch.get("status") not in (
            "dispatch_failed", "dead", "failed", "errored"):
        return None
    response = (dispatch.get("dispatch") or {}).get("response") or {}
    nhcx = response.get("error") if isinstance(response, dict) else None
    if isinstance(nhcx, dict) and nhcx.get("code"):
        return f'{nhcx["code"]}: {nhcx.get("message") or "NHCX refused the request."}'
    return (dispatch.get("errorMessage") or dispatch.get("errorCode")
            or "Dispatch to NHCX failed.")


def parse_claim_response(bundle: dict[str, Any]) -> dict[str, Any]:
    """Flatten the payer's ClaimResponse.

    **`outcome` alone never tells you the decision.** `complete` is returned
    for both a full approval and an outright rejection; what separates them is
    ``adjudication[].reason``. So both are read, and the pair is what
    :func:`verdict_status` decides on.
    """
    resources = [e.get("resource", {}) for e in bundle.get("entry", [])
                 if isinstance(e, dict)]
    verdict = next((r for r in resources
                    if isinstance(r, dict)
                    and r.get("resourceType") == "ClaimResponse"), None)
    if verdict is None:
        raise ValueError("The payer reply carries no ClaimResponse.")

    # The claim-level adjudication, preferring the entry categorised `status`.
    reason = None
    for entry in verdict.get("adjudication") or []:
        if not isinstance(entry, dict):
            continue
        code = _coding(entry.get("reason"))[0]
        if not code:
            continue
        if _coding(entry.get("category"))[0] == "status":
            reason = code
            break
        if reason is None:
            reason = code

    # Totals are a repeating list of categories, not a fixed order — the
    # handbook prints them all against total[1], which is a documentation
    # artefact. Never index positionally.
    totals: dict[str, Any] = {}
    for total in verdict.get("total") or []:
        code = _coding(total.get("category"))[0]
        if code:
            totals[code.lower()] = (total.get("amount") or {}).get("value")

    return {
        "outcome": verdict.get("outcome"),
        "adjudication": (reason or "").lower() or None,
        "disposition": verdict.get("disposition"),
        "preauth_ref": verdict.get("preAuthRef"),
        "approved_amount": totals.get("benefit"),
        "query_note": _query_trail(verdict),
    }


def _query_trail(verdict: dict[str, Any]) -> str | None:
    """What the payer wrote about the adjudication, item by item.

    PMJAY puts its query audit trail in a `reason`-categorised item
    adjudication as a pipe-delimited `USER~datetime~type~comment~trust`
    string rather than a code. It is kept verbatim — there is nothing to look
    up — alongside any processNote.
    """
    notes = []
    for item in verdict.get("item") or []:
        if not isinstance(item, dict):
            continue
        for entry in item.get("adjudication") or []:
            if not isinstance(entry, dict):
                continue
            if _coding(entry.get("category"))[0] != "reason":
                continue
            text = _coding(entry.get("reason"))[1] or ""
            # The payer prefixes these with a bare " : " when it has no code
            # to put in front of the sentence.
            text = text.strip().lstrip(":").strip()
            if text.strip("."):
                notes.append(text)
    for note in verdict.get("processNote") or []:
        if isinstance(note, dict) and note.get("text"):
            notes.append(note["text"])
    unique = list(dict.fromkeys(notes))
    return "\n".join(unique) or None


# outcome + adjudication reason → what actually happened. Straight from the
# handbook's §8.5 / §9.5.1 tables; the live sandbox adds one more shape, an
# `outcome: queued` acknowledgement that is not a verdict at all.
def verdict_status(parsed: dict[str, Any]) -> str:
    """Read the pair the handbook insists on reading together."""
    outcome = (parsed.get("outcome") or "").lower()
    reason = (parsed.get("adjudication") or "").lower()

    if outcome in ("queued", "acknowledged") or reason in ("submitted",
                                                           "acknowledged"):
        # "Request acknowledged and accepted for further processing" — the
        # payer has it, and will answer again. Not a decision.
        return "submitting"
    if reason == "cancelled":
        return "rejected"
    if reason == "queried":
        return "queried"
    if outcome == "error":
        return "rejected"
    if outcome == "partial":
        return "partial" if reason == "approved" else "queried"
    if outcome == "complete":
        return "approved" if reason in ("approved", "", None) else "queried"
    return "queried"


def apply_preauth(preauth_id: int, parsed: dict[str, Any],
                  bundle: dict[str, Any], api_call_id: str = "") -> None:
    """Store the payer's preauth verdict and settle its status.

    A pre-authorisation is answered more than once: an acknowledgement first,
    then the decision, and a query in between if the payer wants more. Each
    reply lands here, and one that is not yet a decision leaves the row
    waiting rather than settling it.
    """
    status = verdict_status(parsed)
    settled = status != "submitting"
    db.update("claim_preauth", preauth_id, dict(parsed) | {
        "status": status,
        "api_call_id": api_call_id or None,
        "settled_at": db.now_iso() if settled else None,
        "error_message": None,
        "response_json": json.dumps(bundle, ensure_ascii=False),
    })


# ------------------------------------------------------- supporting documents
DOCUMENT_TYPES = {
    "application/pdf": "PDF",
    "image/jpeg": "JPEG image",
    "image/png": "PNG image",
    "image/webp": "WebP image",
}
MAX_DOCUMENT_BYTES = 10 * 1024 * 1024


def add_document(claim_id: int, filename: str, content_type: str,
                 data: bytes, label: str = "", code: str = "",
                 category: str = "", stage: str = "preauth") -> int:
    """Attach one uploaded PDF or image to the claim.

    ``code`` is the payer's own requirement code when the file was uploaded
    against one — `MAND0671` and the like — so the preauth can quote that back
    instead of filing everything as "other document".
    """
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
        "code": (code or "").strip() or None,
        "category": (category or "").strip() or None,
        "stage": stage or "preauth",
        "size": len(data),
        "data": data,
        "uploaded_at": db.now_iso(),
    })


def attach_required_document(claim_id: int, code: str, filename: str,
                             content_type: str, data: bytes,
                             stage: str = "preauth") -> int:
    """Attach a file against one of the payer's named document requirements.

    The same path a questionnaire's file answer takes: the file joins the
    claim's documents, carrying the code and category the payer asked under,
    so nothing has to be matched up by label later.
    """
    need = next((n for n in required_documents(claim_id, stage)
                 if n["code"] == code), None)
    if need is None:
        where = "pre-authorisation" if stage == "preauth" else "claim"
        raise ValueError(f"That is not a document this {where} was asked "
                         "for.")
    with db.transaction():
        # One file per requirement: choosing another replaces it, rather than
        # sending the payer two files against one code and letting it guess.
        document_id = add_document(claim_id, filename, content_type, data,
                                   need["display"] or code, code=code,
                                   category="INV", stage=stage)
        db.execute("DELETE FROM claim_document WHERE claim_id = ? "
                   "AND code = ? AND id <> ?", (claim_id, code, document_id))
    return document_id


def attach_discharge_summary(claim_id: int, filename: str, content_type: str,
                             data: bytes) -> int:
    """The discharge summary — always wanted, and coded by discharge type.

    It is not one of the payer's `MAND…` requirements; PMJAY carries it under
    its own `HDS` category with the discharge mode as the code, so it is
    filed under `HDS` and the mode is read off the discharge at send time.
    """
    with db.transaction():
        document_id = add_document(claim_id, filename, content_type, data,
                                   "Hospital discharge summary", code="HDS",
                                   category="HDS", stage="claim")
        db.execute("DELETE FROM claim_document WHERE claim_id = ? "
                   "AND code = 'HDS' AND id <> ?", (claim_id, document_id))
    return document_id


def document_for(claim_id: int, code: str):
    """The file already attached against one requirement, if there is one."""
    return db.one("SELECT id, claim_id, filename, content_type, label, code, "
                  "stage, size, uploaded_at FROM claim_document "
                  "WHERE claim_id = ? AND code = ? ORDER BY id DESC",
                  (claim_id, code))


def documents(claim_id: int) -> list:
    """The claim's attachments, without their payloads."""
    return db.query(
        "SELECT id, claim_id, filename, content_type, label, code, category, "
        "stage, size, uploaded_at FROM claim_document WHERE claim_id = ? "
        "ORDER BY id", (claim_id,))


def document(doc_id: int):
    return db.one("SELECT * FROM claim_document WHERE id = ?", (doc_id,))


def delete_document(doc_id: int) -> None:
    db.execute("DELETE FROM claim_document WHERE id = ?", (doc_id,))
