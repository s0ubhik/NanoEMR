"""PMJAY adjudicator — the payer side of cases this EMR raised."""

from __future__ import annotations

import json

from ... import adjudicator, claims, db
from .. import ui
from ..common import not_found, render
from ..router import Request, redirect

st = ui.st

STAGE_TONE = {"preauth": "info", "claim": "warning"}


def register(app) -> None:
    app.add("GET", "/adjudicator", index)
    app.add("GET", "/adjudicator/<int:cid>/<str:stage>", detail)
    app.add("POST", "/adjudicator/<int:cid>/<str:stage>", act)


def _status_chip(row) -> str:
    labels = (claims.PREAUTH_STATUS if row["stage"] == "preauth"
              else claims.CLAIM_STATUS)
    return ui.label_chip(*labels.get(row["status"], (row["status"], "")))


def index(request: Request):
    """Every leg this EMR has sent that a payer could still be deciding on."""
    rows = adjudicator.cases()
    stage = request.q("stage")
    if stage in adjudicator.STAGES:
        rows = [r for r in rows if r["stage"] == stage]

    table_rows = [[
        f'<a class="z-link" href="/adjudicator/{r["claim_id"]}/{r["stage"]}">'
        f'{ui.esc(r["case_number"])}</a>',
        ui.label_chip(r["stage_label"], STAGE_TONE[r["stage"]]),
        ui.sub(ui.esc(r["beneficiary_name"] or "Unknown"), r["member_id"]),
        _status_chip(r),
        f'₹{r["amount"]:,.0f}' if r["amount"] else "—",
        ui.muted(r["correlation_id"] or "—", "text-xs"),
        ui.when(r["sent_at"]),
    ] for r in rows]

    filters = ('<form method="get" action="/adjudicator" '
               f'class="display-flex gap items-end"{st(gap=2)}>'
               + ui.select("stage", [(k, v[0])
                                     for k, v in adjudicator.STAGES.items()],
                           stage, blank="Both stages")
               + ui.button("Filter", style="z-button-primary", type_="submit")
               + "</form>")

    body = ui.stack(
        ui.card(None, (
            "<p>NHCX carries the message; the <strong>decision</strong> on a "
            "PMJAY case is taken in the NHCX Payer Service, outside the "
            "exchange. Every leg this EMR has sent is listed here with the "
            "<code>x-hcx-correlation_id</code> it went out on — which is what "
            "the payer service needs to tie a decision to a request, and what "
            "a payer console has no way of knowing.</p>"
            + ui.muted("Every call goes through the paired hcxkit; nothing "
                       "here reaches the payer service directly."))),
        ui.card(f"{len(table_rows)} case(s) raised from here",
                ui.table(["Case", "Stage", "Beneficiary", "Our status",
                          "Amount", "Correlation", "Sent"], table_rows,
                         empty="Nothing has been sent to a payer yet."),
                actions=filters))
    return render(request, "PMJAY adjudicator", "adjudicator", body)


def detail(request: Request):
    """One case: who is holding it, and what that role may do."""
    cid, stage = request.params["cid"], request.params["stage"]
    row = adjudicator.case(cid, stage)
    if row is None:
        return not_found(request, "Case")

    # The role is read only when asked for: it is a live call to the payer
    # service, not something to fire on every page view.
    lookup = None
    if request.q("lookup"):
        try:
            lookup = adjudicator.role_for(row["case_number"])
        except ValueError as error:
            lookup = {"error": str(error)}

    blocks = [_case_card(row)]
    if lookup is None:
        blocks.append(ui.card("Who is holding it", (
            "<p>A case sits at exactly one step, and only the role holding it "
            "may act — so the role is read first and the actions offered are "
            "that role's. Until it answers there is nothing to offer.</p>"
            + f'<div class="mt"{st(mt=4)}>'
            + ui.button("Read the current role",
                        f"/adjudicator/{cid}/{stage}?lookup=1",
                        style="z-button-primary", ico="search")
            + "</div>")))
    else:
        blocks.append(_role_card(cid, stage, row, lookup))

    taken = adjudicator.decisions(cid, stage)
    if taken:
        blocks.append(_decisions_card(taken))

    return render(request, f'Case {row["case_number"]}', "adjudicator",
                  ui.stack(*blocks),
                  breadcrumb=[("PMJAY adjudicator", "/adjudicator"),
                              (row["case_number"], None)])


