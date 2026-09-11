"""NHCX claims — policy search, coverage eligibility and the claim ledger."""

from __future__ import annotations

import json

from ... import claims, db, payers
from .. import ui
from ..common import collect_rows, doctor_options, not_found, render
from ..router import Request, Response, json_response, redirect

st = ui.st


def register(app) -> None:
    app.add("GET", "/claims", index)
    app.add("GET", "/claims/new", new)
    app.add("POST", "/claims", create)
    app.add("GET", "/claims/<int:cid>", detail)
    # The claim's state as JSON, for a driver or a test: everything the tabs
    # show, read the same way, after the same polls.
    app.add("GET", "/claims/<int:cid>/state", state)
    app.add("POST", "/claims/<int:cid>/check", check)
    app.add("POST", "/claims/<int:cid>/plan", fetch_plan)
    app.add("GET", "/claims/<int:cid>/plan/forms", plan_forms_page)
    app.add("GET", "/claims/<int:cid>/plan/forms/<int:fid>", form_detail)
    app.add("GET", "/claims/<int:cid>/plan/<int:bid>", benefit_detail)
    app.add("POST", "/claims/<int:cid>/link", link)
    app.add("POST", "/claims/<int:cid>/unlink", unlink)
    app.add("POST", "/claims/<int:cid>/preauth", save_preauth)
    app.add("GET", "/claims/<int:cid>/lines", lines_page)
    app.add("POST", "/claims/<int:cid>/lines", add_line)
    app.add("POST", "/claims/<int:cid>/lines/quantities", save_quantities)
    app.add("POST", "/claims/<int:cid>/lines/<int:lid>/delete", remove_line)
    app.add("POST", "/claims/<int:cid>/forms", save_answers)
    app.add("POST", "/claims/<int:cid>/auth", request_auth)
    app.add("POST", "/claims/<int:cid>/submit", submit_preauth)
    app.add("POST", "/claims/<int:cid>/predetermination", ask_predetermination)
    app.add("POST", "/claims/<int:cid>/cancel", cancel_preauth)
    app.add("POST", "/claims/<int:cid>/status", ask_status)
    app.add("POST", "/claims/<int:cid>/reprocess", ask_reprocess)
    app.add("POST", "/claims/<int:cid>/queries/<int:qid>/reply", answer_query)
    app.add("POST", "/claims/<int:cid>/discharge", save_discharge)
    app.add("POST", "/claims/<int:cid>/claim", submit_claim)
    app.add("POST", "/claims/<int:cid>/claim/documents",
            upload_claim_documents)
    app.add("POST", "/claims/<int:cid>/payments/<int:pid>/ack",
            acknowledge_payment)
    app.add("POST", "/claims/<int:cid>/documents", upload_document)
    app.add("POST", "/claims/<int:cid>/documents/required",
            upload_required_documents)
    app.add("GET", "/claims/<int:cid>/documents/<int:did>", view_document)
    app.add("POST", "/claims/<int:cid>/documents/<int:did>/delete",
            remove_document)
    # The callback door. hcxkit appends the message's own inbound route to the
    # callback base on its participant profile, so a profile configured as
    # `http://127.0.0.1:8765/callback` delivers a coverage on_check to
    # `/callback/v1/coverageeligibility/on_check`. The whole family lands on
    # one handler — the route it arrived on is bookkeeping, the X-Hcxkit-*
    # headers say what the message is. `/nhcx/callback` stays for a profile
    # still pointed at the flat URL.
    app.add("POST", "/callback", callback)
    app.add("POST", "/callback/<path:route>", callback)
    app.add("POST", "/nhcx/callback", callback)


# --------------------------------------------------------- the inbound door
def callback(request: Request) -> Response:
    """Receive what hcxkit's inbound worker delivers.

    Answers every path under ``/callback`` — hcxkit posts a coverage on_check
    to ``/callback/v1/coverageeligibility/on_check`` — plus the flat
    ``/nhcx/callback``; what the message *is* comes off the ``X-Hcxkit-*``
    headers, not the path, so a route the kit's inMap spells differently is
    still read correctly rather than dead-lettered as a 404.

    The worker cannot hold a session and reads the reply as a delivery
    outcome — 2xx delivered, 4xx dead-lettered, 5xx retried with backoff — so
    the status codes here are chosen against that contract, not for a browser.
    An unreadable body is a 400 because redelivering it cannot help; anything
    unexpected raises and the router's 500 asks for the retry.
    """
    if claims.CALLBACK_TOKEN and request.q("token") != claims.CALLBACK_TOKEN:
        return json_response({"error": "Wrong or missing callback token"}, 401)
    try:
        envelope = json.loads(request.raw_body or b"{}")
        if not isinstance(envelope, dict):
            raise ValueError("Expected a JSON object.")
        outcome = claims.receive(
            envelope,
            request.header("X-Hcxkit-Type"),
            request.header("X-Hcxkit-Flow"),
            request.header("X-Hcxkit-Payload-Kind"))
    except ValueError as error:
        return json_response({"status": "rejected", "error": str(error)}, 400)
    return json_response({"status": outcome})


# ---------------------------------------------------------------------------
def _money(value) -> str:
    return "—" if value is None else f"₹{value:,.0f}"


def index(request: Request):
    scope = request.q("status") or ""
    rows = claims.claims_list(scope if scope in claims.STATUS else "")
    table_rows = [[
        f'<a class="z-link" href="/claims/{r["id"]}">{ui.esc(r["claim_no"])}</a>',
        ui.sub(ui.esc(r["beneficiary_name"] or "Unknown"), r["member_id"]),
        ui.sub(ui.esc(r["product_name"] or r["plan_name"] or "—"),
               r["policy_code"] or ""),
        ui.esc(r["payer_name"] or "—"),
        _money(claims.balance(r)),
        ui.label_chip(*claims.STATUS[r["status"]]),
        ui.when(r["created_at"], 10),
    ] for r in rows]

    filters = ('<form method="get" action="/claims" '
               f'class="display-flex gap items-end"{st(gap=2)}>'
               + ui.select("status", [(k, v[0]) for k, v in claims.STATUS.items()],
                           scope, blank="All claims")
               + ui.button("Filter", style="z-button-primary", type_="submit")
               + "</form>")

    body = (
        ui.card(None, (
            "<p>An NHCX claim episode starts from the beneficiary's policy: "
            "search the insurance registry, pick the policy, then run a "
            "coverage eligibility check against the payer. The FHIR exchange "
            "itself — bundle building, JWE encryption and dispatch — is "
            "delegated to the paired hcxkit gateway.</p>"))
        + f'<div class="mt"{st(mt=5)}>'
        + ui.card(f"{len(table_rows)} claim(s)",
                  ui.table(["Claim", "Beneficiary", "Policy", "Payer",
                            "Balance", "Status", "Opened"], table_rows,
                           empty="No claims yet. Start from a policy search."),
                  actions=filters + ui.button("New claim", "/claims/new",
                                              style="z-button-primary",
                                              ico="plus"))
        + "</div>")
    return render(request, "Claims", "claims", body)


# ---------------------------------------------------------------------------
def new(request: Request):
    id_type = request.q("id_type") or "MemberId"
    id_value = (request.q("id_value") or "").strip()

    search = ('<form method="get" action="/claims/new" '
              f'class="display-flex gap items-end flex-wrap"{st(gap=2)}>'
              + ui.field("Identifier type",
                         ui.select("id_type", list(claims.ID_TYPES.items()),
                                   id_type), compact=True)
              + ui.field("Identifier value",
                         ui.text_input("id_value", id_value,
                                       placeholder="98185…, 91-1234-… or member ID",
                                       attrs='style="min-width:16rem"'),
                         compact=True)
              + ui.button("Search policies", style="z-button-primary",
                          type_="submit", ico="search")
              + "</form>")

    results = ""
    if id_value:
        try:
            policies = claims.search_policies(id_type, id_value)
        except ValueError as error:
            results = ui.card(None, f'<p class="color"'
                              f'{st(color="var(--z-danger)")}>'
                              f"{ui.esc(str(error))}</p>")
        else:
            table_rows = [[
                ui.sub(ui.esc(p["name"] or "Unknown"), p["member_id"] or "—"),
                ui.sub(ui.esc(p["product_name"] or "—"),
                       p["policy_code"] or p["product_id"] or ""),
                ui.sub(ui.esc(p["payer_name"] or "—"), p["payer_id"] or ""),
                ui.esc(p["abha_number"] or "—"),
                ui.esc(p["mobile_number"] or "—"),
                ui.post_button("/claims", "Select", style="z-button-primary",
                               ico="circle-plus", fields={
                                   "id_type": id_type, "id_value": id_value,
                                   "policy": json.dumps(p, ensure_ascii=False),
                               }),
            ] for p in policies]
            results = ui.card(
                f"{len(table_rows)} polic{'y' if len(table_rows) == 1 else 'ies'} found",
                ui.table(["Beneficiary", "Product / policy", "Payer", "ABHA",
                          "Mobile", ""], table_rows,
                         empty="No policy matches that identifier."))

    body = (ui.card("Search the beneficiary's policy", search)
            + (f'<div class="mt"{st(mt=5)}>{results}</div>' if results else ""))
    return render(request, "New claim — policy search", "claim-new", body,
                  breadcrumb=[("Claims", "/claims"), ("Policy search", None)])


def create(request: Request):
    try:
        policy = json.loads(request.f("policy") or "{}")
    except json.JSONDecodeError:
        policy = {}
    try:
        claim_id = claims.create_claim(policy, request.f("id_type"),
                                       request.f("id_value"))
    except ValueError as error:
        return redirect("/claims/new", str(error), "danger")
    number = db.scalar("SELECT claim_no FROM claim WHERE id = ?", (claim_id,))
    return redirect(f"/claims/{claim_id}", f"Claim {number} opened.")


# ---------------------------------------------------------------------------
def _photo(row) -> str:
    src = row["patient_photo"]
    if src:
        if not src.startswith(("http://", "https://", "data:")):
            src = f"data:image/jpeg;base64,{src}"
        return (f'<img src="{ui.esc(src)}" alt="Beneficiary photo" '
                'class="z-rounded" style="width:5rem;height:5rem;'
                'object-fit:cover">')
    return ui.avatar({"name": row["beneficiary_name"]}, size="5rem")


