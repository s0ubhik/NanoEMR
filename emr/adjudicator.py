"""PMJAY adjudicator — driving the payer side of a case this EMR raised.

NHCX carries the message; the *decision* on a PMJAY case is taken in the NHCX
Payer Service, which sits outside the exchange. Without it a preauth or a claim
sits at `request.initiated` forever, waiting on somebody at the other end. The
paired hcxkit exposes that payer service behind three internal endpoints, and
this module drives them for the cases this EMR itself raised — which is the
part hcxkit's own console cannot do, because it does not know which
correlation id belongs to which episode.

Everything goes through hcxkit; nothing here talks to the payer service
directly:

  GET  /internal/adjudicator/workflow — the published steps, roles and actions
  POST /internal/adjudicator/role     — which role is holding a case right now
  POST /internal/adjudicator/process  — take one of that role's actions

Two rules the payer service enforces and this module respects rather than
works around:

- **A case sits at exactly one step**, and only the role holding it may act.
  So the role is read first and the actions offered are that role's, not a
  menu of everything.
- **The correlation id is an input, never invented.** It identifies the
  exchange the decision answers. This EMR stores it per leg — which is the
  whole reason a case can be adjudicated from here at all.
"""

from __future__ import annotations

import json
from typing import Any

from . import claims, db

# The stage of an episode a decision is taken against. `claim_preauth` and
# `claim_submission` each hold their own correlation id, so a case can be
# adjudicated at either.
STAGES = {
    "preauth": ("Pre-authorisation", "claim_preauth"),
    "claim": ("Claim", "claim_submission"),
}


def workflow() -> dict[str, Any]:
    """The published steps, straight from hcxkit so the two cannot drift."""
    reply = claims.gateway("/internal/adjudicator/workflow", method="GET")
    return reply if isinstance(reply, dict) else {}


def enabled() -> bool:
    return bool(workflow().get("enabled"))


def cases() -> list[dict[str, Any]]:
    """Every leg this EMR has sent that a payer could be deciding on.

    One row per *leg*, not per claim: a claim raises a pre-authorisation and
    then a claim, each its own exchange with its own correlation id, and each
    adjudicated separately.
    """
    rows: list[dict[str, Any]] = []
    for stage, (label, table) in STAGES.items():
        for leg in db.query(
                f"SELECT l.*, c.claim_no, c.beneficiary_name, c.member_id, "
                f"c.payer_id, c.abha_number FROM {table} l "
                "JOIN claim c ON c.id = l.claim_id "
                "WHERE l.correlation_id IS NOT NULL AND l.correlation_id <> '' "
                "ORDER BY l.id DESC"):
            rows.append({
                "claim_id": leg["claim_id"],
                "stage": stage,
                "stage_label": label,
                # The case number the payer knows it by is the claim number
                # that leg went out under, which is not always the number the
                # episode carries now.
                "case_number": leg["claim_ref"] or leg["claim_no"],
                "claim_no": leg["claim_no"],
                "beneficiary_name": leg["beneficiary_name"],
                "member_id": leg["member_id"],
                "payer_id": leg["payer_id"],
                "status": leg["status"],
                "correlation_id": leg["correlation_id"],
                "sent_at": leg["submitted_at"],
                "amount": leg["requested_amount"],
            })
    rows.sort(key=lambda r: (r["sent_at"] or "", r["case_number"] or ""),
              reverse=True)
    return rows


def case(claim_id: int, stage: str) -> dict[str, Any] | None:
    """One leg, by the claim it belongs to and which exchange it is."""
    for row in cases():
        if row["claim_id"] == claim_id and row["stage"] == stage:
            return row
    return None


def role_for(case_number: str) -> dict[str, Any]:
    """Which role is holding a case, and the step that resolves to.

    The reply carries hcxkit's own reading (`role`, `step`) alongside the raw
    payer-service response, plus a `warning` when the role is one the
    published workflow does not contain — never a silent guess.
    """
    case_number = (case_number or "").strip()
    if not case_number:
        raise ValueError("A case number is needed to read who is holding it.")
    reply = claims.gateway("/internal/adjudicator/role",
                           {"caseNumber": case_number})
    return reply if isinstance(reply, dict) else {}


def actions_for(role: str) -> list[str]:
    """What the named role may do — spelled as the payer service spells it."""
    for step in workflow().get("steps") or []:
        if isinstance(step, dict) and _same(step.get("role"), role):
            return list(step.get("actions") or [])
    return []


def step_for(role: str) -> dict[str, Any] | None:
    for step in workflow().get("steps") or []:
        if isinstance(step, dict) and _same(step.get("role"), role):
            return step
    return None


def _same(left: Any, right: Any) -> bool:
    return str(left or "").strip().lower() == str(right or "").strip().lower()


def process(claim_id: int, stage: str, role: str, action: str,
            remarks: str = "") -> dict[str, Any]:
    """Take one action on a case, on behalf of the role holding it.

    The correlation id is not asked for: it is read off the leg being decided,
    which is the one thing this EMR knows that the payer console does not.
    """
    leg = case(claim_id, stage)
    if leg is None:
        raise ValueError("That case was never sent from here.")
    if not leg["correlation_id"]:
        raise ValueError("That leg has no correlation id — it never reached "
                         "the gateway, so there is nothing to decide on.")
    role = (role or "").strip()
    action = (action or "").strip()
    if not role:
        raise ValueError("Read who is holding the case before acting on it.")
    allowed = actions_for(role)
    if not allowed:
        raise ValueError(f"{role} is not a role in the published workflow.")
    if not any(_same(action, candidate) for candidate in allowed):
        raise ValueError(f'{role} may take {", ".join(allowed)} — not '
                         f'"{action}".')

    org = db.default_org()
    reply = claims.gateway("/internal/adjudicator/process", {
        "caseNumber": leg["case_number"],
        "action": action,
        "role": role,
        "correlationId": leg["correlation_id"],
        "providerCode": (org["participant_code"] if org else "") or "",
        "memberId": leg["member_id"] or "",
        "remarks": (remarks or "").strip(),
    })
    reply = reply if isinstance(reply, dict) else {}
    # NHCX carries no record of a decision taken outside the exchange, so this
    # is the only trace of it — kept whether the payer service took it or not.
    db.insert("claim_adjudication", {
        "claim_id": claim_id,
        "stage": stage,
        "case_number": leg["case_number"],
        "role": reply.get("role") or role,
        "action": reply.get("action") or action,
        "usecase": reply.get("usecase"),
        "correlation_id": leg["correlation_id"],
        "remarks": (remarks or "").strip() or None,
        "success": 1 if reply.get("success") else 0,
        "http_status": reply.get("status"),
        "response_json": json.dumps(reply, ensure_ascii=False),
        "taken_at": db.now_iso(),
    })
    return reply


def decisions(claim_id: int, stage: str = "") -> list:
    """What has been decided from here, newest first."""
    if stage:
        return db.query("SELECT * FROM claim_adjudication WHERE claim_id = ? "
                        "AND stage = ? ORDER BY id DESC", (claim_id, stage))
    return db.query("SELECT * FROM claim_adjudication WHERE claim_id = ? "
                    "ORDER BY id DESC", (claim_id,))
