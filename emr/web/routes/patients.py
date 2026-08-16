"""Patient registration and the patient chart."""

from __future__ import annotations

from ... import db, services
from .. import ui
from ..common import get_patient, not_found, render, status_label, term_options
from ..router import Request, redirect

st = ui.st


def register(app) -> None:
    app.add("GET", "/patients", index)
    app.add("GET", "/patients/new", new)
    app.add("POST", "/patients", create)
    app.add("GET", "/patients/<int:pid>", detail)
    app.add("GET", "/patients/<int:pid>/edit", edit)
    app.add("POST", "/patients/<int:pid>", update)
    app.add("POST", "/patients/<int:pid>/vitals", record_vitals)
    app.add("POST", "/patients/<int:pid>/allergies", add_allergy)
    app.add("POST", "/patients/<int:pid>/allergies/<int:aid>/delete", delete_allergy)
    app.add("POST", "/patients/<int:pid>/problems", add_problem)
    app.add("POST", "/patients/<int:pid>/problems/<int:cid>/delete", delete_problem)


# ---------------------------------------------------------------------------
def index(request: Request):
    term = request.q("q")
    rows = services.search_patients(term)
    table_rows = []
    for row in rows:
        table_rows.append([
            f'<a class="z-link" href="/patients/{row["id"]}">{ui.esc(row["name"])}</a>',
            ui.label_chip(row["mrn"], "info"),
            f'{ui.esc(row["gender"].title())} · {services.display_age(row)}',
            ui.esc(row["phone"]),
            ui.esc(row["abha_number"] or "—"),
            ui.esc((row["city"] or "") + (", " + row["state"] if row["state"] else "")),
            (ui.button("OPD", f'/opd/new?patient={row["id"]}', size="z-button-xsmall")
             + ui.button("Admit", f'/ipd/new?patient={row["id"]}',
                         size="z-button-xsmall", attrs='style="margin-left:.35rem"')),
        ])

    search = (
        '<form method="get" action="/patients" '
        f'class="display-flex gap flex-wrap"{st(gap=2)}>'
        + ui.text_input("q", term, placeholder="Name, MRN, phone or ABHA",
                        attrs='style="min-width:16rem"')
        + ui.button("Search", style="z-button-primary", type_="submit")
        + ui.button("Clear", "/patients", style="z-button-secondary")
        + "</form>")

    body = ui.card(
        f"{len(table_rows)} patient(s)",
        ui.table(["Patient", "MRN", "Age / sex", "Phone", "ABHA", "City", ""],
                 table_rows,
                 empty="No patient matches that search."),
        actions=search + ui.button("Register patient", "/patients/new",
                                   style="z-button-primary", ico="user-plus"))
    return render(request, "Patient directory", "patients", body)