def detail(request: Request):
    cid = request.params["cid"]
    row = claims.claim(cid)
    if row is None:
        return not_found(request, "Claim")

    poll_note = ""
    if row["status"] == "checking":
        try:
            if claims.poll_response(cid):
                row = claims.claim(cid)
        except ValueError as error:
            poll_note = ui.muted(f"Could not poll the gateway: {error}",
                                 "text-sm")

    plan_row = claims.plan(cid)
    plan_note = ""
    if plan_row is not None and plan_row["status"] == "fetching":
        try:
            if claims.poll_plan(cid):
                plan_row = claims.plan(cid)
        except ValueError as error:
            plan_note = ui.muted(f"Could not poll the gateway: {error}",
                                 "text-sm")

    ruling = claims.auth(cid)
    if ruling is not None and ruling["status"] == "checking":
        try:
            claims.poll_auth(cid)
        except ValueError:
            pass  # the card says "awaiting"; a transient failure changes nothing
        ruling = claims.auth(cid)

    claimed = claims.submission(cid)
    if claimed is not None and claimed["status"] == "submitting":
        try:
            claims.poll_claim(cid)
        except ValueError:
            pass  # the card says "awaiting"; a transient failure changes nothing
        claimed = claims.submission(cid)

    if any(q["status"] == "asking" for q in claims.predeterminations(cid)):
        try:
            claims.poll_predetermination(cid)
        except ValueError:
            pass  # the card says "awaiting"; a transient failure changes nothing

    sent = claims.preauth(cid)
    if sent is not None and sent["status"] in ("submitting", "cancelling"):
        poll = (claims.poll_preauth if sent["status"] == "submitting"
                else claims.poll_cancel)
        try:
            poll(cid)
        except ValueError:
            pass  # the card says "awaiting"; a transient failure changes nothing
        sent = claims.preauth(cid)

    if any(e["status"] == "asking" for e in claims.enquiries(cid)):
        try:
            if claims.poll_enquiries(cid):
                claimed = claims.submission(cid)
        except ValueError:
            pass  # the enquiry card says "asking"; nothing to do but wait

    beneficiary = ui.card("Beneficiary", (
        f'<div class="display-flex gap items-start"{st(gap=4)}>'
        + _photo(row)
        + '<div class="flex-1">'
        + ui.dl([
            ("Name", row["beneficiary_name"]),
            ("Member ID", row["member_id"]),
            ("ABHA", row["abha_number"]),
            ("Mobile", row["mobile_number"]),
            ("Gender", (row["patient_gender"] or "").title() or None),
            ("Date of birth", row["patient_dob"]),
            ("Address", row["patient_address"]),
            ("Found by", f'{claims.ID_TYPES.get(row["search_id_type"], "—")} '
                         f'· {row["search_id_value"]}'
             if row["search_id_value"] else None),
        ], cols=3)
        + "</div></div>"))

    policy = ui.card("Policy", ui.dl([
        ("Policy code", row["policy_code"]),
        # dl() only passes a value through raw when it starts with a tag, so
        # the main-line + muted sub-line pair needs a wrapping element.
        ("Product", "<div>" + ui.sub(ui.esc(row["product_name"] or "—"),
                                     row["product_id"] or "") + "</div>"),
        ("Payer", "<div>" + ui.sub(ui.esc(row["payer_name"] or "—"),
                                   row["payer_id"] or "") + "</div>"),
        ("Plan", row["plan_name"]),
        ("Plan period", f'{ui.when(row["plan_period_start"], 10)} → '
                        f'{ui.when(row["plan_period_end"], 10)}'
         if row["plan_period_start"] else None),
        ("Relationship", (row["relationship"] or "").title() or None),
    ], cols=3))

    verdict = ""
    if row["status"] in ("eligible", "not-eligible"):
        stats = (f'<div class="display-grid gap sm:grid-cols lg:grid-cols"'
                 + st(gap=4, sm_grid_cols=2, lg_grid_cols=3) + ">"
                 + ui.stat("Sum insured", _money(row["allowed_amount"]),
                           "shield-check")
                 + ui.stat("Utilised", _money(row["used_amount"]),
                           "trending-down", "warning")
                 + ui.stat("Wallet balance", _money(claims.balance(row)),
                           "wallet", "success" if row["status"] == "eligible"
                           else "danger")
                 + "</div>")
        verdict = ui.card("Payer verdict", stats + f'<div class="mt"{st(mt=4)}>'
                          + ui.dl([
                              ("Disposition", row["disposition"]),
                              ("Outcome", row["outcome"]),
                              ("In force", "Yes" if row["inforce"] else "No"),
                              ("Pre-authorisation",
                               None if row["auth_required"] is None else
                               ("Required" if row["auth_required"]
                                else "Not required")),
                              ("Checked at", ui.when(row["checked_at"])),
                              ("Correlation", row["correlation_id"]),
                          ], cols=3) + "</div>")
    elif row["status"] == "checking":
        # The check form is deliberately not offered here — the exchange is
        # still open, and two checks in flight on one claim would be answered
        # out of order — so the way out of a stalled exchange is one button
        # that re-sends what went.
        resend = ui.post_button(
            f"/claims/{cid}/check", "Send again", ico="refresh-cw",
            fields={"purpose": row["purpose"] or "validation",
                    "policy_code": row["policy_code"] or "",
                    "member_id": row["member_id"] or ""},
            confirm="The payer has not replied to the last check yet. Send "
                    "the same eligibility check again?")
        verdict = ui.card("Payer verdict", (
            f'<p>Eligibility {ui.esc(row["purpose"] or "check")} sent to the '
            "payer; awaiting the on_check reply. Use Refresh to check for it."
            "</p>"
            + (f'<div class="mt"{st(mt=2)}>{poll_note}</div>' if poll_note else "")
            + f'<div class="mt"{st(mt=3)}>' + ui.dl([
                ("Sent at", ui.when(row["checked_at"])),
                ("Transaction", row["txn_id"]),
                ("Correlation", row["correlation_id"]),
            ], cols=3) + "</div>"), actions=resend)
    elif row["status"] == "error":
        verdict = ui.card("Payer verdict", (
            f'<p class="color"{st(color="var(--z-danger)")}>'
            f'{ui.esc(row["error_message"] or "The exchange failed.")}</p>'))

    check_form = (
        f'<form method="post" action="/claims/{cid}/check">'
        + ui.card("Coverage eligibility check", ui.grid(
            ui.field("Purpose", ui.select("purpose",
                                          list(claims.PURPOSES.items()),
                                          row["purpose"] or "validation"),
                     required=True),
            ui.field("Policy code", ui.text_input("policy_code",
                                                  row["policy_code"] or "",
                                                  placeholder="PMJAY/HP/S/G"),
                     help_text="National health plan identifier on the card"),
            ui.field("Member ID", ui.text_input("member_id",
                                                row["member_id"] or "",
                                                required=True), required=True),
            cols=3),
            footer=(f'<div class="display-flex justify-end"{st()}>'
                    + ui.button("Send to payer" if row["status"] == "draft"
                                else "Check again", style="z-button-primary",
                                type_="submit", ico="send") + "</div>"))
        + "</form>")

    chips = (ui.label_chip(row["claim_no"], "info")
             + ui.label_chip(*claims.STATUS[row["status"]]))
    if plan_row is not None:
        chips += ui.label_chip(*claims.PLAN_STATUS[plan_row["status"]])
    if sent is not None:
        chips += ui.label_chip(*claims.PREAUTH_STATUS[sent["status"]])
    if claimed is not None and claimed["status"] != "draft":
        chips += ui.label_chip(*claims.CLAIM_STATUS[claimed["status"]])
    open_queries = [q for q in claims.queries(cid) if q["status"] != "answered"]
    if open_queries:
        chips += ui.label_chip(f"{len(open_queries)} query awaiting reply"
                               if len(open_queries) == 1 else
                               f"{len(open_queries)} queries awaiting reply",
                               "warning")
    settled = claims.paid_total(cid)
    if settled:
        chips += ui.label_chip(f"Paid {_money(settled)}", "success")
    elif row["preauth_saved_at"]:
        chips += ui.label_chip("Preauth drafted", "info")
    awaiting = (row["status"] == "checking"
                or (plan_row is not None and plan_row["status"] == "fetching")
                or (sent is not None
                    and sent["status"] in ("submitting", "cancelling"))
                or (claimed is not None and claimed["status"] == "submitting")
                or (ruling is not None and ruling["status"] == "checking")
                or any(e["status"] == "asking" for e in claims.enquiries(cid)))
    if awaiting:
        # Each load polls the gateway for what is outstanding, so a reload
        # is the check. The page never reloads itself: that would eat
        # whatever is being typed on the other tabs.
        chips += ui.button("Refresh", f"/claims/{cid}?tab={request.q('tab') or ''}",
                           ico="refresh-cw")
    header = (f'<div class="display-flex items-center justify-between gap flex-wrap"'
              f'{st(gap=3)}>'
              f'<h2 class="z-h3">{ui.esc(row["beneficiary_name"] or row["member_id"])}</h2>'
              f'<div class="display-flex items-center gap flex-wrap"{st(gap=2)}>{chips}</div>'
              "</div>")

    eligibility_blocks = [beneficiary, policy]
    if verdict:
        eligibility_blocks.append(verdict)
    if row["status"] != "checking":
        eligibility_blocks.append(check_form)

    tab = request.q("tab") or ""
    tab = TAB_ALIASES.get(tab, tab)
    # The tabs walk the episode in the order it happens: the cover, the
    # payer's own package master, the lines quoted from it, the payer's
    # ruling on that set, then the pre-authorisation dossier and its
    # sending, the payer's questions, the claim, and the money. The
    # pre-authorisation tab appears once the payer has ruled on the lines
    # (at once, for a payer that does not rule).
    tabs = [
        ("eligibility", "Eligibility", ui.stack(*eligibility_blocks)),
        ("plan", "Insurance plan", _plan_pane(
            row, plan_row, plan_note,
            {"q": request.q("q"), "cat": request.q("cat"),
             "kind": request.q("kind")})),
        ("lines", "Line items", _lines_pane(row, plan_row)),
        ("validate", "Validate", _validate_pane(row)),
    ]
    if _preauth_open(row):
        tabs.append(("preauth", "Pre-authorisation", _preauth_pane(row)))
    tabs += [
        ("communication", "Communication", _communication_pane(row)),
        ("claim", "Claim", _claim_pane(row)),
        ("payments", "Payments", _payments_pane(row)),
    ]
    keys = [key for key, _, _ in tabs]
    if tab == "preauth" and "preauth" not in keys:
        tab = "validate"
    panes = ui.tabs([(label, pane) for _, label, pane in tabs],
                    active=keys.index(tab) if tab in keys else 0,
                    key="claim-tabs")

    body = ui.stack(header, panes)

    return render(request, f'Claim {row["claim_no"]}', "claims", body,
                  breadcrumb=[("Claims", "/claims"), (row["claim_no"], None)])


def _plain(row):
    return dict(row) if row is not None else None


def state(request: Request):
    """Where the claim stands, as JSON — after polling the gateway for
    whatever is outstanding, exactly as opening the page would."""
    cid = request.params["cid"]
    row = claims.claim(cid)
    if row is None:
        return json_response({"error": "No such claim"}, 404)
    notes = []
    for waiting, poll in (
            (row["status"] == "checking", claims.poll_response),
            (True, claims.poll_plan),
            (True, claims.poll_auth),
            (True, claims.poll_claim),
            (True, claims.poll_preauth),
            (True, claims.poll_predetermination),
            (True, claims.poll_cancel),
            (True, claims.poll_enquiries)):
        if not waiting:
            continue
        try:
            poll(cid)
        except ValueError as error:
            notes.append(str(error))
    row = claims.claim(cid)
    plan_row = claims.plan(cid)
    return json_response({
        "claim": _plain(row),
        "plan": _plain(plan_row),
        "benefits": [dict(b) for b in db.query(
            "SELECT id, kind, code, display, category_code, category_display, "
            "rate FROM claim_plan_benefit WHERE plan_id = ? ORDER BY seq",
            (plan_row["id"],))] if plan_row is not None else [],
        "admissions": [dict(a) for a in claims.linkable_admissions(row)],
        "lines": [dict(l) for l in claims.lines(cid)],
        "ruling": _plain(claims.auth(cid)),
        "required_documents": {
            "preauth": [dict(n) for n in claims.required_documents(cid, "preauth")],
            "claim": [dict(n) for n in claims.required_documents(cid, "claim")]},
        "forms": {
            stage: [{"url": f["url"], "title": f["title"],
                     "questions": claims.form_questions(f)}
                    for f in claims.required_forms(cid, stage)]
            for stage in ("preauth", "claim")},
        "documents": [{k: d[k] for k in ("id", "code", "label", "filename",
                                         "content_type", "size", "stage")}
                      for d in claims.documents(cid)],
        "preauth": _plain(claims.preauth(cid)),
        "predeterminations": [dict(q) for q in claims.predeterminations(cid)],
        "enhancement_lines": [dict(l) for l in claims.enhancement_lines(cid)],
        "queries": [{**dict(q), "questions": claims.query_questions(q)}
                    for q in claims.queries(cid)],
        "submission": _plain(claims.submission(cid)),
        "payments": [dict(p) for p in claims.payments(cid)],
        "paid_total": claims.paid_total(cid),
        "enquiries": [dict(e) for e in claims.enquiries(cid)],
        "poll_notes": notes,
    })


def check(request: Request):
    cid = request.params["cid"]
    try:
        claims.run_check(cid, request.f("purpose"), request.f("policy_code"),
                         request.f("member_id"))
    except ValueError as error:
        return redirect(f"/claims/{cid}", str(error), "danger")
    return redirect(f"/claims/{cid}",
                    "Eligibility request queued to the payer.")


# ------------------------------------------------- the payer's package master
def fetch_plan(request: Request):
    cid = request.params["cid"]
    try:
        claims.request_plan(cid)
    except ValueError as error:
        return redirect(f"/claims/{cid}?tab=plan", str(error), "danger")
    return redirect(f"/claims/{cid}?tab=plan",
                    "Insurance plan requested from the payer.")


# How many packages one screen shows before the speciality filter has to earn
# its keep — a PMJAY master runs to hundreds of rows.
PLAN_PAGE = 200


# Conditions worth seeing at a glance: the ones that stop a package being
# picked or change what may be added to it. The rest are one chip away.
KEY_CONDITIONS = ("GovtReserved", "Standalone", "IsDayCare",
                  "ApprovalNotRequired", "ImplantApplicable",
                  "StratificationAllowed", "ProcedureType")


def _conditions_chips(row) -> str:
    """The benefit's claim conditions, the ones that constrain it first."""
    conditions = claims.plan_condition_map(row)
    if not conditions:
        return "—"
    ordered = ([(k, conditions[k]) for k in KEY_CONDITIONS if k in conditions]
               + [(k, v) for k, v in conditions.items()
                  if k not in KEY_CONDITIONS])
    shown = ordered[:3]
    chips = "".join(
        ui.label_chip(key if value is True else f"{key}: {value}", "info")
        for key, value in shown)
    if len(ordered) > len(shown):
        chips += ui.muted(f"+{len(ordered) - len(shown)} more")
    return f'<div class="display-flex gap flex-wrap"{st(gap=1)}>{chips}</div>'


def _rate_cell(row) -> str:
    """The package rate, with the tiers that are paid over the top of it."""
    tiers = [t for t in claims.plan_extras(row) if t.get("rate")]
    if not tiers:
        return _money(row["rate"])
    top = max(t["rate"] for t in tiers)
    return ui.sub(_money(row["rate"]),
                  f"+{len(tiers)} tier(s), up to {_money(top)}")


def _documents_cell(row) -> str:
    """How many documents the payer will want with this benefit."""
    docs = claims.plan_documents(row)
    if not docs:
        return "—"
    return ui.sub(f"{len(docs)} required",
                  docs[0].get("display") or docs[0].get("code") or "")


BENEFIT_KINDS = [("Procedure", "Procedure"), ("Implant", "Implant")]


def _plan_filters(plan_row, query: dict[str, str]) -> str:
    """Search, then speciality, then type — the order an operator narrows in.

    Its own row above the table rather than the card header: three controls
    and two buttons do not fit beside a title on a laptop, let alone a tablet.
    """
    cid = plan_row["claim_id"]
    categories = claims.plan_categories(plan_row["id"])
    options = [(c["category_code"] or "",
                f'{c["category_display"] or c["category_code"] or "—"} '
                f'({c["n"]})') for c in categories]
    clear = ""
    if any(query.values()):
        clear = ui.button("Clear", f"/claims/{cid}?tab=plan", ico="x")
    return (f'<form method="get" action="/claims/{cid}" '
            f'class="display-flex gap items-end flex-wrap mb"{st(gap=2, mb=4)}>'
            '<input type="hidden" name="tab" value="plan">'
            + ui.field("Search", ui.text_input(
                "q", query["q"], placeholder="Procedure name or code",
                attrs='style="min-width:18rem"'), compact=True)
            + ui.field("Speciality", ui.select("cat", options, query["cat"],
                                               blank="All specialities"),
                       compact=True)
            + ui.field("Type", ui.select("kind", BENEFIT_KINDS, query["kind"],
                                         blank="Any type"), compact=True)
            + ui.button("Search", style="z-button-primary", type_="submit",
                        ico="search")
            + clear + "</form>")


def _plan_table(plan_row, query: dict[str, str]) -> str:
    cid = plan_row["claim_id"]
    benefits = claims.plan_benefits(plan_row["id"], query["cat"], query["q"],
                                    query["kind"])
    shown = benefits[:PLAN_PAGE]

    table_rows = [[
        ui.esc(b["code"]),
        ui.esc(b["display"] or b["code"]),
        ui.esc(b["category_display"] or b["category_code"] or "—"),
        ui.label_chip(b["kind"] or "—",
                      "info" if b["kind"] == "Procedure" else "warning"),
        f'<a class="z-button z-button-secondary z-button-xsmall" '
        f'href="/claims/{cid}/plan/{b["id"]}" '
        f'onclick="return emrPackage(this)">View</a>',
    ] for b in shown]

    note = ""
    if len(benefits) > len(shown):
        note = (f'<div class="mt"{st(mt=2)}>'
                + ui.muted(f"Showing the first {len(shown)} of "
                           f"{len(benefits)} — search or filter to narrow it.")
                + "</div>")

    return (_plan_filters(plan_row, query)
            + ui.card(f"{len(benefits)} package(s)",
                      ui.table(["Code", "Package", "Speciality", "Type", ""],
                               table_rows,
                               empty="Nothing matches that search.") + note))


# ------------------------------------------------ one package, in full
def benefit_detail(request: Request):
    """One package in full — the modal's contents, and a page on its own.

    The View control is a plain link, so it still opens the whole story with
    JavaScript off; ``?fragment=1`` is what the modal asks for.
    """
    row = claims.plan_benefit(request.params["bid"])
    if row is None or row["claim_id"] != request.params["cid"]:
        return not_found(request, "Package")
    if request.q("fragment"):
        # The modal has no chrome of its own, so the name is part of the body.
        return Response(f'<h2 class="z-h3">{ui.esc(row["display"] or row["code"])}</h2>'
                        + f'<div class="mt"{st(mt=4)}>{_benefit_body(row)}</div>')
    claim_no = db.scalar("SELECT claim_no FROM claim WHERE id = ?",
                         (row["claim_id"],))
    return render(request, f'{row["code"]} — {row["display"] or ""}', "claims",
                  ui.card(ui.esc(row["display"] or row["code"]),
                          _benefit_body(row)),
                  breadcrumb=[("Claims", "/claims"),
                              (claim_no, f'/claims/{row["claim_id"]}?tab=plan'),
                              (row["code"], None)])


QUESTION_TYPES = {"attachment": "File", "choice": "Choice",
                  "datetime": "Date & time", "date": "Date", "time": "Time",
                  "string": "Text", "text": "Long text", "boolean": "Yes / no",
                  "integer": "Number", "decimal": "Number",
                  "quantity": "Number", "url": "Link"}

