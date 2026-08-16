"""Master data screens — staff, pharmacy catalogue, wards and beds, clinical
codes, and the maintenance action that clears patient data."""

from __future__ import annotations

from typing import Any

from ... import db, masters
from ...terminology import ICD10, LOINC, NDHM_IDENTIFIER_TYPE, SNOMED, V2_0203
from .. import ui
from ..common import render, term_options
from ..router import Request, redirect

st = ui.st

GENDER_POLICY = [("any", "Any"), ("male", "Male only"), ("female", "Female only")]
WARD_CLASSES = [("general", "General"), ("semi-private", "Semi private"),
                ("private", "Private"), ("icu", "Intensive care"),
                ("hdu", "High dependency"), ("maternity", "Maternity")]
ITEM_KINDS = [("drug", "Drug"), ("consumable", "Consumable")]
CODE_SYSTEMS = [(SNOMED, "SNOMED CT"), (LOINC, "LOINC"),
                (ICD10, "ICD-10"),
                (V2_0203, "HL7 v2-0203"), (NDHM_IDENTIFIER_TYPE, "NDHM identifier type")]


def register(app) -> None:
    app.add("GET", "/masters", index)
    app.add("GET", "/masters/staff", staff)
    app.add("POST", "/masters/staff", save_staff)
    app.add("POST", "/masters/staff/<int:pid>/active", toggle_staff)
    app.add("GET", "/masters/pharmacy", pharmacy)
    app.add("POST", "/masters/pharmacy", save_item)
    app.add("POST", "/masters/pharmacy/<int:iid>/active", toggle_item)
    app.add("GET", "/masters/wards", wards)
    app.add("POST", "/masters/wards", save_ward)
    app.add("POST", "/masters/wards/<int:wid>/beds", add_bed)
    app.add("POST", "/masters/beds/<int:bid>/retire", retire_bed)
    app.add("POST", "/masters/beds/<int:bid>/restore", restore_bed)
    app.add("GET", "/masters/codes", codes)
    app.add("POST", "/masters/codes", save_code)
    app.add("POST", "/masters/codes/<int:tid>/delete", delete_code)
    app.add("GET", "/masters/maintenance", maintenance)
    app.add("POST", "/masters/maintenance/clear", clear)


# ===========================================================================
def index(request: Request):
    n = masters.counts()
    tiles = "".join([
        ui.stat("Doctors & staff", n["staff"], "user-round-cog"),
        ui.stat("Pharmacy items", n["pharmacy"], "pill", "info"),
        ui.stat("Wards", n["wards"], "layout-grid", "warning"),
        ui.stat("Beds", n["beds"], "bed", "warning"),
        ui.stat("Dialysis machines", n["machines"], "server", "info"),
        ui.stat("Clinical codes", n["codes"], "book-marked", "success"),
    ])

    cards = []
    links = {"staff": "/masters/staff", "pharmacy": "/masters/pharmacy",
             "wards": "/masters/wards", "codes": "/masters/codes"}
    for key, label, _table, blurb in masters.MASTER_SETS:
        cards.append(ui.card(label, (
            f'<p class="color"{st(color="var(--z-muted-f)")}>{ui.esc(blurb)}</p>'
            f'<div class="mt"{st(mt=4)}>'
            + ui.button("Open", links[key], style="z-button-primary",
                        ico="arrow-right")
            + "</div>")))
    cards.append(ui.card("Dialysis machines", (
        f'<p class="color"{st(color="var(--z-muted-f)")}>'
        "The machines themselves, their service dates and availability."
        f'</p><div class="mt"{st(mt=4)}>'
        + ui.button("Open", "/dialysis/machines", style="z-button-primary",
                    ico="arrow-right") + "</div>")))
    cards.append(ui.card("Facility", (
        f'<p class="color"{st(color="var(--z-muted-f)")}>'
        "Name, HFR ID, participant ID and address — these travel in every "
        "exported FHIR document."
        f'</p><div class="mt"{st(mt=4)}>'
        + ui.button("Open", "/settings", style="z-button-primary",
                    ico="arrow-right") + "</div>")))

    danger = ui.card("Maintenance", (
        f'<p class="color"{st(color="var(--z-muted-f)")}>'
        "Clear every patient record while keeping all of the masters above — "
        "for handing a configured system over, or resetting after a trial run."
        f'</p><div class="mt"{st(mt=4)}>'
        + ui.button("Open maintenance", "/masters/maintenance",
                    style="z-button-danger", ico="trash-2") + "</div>"))

    body = (
        f'<div class="display-grid gap sm:grid-cols lg:grid-cols"'
        + st(gap=4, sm_grid_cols=2, lg_grid_cols=3) + ">" + tiles + "</div>"
        + f'<div class="mt display-grid gap md:grid-cols lg:grid-cols"'
        + st(mt=5, gap=4, md_grid_cols=2, lg_grid_cols=3) + ">"
        + "".join(cards) + "</div>"
        + f'<div class="mt"{st(mt=5)}>' + danger + "</div>")
    return render(request, "Masters", "masters", body)