# ---------------------------------------------------------------------------
def _form(patient: dict | None, action: str, submit: str) -> str:
    p = patient or {}

    identity = ui.grid(
        ui.field("Full name", ui.text_input("name", p.get("name", ""), required=True,
                                            placeholder="As per ABHA / Aadhaar"),
                 required=True, field_id="name"),
        ui.field("Gender", ui.select("gender", [
            ("male", "Male"), ("female", "Female"), ("other", "Other"),
            ("unknown", "Unknown")], p.get("gender", ""), blank="Select", required=True),
                 required=True, field_id="gender"),
        ui.field("Date of birth", ui.text_input("birth_date", p.get("birth_date", ""),
                                                type_="date"),
                 help_text="Leave blank and fill age if the DOB is unknown."),
        ui.field("Age (years)", ui.text_input("age_years", p.get("age_years", "") or "",
                                              type_="number", attrs='min="0" max="130"')),
        ui.field("Given name", ui.text_input("given_name", p.get("given_name", ""))),
        ui.field("Family name", ui.text_input("family_name", p.get("family_name", ""))),
        cols=2)

    contact = ui.grid(
        ui.field("Mobile number", ui.text_input("phone", p.get("phone", ""),
                                                required=True, placeholder="+91XXXXXXXXXX"),
                 required=True, field_id="phone"),
        ui.field("Email", ui.text_input("email", p.get("email", ""), type_="email")),
        ui.field("ABHA number", ui.text_input("abha_number", p.get("abha_number", ""),
                                              placeholder="14 digit ABHA"),
                 help_text="Exported as Patient.identifier with type ABHA."),
        ui.field("ABHA address", ui.text_input("abha_address", p.get("abha_address", ""),
                                               placeholder="name@abdm")),
        cols=2)

    demographics = ui.grid(
        ui.field("Marital status", ui.select(
            "marital_status_code", term_options("marital_status", False),
            p.get("marital_status_code", ""), blank="Not recorded")),
        ui.field("Blood group", ui.select(
            "blood_group", term_options("blood_group", False),
            p.get("blood_group", ""), blank="Not recorded")),
        cols=2)

    address = (
        ui.field("Address line", ui.text_input("address_line", p.get("address_line", "")))
        + ui.grid(
            ui.field("City / town", ui.text_input("city", p.get("city", ""))),
            ui.field("District", ui.text_input("district", p.get("district", ""))),
            ui.field("State", ui.text_input("state", p.get("state", ""))),
            ui.field("PIN code", ui.text_input("postal_code", p.get("postal_code", ""))),
            cols=2))

    emergency = ui.grid(
        ui.field("Contact name", ui.text_input("contact_name", p.get("contact_name", ""))),
        ui.field("Relationship", ui.text_input("contact_relation",
                                               p.get("contact_relation", ""))),
        ui.field("Contact phone", ui.text_input("contact_phone",
                                                p.get("contact_phone", ""))),
        cols=3)

    sections = ui.accordion([
        ("Identity", identity),
        ("Contact & ABHA", contact),
        ("Demographics", demographics),
        ("Address", address),
        ("Emergency contact", emergency),
    ], multiple=True)

    return (f'<form method="post" action="{action}">'
            + ui.card(None, sections,
                      footer=(f'<div class="display-flex justify-between gap flex-wrap"'
                              f'{st(gap=2)}>'
                              + ui.button("Cancel", "/patients", style="z-button-secondary")
                              + ui.button(submit, style="z-button-primary",
                                          size="z-button-medium", type_="submit")
                              + "</div>"))
            + "</form>")


def new(request: Request):
    body = _form(None, "/patients", "Register patient")
    return render(request, "Register patient", "patient-new", body,
                  breadcrumb=[("Patients", "/patients"), ("Register", None)])


def _values(request: Request) -> dict:
    marital = request.f("marital_status_code")
    marital_row = db.term("marital_status", marital) if marital else None
    return {
        "name": request.f("name"),
        "given_name": request.f_or_none("given_name"),
        "family_name": request.f_or_none("family_name"),
        "gender": request.f("gender") or "unknown",
        "birth_date": request.f_or_none("birth_date"),
        "age_years": request.f_int("age_years"),
        "phone": request.f("phone"),
        "email": request.f_or_none("email"),
        "abha_number": request.f_or_none("abha_number"),
        "abha_address": request.f_or_none("abha_address"),
        "marital_status_code": marital or None,
        "marital_status_display": marital_row["display"] if marital_row else None,
        "blood_group": request.f_or_none("blood_group"),
        "address_line": request.f_or_none("address_line"),
        "city": request.f_or_none("city"),
        "district": request.f_or_none("district"),
        "state": request.f_or_none("state"),
        "postal_code": request.f_or_none("postal_code"),
        "country": "India",
        "contact_name": request.f_or_none("contact_name"),
        "contact_relation": request.f_or_none("contact_relation"),
        "contact_phone": request.f_or_none("contact_phone"),
    }


def create(request: Request):
    values = _values(request)
    if not values["name"] or not values["phone"]:
        return redirect("/patients/new", "Name and mobile number are mandatory.", "danger")
    patient_id = services.create_patient(values)
    mrn = db.scalar("SELECT mrn FROM patient WHERE id = ?", (patient_id,))
    return redirect(f"/patients/{patient_id}", f"Patient registered with MRN {mrn}.")