# The HTML input type each FHIR answer type wants. Anything unlisted is a
# plain text box, which is what `string` is anyway.
QUESTION_INPUTS = {"datetime": "datetime-local", "date": "date",
                   "time": "time", "integer": "number", "decimal": "number",
                   "quantity": "number", "url": "url"}


def _question_type(question) -> str:
    return (question.get("type") or "").strip().lower()


def _question_label(question) -> str:
    return QUESTION_TYPES.get(_question_type(question),
                              question.get("type") or "—")


def _question_control(cid: int, question, value: str) -> str:
    """The input one questionnaire answer is given through.

    The payer declares a type per question and means it: a `dateTime` gets a
    date-and-time picker, an `attachment` a file input, a `choice` its own
    options. A text box for all of them is how a free-text date reaches a
    payer that will not take one.
    """
    kind = _question_type(question)
    name = f'qa_{question.get("linkId")}'
    if kind == "attachment":
        attached = claims.answer_document(value) if value else None
        current = ""
        if attached is not None:
            current = (f'<div class="mt"{st(mt=1)}>'
                       f'<a class="z-link" href="/claims/{cid}/documents/'
                       f'{attached["id"]}" target="_blank">'
                       f'{ui.esc(attached["filename"])}</a> '
                       + ui.muted("attached") + "</div>")
        return (f'<input class="z-input" type="file" name="{name}" '
                'accept="application/pdf,image/*">' + current)
    options = question.get("options") or []
    if kind == "choice" or options:
        # The payer pre-selects a default on some questions; honour it until
        # somebody answers otherwise.
        return ui.select(name, [(o, o) for o in options],
                         value or question.get("initial") or "", blank="—")
    if kind == "boolean":
        return ui.select(name, [("Yes", "Yes"), ("No", "No")], value,
                         blank="—")
    if kind == "text":
        return ui.textarea(name, value, rows=3)
    return ui.text_input(name, value,
                         type_=QUESTION_INPUTS.get(kind, "text"),
                         placeholder="Answer")


def _form_block(form, questions) -> str:
    """One questionnaire rendered as the form the payer expects back."""
    rows = []
    for question in questions:
        label = ui.esc(question.get("text") or question.get("linkId") or "—")
        if question.get("required"):
            label += ' <span class="color"' + st(color="var(--z-danger)") + ">*</span>"
        indent = f'<span style="padding-left:{question.get("depth", 0)}rem"></span>'
        options = "".join(ui.label_chip(ui.esc(o), "info")
                          for o in question.get("options") or [])
        rows.append([
            indent + label,
            ui.esc(_question_label(question)),
            options or "—",
        ])
    return ui.table(["Question", "Answer type", "Options"], rows,
                    empty="This form carries no questions.")


def _forms_section(forms) -> str:
    if not forms:
        return ""
    blocks = []
    for form in forms:
        questions = claims.form_questions(form)
        blocks.append((f'{form["title"]} ({len(questions)})',
                       _form_block(form, questions)))
    return ui.accordion(blocks, multiple=True)


def _documents_table(documents, forms_by_url) -> str:
    rows = []
    for doc in documents:
        form = forms_by_url.get(doc.get("form"))
        rows.append([
            ui.esc(doc.get("code") or "—"),
            ui.esc(doc.get("display") or "—"),
            ui.esc(doc.get("category_display") or doc.get("category") or "—"),
            ui.esc(form["title"]) if form else
            (ui.muted("form not sent") if doc.get("form") else "—"),
        ])
    return ui.table(["Code", "Document", "Category", "Form"], rows,
                    empty="No documents are listed as mandatory here.")


def _benefit_body(row) -> str:
    """Everything the payer published about one package."""
    tiers = claims.plan_extras(row)
    conditions = claims.plan_condition_map(row)
    documents = claims.plan_documents(row)
    forms = claims.plan_forms(row["plan_id"],
                              [d.get("form") for d in documents])
    forms_by_url = {f["url"]: f for f in forms}

    header = ui.dl([
        ("Code", row["code"]),
        ("Type", row["kind"]),
        ("Speciality", row["category_display"] or row["category_code"]),
        ("Package rate", _money(row["rate"])),
        ("Currency", row["currency"]),
        ("Requirement", row["requirement"]),
        ("Plan", row["plan_title"]),
    ], cols=3)

    cid = row["claim_id"]
    implants = claims.benefit_implants(row)
    wards = [t for t in tiers if t.get("type") != "Implant"]

    ward_block = ui.table(
        ["Tier", "Kind", "Rate"],
        [[ui.sub(ui.esc(t.get("label") or "—"), t.get("code") or ""),
          ui.esc(t.get("type") or "—"), _money(t.get("rate"))] for t in wards],
        empty="No ward or ICU tier is payable over the package rate.")

    # An implant links to its own entry in the plan, where its conditions and
    # documents live; the rate shown is the one quoted for *this* package.
    implant_block = ui.table(
        ["Code", "Implant", "Rate", ""],
        [[ui.esc(i.get("code") or "—"),
          ui.esc(i.get("display") or "—"),
          _money(i.get("rate")),
          (f'<a class="z-button z-button-secondary z-button-xsmall" '
           f'href="/claims/{cid}/plan/{i["id"]}" '
           f'onclick="return emrPackage(this)">View</a>')
          if i.get("id") else ui.muted("not in this plan")]
         for i in implants],
        empty="No implant is approved for this package.")
    implant_rules = [(k, conditions[k]) for k in
                     ("ImplantApplicable", "MultipleImplantsAllowed",
                      "MaximumImplantsAllowed") if k in conditions]
    if implant_rules:
        implant_block += (f'<div class="mt display-flex gap flex-wrap"'
                          f'{st(mt=3, gap=1)}>'
                          + "".join(ui.label_chip(f"{k}: {v}", "info")
                                    for k, v in implant_rules) + "</div>")

    # Read backwards for an implant: which packages may use it.
    used_by = claims.benefit_procedures(row)
    used_block = ui.table(
        ["Code", "Package", "Speciality", ""],
        [[ui.esc(u["code"]), ui.esc(u["display"] or u["code"]),
          ui.esc(u["category_display"] or "—"),
          f'<a class="z-button z-button-secondary z-button-xsmall" '
          f'href="/claims/{cid}/plan/{u["id"]}" '
          f'onclick="return emrPackage(this)">View</a>']
         for u in used_by[:PLAN_PAGE]],
        empty="No package in this plan lists this implant.")

    condition_rows = [[ui.esc(k), "Yes" if v is True else ui.esc(v)]
                      for k, v in conditions.items()]
    condition_block = ui.table(
        ["Condition", "Value"], condition_rows,
        empty="The payer published no conditions for this package.")

    sections = []
    if row["kind"] == "Implant":
        sections.append((f"Allowed with ({len(used_by)})", used_block))
    else:
        sections.append((f"Implants allowed ({len(implants)})", implant_block))
        sections.append((f"Ward & ICU tiers ({len(wards)})", ward_block))
    sections += [
        (f"Claim conditions ({len(conditions)})", condition_block),
        (f"Documents required ({len(documents)})",
         _documents_table(documents, forms_by_url)),
    ]
    if forms:
        sections.append((f"Forms to complete ({len(forms)})",
                         _forms_section(forms)))

    return (header + f'<div class="mt"{st(mt=5)}>'
            + ui.accordion(sections, multiple=True) + "</div>")


# The modal is one shell filled on demand: a payer master runs to a thousand
# packages and inlining a dialog per row would dwarf the page it sits on.
def plan_forms_page(request: Request):
    """Every questionnaire the payer shipped with this plan, searchable."""
    cid = request.params["cid"]
    plan_row = claims.plan(cid)
    if plan_row is None:
        return not_found(request, "Insurance plan")
    term = request.q("fq")
    forms = claims.search_forms(plan_row["id"], term)
    rows = [[
        ui.sub(ui.esc(f["title"]), f["form_id"] or ""),
        ui.label_chip("STG" if f["kind"] == "stgquestionnaire" else "Policy",
                      "warning" if f["kind"] == "stgquestionnaire" else "info"),
        str(len(claims.form_questions(f))),
        f'<a class="z-button z-button-secondary z-button-xsmall" '
        f'href="/claims/{cid}/plan/forms/{f["id"]}" '
        f'onclick="return emrPackage(this)">View</a>',
    ] for f in forms[:PLAN_PAGE]]
    note = ""
    if len(forms) > PLAN_PAGE:
        note = (f'<div class="mt"{st(mt=2)}>'
                + ui.muted(f"Showing the first {PLAN_PAGE} of {len(forms)} — "
                           "search to narrow it.") + "</div>")
    search = (f'<form method="get" action="/claims/{cid}/plan/forms" '
              f'class="display-flex gap items-end"{st(gap=2)}>'
              + ui.field("Search", ui.text_input(
                  "fq", term, placeholder="Form name or id",
                  attrs='style="min-width:16rem"'), compact=True)
              + ui.button("Search", style="z-button-primary", type_="submit",
                          ico="search") + "</form>")
    claim_no = db.scalar("SELECT claim_no FROM claim WHERE id = ?", (cid,))
    body = ui.card(f"{len(forms)} form(s)",
                   ui.table(["Form", "Kind", "Questions", ""], rows,
                            empty="Nothing matches that search.") + note,
                   actions=search) + PACKAGE_MODAL
    return render(request, "Plan forms", "claims", body,
                  breadcrumb=[("Claims", "/claims"),
                              (claim_no, f"/claims/{cid}?tab=plan"),
                              ("Forms", None)])


def form_detail(request: Request):
    """One questionnaire — the modal's contents, and a page on its own."""
    form = claims.plan_form(request.params["fid"])
    if form is None or form["claim_id"] != request.params["cid"]:
        return not_found(request, "Form")
    questions = claims.form_questions(form)
    body = _form_block(form, questions)
    if request.q("fragment"):
        return Response(f'<h2 class="z-h3">{ui.esc(form["title"])}</h2>'
                        + f'<div class="mt"{st(mt=4)}>{body}</div>')
    return render(request, form["title"], "claims",
                  ui.card(ui.esc(form["title"]), body),
                  breadcrumb=[("Claims", "/claims"),
                              ("Forms", f'/claims/{form["claim_id"]}'
                                        "/plan/forms"),
                              (form["form_id"] or "Form", None)])


# The kit ships z-modal / -dialog / -body / -footer but none of UIkit's
# close-button variants, so the close control is an ordinary z-button carrying
# the class the modal component looks for ([class*="z-modal-close"]).
PACKAGE_MODAL = """
<div id="pkg-modal" class="z-modal-container" z-modal>
  <div class="z-modal-dialog">
    <div class="z-modal-body" id="pkg-modal-body"></div>
    <div class="z-modal-footer display-flex justify-end">
      <button class="z-button z-button-secondary z-button-small z-modal-close"
              type="button">Close</button>
    </div>
  </div>
</div>
<script>
function emrPackage(link) {
  var box = document.getElementById("pkg-modal-body");
  box.innerHTML = "<p>Loading\u2026</p>";
  zUIkit.modal("#pkg-modal").show();
  fetch(link.href + "?fragment=1", {headers: {"X-Requested-With": "fetch"}})
    .then(function (r) { return r.ok ? r.text() : Promise.reject(r.status); })
    .then(function (html) { box.innerHTML = html; })
    .catch(function () { location.href = link.href; });
  return false;
}
</script>
"""


def _plan_actions(plan_row) -> str:
    """The plan's own forms, one click from its header."""
    if not claims.plan_forms(plan_row["id"]):
        return ""
    return ui.button("All forms", f'/claims/{plan_row["claim_id"]}/plan/forms',
                     ico="clipboard-list")


def _plan_pane(row, plan_row, poll_note: str, query: dict[str, str]) -> str:
    cid = row["id"]
    intro = (
        "<p>The payer's <strong>package master</strong> for this policy and "
        "this facility: every empanelled speciality, the packages under it "
        "and their rates. NanoEMR asks for it with an InsurancePlan discovery "
        "Task — a lookup that carries no clinical content, only the policy "
        "code and the facility's HFR ID — and the payer answers "
        "asynchronously, exactly like the eligibility check.</p>")
    first_time = plan_row is None
    fetch = ui.post_button(
        f"/claims/{cid}/plan",
        "Fetch insurance plan" if first_time else "Fetch again",
        style="z-button-primary",
        ico="download" if first_time else "refresh-cw",
        confirm="" if first_time else
                "Fetch the insurance plan again? The packages stored for this "
                "claim are replaced by whatever the payer returns.")

    if plan_row is None:
        return ui.card("Insurance plan", intro + (
            f'<div class="mt"{st(mt=4)}>' + fetch + "</div>"))

    summary = ui.card("Insurance plan", ui.dl([
        ("Plan", plan_row["plan_title"]),
        ("Plan identifier", plan_row["plan_identifier"]),
        ("Plan type", plan_row["plan_type"]),
        ("Overall sum insured", _money(plan_row["sum_insured"])),
        ("Asked with", ", ".join(part for part in [
            plan_row["policy_code"], plan_row["provider_id"]] if part) or None),
        ("Fetched at", ui.when(plan_row["fetched_at"])),
        ("Correlation", plan_row["correlation_id"]),
    ], cols=3), actions=_plan_actions(plan_row) + fetch)

    if plan_row["status"] == "fetching":
        return ui.card("Insurance plan", intro + (
            f'<p class="mt"{st(mt=3)}>Discovery Task sent to the payer; '
            "awaiting the on_request reply. Use Refresh to check for it.</p>")
            + (f'<div class="mt"{st(mt=2)}>{poll_note}</div>'
               if poll_note else "")
            + f'<div class="mt"{st(mt=3)}>' + ui.dl([
                ("Sent at", ui.when(plan_row["requested_at"])),
                ("Transaction", plan_row["txn_id"]),
                ("Correlation", plan_row["correlation_id"]),
            ], cols=3) + "</div>",
            actions=ui.post_button(
                f"/claims/{cid}/plan", "Fetch again", ico="refresh-cw",
                confirm="The payer has not returned the plan yet. Send the "
                        "discovery Task again?"))

    if plan_row["status"] == "error":
        return ui.card("Insurance plan", (
            f'<p class="color"{st(color="var(--z-danger)")}>'
            f'{ui.esc(plan_row["error_message"] or "The exchange failed.")}</p>'
            + f'<div class="mt"{st(mt=4)}>' + fetch + "</div>"))

    if plan_row["status"] == "empty":
        return ui.stack(summary, ui.card(None, (
            "<p>The payer returned no plan for this policy and facility. That "
            "is a legitimate answer, not a transport failure — it means no "
            "coverage matches this policy-provider pair, so the "
            "pre-authorisation falls back to the local HBP package master."
            "</p>")))

    return ui.stack(summary, _plan_table(plan_row, query)) + PACKAGE_MODAL


# Old spellings of `?tab=`, so a bookmarked link still lands somewhere
# sensible: `procedure` was the plan tab before the lines got their own.
TAB_ALIASES = {"procedure": "plan"}


