"""NHCX claims — policy search, coverage eligibility and the claim ledger."""

from __future__ import annotations

import json

from ... import claims, db
from .. import ui
from ..common import collect_rows, doctor_options, not_found, render
from ..router import Request, Response, json_response, redirect

st = ui.st


def register(app) -> None:
    app.add("GET", "/claims", index)
    app.add("GET", "/claims/new", new)
    app.add("POST", "/claims", create)
    app.add("GET", "/claims/<int:cid>", detail)
    app.add("POST", "/claims/<int:cid>/check", check)
    app.add("POST", "/claims/<int:cid>/link", link)
    app.add("POST", "/claims/<int:cid>/unlink", unlink)
    app.add("POST", "/claims/<int:cid>/preauth", save_preauth)
    app.add("POST", "/claims/<int:cid>/documents", upload_document)
    app.add("GET", "/claims/<int:cid>/documents/<int:did>", view_document)
    app.add("POST", "/claims/<int:cid>/documents/<int:did>/delete",
            remove_document)
    app.add("POST", "/nhcx/callback", callback)


# --------------------------------------------------------- the inbound door
def callback(request: Request) -> Response:
    """Receive what hcxkit's inbound worker delivers.

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
    id_type = request.q("id_type") or "MobileNo"
    id_value = (request.q("id_value") or "").strip()

    search = ('<form method="get" action="/claims/new" '
              f'class="display-flex gap items-end flex-wrap"{st(gap=2)}>'
              + ui.field("Identifier type",
                         ui.select("id_type", list(claims.ID_TYPES.items()),
                                   id_type))
              + ui.field("Identifier value",
                         ui.text_input("id_value", id_value,
                                       placeholder="98185…, 91-1234-… or member ID",
                                       attrs='style="min-width:16rem"'))
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
        verdict = ui.card("Payer verdict", (
            f'<p>Eligibility {ui.esc(row["purpose"] or "check")} sent to the '
            "payer; awaiting the on_check reply. This page refreshes itself "
            "every few seconds.</p>"
            + (f'<div class="mt"{st(mt=2)}>{poll_note}</div>' if poll_note else "")
            + f'<div class="mt"{st(mt=3)}>' + ui.dl([
                ("Sent at", ui.when(row["checked_at"])),
                ("Transaction", row["txn_id"]),
                ("Correlation", row["correlation_id"]),
            ], cols=3) + "</div>"))
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
    if row["preauth_saved_at"]:
        chips += ui.label_chip("Preauth drafted", "info")
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

    panes = ui.tabs([
        ("Eligibility", ui.stack(*eligibility_blocks)),
        ("Pre-authorisation", _preauth_pane(row)),
    ], active=1 if request.q("tab") == "preauth" else 0, key="claim-tabs")

    body = ui.stack(header, panes)

    scripts = ""
    if row["status"] == "checking":
        scripts = ("<script>setTimeout(function () { location.reload(); }, "
                   "5000);</script>")
    return render(request, f'Claim {row["claim_no"]}', "claims", body,
                  breadcrumb=[("Claims", "/claims"), (row["claim_no"], None)],
                  scripts=scripts)


def check(request: Request):
    cid = request.params["cid"]
    try:
        claims.run_check(cid, request.f("purpose"), request.f("policy_code"),
                         request.f("member_id"))
    except ValueError as error:
        return redirect(f"/claims/{cid}", str(error), "danger")
    return redirect(f"/claims/{cid}",
                    "Eligibility request queued to the payer.")


# ------------------------------------------------------ preauth: link the stay
def _preauth_pane(row) -> str:
    if row["encounter_id"]:
        return ui.stack(_linked_card(row), _preauth_form(row),
                        _documents_card(row))
    if row["status"] != "eligible":
        return ui.card("Pre-authorisation", (
            "<p>The pre-authorisation opens once the payer has confirmed the "
            "policy is <strong>eligible</strong>. Run the coverage "
            "eligibility check on the Eligibility tab first.</p>"))
    return _link_card(row)


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


def _preauth_form(row) -> str:
    children = claims.preauth_children(row["id"])
    case_type = row["case_type"] or "package"

    dx_rows = [_dx_row({"code": d["snomed_code"]})
               for d in children["diagnoses"]] or [_dx_row()]
    team_rows = [_team_row({"doctor": t["practitioner_id"], "role": t["role"]})
                 for t in children["care_team"]] or [_team_row()]
    item_rows = [_item_row({"code": i["code"], "qty": i["quantity"]})
                 for i in children["items"]] or [_item_row()]

    package_options = [(p["code"], f'{p["display"]} — ₹{p["rate"]:,.0f}')
                       for p in claims.packages()]

    def case_radio(value: str, label: str) -> str:
        checked = " checked" if case_type == value else ""
        return (f'<label class="display-flex items-center gap"{st(gap=2)}>'
                f'<input class="z-radio" type="radio" name="case_type" '
                f'value="{value}"{checked} onchange="emrCaseType(this.value)">'
                f'<span>{ui.esc(label)}</span></label>')

    package_box = (
        f'<div id="pkg-box"{"" if case_type == "package" else " style=display:none"}>'
        + ui.field("Package", ui.select("package_code", package_options,
                                        row["package_code"] or "",
                                        blank="Select package"),
                   help_text="The rate is fixed by the package master.")
        + "</div>")
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
        ("Diagnoses (ICD-10)", ui.repeater("pdx", _dx_row(), dx_rows,
                                           "Add diagnosis")),
        ("Care team", ui.repeater("pct", _team_row(), team_rows, "Add doctor")),
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


# ----------------------------------------------------- preauth: the documents
def _size_label(size: int) -> str:
    if size >= 1024 * 1024:
        return f"{size / (1024 * 1024):.1f} MB"
    return f"{max(size, 1024) / 1024:.0f} KB"


def _documents_card(row) -> str:
    cid = row["id"]
    docs = claims.documents(cid)
    table_rows = [[
        f'<a class="z-link" href="/claims/{cid}/documents/{d["id"]}" '
        f'target="_blank">{ui.esc(d["filename"])}</a>',
        ui.esc(d["label"] or "—"),
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
              + ui.cell("Label", ui.text_input(
                  "label", "", placeholder="e.g. Admission note, ID card"),
                  "14rem")
              + ui.button("Attach", style="z-button-primary", type_="submit",
                          ico="paperclip")
              + "</form>")

    return ui.card("Supporting documents",
                   ui.table(["File", "Label", "Type", "Size", "Uploaded", ""],
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
    cid = request.params["cid"]
    uploads = request.files_all("file")
    if not uploads:
        return redirect(_back(cid), "Choose a PDF or image to attach.",
                        "danger")
    label = request.f("label")
    saved = 0
    try:
        for upload in uploads:
            claims.add_document(cid, upload["filename"],
                                upload["content_type"], upload["data"], label)
            saved += 1
    except ValueError as error:
        note = f" ({saved} of {len(uploads)} attached before the failure.)" \
            if saved else ""
        return redirect(_back(cid), str(error) + note, "danger")
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