# ===========================================================================
# doctors and staff
# ===========================================================================
def staff(request: Request):
    edit_id = request.q("edit")
    rows = masters.staff()
    editing = db.one("SELECT * FROM practitioner WHERE id = ?", (edit_id,)) \
        if edit_id else None

    table_rows = [[
        ui.esc(r["name"]),
        ui.esc(r["department"] or "—"),
        ui.esc(r["qualification_display"] or "—"),
        f'{ui.esc(r["identifier_type_code"])} {ui.esc(r["identifier_value"])}',
        ui.esc(r["specialty_display"] or "—"),
        f'₹ {r["consultation_fee"]:.0f}',
        (ui.label_chip("active", "success") if r["active"]
         else ui.label_chip("retired", "")),
        (ui.button("Edit", f'/masters/staff?edit={r["id"]}', size="z-button-xsmall")
         + ui.post_button(f'/masters/staff/{r["id"]}/active',
                          "Retire" if r["active"] else "Reinstate",
                          fields={"active": 0 if r["active"] else 1})),
    ] for r in rows]

    e = dict(editing) if editing else {}
    form = (
        '<form method="post" action="/masters/staff">'
        + (f'<input type="hidden" name="id" value="{e["id"]}">' if e else "")
        + ui.grid(
            ui.field("Name", ui.text_input("name", e.get("name", ""), required=True,
                                           placeholder="Dr. …"), required=True),
            ui.field("Department", ui.select("department",
                                             term_options("department", False),
                                             e.get("department", ""),
                                             blank="Not stated")),
            ui.field("Gender", ui.select("gender", [("female", "Female"),
                                                    ("male", "Male"),
                                                    ("other", "Other")],
                                         e.get("gender", ""), blank="Not stated")),
            ui.field("Qualification", ui.text_input(
                "qualification_display", e.get("qualification_display", ""),
                placeholder="MBBS, MD (General Medicine)")),
            ui.field("Identifier type", ui.select("identifier_type_code", [
                ("HPID", "Healthcare Professional ID (HPID)"),
                ("MD", "Medical council registration"),
                ("OIN", "Other identifier")], e.get("identifier_type_code", "HPID")),
                help_text="Written into Practitioner.identifier.type."),
            ui.field("Identifier number", ui.text_input(
                "identifier_value", e.get("identifier_value", ""), required=True,
                placeholder="71-2233-4455-6677"), required=True),
            ui.field("Specialty (SNOMED CT)", ui.select(
                "specialty_code", term_options("service_type"),
                e.get("specialty_code", ""), blank="Not stated")),
            ui.field("Consultation fee (₹)", ui.text_input(
                "consultation_fee", e.get("consultation_fee", 0), type_="number",
                attrs='step="1" min="0"')),
            ui.field("Phone", ui.text_input("phone", e.get("phone", ""))),
            ui.field("Email", ui.text_input("email", e.get("email", ""),
                                            type_="email")),
            cols=3)
        + f'<div class="display-flex justify-end gap"{st(gap=2)}>'
        + (ui.button("Cancel", "/masters/staff", style="z-button-secondary") if e else "")
        + ui.button("Save practitioner" if e else "Add practitioner",
                    style="z-button-primary", type_="submit", ico="user-round-plus")
        + "</div></form>")

    body = (
        ui.card(f"{len(table_rows)} practitioner(s)",
                ui.table(["Name", "Department", "Qualification", "Identifier",
                          "Specialty", "Fee", "Status", ""], table_rows,
                         empty="No practitioners configured.", align_right=(5,)))
        + f'<div class="mt"{st(mt=5)}>'
        + ui.card("Edit practitioner" if e else "Add a practitioner",
                  ui.accordion([("Practitioner details", form)], open_first=bool(e)))
        + "</div>")
    return render(request, "Doctors & staff", "masters", body,
                  breadcrumb=[("Masters", "/masters"), ("Doctors & staff", None)])