def edit(request: Request):
    patient = get_patient(request.params["pid"])
    if not patient:
        return not_found(request, "Patient")
    body = _form(patient, f"/patients/{patient['id']}", "Save changes")
    return render(request, f"Edit {patient['name']}", "patients", body,
                  breadcrumb=[("Patients", "/patients"),
                              (patient["name"], f"/patients/{patient['id']}"),
                              ("Edit", None)])


def update(request: Request):
    patient = get_patient(request.params["pid"])
    if not patient:
        return not_found(request, "Patient")
    values = _values(request)
    values["updated_at"] = db.now_iso()
    db.update("patient", patient["id"], values)
    return redirect(f"/patients/{patient['id']}", "Patient record updated.")


# ---------------------------------------------------------------------------
def detail(request: Request):
    pid = request.params["pid"]
    patient = get_patient(pid)
    if not patient:
        return not_found(request, "Patient")

    visits = db.query(
        "SELECT e.*, d.name AS doctor_name FROM encounter e "
        "LEFT JOIN practitioner d ON d.id = e.practitioner_id "
        "WHERE e.patient_id = ? ORDER BY e.id DESC", (pid,))
    labs = db.query(
        "SELECT * FROM lab_order WHERE patient_id = ? ORDER BY id DESC", (pid,))
    bills = db.query(
        "SELECT * FROM invoice WHERE patient_id = ? ORDER BY id DESC", (pid,))
    problems = db.query(
        "SELECT * FROM condition WHERE patient_id = ? AND category IN "
        "('diagnosis','medical-history') "
        "ORDER BY (encounter_id IS NOT NULL), id DESC", (pid,))
    allergies = db.query(
        "SELECT * FROM allergy WHERE patient_id = ? ORDER BY "
        "CASE criticality WHEN 'high' THEN 0 ELSE 1 END, id DESC", (pid,))

    overview = (
        ui.dl([
            ("MRN", ui.label_chip(patient["mrn"], "info")),
            ("ABHA number", patient["abha_number"]),
            ("ABHA address", patient["abha_address"]),
            ("Gender", patient["gender"].title()),
            ("Date of birth", patient["birth_date"]),
            ("Age", services.display_age(patient)),
            ("Mobile", patient["phone"]),
            ("Email", patient["email"]),
            ("Marital status", patient["marital_status_display"]),
            ("Blood group", patient["blood_group"]),
            ("Address", ", ".join(filter(None, [
                patient["address_line"], patient["city"], patient["district"],
                patient["state"], patient["postal_code"]]))),
            ("Emergency contact", ", ".join(filter(None, [
                patient["contact_name"], patient["contact_relation"],
                patient["contact_phone"]]))),
        ], cols=3))


    visit_rows = [[
        f'<a class="z-link" href="/{"opd" if v["kind"] == "OPD" else "ipd"}/{v["id"]}">'
        f'{ui.esc(v["encounter_no"])}</a>',
        ui.label_chip(v["kind"], "info" if v["kind"] == "OPD" else "warning"),
        ui.when(v["period_start"]),
        ui.esc(v["doctor_name"] or "—"),
        ui.esc(v["department"] or "—"),
        status_label(v["status"]),
    ] for v in visits]

    lab_rows = [[
        f'<a class="z-link" href="/lab/{o["id"]}">{ui.esc(o["order_no"])}</a>',
        ui.esc(o["panel_display"]),
        ui.when(o["ordered_at"]),
        status_label(o["status"]),
    ] for o in labs]

    bill_rows = [[
        f'<a class="z-link" href="/billing/{b["id"]}">{ui.esc(b["invoice_no"])}</a>',
        ui.esc(b["type_display"]),
        ui.when(b["date"], 10),
        f'₹ {b["total_gross"]:.2f}',
        status_label(b["status"]),
    ] for b in bills]


    open_encounters = [e for e in visits if e["status"] != "finished"]
    encounter_choices = [
        (e["id"], f'{e["encounter_no"]} · {e["kind"]} · '
                  f'{ui.when(e["period_start"])}') for e in visits[:25]]
    encounter_index = {e["id"]: (e["encounter_no"], e["kind"]) for e in visits}

    tabs = [
        ("Overview", ui.card(None, overview)
         + f'<div class="mt"{st(mt=4)}>' + _vitals_section(pid, encounter_choices,
                                                           open_encounters) + "</div>"
),
        ("Encounters", ui.card(
            f"{len(visit_rows)} encounter(s)",
            ui.table(["Number", "Type", "When", "Doctor", "Department", "Status"],
                     visit_rows,
                     empty="No encounters recorded — start an OPD visit or admit "
                           "the patient."),
            actions=(ui.button("New OPD visit", f"/opd/new?patient={pid}",
                               style="z-button-primary", ico="stethoscope")
                     + ui.button("Admit", f"/ipd/new?patient={pid}", ico="bed")))),
        ("Problems & allergies",
         _problems_section(pid, problems, encounter_choices, encounter_index)
         + f'<div class="mt"{st(mt=4)}>'
         + _allergies_section(pid, allergies, encounter_choices) + "</div>"),
        ("Laboratory", ui.card(
            f"{len(lab_rows)} lab order(s)",
            ui.table(["Order", "Panel", "Ordered", "Status"], lab_rows,
                     empty="No lab orders for this patient."),
            actions=ui.button("Order investigation", f"/lab/new?patient={pid}",
                              style="z-button-primary", ico="flask-conical"))),
        ("Billing", ui.card(
            f"{len(bill_rows)} invoice(s) · ₹ "
            f'{sum(b["total_gross"] for b in bills):,.2f}',
            ui.table(["Invoice", "Type", "Date", "Amount", "Status"], bill_rows,
                     empty="No invoices raised.", align_right=(3,)),
            actions=ui.button("Raise invoice", f"/billing/new?patient={pid}",
                              style="z-button-primary",
                              ico="receipt-indian-rupee"))),
    ]

    body = (
        ui.patient_header(patient, extra=(
            ui.button("Edit", f"/patients/{pid}/edit", ico="pencil")
            + ui.button("New OPD visit", f"/opd/new?patient={pid}",
                        style="z-button-primary", ico="stethoscope")
            + ui.button("Admit", f"/ipd/new?patient={pid}",
                        style="z-button-secondary", ico="bed")))
        + f'<div class="mt"{st(mt=5)}>'
        + ui.tabs(tabs, active=TAB_INDEX.get(request.q("tab"), 0))
        + "</div>")

    return render(request, patient["name"], "patients", body,
                  breadcrumb=[("Patients", "/patients"), (patient["name"], None)])