def _preauth_open(row) -> bool:
    """Is the pre-authorisation tab there yet?

    It opens once the payer has ruled on the procedure set — the ruling is
    what says which documents and forms the dossier needs — or at once for
    a payer that does not rule. A pre-authorisation already sent keeps its
    tab whatever happened to the ruling since.
    """
    cid = row["id"]
    if claims.preauth(cid) is not None:
        return True
    if not payers.for_claim(row)["auth_requirements"]:
        return True
    ruling = claims.auth(cid)
    return ruling is not None and ruling["status"] == "ready"


# ------------------------------------------------ the procedure being done
def _lines_pane(row, plan_row) -> str:
    """The lines quoted from the payer's master — once there is a master."""
    if plan_row is not None and plan_row["status"] == "ready":
        return _lines_card(row)
    return ui.card(
        "Line items",
        "<p>The pre-authorisation quotes the payer's own packages and rates, "
        "so the insurance plan has to be fetched first.</p>"
        + f'<div class="mt"{st(mt=4)}>'
        + ui.button("Go to the insurance plan", f'/claims/{row["id"]}?tab=plan',
                    style="z-button-primary", ico="download") + "</div>")


# ------------------------------------------------------ preauth: link the stay
def _preauth_pane(row) -> str:
    """The dossier — the stay, the diagnoses, the team, the forms, the
    files — and the button that sends it."""
    if row["encounter_id"]:
        blocks = [_linked_card(row), _preauth_form(row),
                  _forms_card(row), _documents_card(row), _quote_card(row),
                  _submit_card(row)]
        if claims.preauth(row["id"]) is not None:
            # Once sent, what the payer said is the first thing to read —
            # the approval, the reference, the query — not the last.
            blocks = [_submit_card(row)] + blocks[:-1]
        return ui.stack(*[b for b in blocks if b])
    if row["status"] != "eligible":
        return ui.card("Pre-authorisation", (
            "<p>The pre-authorisation opens once the payer has confirmed the "
            "policy is <strong>eligible</strong>. Run the coverage "
            "eligibility check on the Eligibility tab first.</p>"))
    return _link_card(row)


# ------------------------------------------------------------- validating
def _validate_pane(row) -> str:
    """Ask the payer what the procedure set needs."""
    cid = row["id"]
    if not claims.lines(cid):
        return ui.card("Validate", (
            "<p>Choose the line items on the previous tab first. This tab "
            "then sends that procedure set to the payer, which rules on "
            "each line and names the documents and forms the "
            "pre-authorisation needs.</p>"))
    blocks = [_auth_card(row)]
    if _preauth_open(row):
        blocks.append(ui.card(None, (
            "<p>The procedure set is validated; the "
            "<strong>Pre-authorisation</strong> tab is open. Fill in the "
            "dossier there and send it.</p>"
            + f'<div class="mt"{st(mt=3)}>'
            + ui.button("Go to the pre-authorisation",
                        f"/claims/{cid}?tab=preauth",
                        style="z-button-primary", ico="arrow-right")
            + "</div>")))
    return ui.stack(*[b for b in blocks if b])


# ------------------------------------------------------- the payer's queries
def _communication_pane(row) -> str:
    """Every question the payer has asked on this episode, and its answer."""
    blocks = [_queries_card(row, "preauth"), _queries_card(row, "claim")]
    blocks = [b for b in blocks if b]
    if blocks:
        return ui.stack(*blocks)
    adapter = payers.for_claim(row)
    how = ("as a <code>CommunicationRequest</code> on a thread of its own, "
           "answered here with a <code>Communication</code>"
           if adapter["key"] == "kyrocare" else
           "inside the <code>ClaimResponse</code> — answered by submitting "
           "the leg again, with what was missing, from the Validate &amp; "
           "submit tab")
    return ui.card("Communication", (
        f"<p>No queries from the payer yet. {ui.esc(adapter['name'])} asks "
        f"for more {how}. Questions and answers on both the "
        "pre-authorisation and the claim collect here.</p>"))


def _link_card(row) -> str:
    if not row["abha_number"]:
        return ui.card("Link the admitted patient", (
            "<p>This policy carries no ABHA number, so it cannot be matched "
            "to an admission. Re-run the eligibility check — the payer's "
            "reply usually carries the beneficiary's ABHA.</p>"))
    matches = claims.linkable_admissions(row)
    table_rows = [[
        f'<a class="z-link" href="/patients/{m["patient_id"]}">'
        f'{ui.esc(m["patient_name"])}</a>',
        ui.esc(m["mrn"]),
        f'<a class="z-link" href="/ipd/{m["encounter_id"]}">'
        f'{ui.esc(m["encounter_no"])}</a>',
        ui.esc(m["ward"] or "—") + (f' / {ui.esc(m["bed"])}' if m["bed"] else ""),
        ui.when(m["period_start"]),
        ui.esc(m["doctor_name"] or "—"),
        ui.post_button(f'/claims/{row["id"]}/link', "Link",
                       style="z-button-primary", ico="link",
                       fields={"encounter_id": m["encounter_id"]}),
    ] for m in matches]
    return ui.card("Link the admitted patient", (
        f"<p>The policy is eligible for <strong>"
        f'{ui.esc(row["beneficiary_name"] or row["member_id"])}</strong> '
        f'(ABHA {ui.esc(row["abha_number"])}). Link the claim to their '
        "current IPD stay to start the pre-authorisation.</p>"
        + f'<div class="mt"{st(mt=4)}>'
        + ui.table(["Patient", "MRN", "Admission", "Ward / bed", "Admitted",
                    "Doctor", ""], table_rows,
                   empty="No current IPD admission matches this ABHA number. "
                         "Register the patient with this ABHA and admit them "
                         "first.")
        + "</div>"
        + (f'<div class="mt display-flex gap"{st(mt=3, gap=2)}>'
           + ui.button("Register patient", "/patients/new", ico="user-plus")
           + ui.button("New admission", "/ipd/new", ico="hospital")
           + "</div>" if not table_rows else "")))


def _linked_card(row) -> str:
    enc = db.one(
        "SELECT e.*, p.name AS patient_name, p.mrn, d.name AS doctor_name "
        "FROM encounter e JOIN patient p ON p.id = e.patient_id "
        "LEFT JOIN practitioner d ON d.id = e.practitioner_id "
        "WHERE e.id = ?", (row["encounter_id"],))
    if enc is None:
        return ""
    return ui.card("Linked admission", ui.dl([
        ("Patient", f'<a class="z-link" href="/patients/{enc["patient_id"]}">'
                    f'{ui.esc(enc["patient_name"])}</a>'),
        ("MRN", enc["mrn"]),
        ("Admission", f'<a class="z-link" href="/ipd/{enc["id"]}">'
                      f'{ui.esc(enc["encounter_no"])}</a>'),
        ("Ward / bed", f'{enc["ward"] or "—"} / {enc["bed"] or "—"}'),
        ("Admitted", ui.when(enc["period_start"])),
        ("Consultant", enc["doctor_name"]),
    ], cols=3), actions=ui.post_button(
        f'/claims/{row["id"]}/unlink', "Unlink", ico="unlink",
        confirm="Detach this claim from the admission? The preauth draft is "
                "kept."))


# --------------------------------------------------------- preauth: the draft
def _dx_options() -> list[tuple[str, str]]:
    """Diagnosis picker labelled by the ICD-10 coding the preauth quotes."""
    return [(r["code"], f'{r["alt_code"] or r["code"]} — '
                        f'{r["alt_display"] or r["display"]}')
            for r in db.terms("diagnosis")]


def _dx_row(values: dict | None = None) -> str:
    v = values or {}
    return ui.row_shell(ui.cell("Diagnosis (ICD-10)", ui.select(
        "pdx_code", _dx_options(), v.get("code", ""),
        blank="Select diagnosis"), "24rem"))


def _team_row(values: dict | None = None) -> str:
    v = values or {}
    return ui.row_shell(
        ui.cell("Doctor", ui.select("pct_doctor", doctor_options(),
                                    v.get("doctor", ""), blank="Select doctor"),
                "18rem")
        + ui.cell("Role", ui.select("pct_role", list(claims.CARE_ROLES.items()),
                                    v.get("role", "treating")), "12rem"))


def _item_row(values: dict | None = None) -> str:
    v = values or {}
    options = [(c["code"], f'{c["display"]} — ₹{c["price"]:,.0f}')
               for c in claims.charge_items()]
    return ui.row_shell(
        ui.cell("Item (fixed price)", ui.select(
            "pit_code", options, v.get("code", ""), blank="Select item"),
            "20rem")
        + ui.cell("Quantity", ui.text_input(
            "pit_qty", v.get("qty", ""), type_="number",
            attrs='min="0.5" step="any"'), "8rem"))


def _package_case(row) -> str:
    """The package side of the toggle.

    With the payer's master fetched a package case is quoted line by line —
    the procedure, the implants it approves, the ward tier — so this shows
    what is quoted and hands over to that screen. Only a claim with no plan
    falls back to picking one code out of the local HBP list.
    """
    cid = row["id"]
    plan_row = claims.plan(cid)
    if plan_row is None or plan_row["status"] != "ready":
        options = [(p["code"], f'{p["display"]} — ₹{p["rate"]:,.0f}')
                   for p in claims.packages(cid)]
        return ui.field("Package", ui.select("package_code", options,
                                             row["package_code"] or "",
                                             blank="Select package"),
                        help_text="The rate is fixed by the local HBP master. "
                                  "Fetch the payer's insurance plan to quote "
                                  "against its own packages instead.")

    chosen = claims.lines(cid)
    table = ui.table(
        ["Kind", "Item", "Rate", "Quantity", "Amount"],
        [[ui.label_chip(claims.LINE_KINDS.get(line["kind"], line["kind"]),
                        {"Procedure": "info", "Implant": "warning"}.get(
                            line["kind"], "")),
          ui.sub(ui.esc(line["display"] or line["code"]), line["code"]),
          _money(line["unit_price"]), f'{line["quantity"]:g}',
          _money(line["amount"])] for line in chosen],
        empty="Nothing quoted yet — choose the procedure being done, the "
              "implants the payer approves for it and the ward tier.")
    total = _line_total_row(cid) if chosen else ""
    return (table + total
            + f'<div class="display-flex justify-end mt"{st(mt=3)}>'
            + ui.button("Choose line items", f"/claims/{cid}/lines",
                        style="z-button-primary", ico="list") + "</div>")


def _preauth_form(row) -> str:
    children = claims.preauth_children(row["id"])
    case_type = row["case_type"] or "package"

    dx_rows = [_dx_row({"code": d["snomed_code"]})
               for d in children["diagnoses"]] or [_dx_row()]
    team_rows = [_team_row({"doctor": t["practitioner_id"], "role": t["role"]})
                 for t in children["care_team"]] or [_team_row()]
    recorded = claims.admission_dossier(row["encounter_id"])
    ipd_link = f'/ipd/{row["encounter_id"]}'
    if recorded["diagnoses"]:
        names = []
        for entry in recorded["diagnoses"]:
            term = db.term("diagnosis", entry.get("code") or "")
            names.append(f'{term["alt_code"] or term["code"]} — '
                         f'{term["alt_display"] or term["display"]}'
                         if term else f'{entry.get("icd10_code")} — '
                                      f'{entry.get("icd10_display") or ""}')
        diagnoses_block = ("<ul>" + "".join(f"<li>{ui.esc(n)}</li>" for n in names)
                           + "</ul>"
                           + ui.muted("Recorded on the admission; change it "
                                      f'<a class="z-link" href="{ipd_link}">'
                                      "there</a>."))
    else:
        diagnoses_block = (ui.muted("The admission has no diagnosis recorded "
                                    f'yet — add it <a class="z-link" '
                                    f'href="{ipd_link}">on the admission</a>, '
                                    "or pick one here for now.")
                           + ui.repeater("pdx", _dx_row(), dx_rows,
                                         "Add diagnosis"))
    if recorded["team"]:
        doctor = db.one("SELECT name FROM practitioner WHERE id = ?",
                        (int(recorded["team"][0]["doctor"]),))
        team_block = (f'<p>{ui.esc(doctor["name"] if doctor else "—")} '
                      f'<span class="z-muted">— treating doctor</span></p>'
                      + ui.muted("The consultant on the admission; change it "
                                 f'<a class="z-link" href="{ipd_link}">there'
                                 "</a>."))
    else:
        team_block = (ui.muted("The admission names no consultant yet — set "
                               f'one <a class="z-link" href="{ipd_link}">on '
                               "the admission</a>, or add the doctor here.")
                      + ui.repeater("pct", _team_row(), team_rows, "Add doctor"))
    item_rows = [_item_row({"code": i["code"], "qty": i["quantity"]})
                 for i in children["items"]] or [_item_row()]

    def case_radio(value: str, label: str) -> str:
        checked = " checked" if case_type == value else ""
        return (f'<label class="display-flex items-center gap"{st(gap=2)}>'
                f'<input class="z-radio" type="radio" name="case_type" '
                f'value="{value}"{checked} onchange="emrCaseType(this.value)">'
                f'<span>{ui.esc(label)}</span></label>')

    package_box = (
        f'<div id="pkg-box"{"" if case_type == "package" else " style=display:none"}>'
        + _package_case(row) + "</div>")
    items_box = (
        f'<div id="items-box"{"" if case_type == "nonpackage" else " style=display:none"}>'
        + ui.field("Items", "", help_text="Prices are fixed by the charge "
                                          "master; you choose the quantity.")
        + ui.repeater("pit", _item_row(), item_rows, "Add item")
        + "</div>")

    saved = ""
    if row["preauth_saved_at"]:
        saved = f'<div class="mb"{st(mb=4)}>' + ui.dl([
            ("Last saved", ui.when(row["preauth_saved_at"])),
            ("Case", claims.CASE_TYPES.get(row["case_type"] or "", "—")),
            ("Estimated amount",
             f'₹{row["preauth_total"]:,.2f}' if row["preauth_total"] else "—"),
        ], cols=3) + "</div>"

    sections = ui.accordion([
        ("Stay", ui.grid(
            ui.field("Admission date", ui.text_input(
                "admission_date", row["admission_date"] or "", type_="date",
                required=True), required=True),
            ui.field("Provisional discharge date", ui.text_input(
                "expected_discharge_date", row["expected_discharge_date"] or "",
                type_="date")),
            cols=2)),
        ("Diagnoses (ICD-10)", diagnoses_block),
        ("Treating doctor", team_block),
        ("Case and estimate", (
            f'<div class="display-flex gap flex-wrap mb"{st(gap=4, mb=4)}>'
            + case_radio("package", claims.CASE_TYPES["package"])
            + case_radio("nonpackage", claims.CASE_TYPES["nonpackage"])
            + "</div>" + package_box + items_box)),
    ], multiple=True)

    toggle = ("<script>function emrCaseType(value) {"
              'document.getElementById("pkg-box").style.display = '
              'value === "package" ? "" : "none";'
              'document.getElementById("items-box").style.display = '
              'value === "nonpackage" ? "" : "none";}</script>')

    return (f'<form method="post" action="/claims/{row["id"]}/preauth">'
            + ui.card("Pre-authorisation draft", saved + sections + toggle,
                      footer=(f'<div class="display-flex justify-end"{st()}>'
                              + ui.button("Save preauth draft",
                                          style="z-button-primary",
                                          size="z-button-medium",
                                          type_="submit", ico="save")
                              + "</div>"))
            + "</form>")