def save_staff(request: Request):
    specialty = db.term("service_type", request.f("specialty_code")) \
        if request.f("specialty_code") else None
    dept = db.term("department", request.f("department")) \
        if request.f("department") else None
    id_code = request.f("identifier_type_code") or "HPID"
    id_display = {"HPID": "Healthcare Professional ID (HPID)",
                  "MD": "Medical License number",
                  "OIN": "Other identifier"}.get(id_code, id_code)
    values: dict[str, Any] = {
        "name": request.f("name"),
        "prefix": "Dr",
        "gender": request.f_or_none("gender"),
        "department": dept["display"] if dept else request.f_or_none("department"),
        "qualification_display": request.f_or_none("qualification_display"),
        "qualification_code": "BS",
        "qualification_system": V2_0203,
        "identifier_type_system": (NDHM_IDENTIFIER_TYPE if id_code == "HPID"
                                   else V2_0203),
        "identifier_type_code": id_code,
        "identifier_type_display": id_display,
        "identifier_system": "https://doctor.abdm.gov.in",
        "identifier_value": request.f("identifier_value"),
        "role_code": "158965000", "role_display": "Medical practitioner",
        "specialty_code": specialty["code"] if specialty else None,
        "specialty_display": specialty["display"] if specialty else None,
        "consultation_fee": request.f_float("consultation_fee", 0.0),
        "phone": request.f_or_none("phone"),
        "email": request.f_or_none("email"),
        "active": 1,
    }
    try:
        masters.save_practitioner(request.f_int("id"), values)
    except ValueError as error:
        return redirect("/masters/staff", str(error), "danger")
    return redirect("/masters/staff", "Practitioner saved.")


def toggle_staff(request: Request):
    masters.set_practitioner_active(request.params["pid"],
                                    request.f("active") == "1")
    return redirect("/masters/staff", "Practitioner updated.")