def _case_card(row) -> str:
    return ui.card("Case", ui.dl([
        ("Case number", row["case_number"]),
        ("Stage", row["stage_label"]),
        ("Beneficiary", row["beneficiary_name"]),
        ("Member ID", row["member_id"]),
        ("Payer", row["payer_id"]),
        ("Our status", claims.PREAUTH_STATUS.get(row["status"],
                                                 (row["status"],))[0]
         if row["stage"] == "preauth"
         else claims.CLAIM_STATUS.get(row["status"], (row["status"],))[0]),
        ("Amount", f'₹{row["amount"]:,.0f}' if row["amount"] else None),
        ("Correlation", row["correlation_id"]),
        ("Sent at", ui.when(row["sent_at"])),
    ], cols=3), actions=ui.button("Open the claim",
                                  f'/claims/{row["claim_id"]}?tab='
                                  + ("preauth" if row["stage"] == "preauth"
                                     else "claim"), ico="external-link"))


def _role_card(cid: int, stage: str, row, lookup) -> str:
    """What came back, and only the actions that role may take."""
    role = (lookup.get("role") or "").strip()
    warning = lookup.get("warning")
    error = lookup.get("error")

    head = ui.dl([
        ("Role holding it", role or None),
        ("Step", lookup.get("step", {}).get("stage")
         if isinstance(lookup.get("step"), dict) else None),
        ("Usecase sent", lookup.get("step", {}).get("usecase")
         if isinstance(lookup.get("step"), dict) else None),
    ], cols=3)

    notes = ""
    if error:
        notes += (f'<p class="color mt"{st(color="var(--z-danger)", mt=3)}>'
                  f"{ui.esc(error)}</p>")
    if warning:
        notes += f'<div class="mt"{st(mt=2)}>{ui.muted(warning)}</div>'

    raw = ""
    if lookup.get("response") is not None:
        raw = (f'<div class="mt"{st(mt=4)}>'
               + ui.field("What the payer service said", ui.code_block(
                   json.dumps(lookup["response"], indent=1)))
               + "</div>")

    actions = adjudicator.actions_for(role)
    form = ""
    if actions:
        form = (f'<form method="post" action="/adjudicator/{cid}/{stage}" '
                f'class="display-flex gap items-end flex-wrap mt"'
                f'{st(gap=2, mt=4)}>'
                f'<input type="hidden" name="role" value="{ui.esc(role)}">'
                + ui.field("Action", ui.select(
                    "action", [(a, a) for a in actions], "",
                    blank="Select an action"), compact=True, required=True)
                + ui.field("Remarks", ui.text_input(
                    "remarks", "", placeholder="Recorded with the decision",
                    attrs='style="min-width:18rem"'), compact=True)
                + ui.button("Take this action", style="z-button-primary",
                            type_="submit", ico="gavel")
                + "</form>"
                + f'<div class="mt"{st(mt=2)}>'
                + ui.muted(f'{role} may take {", ".join(actions)} — nothing '
                           "else is offered, because nothing else would be "
                           "accepted.") + "</div>")
    elif role:
        form = (f'<div class="mt"{st(mt=3)}>'
                + ui.muted(f'"{role}" is not a role in the published '
                           "workflow, so no action can be offered for it.")
                + "</div>")

    return ui.card("Who is holding it", head + notes + form + raw,
                   actions=ui.button("Read again",
                                     f"/adjudicator/{cid}/{stage}?lookup=1",
                                     ico="refresh-cw"))


def _decisions_card(taken) -> str:
    """What has been decided from here — NHCX keeps no record of it."""
    blocks = []
    for row in taken:
        try:
            reply = json.loads(row["response_json"] or "{}")
        except ValueError:
            reply = {}
        tone = "success" if row["success"] else "danger"
        title = (f'{row["action"]} as {row["role"]} — '
                 f'{"accepted" if row["success"] else "refused"}')
        blocks.append((title, (
            ui.dl([
                ("Taken at", ui.when(row["taken_at"])),
                ("Usecase", row["usecase"]),
                ("HTTP", row["http_status"]),
                ("Correlation", row["correlation_id"]),
                ("Remarks", row["remarks"]),
            ], cols=3)
            + f'<div class="mt"{st(mt=4)}>'
            + ui.field("What the payer service said", ui.code_block(
                json.dumps(reply.get("response") or reply.get("raw")
                           or reply.get("error") or reply, indent=1)))
            + "</div>")))
        blocks[-1] = (ui.label_chip(row["action"] or "—", tone) + " " + title,
                      blocks[-1][1])
    return ui.card(f"Decisions taken from here ({len(taken)})",
                   ui.accordion(blocks, multiple=False))


def act(request: Request):
    cid, stage = request.params["cid"], request.params["stage"]
    try:
        reply = adjudicator.process(cid, stage, request.f("role"),
                                    request.f("action"), request.f("remarks"))
    except ValueError as error:
        return redirect(f"/adjudicator/{cid}/{stage}?lookup=1", str(error),
                        "danger")
    flash = ("Decision sent to the payer service."
             if reply.get("success")
             else reply.get("error") or "The payer service refused it.")
    return redirect(f"/adjudicator/{cid}/{stage}?lookup=1", flash,
                    "success" if reply.get("success") else "danger")