# ------------------------------------------------------ the small exchanges
def _enquiry_rows(cid: int, kind: str, stage: str = "") -> str:
    """What the payer answered to the asks of one kind, newest first."""
    asked = claims.enquiries(cid, kind, stage)
    if not asked:
        return ""
    rows = []
    for e in asked[:5]:
        if e["status"] == "asking":
            said = ui.label_chip("Asking — use Refresh", "info")
        elif e["status"] == "error":
            said = (ui.label_chip("Failed", "danger") + " "
                    + ui.esc(e["error_message"] or ""))
        else:
            tone = {"approved": "success", "partial": "warning",
                    "rejected": "danger", "queried": "warning",
                    "reopened": "success", "refused": "danger",
                    "not-found": "danger"}.get((e["answer"] or "").lower(), "info")
            said = ui.label_chip(e["answer"] or "—", tone)
            if e["amount"] is not None:
                said += f" {_money(e['amount'])}"
            if e["detail"]:
                said += f' <span class="z-muted">{ui.esc(e["detail"])}</span>'
        rows.append([ui.when(e["requested_at"]), said,
                     ui.when(e["answered_at"]) if e["answered_at"] else "—"])
    return (f'<div class="mt"{st(mt=3)}>'
            + ui.table(["Asked", "The payer said", "Answered"], rows, empty="")
            + "</div>")


def _status_button(cid: int, stage: str) -> str:
    return ui.post_button(f"/claims/{cid}/status", "Ask where it stands",
                          ico="help-circle", fields={"stage": stage})


def _reprocess_form(cid: int, state) -> str:
    """Appeal a decided claim: a Task coded reprocess, with the reason."""
    if state is None or state["status"] not in ("rejected", "partial", "approved"):
        return ""
    if claims.paid_total(cid):
        return ""
    return (f'<form method="post" action="/claims/{cid}/reprocess" class="mt"'
            f'{st(mt=4)}>'
            + ui.field("Ask the payer to reprocess the claim", ui.textarea(
                "reason", "", rows=2,
                placeholder="Why the verdict should be looked at again"),
                help_text="A Task coded reprocess. The payer reopens the "
                          "claim for a person, and the new verdict arrives "
                          "on the claim's own thread.")
            + f'<div class="display-flex justify-end"{st()}>'
            + ui.button("Send reprocess request", style="z-button-primary",
                        type_="submit", ico="rotate-ccw")
            + "</div></form>"
            + _enquiry_rows(cid, "reprocess"))


# ----------------------------------------------------- preauth: the documents
def _size_label(size: int) -> str:
    if size >= 1024 * 1024:
        return f"{size / (1024 * 1024):.1f} MB"
    return f"{max(size, 1024) / 1024:.0f} KB"


def _filed_as_select(wanted) -> str:
    """The document-type picker on every upload.

    What the payer's ruling named comes first, then the documents a
    hospital attaches most, with the clinical document selected — the case
    sheet is what most uploads are. Nothing has to be filed as "other".
    """
    options = []
    seen = set()
    for need in wanted or []:
        if not need["code"] or need["code"] in seen:
            continue
        seen.add(need["code"])
        options.append((need["code"], f'{need["code"]} — {need["display"]}'))
    for code, label in claims.COMMON_DOCUMENTS:
        if code in seen:
            continue
        seen.add(code)
        options.append((code, f"{code} — {label}"))
    return ui.select("code", options, claims.DEFAULT_DOCUMENT_CODE)


def _required_documents_form(cid: int, wanted) -> str:
    """A file picker per document the payer named, like a form's file answer.

    Uploading here files the PDF under the payer's own code, so the preauth
    quotes `MAND0671` back rather than filing everything as "other document"
    and leaving the payer to match them up by label.
    """
    if not wanted:
        return ""
    rows = []
    for need in wanted:
        attached = claims.document_for(cid, need["code"])
        current = ui.muted("not attached yet")
        if attached is not None:
            current = (f'<a class="z-link" href="/claims/{cid}/documents/'
                       f'{attached["id"]}" target="_blank">'
                       f'{ui.esc(attached["filename"])}</a>')
        rows.append([
            ui.esc(need["code"] or "—"),
            ui.sub(ui.esc(need["display"] or "—"), need["for_code"] or ""),
            current,
            f'<input class="z-input" type="file" '
            f'name="req_{ui.esc(need["code"])}" '
            'accept="application/pdf,image/*">',
        ])
    return (f'<form method="post" action="/claims/{cid}/documents/required" '
            'enctype="multipart/form-data">'
            + ui.table(["Code", "Document", "Attached", "Choose a PDF"], rows,
                       empty="")
            + f'<div class="display-flex items-center justify-between gap mt"'
            f'{st(gap=3, mt=3)}>'
            + ui.muted("Named by the payer's authorisation-requirements "
                       "ruling for this procedure set. Re-choosing a file "
                       "replaces what is attached for that code.")
            + ui.button("Attach chosen files", style="z-button-primary",
                        type_="submit", ico="paperclip")
            + "</div></form>"
            + f'<div class="mt mb"{st(mt=5, mb=4)}></div>')


def _documents_card(row) -> str:
    cid = row["id"]
    docs = claims.documents(cid)
    # Once the payer has ruled on the procedure set it has said which
    # documents it wants *now*; the rest it asked for belong with the claim.
    wanted = claims.required_documents(cid)
    asked = _required_documents_form(cid, wanted)
    table_rows = [[
        f'<a class="z-link" href="/claims/{cid}/documents/{d["id"]}" '
        f'target="_blank">{ui.esc(d["filename"])}</a>',
        ui.sub(ui.esc(d["label"] or "—"), d["code"] or ""),
        ui.esc(claims.DOCUMENT_TYPES.get(d["content_type"],
                                         d["content_type"])),
        _size_label(d["size"]),
        ui.when(d["uploaded_at"]),
        ui.confirm_form(f"/claims/{cid}/documents/{d['id']}/delete", "Remove",
                        "Remove this document from the claim?"),
    ] for d in docs]

    upload = (f'<form method="post" action="/claims/{cid}/documents" '
              'enctype="multipart/form-data" '
              f'class="display-flex gap items-end flex-wrap"{st(gap=2)}>'
              + ui.cell("Files (PDF or image)",
                        '<input class="z-input" type="file" name="file" '
                        'multiple required accept="application/pdf,image/*">',
                        "16rem")
              + ui.cell("Filed as", _filed_as_select(wanted), "18rem")
              + ui.cell("Label", ui.text_input(
                  "label", "", placeholder="e.g. Admission note, ID card"),
                  "14rem")
              + ui.button("Attach", style="z-button-primary", type_="submit",
                          ico="paperclip")
              + "</form>")

    return ui.card("Supporting documents",
                   asked
                   + ui.table(["File", "Filed as", "Type", "Size", "Uploaded",
                               ""],
                              table_rows,
                              empty="No documents attached yet. The payer "
                                    "expects the admission note and the "
                                    "beneficiary's ID as a minimum.")
                   + f'<div class="mt"{st(mt=4)}>{upload}</div>')


# ------------------------------------------------------------------- handlers
def _back(cid: int) -> str:
    return f"/claims/{cid}?tab=preauth"


def link(request: Request):
    cid = request.params["cid"]
    try:
        claims.link_admission(cid, request.f_int("encounter_id") or 0)
    except ValueError as error:
        return redirect(_back(cid), str(error), "danger")
    return redirect(_back(cid), "Admission linked to the claim.")


def unlink(request: Request):
    cid = request.params["cid"]
    if claims.claim(cid) is None:
        return not_found(request, "Claim")
    claims.unlink_admission(cid)
    return redirect(_back(cid), "Admission unlinked.")


def save_preauth(request: Request):
    cid = request.params["cid"]
    try:
        claims.save_preauth(
            cid,
            {"admission_date": request.f("admission_date"),
             "expected_discharge_date": request.f("expected_discharge_date"),
             "case_type": request.f("case_type"),
             "package_code": request.f("package_code")},
            collect_rows(request, "pdx", ["code"]),
            collect_rows(request, "pct", ["doctor", "role"]),
            collect_rows(request, "pit", ["code", "qty"]))
    except ValueError as error:
        return redirect(_back(cid), str(error), "danger")
    return redirect(_back(cid), "Pre-authorisation draft saved.")


def upload_document(request: Request):
    """Attach any number of files to one leg — the pre-auth unless told
    otherwise — under the payer's code or as "other document"."""
    cid = request.params["cid"]
    stage = "claim" if request.f("stage") == "claim" else "preauth"
    back = f"/claims/{cid}?tab=claim" if stage == "claim" else _back(cid)
    uploads = request.files_all("file")
    if not uploads:
        return redirect(back, "Choose a PDF or image to attach.", "danger")
    label = request.f("label")
    code = request.f("code")
    saved = 0
    try:
        for upload in uploads:
            claims.add_document(cid, upload["filename"],
                                upload["content_type"], upload["data"], label,
                                code=code, stage=stage)
            saved += 1
    except ValueError as error:
        note = f" ({saved} of {len(uploads)} attached before the failure.)" \
            if saved else ""
        return redirect(back, str(error) + note, "danger")
    return redirect(back, f"{saved} document(s) attached.")


def upload_required_documents(request: Request):
    """Attach a PDF against each payer requirement a file was chosen for.

    One form, one file field per requirement, so an operator fills what they
    have and leaves the rest — a field nobody chose a file for is skipped, not
    treated as clearing what is already attached.
    """
    cid = request.params["cid"]
    saved = 0
    try:
        for field, files in (request.files_map() or {}).items():
            if not field.startswith("req_"):
                continue
            upload = next((f for f in files if f.get("data")), None)
            if upload is None:
                continue
            claims.attach_required_document(cid, field[4:],
                                            upload["filename"],
                                            upload["content_type"],
                                            upload["data"])
            saved += 1
    except ValueError as error:
        note = f" ({saved} attached before the failure.)" if saved else ""
        return redirect(_back(cid), str(error) + note, "danger")
    if not saved:
        return redirect(_back(cid), "Choose a PDF for at least one of them.",
                        "danger")
    return redirect(_back(cid), f"{saved} document(s) attached.")


def view_document(request: Request):
    doc = claims.document(request.params["did"])
    if doc is None or doc["claim_id"] != request.params["cid"]:
        return not_found(request, "Document")
    filename = doc["filename"].replace('"', "")
    return Response(doc["data"], content_type=doc["content_type"],
                    headers=[("Content-Disposition",
                              f'inline; filename="{filename}"')])


def remove_document(request: Request):
    cid = request.params["cid"]
    doc = claims.document(request.params["did"])
    if doc is None or doc["claim_id"] != cid:
        return not_found(request, "Document")
    claims.delete_document(doc["id"])
    return redirect(_back(cid), "Document removed.")


# ======================================================= preauth: the lines
def _line_total_row(claim_id: int) -> str:
    return (f'<div class="display-flex justify-end mt"{st(mt=3)}>'
            f'<strong>Total {_money(claims.lines_total(claim_id))}</strong>'
            "</div>")


def lines_page(request: Request):
    """Choose what the preauth quotes: procedures, implants, ward tiers."""
    cid = request.params["cid"]
    row = claims.claim(cid)
    if row is None:
        return not_found(request, "Claim")
    plan_row = claims.plan(cid)
    claim_no = row["claim_no"]
    breadcrumb = [("Claims", "/claims"), (claim_no, f"/claims/{cid}?tab=lines"),
                  ("Line items", None)]

    if plan_row is None or plan_row["status"] != "ready":
        return render(request, "Line items", "claims", ui.card(
            "Line items", "<p>The preauth quotes the payer's own packages and "
            "rates, so the insurance plan has to be fetched first.</p>"
            + f'<div class="mt"{st(mt=4)}>'
            + ui.button("Go to the insurance plan",
                        f"/claims/{cid}?tab=plan", style="z-button-primary",
                        ico="download") + "</div>"), breadcrumb=breadcrumb)

    body = ui.stack(_chosen_card(cid), _suggested_card(cid),
                    _catalogue_card(cid, plan_row,
                                    {"q": request.q("q"),
                                     "cat": request.q("cat"),
                                     "kind": request.q("kind")}))
    return render(request, f"Line items — {claim_no}", "claims", body,
                  breadcrumb=breadcrumb,
                  actions=ui.button("Back to the claim",
                                    f"/claims/{cid}?tab=lines", ico="undo"))


def _chosen_card(cid: int) -> str:
    chosen = claims.lines(cid)
    rows = [[
        ui.label_chip(claims.LINE_KINDS.get(line["kind"], line["kind"]),
                      {"Procedure": "info", "Implant": "warning"}.get(
                          line["kind"], "")),
        ui.sub(ui.esc(line["display"] or line["code"]), line["code"]),
        ui.esc(line["category_display"] or "—"),
        # A package the plan prices at zero is billed at the hospital's
        # own price — PMJAY prices 166 of them that way — so the rate is an
        # input on exactly those lines and a figure on the rest.
        ui.text_input(f'price_{line["id"]}', line["unit_price"],
                      type_="number", attrs='min="0" step="0.01" '
                                            'style="width:8rem"')
        if claims.price_is_open(cid, line) else _money(line["unit_price"]),
        ui.text_input(f'qty_{line["id"]}', int(line["quantity"] or 1),
                      type_="number",
                      attrs='min="1" step="1" style="width:6rem"'),
        _money(line["amount"]),
        ui.confirm_form(f'/claims/{cid}/lines/{line["id"]}/delete', "Remove",
                        "Take this line off the preauth?"),
    ] for line in chosen]
    table = ui.table(["Kind", "Item", "Speciality", "Rate", "Quantity",
                      "Amount", ""], rows,
                     empty="Nothing quoted yet — add the procedure being done "
                           "from the catalogue below.")
    if not chosen:
        return ui.card("On this preauth", table)
    return (f'<form method="post" action="/claims/{cid}/lines/quantities">'
            + ui.card("On this preauth", table + _line_total_row(cid),
                      footer=(f'<div class="display-flex justify-end"{st()}>'
                              + ui.button("Update lines",
                                          style="z-button-primary",
                                          type_="submit", ico="save")
                              + "</div>"))
            + "</form>")


def _add_button(cid: int, kind: str, code: str, parent: str = "",
                label: str = "Add") -> str:
    fields = {"kind": kind, "code": code}
    if parent:
        fields["parent_code"] = parent
    return ui.post_button(f"/claims/{cid}/lines", label,
                          style="z-button-primary", ico="plus", fields=fields)


def _suggested_card(cid: int) -> str:
    """What the payer says goes with the procedures already chosen."""
    suggestions = claims.line_suggestions(cid)
    implants, tiers = suggestions["implants"], suggestions["tiers"]
    if not implants and not tiers:
        return ""
    blocks = []
    if implants:
        blocks.append(("Implants approved for these procedures",
                       ui.table(["Code", "Implant", "Rate", ""], [[
                           ui.esc(i["code"]), ui.esc(i["display"] or "—"),
                           _money(i["rate"]),
                           _add_button(cid, "Implant", i["code"]),
                       ] for i in implants], empty="")))
    if tiers:
        blocks.append(("Ward and ICU tiers", ui.table(
            ["Code", "Tier", "For", "Rate", ""], [[
                ui.esc(t["code"]), ui.esc(t["label"] or "—"),
                ui.sub(ui.esc(t["parent_display"] or ""), t["parent"]),
                _money(t["rate"]),
                _add_button(cid, "Stratification", t["code"], t["parent"]),
            ] for t in tiers], empty="")))
    return ui.card("Suggested by the payer's plan", ui.accordion(blocks,
                                                                multiple=True))