# ===========================================================================
# pharmacy catalogue
# ===========================================================================
def pharmacy(request: Request):
    edit_id = request.q("edit")
    editing = db.one("SELECT * FROM stock_item WHERE id = ?", (edit_id,)) \
        if edit_id else None
    rows = db.query("SELECT * FROM stock_item ORDER BY active DESC, kind, name")

    table_rows = [[
        ui.esc(r["code"]),
        ui.esc(r["name"]) + (f'<div class="text-xs color"'
                             f'{st(color="var(--z-muted-f)")}>'
                             f'{ui.esc(r["strength"])}</div>' if r["strength"] else ""),
        ui.label_chip(r["kind"], "info" if r["kind"] == "drug" else ""),
        ui.esc(r["form"] or "—"),
        ui.esc(r["unit"]),
        f'₹ {r["mrp"]:.2f}',
        f'{r["gst_pct"]:g}%',
        f'{r["reorder_level"]:g}',
        (ui.label_chip("active", "success") if r["active"]
         else ui.label_chip("inactive", "")),
        (ui.button("Edit", f'/masters/pharmacy?edit={r["id"]}',
                   size="z-button-xsmall")
         + ui.post_button(f'/masters/pharmacy/{r["id"]}/active',
                          "Retire" if r["active"] else "Restore",
                          fields={"active": 0 if r["active"] else 1})),
    ] for r in rows]

    e = dict(editing) if editing else {}
    form = (
        '<form method="post" action="/masters/pharmacy">'
        + (f'<input type="hidden" name="id" value="{e["id"]}">' if e else "")
        + ui.grid(
            ui.field("Item code", ui.text_input("code", e.get("code", ""),
                                                required=True,
                                                placeholder="MED021"), required=True),
            ui.field("Name", ui.text_input("name", e.get("name", ""), required=True),
                     required=True),
            ui.field("Kind", ui.select("kind", ITEM_KINDS, e.get("kind", "drug"))),
            ui.field("Form", ui.text_input("form", e.get("form", ""),
                                           placeholder="Tablet")),
            ui.field("Strength", ui.text_input("strength", e.get("strength", ""),
                                               placeholder="500 mg")),
            ui.field("Issue unit", ui.text_input("unit", e.get("unit", "unit"),
                                                 placeholder="tablet")),
            ui.field("MRP (₹)", ui.text_input("mrp", e.get("mrp", 0), type_="number",
                                              attrs='step="0.01" min="0"')),
            ui.field("Purchase price (₹)", ui.text_input(
                "purchase_price", e.get("purchase_price", 0), type_="number",
                attrs='step="0.01" min="0"')),
            ui.field("GST %", ui.text_input("gst_pct", e.get("gst_pct", 12),
                                            type_="number",
                                            attrs='step="0.01" min="0"')),
            ui.field("Reorder level", ui.text_input(
                "reorder_level", e.get("reorder_level", 0), type_="number",
                attrs='step="1" min="0"')),
            ui.field("HSN code", ui.text_input("hsn_code", e.get("hsn_code", ""))),
            ui.field("Medicine (SNOMED CT)", ui.select(
                "snomed_code", term_options("medicine"), e.get("snomed_code", ""),
                blank="Not coded"),
                help_text="Used when the item is dispensed against a prescription."),
            cols=3)
        + f'<div class="display-flex justify-end gap"{st(gap=2)}>'
        + (ui.button("Cancel", "/masters/pharmacy", style="z-button-secondary")
           if e else "")
        + ui.button("Save item" if e else "Add item", style="z-button-primary",
                    type_="submit", ico="plus")
        + "</div></form>")

    body = (
        ui.card(f"{len(table_rows)} item(s)",
                ui.table(["Code", "Item", "Kind", "Form", "Unit", "MRP", "GST",
                          "Reorder", "Status", ""], table_rows,
                         empty="No items configured.", align_right=(5, 6, 7)))
        + f'<div class="mt"{st(mt=5)}>'
        + ui.card("Edit item" if e else "Add an item",
                  ui.accordion([("Item details", form)], open_first=bool(e)))
        + "</div>")
    return render(request, "Pharmacy catalogue", "masters", body,
                  breadcrumb=[("Masters", "/masters"), ("Pharmacy", None)])


def save_item(request: Request):
    medicine = db.term("medicine", request.f("snomed_code")) \
        if request.f("snomed_code") else None
    values: dict[str, Any] = {
        "code": request.f("code"), "name": request.f("name"),
        "kind": request.f("kind") or "drug",
        "form": request.f_or_none("form"), "strength": request.f_or_none("strength"),
        "unit": request.f("unit") or "unit",
        "snomed_code": medicine["code"] if medicine else None,
        "snomed_display": medicine["display"] if medicine else None,
        "hsn_code": request.f_or_none("hsn_code"),
        "mrp": request.f_float("mrp", 0.0),
        "purchase_price": request.f_float("purchase_price", 0.0),
        "gst_pct": request.f_float("gst_pct", 0.0),
        "reorder_level": request.f_float("reorder_level", 0.0),
        "active": 1,
    }
    try:
        masters.save_stock_item(request.f_int("id"), values)
    except ValueError as error:
        return redirect("/masters/pharmacy", str(error), "danger")
    return redirect("/masters/pharmacy", "Item saved.")