TAB_INDEX = {"overview": 0, "encounters": 1, "problems": 2, "lab": 3, "billing": 4}


# ---------------------------------------------------------------------------
# vitals
# ---------------------------------------------------------------------------
def _vitals_section(pid: int, encounter_choices, open_encounters) -> str:
    masters = db.terms("vital")
    sets = services.vital_sets(pid)
    latest = sets[0] if sets else {}

    chips = []
    for master in masters:
        reading = (latest.get("readings") or {}).get(master["code"])
        if reading is None:
            continue
        label, kind = ui.FLAG_CHIP.get(reading["interpretation"] or "", ("", ""))
        chips.append(
            '<div>'
            f'<div class="text-xs uppercase tracking-wide color"'
            f'{st(color="var(--z-muted-f)")}>{ui.esc(ui.short_label(master["display"]))}</div>'
            f'<div class="mt display-flex items-baseline gap"{st(mt=1, gap=2)}>'
            f'<span class="z-h5">{reading["value_quantity"]:g}</span>'
            f'<span class="text-xs color"{st(color="var(--z-muted-f)")}>'
            f'{ui.esc(master["unit"] or "")}</span>'
            + (ui.label_chip(label, kind) if label else "") + "</div></div>")

    if chips:
        taken = ui.when(latest["effective_ts"])
        source = (f' · {latest["encounter_no"]}' if latest.get("encounter_no")
                  else " · recorded on the chart")
        snapshot = (
            f'<div class="text-sm color mb"{st(color="var(--z-muted-f)", mb=3)}>'
            f'Last recorded {ui.esc(taken)}{ui.esc(source)}</div>'
            f'<div class="display-grid gap sm:grid-cols lg:grid-cols"'
            + st(gap=4, sm_grid_cols=2, lg_grid_cols=4) + ">" + "".join(chips) + "</div>")
    else:
        snapshot = (f'<p class="color"{st(color="var(--z-muted-f)")}>'
                    "No vitals recorded yet.</p>")

    inputs = []
    for master in masters:
        current = ""
        placeholder = ""
        if master["ref_low"] is not None or master["ref_high"] is not None:
            placeholder = (f'{master["ref_low"] if master["ref_low"] is not None else ""}'
                           f'–{master["ref_high"] if master["ref_high"] is not None else ""}')
        inputs.append(ui.field(
            f'{ui.short_label(master["display"])} ({master["unit"]})',
            ui.text_input(f'vital_{master["code"]}', current, type_="number",
                          placeholder=placeholder, attrs='step="0.1"'),
            help_text=f'LOINC {master["code"]}'))

    default_encounter = open_encounters[0]["id"] if open_encounters else ""
    form = (
        f'<form method="post" action="/patients/{pid}/vitals">'
        + f'<div class="display-grid gap sm:grid-cols lg:grid-cols"'
        + st(gap=3, sm_grid_cols=2, lg_grid_cols=3) + ">" + "".join(inputs) + "</div>"
        + ui.grid(
            ui.field("Taken at", ui.text_input("effective_ts", db.now_iso()[:16],
                                               type_="datetime-local")),
            ui.field("Attach to encounter", ui.select(
                "encounter_id", encounter_choices, default_encounter,
                blank="Not linked to a visit"),
                help_text="Only vitals attached to an encounter enter that "
                          "encounter's FHIR document."),
            ui.field("Note", ui.text_input("note", "", placeholder="e.g. post-medication")),
            cols=3)
        + f'<div class="display-flex justify-end"{st()}>'
        + ui.button("Record vitals", style="z-button-primary", type_="submit",
                    ico="heart-pulse")
        + "</div></form>")

    history_rows = []
    for batch in sets:
        cells = []
        for master in masters:
            reading = batch["readings"].get(master["code"])
            if reading is None:
                cells.append("—")
                continue
            label, kind = ui.FLAG_CHIP.get(reading["interpretation"] or "", ("", ""))
            value = f'{reading["value_quantity"]:g}'
            cells.append(value if not kind or kind == "success"
                         else f'{value} {ui.label_chip(label, kind)}')
        history_rows.append(
            [ui.when(batch["effective_ts"]),
             (f'<a class="z-link" href="/{"opd" if batch["encounter_kind"] == "OPD" else "ipd"}'
              f'/{batch["encounter_id"]}">{ui.esc(batch["encounter_no"])}</a>'
              if batch["encounter_no"] else "—")] + cells)

    history = ui.table(
        ["Taken at", "Encounter"] + [ui.short_label(m["display"]) for m in masters],
        history_rows, empty="No vitals recorded yet.")

    return ui.card("Vitals", snapshot + f'<div class="mt"{st(mt=5)}>' + ui.accordion([
        ("Record a new set of vitals", form),
        (f"History — {len(sets)} recording(s)", history),
    ], multiple=True, open_first=not chips) + "</div>")