def _catalogue_card(cid: int, plan_row, query: dict[str, str]) -> str:
    benefits = claims.plan_benefits(plan_row["id"], query["cat"], query["q"],
                                    query["kind"])
    shown = benefits[:PLAN_PAGE]
    rows = [[
        ui.esc(b["code"]),
        ui.esc(b["display"] or b["code"]),
        ui.esc(b["category_display"] or "—"),
        ui.label_chip(b["kind"] or "—",
                      "info" if b["kind"] == "Procedure" else "warning"),
        _money(b["rate"]),
        _add_button(cid, b["kind"] or "Procedure", b["code"]),
    ] for b in shown]
    note = ""
    if len(benefits) > len(shown):
        note = (f'<div class="mt"{st(mt=2)}>'
                + ui.muted(f"Showing the first {len(shown)} of "
                           f"{len(benefits)} — search to narrow it.")
                + "</div>")
    categories = claims.plan_categories(plan_row["id"])
    options = [(c["category_code"] or "",
                f'{c["category_display"] or c["category_code"] or "—"} '
                f'({c["n"]})') for c in categories]
    filters = (f'<form method="get" action="/claims/{cid}/lines" '
               f'class="display-flex gap items-end flex-wrap mb"'
               f'{st(gap=2, mb=4)}>'
               + ui.field("Search", ui.text_input(
                   "q", query["q"], placeholder="Procedure name or code",
                   attrs='style="min-width:18rem"'), compact=True)
               + ui.field("Speciality", ui.select("cat", options, query["cat"],
                                                  blank="All specialities"),
                          compact=True)
               + ui.field("Type", ui.select("kind", BENEFIT_KINDS,
                                            query["kind"], blank="Any type"),
                          compact=True)
               + ui.button("Search", style="z-button-primary", type_="submit",
                           ico="search")
               + "</form>")
    return filters + ui.card(
        f"{len(benefits)} in the payer's package master",
        ui.table(["Code", "Package", "Speciality", "Type", "Rate", ""], rows,
                 empty="Nothing matches that search.") + note)


def add_line(request: Request):
    cid = request.params["cid"]
    try:
        claims.add_line(cid, request.f("kind"), request.f("code"),
                        request.f("parent_code"))
    except ValueError as error:
        return redirect(f"/claims/{cid}/lines", str(error), "danger")
    return redirect(f"/claims/{cid}/lines", f'{request.f("code")} added.')


def save_quantities(request: Request):
    cid = request.params["cid"]
    wanted, prices = {}, {}
    for line in claims.lines(cid):
        value = request.f(f'qty_{line["id"]}')
        if value != "":
            wanted[line["id"]] = value
        price = request.f(f'price_{line["id"]}')
        if price != "":
            prices[line["id"]] = price
    try:
        claims.save_quantities(cid, wanted, prices)
    except ValueError as error:
        return redirect(f"/claims/{cid}/lines", str(error), "danger")
    return redirect(f"/claims/{cid}/lines", "Lines updated.")


def remove_line(request: Request):
    cid = request.params["cid"]
    claims.remove_line(cid, request.params["lid"])
    return redirect(f"/claims/{cid}/lines", "Line removed.")


def save_answers(request: Request):
    """Store one form's answers, uploading any file answers as they arrive.

    A file field posts empty when nothing was chosen, and an empty answer
    clears its row — so an untouched file question must be left out entirely
    rather than passed through, or re-saving the form would detach the file
    already given for it.
    """
    cid = request.params["cid"]
    url = request.f("form_url")
    given = {name[3:]: request.f(name)
             for name in request.field_names("qa_")}
    uploaded = 0
    try:
        for field, files in (request.files_map() or {}).items():
            if not field.startswith("qa_"):
                continue
            link_id = field[3:]
            given.pop(link_id, None)
            upload = next((f for f in files if f.get("data")), None)
            if upload is None:
                continue
            claims.save_answer_file(cid, url, link_id,
                                    _question_text(cid, url, link_id),
                                    upload["filename"], upload["content_type"],
                                    upload["data"])
            uploaded += 1
        claims.save_answers(cid, url, given)
    except ValueError as error:
        return redirect(f"/claims/{cid}?tab=preauth", str(error), "danger")
    note = f" {uploaded} file(s) attached." if uploaded else ""
    return redirect(f"/claims/{cid}?tab=preauth", "Answers saved." + note)


def _question_text(cid: int, form_url: str, link_id: str) -> str:
    """The question a file answer belongs to, so its document is labelled."""
    plan_row = claims.plan(cid)
    if plan_row is None:
        return ""
    for form in claims.plan_forms(plan_row["id"], [form_url]):
        for question in claims.form_questions(form):
            if str(question.get("linkId")) == str(link_id):
                return question.get("text") or ""
    return ""


def cancel_preauth(request: Request):
    cid = request.params["cid"]
    try:
        claims.cancel_preauth(cid, request.f("reason"), request.f("note"))
    except ValueError as error:
        return redirect(f"/claims/{cid}?tab=preauth", str(error), "danger")
    return redirect(f"/claims/{cid}?tab=preauth",
                    "Cancellation sent to the payer.")


def submit_preauth(request: Request):
    cid = request.params["cid"]
    try:
        claims.submit_preauth(cid)
    except ValueError as error:
        return redirect(f"/claims/{cid}?tab=preauth", str(error), "danger")
    return redirect(f"/claims/{cid}?tab=preauth",
                    "Pre-authorisation submitted to the payer.")


def ask_predetermination(request: Request):
    cid = request.params["cid"]
    try:
        claims.ask_predetermination(cid)
    except ValueError as error:
        return redirect(f"/claims/{cid}?tab=preauth", str(error), "danger")
    return redirect(f"/claims/{cid}?tab=preauth",
                    "Predetermination sent to the payer.")


# =============================================== preauth: quoting and sending
def _lines_card(row) -> str:
    """What this preauth quotes, and the way in to change it."""
    cid = row["id"]
    plan_row = claims.plan(cid)
    if plan_row is None or plan_row["status"] != "ready":
        return ""
    chosen = claims.lines(cid)
    table = ui.table(
        ["Kind", "Item", "Rate", "Quantity", "Amount"],
        [[ui.label_chip(claims.LINE_KINDS.get(l["kind"], l["kind"]),
                        {"Procedure": "info", "Implant": "warning"}.get(
                            l["kind"], "")),
          ui.sub(ui.esc(l["display"] or l["code"]), l["code"]),
          _money(l["unit_price"]), f'{l["quantity"]:g}', _money(l["amount"])]
         for l in chosen],
        empty="Nothing quoted yet. The payer prices this preauth from its own "
              "package master — choose the procedure, the implants it "
              "approves and the ward tier.")
    total = _line_total_row(cid) if chosen else ""
    return ui.card("Line items", table + total,
                   actions=ui.button("Choose line items",
                                     f"/claims/{cid}/lines",
                                     style="z-button-primary", ico="list"))


def _forms_card(row) -> str:
    """The payer's questionnaires for the chosen lines, as forms to answer."""
    cid = row["id"]
    forms = claims.required_forms(cid, "preauth")
    if not forms:
        return ""
    return _forms_accordion(cid, forms, "Forms the payer requires")


def _forms_accordion(cid: int, forms, title: str) -> str:
    """One answerable form per questionnaire, whichever stage wants it."""
    given = claims.answers(cid)
    blocks = []
    for form in forms:
        questions = claims.form_questions(form)
        answered = sum(1 for q in questions
                       if given.get(f'{form["url"]}|{q.get("linkId")}'))
        controls = []
        for question in questions:
            link_id = question.get("linkId")
            value = given.get(f'{form["url"]}|{link_id}', "")
            controls.append(ui.field(
                question.get("text") or link_id,
                _question_control(cid, question, value),
                help_text=_question_label(question),
                required=question.get("required", False)))
        # A file answer means the form posts multipart, so every form does.
        body = (f'<form method="post" action="/claims/{cid}/forms" '
                'enctype="multipart/form-data">'
                f'<input type="hidden" name="form_url" '
                f'value="{ui.esc(form["url"])}">'
                + ui.grid(*controls, cols=2)
                + f'<div class="display-flex justify-end mt"{st(mt=4)}>'
                + ui.button("Save answers", style="z-button-primary",
                            type_="submit", ico="save")
                + "</div></form>")
        blocks.append((f'{form["title"]} — {answered}/{len(questions)} '
                       "answered", body))
    return ui.card(title, ui.accordion(blocks, multiple=True))


def _quote_card(row) -> str:
    """A predetermination: the dossier priced by the payer before it is
    committed — what the policy would allow and what it would cut."""
    cid = row["id"]
    quotes = claims.predeterminations(cid)
    ask = ui.post_button(
        f"/claims/{cid}/predetermination",
        "Ask for a quote" if not quotes else "Ask again",
        ico="calculator",
        confirm="Send the dossier to the payer as a predetermination?"
        if quotes else "")
    if not quotes:
        return ui.card("Predetermination", (
            "<p>The same bundle can go first as a "
            "<code>predetermination</code>: the payer prices it at once and "
            "answers with what the policy would allow and what it would cut. "
            "It binds nobody and opens no case.</p>"), actions=ask)
    latest = quotes[0]
    if latest["status"] == "asking":
        return ui.card("Predetermination", (
            "<p>Sent; awaiting the payer's quote. Use Refresh to check for "
            "it.</p>" + f'<div class="mt"{st(mt=3)}>' + ui.dl([
                ("Sent at", ui.when(latest["requested_at"])),
                ("Requested", _money(latest["requested_amount"])),
                ("Transaction", latest["txn_id"]),
                ("Correlation", latest["correlation_id"]),
            ], cols=3) + "</div>"))
    if latest["status"] == "error":
        return ui.card("Predetermination", (
            f'<p class="color"{st(color="var(--z-danger)")}>'
            f'{ui.esc(latest["error_message"] or "The exchange failed.")}</p>'),
            actions=ask)
    stats = (f'<div class="display-grid gap sm:grid-cols"'
             + st(gap=4, sm_grid_cols=2) + ">"
             + ui.stat("Requested", _money(latest["requested_amount"]),
                       "file-text")
             + ui.stat("Would be allowed", _money(latest["allowed_amount"]),
                       "calculator",
                       "success" if latest["outcome"] == "complete" else "warning")
             + "</div>")
    return ui.card("Predetermination", stats + f'<div class="mt"{st(mt=4)}>'
                   + ui.dl([
                       ("Outcome", f'{latest["outcome"] or "—"}'
                                   f' · {latest["adjudication"] or "—"}'),
                       ("Disposition", latest["disposition"]),
                       ("Quoted at", ui.when(latest["answered_at"])),
                       ("Quotes asked", str(len(quotes))),
                   ], cols=2) + "</div>"
                   + ui.muted("A quote binds nobody: the pre-authorisation "
                              "below is what commits the payer."),
                   actions=ask)


def _submit_card(row) -> str:
    """Send the preauth, and show what came back."""
    cid = row["id"]
    sent = claims.preauth(cid)
    total = claims.lines_total(cid)
    send = ui.post_button(
        f"/claims/{cid}/submit",
        "Submit to payer" if sent is None else "Submit again",
        style="z-button-primary", ico="send",
        confirm="Send this pre-authorisation to the payer?"
        if sent is not None else "")

    if sent is None:
        return ui.card("Pre-authorisation", (
            f"<p>Everything above goes to the payer as one FHIR Claim bundle: "
            f"the beneficiary, the admission, the diagnoses, the care team, "
            f"the {len(claims.lines(cid))} quoted line(s) at "
            f"{_money(total)}, the attached documents and the answered "
            "forms.</p>"), actions=send)

    if sent["status"] == "submitting":
        return ui.card("Pre-authorisation", (
            "<p>Submitted; awaiting the payer's <code>on_submit</code> reply. "
            "Use Refresh to check for it, or ask the payer where it stands.</p>"
            + f'<div class="mt"{st(mt=3)}>' + ui.dl([
                ("Sent at", ui.when(sent["submitted_at"])),
                ("Requested", _money(sent["requested_amount"])),
                ("Transaction", sent["txn_id"]),
                ("Correlation", sent["correlation_id"]),
            ], cols=3) + "</div>"
            + _enquiry_rows(cid, "status", "preauth")
            + _cancel_form(cid, sent)),
            actions=_status_button(cid, "preauth") + send)

    if sent["status"] == "cancelling":
        return ui.card("Pre-authorisation", (
            "<p>Cancellation sent to the payer; awaiting its answer to the "
            "Task. Use Refresh to check for it.</p>"
            + f'<div class="mt"{st(mt=3)}>' + ui.dl([
                ("Reason", claims.CANCEL_REASONS.get(sent["cancel_reason"],
                                                     sent["cancel_reason"])),
                ("Note", sent["cancel_note"]),
                ("Sent at", ui.when(sent["cancel_requested_at"])),
                ("Transaction", sent["cancel_txn_id"]),
                ("Correlation", sent["cancel_correlation_id"]),
            ], cols=3) + "</div>"))

    if sent["status"] == "cancelled":
        claim_no = db.scalar("SELECT claim_no FROM claim WHERE id = ?", (cid,))
        retired = ""
        if sent["claim_ref"] and sent["claim_ref"] != claim_no:
            retired = (f'<p class="mt"{st(mt=2)}>The payer holds '
                       f'<strong>{ui.esc(sent["claim_ref"])}</strong> against '
                       "the withdrawn pre-authorisation, so the episode "
                       f'carries on as <strong>{ui.esc(claim_no)}</strong> — '
                       "anything sent under the old number would be a "
                       "duplicate of a cancelled case.</p>")
        return ui.card("Pre-authorisation", (
            "<p>The payer accepted the cancellation; this "
            "pre-authorisation is withdrawn.</p>" + retired
            + f'<div class="mt"{st(mt=3)}>' + ui.dl([
                ("Reason", claims.CANCEL_REASONS.get(sent["cancel_reason"],
                                                     sent["cancel_reason"])),
                ("Note", sent["cancel_note"]),
                ("Withdrawn under", sent["claim_ref"]),
                ("Pre-auth reference", sent["preauth_ref"]),
                ("Cancelled at", ui.when(sent["settled_at"])),
                ("Disposition", sent["disposition"]),
            ], cols=3) + "</div>"), actions=send)

    if sent["status"] == "error":
        return ui.card("Pre-authorisation", (
            f'<p class="color"{st(color="var(--z-danger)")}>'
            f'{ui.esc(sent["error_message"] or "The exchange failed.")}</p>'),
            actions=send)

    stats = (f'<div class="display-grid gap sm:grid-cols lg:grid-cols"'
             + st(gap=4, sm_grid_cols=2, lg_grid_cols=3) + ">"
             + ui.stat("Requested", _money(sent["requested_amount"]),
                       "file-text")
             + ui.stat("Approved", _money(sent["approved_amount"]),
                       "badge-check",
                       "success" if sent["status"] == "approved" else "warning")
             + ui.stat("Pre-auth reference", sent["preauth_ref"] or "—",
                       "hash", "info")
             + "</div>")
    # Decided. Adding a line after that is an enhancement — the hospital
    # coming back for more — and it is what the button sends; with nothing
    # added there is nothing to send again.
    added = claims.enhancement_lines(cid)
    enhancement = ""
    actions = ""
    if added:
        more = round(sum((r["amount"] or 0) for r in added), 2)
        enhancement = (f'<div class="mt"{st(mt=3)}>'
                       + ui.badge(f"Enhancement pending: {len(added)} new "
                                  f"line(s), {_money(more)}", "warning")
                       + "<ul>" + "".join(
                           f'<li>{ui.esc(r["display"] or r["code"])} '
                           f'<span class="z-muted">× {int(r["quantity"] or 1)}'
                           f' · {_money(r["amount"])}</span></li>'
                           for r in added) + "</ul>"
                       + ui.muted("Added since the payer decided. Sending "
                                  "the enhancement asks for these against "
                                  "the same pre-authorisation.") + "</div>")
        actions = ui.post_button(
            f"/claims/{cid}/submit",
            f"Submit enhancement ({_money(more)})",
            style="z-button-primary", ico="send",
            confirm="Send the added lines to the payer as an enhancement "
                    "of this pre-authorisation?")
    elif sent["status"] not in ("approved", "partial"):
        actions = send
    rounds = sent["enhancement_no"] or 0
    return ui.card("Pre-authorisation", stats + f'<div class="mt"{st(mt=4)}>'
                   + ui.dl([
                       ("Status", claims.PREAUTH_STATUS[sent["status"]][0]),
                       ("Disposition", sent["disposition"]),
                       # `outcome` alone does not say what happened; the
                       # adjudication reason is the half that decides.
                       ("Outcome", f'{sent["outcome"] or "—"}'
                                   f' · {sent["adjudication"] or "—"}'),
                       ("Settled at", ui.when(sent["settled_at"])),
                       ("Correlation", sent["correlation_id"]),
                       ("Enhancements", str(rounds) if rounds else "—"),
                   ], cols=3) + "</div>"
                   + enhancement + _query_note(sent)
                   + _enquiry_rows(cid, "status", "preauth")
                   + _cancel_form(cid, sent),
                   actions=_status_button(cid, "preauth") + actions)