def toggle_item(request: Request):
    masters.set_item_active(request.params["iid"], request.f("active") == "1")
    return redirect("/masters/pharmacy", "Item updated.")


# ===========================================================================
# wards and beds
# ===========================================================================
def wards(request: Request):
    edit_id = request.q("edit")
    editing = db.one("SELECT * FROM ward WHERE id = ?", (edit_id,)) if edit_id else None
    ward_rows = db.query("SELECT * FROM ward ORDER BY sort_order, name")

    cards = []
    for ward in ward_rows:
        beds = db.query("SELECT * FROM bed WHERE ward_id = ? ORDER BY code",
                        (ward["id"],))
        rows = [[
            ui.esc(b["code"]),
            ui.label_chip(*{"vacant": ("Vacant", "success"),
                            "occupied": ("Occupied", "danger"),
                            "cleaning": ("Cleaning", "warning"),
                            "blocked": ("Blocked", "")}[b["status"]]),
            f'₹ {b["tariff"]:.0f}' if b["tariff"] is not None else "ward rate",
            (ui.label_chip("in service", "success") if b["active"]
             else ui.label_chip("retired", "")),
            ui.post_button(
                f'/masters/beds/{b["id"]}/{"retire" if b["active"] else "restore"}',
                "Retire" if b["active"] else "Restore"),
        ] for b in beds]

        add = (f'<form method="post" action="/masters/wards/{ward["id"]}/beds">'
               + ui.grid(
                   ui.field("Bed code", ui.text_input("code", "",
                                                      placeholder="GM-07",
                                                      required=True), required=True),
                   ui.field("Tariff override (₹/day)", ui.text_input(
                       "tariff", "", type_="number", attrs='step="1" min="0"'),
                       help_text="Leave blank to use the ward rate."),
                   cols=2)
               + f'<div class="display-flex justify-end"{st()}>'
               + ui.button("Add bed", style="z-button-primary", type_="submit",
                           ico="plus") + "</div></form>")

        cards.append(ui.card(
            f'{ward["name"]} — {len(beds)} bed(s)',
            ui.table(["Bed", "Status", "Tariff", "Service", ""], rows,
                     empty="No beds in this ward.")
            + f'<div class="mt"{st(mt=4)}>'
            + ui.accordion([("Add a bed", add)], open_first=not beds) + "</div>",
            actions=(ui.label_chip(f'₹{ward["tariff"]:.0f}/day')
                     + ui.label_chip(ward["class"], "info")
                     + (ui.label_chip(ward["gender_policy"], "warning")
                        if ward["gender_policy"] != "any" else "")
                     + ui.button("Edit ward", f'/masters/wards?edit={ward["id"]}',
                                 size="z-button-xsmall"))))

    e = dict(editing) if editing else {}
    ward_form = (
        '<form method="post" action="/masters/wards">'
        + (f'<input type="hidden" name="id" value="{e["id"]}">' if e else "")
        + ui.grid(
            ui.field("Code", ui.text_input("code", e.get("code", ""), required=True,
                                           placeholder="GWM"), required=True),
            ui.field("Name", ui.text_input("name", e.get("name", ""), required=True),
                     required=True),
            ui.field("Class", ui.select("class", WARD_CLASSES,
                                        e.get("class", "general"))),
            ui.field("Bed tariff (₹/day)", ui.text_input(
                "tariff", e.get("tariff", 0), type_="number",
                attrs='step="1" min="0"')),
            ui.field("Nursing rate (₹/day)", ui.text_input(
                "nursing_rate", e.get("nursing_rate", 0), type_="number",
                attrs='step="1" min="0"')),
            ui.field("Gender policy", ui.select("gender_policy", GENDER_POLICY,
                                                e.get("gender_policy", "any")),
                     help_text="Filters which beds are offered on admission."),
            cols=3)
        + f'<div class="display-flex justify-end gap"{st(gap=2)}>'
        + (ui.button("Cancel", "/masters/wards", style="z-button-secondary")
           if e else "")
        + ui.button("Save ward" if e else "Add ward", style="z-button-primary",
                    type_="submit", ico="plus")
        + "</div></form>")

    body = ("".join(f'<div class="mb"{st(mb=4)}>{c}</div>' for c in cards)
            + ui.card("Edit ward" if e else "Add a ward",
                      ui.accordion([("Ward details", ward_form)],
                                   open_first=bool(e) or not ward_rows)))
    return render(request, "Wards & beds", "masters", body,
                  breadcrumb=[("Masters", "/masters"), ("Wards & beds", None)])


