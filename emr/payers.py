"""Payer adapters — the per-scheme customisation the NHCX protocol leaves open.

NHCX standardises the envelope, not the contents. What a payer actually puts
in a bundle is scheme-specific: PMJAY quotes package codes under its own
`payer.pmjay.nha.gov.in` system, stamps every claim line `AB-PMJAY`, answers
`auth-requirements` with a document list whose *stage* is buried in a free-text
`Type: pre` string, and ships its questionnaires by `fullUrl:` in another one.
None of that is in the spec; all of it is in the samples.

So the scheme-specific parts live here, behind one lookup, and the exchange
code in :mod:`emr.claims` asks the adapter rather than hard-coding PMJAY.

Which adapter a claim uses is **configuration, not code**: the mapping from
NHCX participant code to adapter key is the ``payer_adapter`` terminology kind,
editable under Masters → Clinical codes, one row per payer:

    code    1518@hcx          the payer's NHCX participant code
    display PMJAY / NHA       what an operator calls them
    extra   pmjay             the adapter key below

A second row, `1000004805@hcx → kyrocare`, maps the repository's own IRDAI
payer portal, which speaks the PMJAY dialect and answers the same checks.

An unmapped payer falls back to :data:`GENERIC`, which sends a plain NRCES
bundle with none of PMJAY's extras — the right default for a payer nobody has
characterised yet, and a visible one: the claim screen names the adapter in
use.
"""

from __future__ import annotations

import re
from typing import Any

from . import db

# --------------------------------------------------------------- the adapters
# `key` is what a `payer_adapter` row stores in `extra`.
PMJAY: dict[str, Any] = {
    "key": "pmjay",
    "name": "PMJAY / Ayushman Bharat",
    # The identifier system PMJAY puts on its own codes — package codes,
    # diagnosis codes, bundle identifiers.
    "payer_system": "https://payer.pmjay.nha.gov.in",
    # Every claim line is stamped with the scheme it is claimed under.
    "program_code": "AB-PMJAY",
    "program_display": ("Ayushman Bharat Pradhan Mantri Jan Arogya Yojana "
                        "(AB-PMJAY)"),
    # PMJAY answers a purpose=auth-requirements check with the documents and
    # questionnaires a procedure set needs. Payers that do not are checked
    # for eligibility only.
    "auth_requirements": True,
    # `authorizationSupporting[].text` reads "Type: pre\\n Procedure Code:X"
    # for a document and "fullUrl: https://…" for a questionnaire. Only the
    # stages listed here are wanted at pre-authorisation; the rest are
    # collected later, when the claim itself goes in.
    "preauth_stages": ("pre",),
    "package_master": True,
}

# The Dummy IRDAI Payer portal (`apps/irdai-payer`), the other half of this
# repository. It publishes a package master the PMJAY way — a `Procedure` cost
# per package, conditions and document requirements on the benefit, a
# treatment-guideline questionnaire beside it — and answers an
# `auth-requirements` check per item with the documents due at each stage,
# spelled the way PMJAY spells them (`Type: pre`, `fullUrl:`), so the same
# reader serves both. It has no scheme programme code: an indemnity policy is
# not a government scheme, and stamping one on the claim would be a lie.
KYROCARE: dict[str, Any] = {
    "key": "kyrocare",
    "name": "Dummy IRDAI Payer",
    # The namespace the portal's own identifiers and code systems live under
    # (its `fhir_base_url`); its document taxonomy hangs off it.
    "payer_system": "https://kyro.care/fhir",
    "program_code": None,
    "program_display": None,
    "auth_requirements": True,
    "preauth_stages": ("pre",),
    "package_master": True,
}

GENERIC: dict[str, Any] = {
    "key": "generic",
    "name": "Generic NHCX payer",
    "payer_system": "https://nhcx.abdm.gov.in",
    "program_code": None,
    "program_display": None,
    "auth_requirements": False,
    "preauth_stages": ("pre",),
    "package_master": True,
}

ADAPTERS = {a["key"]: a for a in (PMJAY, KYROCARE, GENERIC)}

# The kind of the terminology rows that map participant codes onto adapters.
ADAPTER_KIND = "payer_adapter"


def _normalise(code: str | None) -> str:
    """NHCX participant codes are written with and without the `@hcx` suffix."""
    code = (code or "").strip()
    return code.split("@")[0].lower()


def configured() -> list[dict[str, Any]]:
    """The payer → adapter mapping as an operator has it configured."""
    rows = []
    for term in db.terms(ADAPTER_KIND):
        key = (term["extra"] or "").strip() or GENERIC["key"]
        rows.append({"participant_code": term["code"],
                     "name": term["display"],
                     "adapter": ADAPTERS.get(key, GENERIC)})
    return rows


def adapter_for(participant_code: str | None) -> dict[str, Any]:
    """The adapter configured for one payer participant code.

    Matched on the numeric part, so `1518`, `1518@hcx` and `1518@HCX` all
    reach the same row. Nothing configured means :data:`GENERIC` — a payer
    whose quirks nobody has written down yet gets a plain bundle rather than
    another scheme's.
    """
    wanted = _normalise(participant_code)
    if not wanted:
        return GENERIC
    for row in configured():
        if _normalise(row["participant_code"]) == wanted:
            return row["adapter"]
    return GENERIC


def for_claim(row) -> dict[str, Any]:
    """The adapter for a claim, taken from the payer it is addressed to."""
    try:
        payer = row["payer_id"]
    except (KeyError, IndexError, TypeError):
        payer = None
    return adapter_for(payer)


# ------------------------------------------------- reading what a payer sends
_STAGE = re.compile(r"type\s*:\s*(\w+)", re.I)
_FULL_URL = re.compile(r"fullurl\s*:\s*(\S+)", re.I)
_PROCEDURE = re.compile(r"procedure\s*code\s*:\s*(\S+)", re.I)


def supporting_entry(adapter: dict[str, Any],
                     entry: dict[str, Any]) -> dict[str, Any]:
    """Read one `authorizationSupporting` entry into something usable.

    PMJAY overloads the entry's free-text `text` field with the two facts that
    decide what to do with it — a questionnaire carries `fullUrl:` and is a
    form to answer, a document carries `Type:` saying *when* it is wanted and
    `Procedure Code:` saying what for. Neither is a coded element, so this is
    string work by necessity, and it is kept here rather than in the exchange.
    """
    coding = (entry.get("coding") or [{}])[0]
    text = str(entry.get("text") or "")
    url = _FULL_URL.search(text)
    stage = _STAGE.search(text)
    procedure = _PROCEDURE.search(text)
    kind = "form" if url else "document"
    stage_value = (stage.group(1).lower() if stage else
                   ("" if kind == "form" else "pre"))
    return {
        "kind": kind,
        "code": coding.get("code"),
        "display": coding.get("display"),
        "form_url": url.group(1) if url else None,
        "stage": stage_value,
        "for_code": procedure.group(1) if procedure else None,
        # A form is always wanted up front; a document only if its stage says
        # so — the rest belong with the claim, not the pre-authorisation.
        "at_preauth": kind == "form"
        or stage_value in adapter["preauth_stages"],
    }