def _query_note(row) -> str:
    """What the payer wrote back, when it wrote anything."""
    try:
        note = row["query_note"]
    except (KeyError, IndexError):
        note = None
    if not note:
        return ""
    return (f'<div class="mt"{st(mt=4)}>'
            + ui.field("What the payer says", ui.code_block(note))
            + "</div>")


def _cancel_form(cid: int, sent) -> str:
    """Withdraw a pre-authorisation the payer still has, or has granted.

    Offered while it is out and once it is approved or queried — not on a
    refusal, and not twice. `Other reason` makes the note mandatory: it is the
    only place the payer can read a justification that has no code.
    """
    if sent["status"] not in claims.CANCELLABLE:
        return ""
    return (f'<div class="mt"{st(mt=5)}>'
            + f'<form method="post" action="/claims/{cid}/cancel" '
            'onsubmit="return confirm(\'Ask the payer to cancel this '
            'pre-authorisation?\')" '
            f'class="display-flex gap items-end flex-wrap"{st(gap=2)}>'
            + ui.field("Cancel because",
                       ui.select("reason", list(claims.CANCEL_REASONS.items()),
                                 "", blank="Select a reason"), compact=True)
            + ui.field("Note", ui.text_input(
                "note", "", placeholder="Required for “Other reason”",
                attrs='style="min-width:18rem"'), compact=True)
            + ui.button("Cancel pre-authorisation", style="z-button-danger",
                        type_="submit", ico="ban")
            + "</form></div>")


# ================================================ the payer's queries
def _queries_card(row, stage: str) -> str:
    """The payer's queries on one leg, and the way to answer each.

    PMJAY queries inside the ClaimResponse and is answered by submitting the
    leg again from the Pre-authorisation tab. An IRDAI payer asks on a
    thread of its own, and each question is answered here — text, plus
    whichever of the claim's documents should ride with it — as a
    Communication on that thread.
    """
    cid = row["id"]
    asked = claims.queries(cid, stage)
    if not asked:
        return ""
    blocks = []
    for q in asked:
        questions = claims.query_questions(q)
        asked_list = ("<ul>" + "".join(f"<li>{ui.esc(line)}</li>"
                                       for line in questions) + "</ul>"
                      if questions else ui.muted("The payer asked for more "
                                                 "without saying what."))
        head = ui.dl([
            ("Received", ui.when(q["received_at"])),
            ("From", q["sender_code"]),
            ("About", q["claim_ref"]),
            ("Status", ui.label_chip(*claims.QUERY_STATUS.get(
                q["status"], (q["status"], "")))),
        ], cols=4)
        if q["status"] == "answered":
            sent_docs = claims.query_reply_documents(q)
            body = (head + f'<div class="mt"{st(mt=3)}>'
                    + ui.field("What the payer asked", asked_list)
                    + ui.field("Our reply", ui.code_block(q["reply_text"] or "—"))
                    + (ui.field("Attached", "<ul>" + "".join(
                        f'<li><a class="z-link" href="/claims/{cid}/documents/'
                        f'{d["id"]}" target="_blank">'
                        f'{ui.esc(d["label"] or d["filename"])}</a></li>'
                        for d in sent_docs) + "</ul>") if sent_docs else "")
                    + ui.dl([("Sent at", ui.when(q["answered_at"])),
                             ("Transaction", q["reply_txn_id"]),
                             ("Thread", q["correlation_id"])], cols=3)
                    + "</div>")
            blocks.append((f'Query answered {ui.when(q["answered_at"], 10)}',
                           body))
            continue

        error = ""
        if q["status"] == "error":
            error = (f'<p class="color mt"{st(color="var(--z-danger)", mt=2)}>'
                     f'{ui.esc(q["error_message"] or "The reply failed.")} '
                     "Nothing reached the payer — send it again.</p>")
        # The files answering the query are chosen here: filed on the
        # claim at this leg's stage and sent with the reply, under the
        # payer's own code when the ruling named one, else a common one.
        upload = (f'<div class="display-flex gap items-end flex-wrap"'
                  f'{st(gap=2)}>'
                  + ui.cell("Add files (PDF or image)",
                            '<input class="z-input" type="file" name="file" '
                            'multiple accept="application/pdf,image/*">',
                            "16rem")
                  + ui.cell("Filed as", _filed_as_select(
                      claims.required_documents(cid, q["stage"])), "16rem")
                  + ui.cell("Label", ui.text_input(
                      "label", "", placeholder="e.g. Implant invoice"),
                      "14rem")
                  + "</div>")
        form = (f'<form method="post" action="/claims/{cid}/queries/{q["id"]}'
                f'/reply" enctype="multipart/form-data" class="mt"{st(mt=3)}>'
                + ui.field("Reply", ui.textarea(
                    "text", q["reply_text"] or "", rows=4,
                    placeholder="What the payer asked for, and where to find "
                                "it in the attachments"))
                + ui.field("Attach documents", upload,
                           help_text="Any number of files. They are kept on "
                                     "the claim as well as sent — choose the "
                                     "payer's code where it named one.")
                + f'<div class="display-flex justify-end mt"{st(mt=3)}>'
                + ui.button("Send reply to payer", style="z-button-primary",
                            type_="submit", ico="send")
                + "</div></form>")
        blocks.append((f'Query raised {ui.when(q["received_at"], 10)} — '
                       "awaiting our reply",
                       head + f'<div class="mt"{st(mt=3)}>'
                       + ui.field("What the payer asked", asked_list)
                       + error + form + "</div>"))
    return ui.card("Payer queries", ui.accordion(blocks, multiple=True))


def answer_query(request: Request):
    cid = request.params["cid"]
    qid = request.params["qid"]
    asked = claims.query(qid)
    back = f"/claims/{cid}?tab=communication"
    if asked is None or asked["claim_id"] != cid:
        return redirect(back, "That query is not on this claim.", "danger")
    ids = []
    for value in request.f_all("document_id"):
        try:
            ids.append(int(value))
        except (TypeError, ValueError):
            continue
    uploads = [dict(upload, label=request.f("label"), code=request.f("code"))
               for upload in request.files_all("file") if upload.get("data")]
    try:
        claims.answer_query(qid, request.f("text"), ids, uploads)
    except ValueError as error:
        return redirect(back, str(error), "danger")
    sent = f" {len(uploads)} new document(s) attached." if uploads else ""
    return redirect(back, "Reply sent to the payer." + sent)


# ============================================ preauth: validating the set
def request_auth(request: Request):
    cid = request.params["cid"]
    try:
        claims.request_auth(cid)
    except ValueError as error:
        return redirect(f"/claims/{cid}?tab=validate", str(error), "danger")
    return redirect(f"/claims/{cid}?tab=validate",
                    "Procedure set sent to the payer for validation.")


def ask_status(request: Request):
    cid = request.params["cid"]
    stage = request.f("stage") or "preauth"
    back = f"/claims/{cid}?tab={'claim' if stage == 'claim' else 'preauth'}"
    try:
        claims.ask_status(cid, stage)
    except ValueError as error:
        return redirect(back, str(error), "danger")
    return redirect(back, "Status enquiry sent to the payer.")


def ask_reprocess(request: Request):
    cid = request.params["cid"]
    try:
        claims.ask_reprocess(cid, request.f("reason"))
    except ValueError as error:
        return redirect(f"/claims/{cid}?tab=claim", str(error), "danger")
    return redirect(f"/claims/{cid}?tab=claim",
                    "Reprocess request sent to the payer.")


def _requirement_table(needs) -> str:
    return ui.table(["Kind", "Code", "What", "For", "Stage"], [[
        ui.label_chip("Form" if n["kind"] == "form" else "Document",
                      "info" if n["kind"] == "form" else "warning"),
        ui.esc(n["code"] or "—"),
        ui.esc(n["display"] or "—"),
        ui.esc(n["for_code"] or "—"),
        ui.esc(n["stage"] or "—"),
    ] for n in needs], empty="Nothing listed.")


def _auth_card(row) -> str:
    """The payer's ruling on the procedure set, and the way to ask for it."""
    cid = row["id"]
    adapter = payers.for_claim(row)
    quoted = claims.lines(cid)
    ruling = claims.auth(cid)
    scheme = ui.muted(f'Payer adapter: {adapter["name"]}')

    if not adapter["auth_requirements"]:
        return ui.card("Authorisation requirements", (
            f'<p>{ui.esc(adapter["name"])} does not answer a procedure-set '
            "check, so the pre-authorisation goes in on the eligibility "
            "verdict alone.</p>" + scheme))

    ask = ui.post_button(
        f"/claims/{cid}/auth",
        "Validate procedure set" if ruling is None else "Check again",
        style="z-button-primary", ico="shield-check")

    if ruling is None:
        return ui.card("Authorisation requirements", (
            f"<p>Before sending the pre-authorisation, the "
            f"{len(quoted)} quoted line(s) go to the payer as a "
            "<code>auth-requirements</code> check: it rules on each one and "
            "names the documents and forms this particular procedure set "
            "needs.</p>" + scheme), actions=ask)

    if ruling["status"] == "checking":
        return ui.card("Authorisation requirements", (
            "<p>Procedure set sent; awaiting the payer's ruling. Use "
            "Refresh to check for it.</p>"
            + f'<div class="mt"{st(mt=3)}>' + ui.dl([
                ("Sent at", ui.when(ruling["requested_at"])),
                ("Transaction", ruling["txn_id"]),
                ("Correlation", ruling["correlation_id"]),
            ], cols=3) + "</div>"), actions=ask)

    if ruling["status"] == "error":
        return ui.card("Authorisation requirements", (
            f'<p class="color"{st(color="var(--z-danger)")}>'
            f'{ui.esc(ruling["error_message"] or "The exchange failed.")}</p>'),
            actions=ask)

    items = claims.auth_items(ruling["id"])
    verdict = ui.table(["Line", "Authorisation", "Excluded", "Benefit",
                        "Allowed"], [[
        ui.sub(ui.esc(i["display"] or i["code"]), i["code"]),
        "—" if i["auth_required"] is None else
        ("Required" if i["auth_required"] else "Not required"),
        ui.label_chip("Excluded", "danger") if i["excluded"] else "No",
        ui.esc(i["benefit_type"] or "—"),
        _money(i["allowed_amount"]),
    ] for i in items], empty="The payer ruled on no lines.")

    now = claims.auth_requirements(ruling["id"], at_preauth=True)
    later = claims.auth_requirements(ruling["id"], at_preauth=False)
    sections = [
        (f"Ruling on {len(items)} line(s)", verdict),
        (f"Needed for this pre-authorisation ({len(now)})",
         _requirement_table(now)),
    ]
    if later:
        sections.append((f"Needed later, with the claim ({len(later)})",
                         _requirement_table(later)
                         + f'<div class="mt"{st(mt=2)}>'
                         + ui.muted("The payer marked these for a stage after "
                                    "pre-authorisation, so they are not asked "
                                    "for here.") + "</div>"))
    head = ui.dl([
        ("Disposition", ruling["disposition"]),
        ("Outcome", ruling["outcome"]),
        ("Checked at", ui.when(ruling["settled_at"])),
    ], cols=3)
    if claims.ruling_is_stale(cid):
        head = (f'<div class="mb"{st(mb=3)}>'
                + ui.badge("The line items have changed since this ruling",
                           "warning")
                + f'<p class="mt"{st(mt=2)}>The payer ruled on a different '
                "procedure set. Check again so the documents and forms it "
                "names are the ones this set needs.</p></div>" + head)
    return ui.card("Authorisation requirements",
                   head + f'<div class="mt"{st(mt=4)}>'
                   + ui.accordion(sections, multiple=True) + "</div>"
                   + f'<div class="mt"{st(mt=3)}>{scheme}</div>',
                   actions=ask)


# ================================================================= the claim
def _claim_pane(row) -> str:
    """Everything the claim needs that the pre-authorisation did not."""
    cid = row["id"]
    granted = claims.preauth(cid)
    if granted is None or granted["status"] not in ("approved", "queried"):
        return ui.card("Claim", (
            "<p>A claim goes in against an <strong>approved</strong> "
            "pre-authorisation, once the patient has left. Get the "
            "pre-authorisation answered on the previous tab first.</p>"))
    return ui.stack(_discharge_card(row), _claim_documents_card(row),
                    _claim_forms_card(row), _claim_submit_card(row))