def save_ward(request: Request):
    values = {
        "code": request.f("code"), "name": request.f("name"),
        "class": request.f("class") or "general",
        "tariff": request.f_float("tariff", 0.0),
        "nursing_rate": request.f_float("nursing_rate", 0.0),
        "gender_policy": request.f("gender_policy") or "any",
        "active": 1,
    }
    try:
        masters.save_ward(request.f_int("id"), values)
    except ValueError as error:
        return redirect("/masters/wards", str(error), "danger")
    return redirect("/masters/wards", "Ward saved.")


def add_bed(request: Request):
    tariff = request.f("tariff")
    try:
        masters.add_bed(request.params["wid"], request.f("code"),
                        float(tariff) if tariff else None)
    except ValueError as error:
        return redirect("/masters/wards", str(error), "danger")
    return redirect("/masters/wards", "Bed added.")


def retire_bed(request: Request):
    try:
        masters.retire_bed(request.params["bid"])
    except ValueError as error:
        return redirect("/masters/wards", str(error), "danger")
    return redirect("/masters/wards", "Bed taken out of service.")


def restore_bed(request: Request):
    masters.restore_bed(request.params["bid"])
    return redirect("/masters/wards", "Bed back in service.")


# ===========================================================================
# clinical codes
# ===========================================================================
def codes(request: Request):
    kind = request.q("kind") or "diagnosis"
    if kind not in masters.CODE_LABEL:
        kind = "diagnosis"
    edit_id = request.q("edit")
    editing = db.one("SELECT * FROM terminology WHERE id = ?", (edit_id,)) \
        if edit_id else None
    rows = db.terms(kind)

    groups = []
    for group, kinds in masters.CODE_GROUPS:
        links = []
        for key, label in kinds:
            cls = ' class="z-active"' if key == kind else ""
            count = db.scalar("SELECT COUNT(*) FROM terminology WHERE kind = ?",
                              (key,), default=0)
            links.append(f'<li{cls}><a href="/masters/codes?kind={key}">'
                         f'{ui.esc(label)}'
                         f'<span class="ml text-xs color"'
                         f'{st(ml=2, color="var(--z-muted-f)")}>{count}</span>'
                         f'</a></li>')
        groups.append(f'<li class="z-nav-header">{ui.esc(group)}</li>'
                      + "".join(links))
    picker = ('<ul class="z-nav z-nav-default"'
              + st(z_nav_item_padding="0.25rem 0.5rem") + ">"
              + "".join(groups) + "</ul>")

    table_rows = [[
        f'<code class="z-codespan">{ui.esc(r["code"])}</code>',
        ui.esc(r["display"]),
        (f'<code class="z-codespan">{ui.esc(r["alt_code"])}</code> '
         f'{ui.esc(r["alt_display"] or "")}' if r["alt_code"] else "—"),
        ui.esc(r["unit"] or "—"),
        ui.esc(f'{r["ref_low"]}–{r["ref_high"]}'
               if r["ref_low"] is not None or r["ref_high"] is not None else "—"),
        ui.esc(r["extra"] or "—"),
        (ui.button("Edit", f'/masters/codes?kind={kind}&edit={r["id"]}',
                   size="z-button-xsmall")
         + ui.confirm_form(f'/masters/codes/{r["id"]}/delete', "Delete",
                           f'Remove "{r["display"]}" from this picker? Records '
                           "already using it keep the code they were saved with.")),
    ] for r in rows]

    e = dict(editing) if editing else {}
    form = (
        '<form method="post" action="/masters/codes">'
        + f'<input type="hidden" name="kind" value="{ui.esc(kind)}">'
        + (f'<input type="hidden" name="id" value="{e["id"]}">' if e else "")
        + ui.grid(
            ui.field("Code", ui.text_input("code", e.get("code", ""), required=True),
                     required=True,
                     help_text="The code as published by the owning system."),
            ui.field("Display", ui.text_input("display", e.get("display", ""),
                                              required=True), required=True),
            ui.field("Code system", ui.select("system", CODE_SYSTEMS,
                                              e.get("system", SNOMED),
                                              blank="Local to this facility"),
                     help_text="Leave local for anything with no standard code."),
            ui.field("Secondary code", ui.text_input("alt_code",
                                                     e.get("alt_code", ""),
                                                     placeholder="ICD-10")),
            ui.field("Secondary display", ui.text_input("alt_display",
                                                        e.get("alt_display", ""))),
            ui.field("Unit (UCUM)", ui.text_input("unit", e.get("unit", ""),
                                                  placeholder="mg/dL")),
            ui.field("Reference low", ui.text_input(
                "ref_low", e.get("ref_low", "") if e.get("ref_low") is not None else "",
                type_="number", attrs='step="0.01"')),
            ui.field("Reference high", ui.text_input(
                "ref_high", e.get("ref_high", "") if e.get("ref_high") is not None
                else "", type_="number", attrs='step="0.01"')),
            ui.field("Sort order", ui.text_input("sort_order",
                                                 e.get("sort_order", 100),
                                                 type_="number", attrs='step="1"')),
            cols=3)
        + ui.field("Extra", ui.text_input("extra", e.get("extra", "")),
                   help_text=masters.EXTRA_HINT.get(
                       kind, "Free field; only some sets use it."))
        + f'<div class="display-flex justify-end gap"{st(gap=2)}>'
        + (ui.button("Cancel", f"/masters/codes?kind={kind}",
                     style="z-button-secondary") if e else "")
        + ui.button("Save concept" if e else "Add concept", style="z-button-primary",
                    type_="submit", ico="plus")
        + "</div></form>")

    listing = (
        ui.card(f'{masters.CODE_LABEL[kind]} — {len(table_rows)} concept(s)',
                ui.table(["Code", "Display", "Secondary", "Unit", "Reference",
                          "Extra", ""], table_rows,
                         empty="This picker is empty."))
        + f'<div class="mt"{st(mt=5)}>'
        + ui.card("Edit concept" if e else "Add a concept",
                  ui.accordion([("Concept details", form)], open_first=bool(e)))
        + "</div>")

    body = (f'<div class="display-grid gap lg:[grid-cols]"'
            + st(gap=4, lg_grid_cols="260px minmax(0, 1fr)") + ">"
            + ui.card(None, picker) + f'<div class="min-w"{st(min_w=0)}>'
            + listing + "</div></div>")
    return render(request, "Clinical codes", "masters", body,
                  breadcrumb=[("Masters", "/masters"), ("Clinical codes", None)])