# ---------------------------------------------------------------------------
# problems
# ---------------------------------------------------------------------------
def _problems_section(pid: int, problems, encounter_choices, encounter_index) -> str:
    rows = []
    for c in problems:
        source = "—"
        entry = encounter_index.get(c["encounter_id"])
        if entry:
            number, kind = entry
            path = "opd" if kind == "OPD" else "ipd"
            source = (f'<a class="z-link" href="/{path}/{c["encounter_id"]}">'
                      f'{ui.esc(number)}</a>')
        remove = ""
        if c["category"] == "medical-history" and c["encounter_id"] is None:
            remove = ui.confirm_form(
                f'/patients/{pid}/problems/{c["id"]}/delete', "Remove",
                f'Remove "{c["text"]}" from the problem list?')
        rows.append([
            ui.esc(c["text"]),
            ui.esc(c["icd10_code"] or "—"),
            ui.esc(c["snomed_code"] or "—"),
            ui.label_chip("diagnosis" if c["category"] == "diagnosis"
                          else "history",
                          "info" if c["category"] == "diagnosis" else ""),
            ui.esc(c["clinical_status"]),
            source,
            ui.when(c["recorded_at"], 10),
            remove,
        ])

    form = (
        f'<form method="post" action="/patients/{pid}/problems">'
        + ui.grid(
            ui.field("Problem (SNOMED CT + ICD-10)", ui.select(
                "code", term_options("diagnosis"), "", blank="Free text only")),
            ui.field("Description", ui.text_input(
                "text", "", placeholder="Used when no coded problem is picked")),
            ui.field("Clinical status", ui.select("clinical_status", [
                ("active", "Active"), ("recurrence", "Recurrence"),
                ("remission", "Remission"), ("resolved", "Resolved"),
                ("inactive", "Inactive")], "active")),
            ui.field("Onset", ui.text_input("onset", "", type_="date")),
            ui.field("Attach to encounter", ui.select(
                "encounter_id", encounter_choices, "",
                blank="Chart-level (appears in every document)")),
            ui.field("Note", ui.text_input("note", "")),
            cols=3)
        + f'<div class="display-flex justify-end"{st()}>'
        + ui.button("Add to problem list", style="z-button-primary", type_="submit",
                    ico="plus")
        + "</div></form>")

    return ui.card(
        f"Problem list — {len(rows)} entr(y/ies)",
        ui.table(["Problem", "ICD-10", "SNOMED CT", "Type", "Status", "Source",
                  "Recorded", ""], rows,
                 empty="No problems recorded. Chart-level problems flow into the "
                       "Medical history section of every document.")
        + f'<div class="mt"{st(mt=5)}>'
        + ui.accordion([("Add a problem", form)], open_first=not rows) + "</div>")


