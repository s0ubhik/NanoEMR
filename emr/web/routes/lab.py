"""Laboratory orders and result entry."""

from __future__ import annotations

from ... import db, services
from .. import ui
from ..common import (
    patient_cell,
    get_patient, not_found, patient_options, render, status_label, term_options,
)
from ..router import Request, redirect

st = ui.st


def register(app) -> None:
    app.add("GET", "/lab", index)
    app.add("GET", "/lab/new", new)
    app.add("POST", "/lab", create)
    app.add("GET", "/lab/<int:oid>", detail)
    app.add("POST", "/lab/<int:oid>", save)


def index(request: Request):
    rows = db.query(
        "SELECT o.*, p.name AS patient_name, p.mrn FROM lab_order o "
        "JOIN patient p ON p.id = o.patient_id ORDER BY o.id DESC LIMIT 200")
    table_rows = [[
        f'<a class="z-link" href="/lab/{r["id"]}">{ui.esc(r["order_no"])}</a>',
        patient_cell(r),
        ui.esc(r["panel_display"]),
        ui.esc(r["specimen_display"] or "—"),
        ui.when(r["ordered_at"]),
        status_label(r["status"]),
        (ui.button("Enter results", f'/lab/{r["id"]}', size="z-button-xsmall",
                   style="z-button-primary") if r["status"] != "final"
         else ui.button("View", f'/lab/{r["id"]}', size="z-button-xsmall")),
    ] for r in rows]
    body = ui.card(
        f"{len(table_rows)} lab order(s)",
        ui.table(["Order", "Patient", "Panel", "Specimen", "Ordered", "Status", ""],
                 table_rows, empty="No lab orders yet."),
        actions=ui.button("New lab order", "/lab/new", style="z-button-primary",
                          ico="flask-conical"))
    return render(request, "Laboratory", "lab", body)


def new(request: Request):
    encounter_id = request.q("encounter")
    preset_patient = request.q("patient")
    if encounter_id:
        enc = db.one("SELECT * FROM encounter WHERE id = ?", (encounter_id,))
        if enc is not None:
            preset_patient = str(enc["patient_id"])

    panel_options = [(p["code"], f'{p["display"]} — ₹{(p["extra"] or "||0").split("|")[2]}')
                     for p in db.terms("lab_panel")]
    pathologists = [(r["id"], r["name"]) for r in db.query(
        "SELECT id, name FROM practitioner WHERE active = 1 ORDER BY "
        "CASE WHEN department = 'Laboratory' THEN 0 ELSE 1 END, name")]

    body = (
        '<form method="post" action="/lab">'
        + f'<input type="hidden" name="encounter_id" value="{ui.esc(encounter_id)}">'
        + ui.card("Order investigation", (
            ui.field("Patient", ui.select("patient_id", patient_options(),
                                          preset_patient, blank="Select patient",
                                          required=True), required=True)
            + ui.grid(
                ui.field("Panel / test (LOINC)", ui.select(
                    "panel_code", panel_options, "", blank="Select panel",
                    required=True), required=True),
                ui.field("Specimen (SNOMED CT)", ui.select(
                    "specimen_code", term_options("specimen", False), "",
                    blank="Use the panel default")),
                ui.field("Results interpreter", ui.select(
                    "interpreter_id", pathologists, "", blank="Select pathologist",
                    required=True),
                    help_text="DiagnosticReportLab.resultsInterpreter is mandatory "
                              "in the NRCES profile.", required=True),
                cols=2)),
            footer=(f'<div class="display-flex justify-between gap"{st(gap=2)}>'
                    + ui.button("Cancel", "/lab", style="z-button-secondary")
                    + ui.button("Create order", style="z-button-primary",
                                size="z-button-medium", type_="submit") + "</div>"))
        + "</form>")
    return render(request, "New lab order", "lab", body,
                  breadcrumb=[("Laboratory", "/lab"), ("New order", None)])


def create(request: Request):
    patient_id = request.f_int("patient_id")
    panel_code = request.f("panel_code")
    if not patient_id or not panel_code:
        return redirect("/lab/new", "Patient and panel are mandatory.", "danger")
    order_id = services.create_lab_order(
        patient_id, request.f_int("encounter_id"), panel_code,
        request.f_int("interpreter_id"), request.f_or_none("specimen_code"))
    number = db.scalar("SELECT order_no FROM lab_order WHERE id = ?", (order_id,))
    return redirect(f"/lab/{order_id}", f"Lab order {number} created.")