def _discharge_card(row) -> str:
    """How the stay ended — which decides what the claim may carry."""
    cid = row["id"]
    state = claims.submission(cid)
    mode = state["discharge_mode"] if state else ""
    locked = state is not None and state["status"] == "submitting"

    warning = ""
    if claims.lama_dama_only(state):
        warning = (f'<div class="mt"{st(mt=3)}>'
                   + ui.badge(f"Claimed as {claims.LAMA_DAMA_CODE} only",
                              "warning")
                   + f'<p class="mt"{st(mt=2)}>The payer accepts only '
                   f"<strong>{claims.LAMA_DAMA_CODE}</strong> for a LAMA / "
                   "DAMA case that ended before or during surgery, and "
                   "disqualifies everything the pre-authorisation approved. "
                   "The claim below quotes it in their place.</p></div>")

    form = (f'<form method="post" action="/claims/{cid}/discharge">'
            + ui.grid(
                ui.field("Discharge type", ui.select(
                    "discharge_mode",
                    [(k, v[2]) for k, v in claims.DISCHARGE_MODES.items()],
                    mode, blank="Select"), required=True,
                    help_text="Normal discharge, LAMA, DAMA or death in "
                              "hospital."),
                ui.field("Stage", ui.select(
                    "discharge_stage",
                    [(s, s) for s in claims.DISCHARGE_STAGES],
                    state["discharge_stage"] if state else "",
                    blank="Select"), required=True,
                    help_text="Whether that was before, during or after "
                              "surgery."),
                ui.field("Discharge date", ui.text_input(
                    "discharge_date",
                    state["discharge_date"] if state else "", type_="date",
                    required=True), required=True),
                ui.field("Surgery date", ui.text_input(
                    "surgery_date", state["surgery_date"] if state else "",
                    type_="date"), help_text="Leave blank for a conservative "
                                             "case."),
                ui.field("Date and time of death", ui.text_input(
                    "death_date", state["death_date"] if state else "",
                    type_="datetime-local"),
                    help_text="Required when the discharge type is death."),
                cols=2)
            + warning
            + (f'<div class="display-flex justify-end mt"{st(mt=4)}>'
               + ui.button("Save discharge", style="z-button-primary",
                           type_="submit", ico="save") + "</div>"
               if not locked else "")
            + "</form>")
    return ui.card("Discharge", form)


def _claim_documents_card(row) -> str:
    """The documents the payer deferred to the claim, plus the summary."""
    cid = row["id"]
    wanted = claims.required_documents(cid, "claim")
    rows = []
    summary = claims.document_for(cid, "HDS")
    for need in [{"code": "HDS", "display": "Hospital discharge summary",
                  "for_code": ""}] + [dict(n) for n in wanted]:
        attached = claims.document_for(cid, need["code"])
        current = ui.muted("not attached yet")
        if attached is not None:
            current = (f'<a class="z-link" href="/claims/{cid}/documents/'
                       f'{attached["id"]}" target="_blank">'
                       f'{ui.esc(attached["filename"])}</a>')
        rows.append([
            ui.esc(need["code"] or "—"),
            ui.sub(ui.esc(need["display"] or "—"), need.get("for_code") or ""),
            current,
            f'<input class="z-input" type="file" '
            f'name="req_{ui.esc(need["code"])}" '
            'accept="application/pdf,image/*">',
        ])
    note = ui.muted("The discharge summary is always wanted; the rest are the "
                    "requirements the payer's ruling deferred to this stage.")
    if not wanted and summary is None:
        note = ui.muted("Only the discharge summary is listed — run the "
                        "authorisation-requirements check on the "
                        "pre-authorisation tab to learn what else this payer "
                        "wants with the claim.")
    # Anything else the hospital wants to send with the claim — any number
    # of files, under the payer's code where it named one, or as "other
    # document". They ride on the claim leg beside the ones asked for.
    extra_rows = [[
        f'<a class="z-link" href="/claims/{cid}/documents/{d["id"]}" '
        f'target="_blank">{ui.esc(d["filename"])}</a>',
        ui.sub(ui.esc(d["label"] or "—"), d["code"] or ""),
        _size_label(d["size"]), ui.when(d["uploaded_at"]),
        ui.confirm_form(f"/claims/{cid}/documents/{d['id']}/delete", "Remove",
                        "Remove this document from the claim?"),
    ] for d in claims.documents(cid) if d["stage"] == "claim"]
    extra = (f'<form method="post" action="/claims/{cid}/documents" '
             'enctype="multipart/form-data" '
             f'class="display-flex gap items-end flex-wrap"{st(gap=2)}>'
             '<input type="hidden" name="stage" value="claim">'
             + ui.cell("Files (PDF or image)",
                       '<input class="z-input" type="file" name="file" '
                       'multiple required accept="application/pdf,image/*">',
                       "16rem")
             + ui.cell("Filed as", _filed_as_select(wanted), "18rem")
             + ui.cell("Label", ui.text_input(
                 "label", "", placeholder="e.g. Final bill, OT notes"),
                 "14rem")
             + ui.button("Attach", style="z-button-primary", type_="submit",
                         ico="paperclip")
             + "</form>")
    return ui.card("Documents for the claim", (
        f'<form method="post" action="/claims/{cid}/claim/documents" '
        'enctype="multipart/form-data">'
        + ui.table(["Code", "Document", "Attached", "Choose a PDF"], rows,
                   empty="")
        + f'<div class="display-flex items-center justify-between gap mt"'
        f'{st(gap=3, mt=3)}>' + note
        + ui.button("Attach chosen files", style="z-button-primary",
                    type_="submit", ico="paperclip")
        + "</div></form>"
        + f'<div class="mt"{st(mt=5)}>'
        + ui.field("Everything attached for the claim", ui.table(
            ["File", "Filed as", "Size", "Uploaded", ""], extra_rows,
            empty="Nothing beyond the requirements yet."))
        + f'<div class="mt"{st(mt=3)}>{extra}</div></div>'))


def _claim_forms_card(row) -> str:
    """The questionnaires the payer deferred to the claim."""
    cid = row["id"]
    forms = claims.required_forms(cid, "claim")
    if not forms:
        return ""
    return _forms_accordion(cid, forms, "Forms for the claim")


def _claim_submit_card(row) -> str:
    """Send the claim, and show what came back."""
    cid = row["id"]
    state = claims.submission(cid)
    quoted = claims.claim_lines(cid)
    total = claims.claim_total(cid)
    send = ui.post_button(
        f"/claims/{cid}/claim",
        "Submit claim" if state is None or state["status"] == "draft"
        else "Submit again",
        style="z-button-primary", ico="send",
        confirm="Send this claim to the payer?")

    quote = ui.table(
        ["Kind", "Item", "Rate", "Quantity", "Amount"],
        [[ui.label_chip(claims.LINE_KINDS.get(l["kind"], l["kind"]),
                        {"Procedure": "info", "Implant": "warning"}.get(
                            l["kind"], "")),
          ui.sub(ui.esc(l["display"] or l["code"]), l["code"]),
          _money(l["unit_price"]), f'{l["quantity"]:g}', _money(l["amount"])]
         for l in quoted],
        empty="Nothing to claim — quote the line items on the "
              "pre-authorisation tab first.")
    quote += (f'<div class="display-flex justify-end mt"{st(mt=3)}>'
              f"<strong>Total {_money(total)}</strong></div>")

    if state is None or state["status"] == "draft":
        return ui.card("Claim", quote, actions=send)

    if state["status"] == "submitting":
        return ui.card("Claim", (
            "<p>Claim submitted; awaiting the payer's <code>on_submit</code> "
            "reply. Use Refresh to check for it, or ask the payer where it "
            "stands.</p>"
            + f'<div class="mt"{st(mt=3)}>' + ui.dl([
                ("Sent at", ui.when(state["submitted_at"])),
                ("Claimed", _money(state["requested_amount"])),
                ("Transaction", state["txn_id"]),
                ("Correlation", state["correlation_id"]),
            ], cols=3) + "</div>"
            + _enquiry_rows(cid, "status", "claim")
            + _enquiry_rows(cid, "reprocess")),
            actions=_status_button(cid, "claim"))

    if state["status"] == "error":
        return ui.card("Claim", (
            f'<p class="color"{st(color="var(--z-danger)")}>'
            f'{ui.esc(state["error_message"] or "The exchange failed.")}</p>'),
            actions=send)

    stats = (f'<div class="display-grid gap sm:grid-cols lg:grid-cols"'
             + st(gap=4, sm_grid_cols=2, lg_grid_cols=3) + ">"
             + ui.stat("Claimed", _money(state["requested_amount"]),
                       "file-text")
             + ui.stat("Approved", _money(state["approved_amount"]),
                       "badge-check",
                       "success" if state["status"] == "approved"
                       else "warning")
             + ui.stat("Status", claims.CLAIM_STATUS[state["status"]][0],
                       "hash", "info")
             + "</div>")
    return ui.card("Claim", stats + f'<div class="mt"{st(mt=4)}>' + ui.dl([
        ("Disposition", state["disposition"]),
        ("Outcome", f'{state["outcome"] or "—"}'
                    f' · {state["adjudication"] or "—"}'),
        ("Claimed under", state["claim_ref"]),
        ("Settled at", ui.when(state["settled_at"])),
        ("Correlation", state["correlation_id"]),
    ], cols=3) + "</div>" + _query_note(state)
        + _enquiry_rows(cid, "status", "claim")
        + _reprocess_form(cid, state),
        actions=_status_button(cid, "claim") + send)


def save_discharge(request: Request):
    cid = request.params["cid"]
    try:
        claims.save_discharge(cid, {
            "discharge_mode": request.f("discharge_mode"),
            "discharge_stage": request.f("discharge_stage"),
            "discharge_date": request.f("discharge_date"),
            "surgery_date": request.f("surgery_date"),
            "death_date": request.f("death_date"),
        })
    except ValueError as error:
        return redirect(f"/claims/{cid}?tab=claim", str(error), "danger")
    return redirect(f"/claims/{cid}?tab=claim", "Discharge recorded.")


def upload_claim_documents(request: Request):
    """Attach the claim-stage documents, including the discharge summary."""
    cid = request.params["cid"]
    saved = 0
    try:
        for field, files in (request.files_map() or {}).items():
            if not field.startswith("req_"):
                continue
            upload = next((f for f in files if f.get("data")), None)
            if upload is None:
                continue
            code = field[4:]
            if code == "HDS":
                claims.attach_discharge_summary(cid, upload["filename"],
                                                upload["content_type"],
                                                upload["data"])
            else:
                claims.attach_required_document(cid, code, upload["filename"],
                                                upload["content_type"],
                                                upload["data"], stage="claim")
            saved += 1
    except ValueError as error:
        note = f" ({saved} attached before the failure.)" if saved else ""
        return redirect(f"/claims/{cid}?tab=claim", str(error) + note,
                        "danger")
    if not saved:
        return redirect(f"/claims/{cid}?tab=claim",
                        "Choose a PDF for at least one of them.", "danger")
    return redirect(f"/claims/{cid}?tab=claim", f"{saved} document(s) attached.")


def submit_claim(request: Request):
    cid = request.params["cid"]
    try:
        claims.submit_claim(cid)
    except ValueError as error:
        return redirect(f"/claims/{cid}?tab=claim", str(error), "danger")
    return redirect(f"/claims/{cid}?tab=claim", "Claim submitted to the payer.")


# ============================================================== the payments
# Payment status codes carry their own colour: money that has actually moved
# reads differently from money that has only been announced.
PAYMENT_TONE = {"paid": "success", "cleared": "success",
                "issued": "info", "pending": "warning",
                "partial": "warning", "failed": "danger",
                "returned": "danger"}


def _payments_pane(row) -> str:
    """Every notice the payer has sent about this claim, newest first."""
    cid = row["id"]
    notices = claims.payments(cid)
    claimed = claims.submission(cid)
    intro = ui.card("Payments", (
        "<p>The payer starts this leg: it posts a "
        "<strong>payment notice</strong> when money moves — typically one "
        "when payment is initiated and another when it clears — matched to "
        "this claim by the <code>CLN</code> number inside it. Each is "
        "acknowledged back automatically the moment it lands.</p>"
        + f'<div class="mt"{st(mt=4)}>'
        + f'<div class="display-grid gap sm:grid-cols"{st(gap=4, sm_grid_cols=3)}>'
        + ui.stat("Notices", len(notices), "bell", "info")
        + ui.stat("Paid so far", _money(claims.paid_total(cid)), "wallet",
                  "success" if claims.paid_total(cid) else "")
        + ui.stat("Claimed", _money(
            claimed["requested_amount"] if claimed else None), "file-text")
        + "</div></div>"))
    if not notices:
        return ui.stack(intro, ui.card(None, ui.empty_state(
            "No payment notice yet. One arrives on "
            "/callback/v1/paymentnotice/request when the payer moves money "
            "for this claim.")))
    return ui.stack(intro, *[_payment_card(cid, n) for n in notices])


def _payment_card(cid: int, notice) -> str:
    """One notice, as its own card — several land over a claim's life."""
    status = (notice["payment_status"] or "").lower()
    if claims.payment_initiated_only(notice):
        chips = ui.label_chip("Initiated — UTR awaited", "warning")
    else:
        chips = ui.label_chip(notice["payment_status"] or "—",
                              PAYMENT_TONE.get(status, "info"))
    chips += ui.label_chip(*claims.PAYMENT_STATUS[notice["ack_status"]])

    details = claims.payment_details(notice["id"])
    breakdown = ""
    if details:
        breakdown = (f'<div class="mt"{st(mt=4)}>'
                     + ui.table(["Reference", "Type", "Date", "Amount"], [[
                         ui.esc(d["reference"] or "—"),
                         ui.esc(d["type_display"] or d["type_code"] or "—"),
                         ui.when(d["date"], 10),
                         _money(d["amount"]),
                     ] for d in details], empty="") + "</div>")

    resend = ""
    if notice["ack_status"] != "sent":
        resend = ui.post_button(
            f'/claims/{cid}/payments/{notice["id"]}/ack',
            "Acknowledge again" if notice["ack_status"] == "error"
            else "Acknowledge", style="z-button-primary", ico="check")
    failure = ""
    if notice["ack_error"]:
        failure = (f'<p class="color mt"{st(color="var(--z-danger)", mt=3)}>'
                   f'{ui.esc(notice["ack_error"])}</p>')

    title = notice["disposition"] or "Payment notice"
    return ui.card(title, (
        f'<div class="display-flex gap flex-wrap mb"{st(gap=2, mb=4)}>'
        f"{chips}</div>"
        + ui.dl([
            ("Amount", _money(notice["amount"])),
            ("Currency", notice["currency"]),
            ("Payment date", ui.when(notice["payment_date"], 10)),
            ("UTR", notice["utr"]),
            ("Claim number", notice["claim_ref"]),
            ("Received", ui.when(notice["received_at"])),
            ("Acknowledged", ui.when(notice["acknowledged_at"])),
        ], cols=3) + breakdown + failure), actions=resend)


def acknowledge_payment(request: Request):
    cid = request.params["cid"]
    money = claims.payment(request.params["pid"])
    if money is None or money["claim_id"] != cid:
        return not_found(request, "Payment notice")
    try:
        claims.acknowledge_payment(money["id"])
    except ValueError as error:
        return redirect(f"/claims/{cid}?tab=payments", str(error), "danger")
    return redirect(f"/claims/{cid}?tab=payments",
                    "Payment acknowledged to the payer.")