def save_code(request: Request):
    kind = request.f("kind")
    if kind not in masters.CODE_LABEL:
        return redirect("/masters/codes", "Unknown code set.", "danger")

    def number(name: str):
        raw = request.f(name)
        try:
            return float(raw) if raw else None
        except ValueError:
            return None

    values: dict[str, Any] = {
        "code": request.f("code"), "display": request.f("display"),
        "alt_code": request.f_or_none("alt_code"),
        "alt_display": request.f_or_none("alt_display"),
        "unit": request.f_or_none("unit"),
        "ref_low": number("ref_low"), "ref_high": number("ref_high"),
        "extra": request.f_or_none("extra"),
        "sort_order": int(number("sort_order") or 100),
    }
    system = request.f("system")
    if system:
        values["system"] = system
    if values["alt_code"] and not request.f_or_none("alt_system"):
        values["alt_system"] = ICD10

    try:
        term_id = request.f_int("id")
        if term_id:
            masters.update_code(term_id, values)
        else:
            masters.add_code(kind, values)
    except ValueError as error:
        return redirect(f"/masters/codes?kind={kind}", str(error), "danger")
    return redirect(f"/masters/codes?kind={kind}", "Concept saved.")


def delete_code(request: Request):
    row = db.one("SELECT kind FROM terminology WHERE id = ?", (request.params["tid"],))
    masters.delete_code(request.params["tid"])
    kind = row["kind"] if row else "diagnosis"
    return redirect(f"/masters/codes?kind={kind}", "Concept removed from the picker.")