def detail(request: Request):
    oid = request.params["oid"]
    order = db.one("SELECT * FROM lab_order WHERE id = ?", (oid,))
    if order is None:
        return not_found(request, "Lab order")
    patient = get_patient(order["patient_id"])
    results = db.query(
        "SELECT * FROM observation WHERE lab_order_id = ? ORDER BY sort_order, id", (oid,))
    interpreter = db.one("SELECT * FROM practitioner WHERE id = ?",
                         (order["interpreter_id"],))

    rows = []
    for r in results:
        rng = "—"
        if r["ref_low"] is not None or r["ref_high"] is not None:
            rng = f'{r["ref_low"] if r["ref_low"] is not None else ""}–' \
                  f'{r["ref_high"] if r["ref_high"] is not None else ""}'
        flag = ui.flag_chip(r["interpretation"]) or "—"
        current = r["value_quantity"] if r["value_quantity"] is not None else (
            r["value_string"] or "")
        rows.append([
            ui.sub(f'{ui.esc(r["loinc_display"])}',
                   f'LOINC {r["loinc_code"]}'),
            ui.text_input(f'result_{r["id"]}', current, size="z-form-small",
                          attrs='style="max-width:9rem"'),
            ui.esc(r["value_unit"] or "—"),
            ui.esc(rng),
            flag,
        ])

    entry = (
        f'<form method="post" action="/lab/{oid}">'
        + ui.card("Analyte results", (
            ui.table(["Analyte", "Result", "Unit", "Reference range", "Flag"], rows,
                     empty="This panel has no analytes configured.")
            + f'<div class="mt"{st(mt=4)}>'
            + ui.field("Conclusion", ui.textarea(
                "conclusion", order["conclusion"] or "", rows=3,
                placeholder="Pathologist's impression — mandatory in "
                            "DiagnosticReportLab."))
            + "</div>"),
            footer=(f'<div class="display-flex justify-between gap flex-wrap"{st(gap=2)}>'
                    + ui.button("Save as preliminary", style="z-button-secondary",
                                type_="submit", attrs='name="action" value="save"')
                    + ui.button("Finalise report", style="z-button-primary",
                                size="z-button-medium", type_="submit",
                                attrs='name="action" value="finalise"') + "</div>"))
        + "</form>")

    facts = ui.card("Order", ui.dl([
        ("Order number", order["order_no"]),
        ("Panel", f'{order["panel_display"]} (LOINC {order["panel_code"]})'),
        ("Category", f'{order["category_display"]} (SNOMED {order["category_code"]})'),
        ("Specimen", f'{order["specimen_display"]} (SNOMED {order["specimen_code"]})'
         if order["specimen_code"] else None),
        ("Ordered", ui.when(order["ordered_at"])),
        ("Interpreter", interpreter["name"] if interpreter else None),
        ("Status", order["status"]),
        ("Conclusion code", f'{order["conclusion_display"]} ({order["conclusion_code"]})'
         if order["conclusion_code"] else None),
    ], cols=3))


    extra = ui.label_chip(order["order_no"], "info") + status_label(order["status"])

    body = (ui.patient_header(patient, extra=extra)
            + f'<div class="mt"{st(mt=5)}>' + facts + "</div>"
            + f'<div class="mt"{st(mt=5)}>' + entry + "</div>")

    return render(request, f"Lab order {order['order_no']}", "lab", body,
                  breadcrumb=[("Laboratory", "/lab"), (order["order_no"], None)])


def save(request: Request):
    oid = request.params["oid"]
    order = db.one("SELECT * FROM lab_order WHERE id = ?", (oid,))
    if order is None:
        return not_found(request, "Lab order")
    finalise = request.f("action") == "finalise"
    values = {f"result_{r['id']}": request.f(f"result_{r['id']}")
              for r in db.query("SELECT id FROM observation WHERE lab_order_id = ?", (oid,))}
    services.save_lab_results(oid, values, request.f("conclusion"), finalise)
    return redirect(f"/lab/{oid}",
                    "Report finalised." if finalise else "Results saved as preliminary.")