# ---------------------------------------------------------------------------
# allergies
# ---------------------------------------------------------------------------
CRITICALITY = [("low", "Low risk"), ("high", "High risk"),
               ("unable-to-assess", "Unable to assess")]
ALLERGY_CATEGORY = [("medication", "Medication"), ("food", "Food"),
                    ("environment", "Environment"), ("biologic", "Biologic")]


def _allergies_section(pid: int, allergies, encounter_choices) -> str:
    rows = []
    for a in allergies:
        crit = {"high": ("high risk", "danger"), "low": ("low risk", "success"),
                "unable-to-assess": ("unassessed", "warning")}.get(
            a["criticality"] or "", (a["criticality"] or "—", ""))
        rows.append([
            ui.esc(a["text"]),
            ui.esc(a["snomed_code"] or "text only"),
            ui.esc((a["category"] or "—").title()),
            ui.label_chip(crit[0], crit[1]) if a["criticality"] else "—",
            ui.esc(a["reaction"] or "—"),
            ui.esc(a["clinical_status"]),
            ui.when(a["recorded_at"], 10),
            ui.confirm_form(f'/patients/{pid}/allergies/{a["id"]}/delete', "Remove",
                            f'Remove the recorded allergy to {a["text"]}?'),
        ])

    form = (
        f'<form method="post" action="/patients/{pid}/allergies">'
        + ui.grid(
            ui.field("Allergen (SNOMED CT)", ui.select(
                "code", term_options("allergen"), "", blank="Free text only"),
                help_text="AllergyIntolerance.code is mandatory and SNOMED-coded "
                          "in the NRCES profile."),
            ui.field("Description", ui.text_input(
                "text", "", placeholder="Used when no coded allergen is picked")),
            ui.field("Category", ui.select("category", ALLERGY_CATEGORY, "",
                                           blank="From the allergen")),
            ui.field("Criticality", ui.select("criticality", CRITICALITY, "",
                                              blank="Not assessed")),
            ui.field("Reaction (SNOMED CT)", ui.select(
                "reaction_code", term_options("reaction", False), "", blank="None")),
            ui.field("Reaction detail", ui.text_input(
                "reaction_text", "", placeholder="Overrides the coded reaction")),
            cols=3)
        + ui.field("Noted during encounter", ui.select(
            "encounter_id", encounter_choices, "",
            blank="Chart-level (appears in every document)"))
        + f'<div class="display-flex justify-end"{st()}>'
        + ui.button("Add allergy", style="z-button-primary", type_="submit",
                    ico="triangle-alert")
        + "</div></form>")

    banner = ""
    if any(a["criticality"] == "high" for a in allergies):
        names = ", ".join(a["text"] for a in allergies if a["criticality"] == "high")
        banner = (f'<div class="z-alert z-alert-danger mb" data-z-alert{st(mb=4)}>'
                  f'<strong>High risk allergy:</strong> {ui.esc(names)}</div>')

    return ui.card(
        f"Allergies — {len(rows)} recorded",
        banner
        + ui.table(["Allergen", "SNOMED CT", "Category", "Criticality", "Reaction",
                    "Status", "Recorded", ""], rows,
                   empty="No allergies recorded. Entries here populate the Allergies "
                         "section of the OP consult and discharge summary documents.")
        + f'<div class="mt"{st(mt=5)}>'
        + ui.accordion([("Add an allergy", form)], open_first=not rows) + "</div>")