# ===========================================================================
# maintenance
# ===========================================================================
def maintenance(request: Request):
    counts = masters.transactional_counts()
    total = sum(counts.values())
    kept = masters.counts()

    clear_rows = [[ui.esc(table), f"{n:,}"] for table, n in counts.items() if n]
    keep_rows = [
        ["Doctors & staff", f'{kept["staff"]:,}'],
        ["Pharmacy catalogue", f'{kept["pharmacy"]:,}'],
        ["Wards", f'{kept["wards"]:,}'],
        ["Beds", f'{kept["beds"]:,} (occupancy reset)'],
        ["Dialysis machines", f'{kept["machines"]:,} (status reset)'],
        ["Clinical codes", f'{kept["codes"]:,}'],
        ["Facility settings", "1"],
    ]

    unlisted = masters.unlisted_tables()
    warning = ""
    if unlisted:
        warning = (f'<div class="z-alert z-alert-warning mb" data-z-alert{st(mb=4)}>'
                   "<strong>Unclassified tables:</strong> "
                   + ui.esc(", ".join(unlisted))
                   + " — these are neither listed as masters nor cleared. "
                     "Classify them in <code class=\"z-codespan\">emr/masters.py"
                     "</code>.</div>")

    form = (
        '<form method="post" action="/masters/maintenance/clear" '
        'onsubmit="return confirm('
        "'This deletes every patient record. Masters are kept. Continue?'"
        ')">'
        + ui.field("Type CLEAR to confirm",
                   ui.text_input("confirm", "", placeholder="CLEAR", required=True),
                   help_text="Deliberately awkward — there is no undo.",
                   required=True)
        + f'<div class="display-flex justify-end"{st()}>'
        + ui.button(f"Delete {total:,} record(s)", style="z-button-danger",
                    size="z-button-medium", type_="submit", ico="trash-2")
        + "</div></form>")

    body = (
        warning
        + ui.card("What a reset does", (
            "<p>Clears every patient record so a configured system can be handed "
            "over or a trial run wiped, while keeping everything the clinic "
            "configured. It runs in a single transaction: either the whole clinic "
            "resets or nothing does.</p>"
            "<p>Numbering restarts — the next patient is MRN00001 again.</p>"))
        + f'<div class="mt display-grid gap lg:grid-cols"'
        + st(mt=5, gap=4, lg_grid_cols=2) + ">"
        + ui.card(f"Will be deleted — {total:,} row(s)",
                  ui.table(["Table", "Rows"], clear_rows,
                           empty="There is no patient data to clear.",
                           align_right=(1,)))
        + ui.card("Will be kept",
                  ui.table(["Master", "Rows"], keep_rows, align_right=(1,)))
        + "</div>"
        + f'<div class="mt"{st(mt=5)}>'
        + ui.card("Danger zone", form, style="z-card-danger")
        + "</div>")
    return render(request, "Maintenance", "masters", body,
                  breadcrumb=[("Masters", "/masters"), ("Maintenance", None)])


def clear(request: Request):
    if request.f("confirm") != "CLEAR":
        return redirect("/masters/maintenance",
                        "Type CLEAR exactly to confirm the reset.", "danger")
    deleted = masters.clear_transactional_data()
    total = sum(deleted.values())
    if not total:
        return redirect("/masters/maintenance", "There was nothing to clear.",
                        "warning")
    biggest = ", ".join(f"{table} {n:,}" for table, n in
                        sorted(deleted.items(), key=lambda kv: -kv[1])[:4])
    return redirect("/masters/maintenance",
                    f"Cleared {total:,} record(s) across {len(deleted)} table(s) "
                    f"({biggest}…). Masters kept.")