# ---------------------------------------------------------------------------
# capture endpoints
# ---------------------------------------------------------------------------
def record_vitals(request: Request):
    pid = request.params["pid"]
    if not get_patient(pid):
        return not_found(request, "Patient")
    readings = {m["code"]: request.f(f'vital_{m["code"]}') for m in db.terms("vital")}
    if not any(v.strip() for v in readings.values()):
        return redirect(f"/patients/{pid}?tab=overview",
                        "Enter at least one reading.", "danger")
    count = services.record_vitals(
        pid, request.f_int("encounter_id"), readings,
        effective_ts=request.f_or_none("effective_ts"),
        note=request.f_or_none("note"))
    return redirect(f"/patients/{pid}?tab=overview",
                    f"Recorded {count} vital sign(s).")


def add_allergy(request: Request):
    pid = request.params["pid"]
    if not get_patient(pid):
        return not_found(request, "Patient")
    try:
        services.add_allergy(
            pid, request.f_int("encounter_id"), request.f_or_none("code"),
            request.f("text"), request.f_or_none("category"),
            request.f_or_none("criticality"), request.f_or_none("reaction_code"),
            request.f_or_none("reaction_text"))
    except ValueError as error:
        return redirect(f"/patients/{pid}?tab=problems", str(error), "danger")
    return redirect(f"/patients/{pid}?tab=problems", "Allergy recorded.")


def delete_allergy(request: Request):
    pid = request.params["pid"]
    db.execute("DELETE FROM allergy WHERE id = ? AND patient_id = ?",
               (request.params["aid"], pid))
    return redirect(f"/patients/{pid}?tab=problems", "Allergy removed.")


def add_problem(request: Request):
    pid = request.params["pid"]
    if not get_patient(pid):
        return not_found(request, "Patient")
    try:
        services.add_problem(
            pid, request.f_int("encounter_id"), request.f_or_none("code"),
            request.f("text"), request.f("clinical_status") or "active",
            request.f_or_none("onset"), request.f_or_none("note"))
    except ValueError as error:
        return redirect(f"/patients/{pid}?tab=problems", str(error), "danger")
    return redirect(f"/patients/{pid}?tab=problems", "Problem added.")


def delete_problem(request: Request):
    pid = request.params["pid"]
    db.execute("DELETE FROM condition WHERE id = ? AND patient_id = ? "
               "AND encounter_id IS NULL", (request.params["cid"], pid))
    return redirect(f"/patients/{pid}?tab=problems", "Problem removed.")
