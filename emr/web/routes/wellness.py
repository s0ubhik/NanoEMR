"""Wellness records — capture screens for the NRCES WellnessRecord artifact."""

from __future__ import annotations

from ... import db, wellness
from .. import ui
from ..common import (
    patient_cell,
    doctor_options, not_found, patient_options, render,
)
from ..router import Request, redirect

st = ui.st


def register(app) -> None:
    app.add("GET", "/wellness", index)
    app.add("GET", "/wellness/new", new)
    app.add("POST", "/wellness", create)
    app.add("GET", "/wellness/<int:wid>", detail)
    app.add("POST", "/wellness/<int:wid>/sections/<str:key>", save_section)
    app.add("POST", "/wellness/<int:wid>/finalise", finalise)
    app.add("POST", "/wellness/<int:wid>/reopen", reopen)
    app.add("POST", "/wellness/<int:wid>/regenerate", regenerate)


# ---------------------------------------------------------------------------
def index(request: Request):
    scope = request.q("status") or ""
    rows = wellness.records(scope if scope in wellness.STATUS else "")
    table_rows = [[
        f'<a class="z-link" href="/wellness/{r["id"]}">{ui.esc(r["record_no"])}</a>',
        patient_cell(r),
        ui.esc(r["recorded_on"]),
        ui.esc(r["source"] or "—"),
        ui.esc(r["doctor_name"] or "—"),
        f'{r["entries"]} observation(s)',
        ui.label_chip(*wellness.STATUS[r["status"]]),
    ] for r in rows]

    filters = ('<form method="get" action="/wellness" '
               f'class="display-flex gap items-end"{st(gap=2)}>'
               + ui.select("status", [(k, v[0]) for k, v in wellness.STATUS.items()],
                           scope, blank="All records")
               + ui.button("Filter", style="z-button-primary", type_="submit")
               + "</form>")

    body = (
        ui.card(None, (
            "<p>A wellness record is the periodic, largely self-reported health "
            "picture ABDM expects in a PHR — vitals, body measurements, activity, "
            "general assessment, women's health and lifestyle. Every concept "
            "offered below is a member of the ValueSet its NRCES Observation "
            "profile binds to.</p>"))
        + f'<div class="mt"{st(mt=5)}>'
        + ui.card(f"{len(table_rows)} record(s)",
                  ui.table(["Record", "Patient", "Recorded on", "Source",
                            "Recorded by", "Content", "Status"], table_rows,
                           empty="No wellness records yet."),
                  actions=filters + ui.button("New record", "/wellness/new",
                                              style="z-button-primary", ico="plus"))
        + "</div>")
    return render(request, "Wellness records", "wellness", body)


def new(request: Request):
    preset = request.q("patient")
    encounters = [(e["id"], f'{e["encounter_no"]} · {e["kind"]} · '
                            f'{ui.when(e["period_start"], 10)}')
                  for e in db.query("SELECT * FROM encounter ORDER BY id DESC "
                                    "LIMIT 50")]
    body = (
        '<form method="post" action="/wellness">'
        + ui.card("New wellness record", ui.grid(
            ui.field("Patient", ui.select("patient_id", patient_options(), preset,
                                          blank="Select patient", required=True),
                     required=True),
            ui.field("Recorded on", ui.text_input("recorded_on", db.today_iso(),
                                                  type_="date", required=True),
                     required=True),
            ui.field("Recorded by", ui.select("practitioner_id", doctor_options(), "",
                                              blank="Not stated")),
            ui.field("Source", ui.select("source", [
                ("Clinic", "Clinic"), ("Health camp", "Health camp"),
                ("Home / PHR app", "Home / PHR app"),
                ("Telephonic follow-up", "Telephonic follow-up")], "Clinic")),
            ui.field("Against encounter", ui.select("encounter_id", encounters, "",
                                                    blank="Not linked")),
            ui.field("Note", ui.text_input("note", "")),
            cols=3),
            footer=(f'<div class="display-flex justify-between gap"{st(gap=2)}>'
                    + ui.button("Cancel", "/wellness", style="z-button-secondary")
                    + ui.button("Create record", style="z-button-primary",
                                size="z-button-medium", type_="submit") + "</div>"))
        + "</form>")
    return render(request, "New wellness record", "wellness", body,
                  breadcrumb=[("Wellness", "/wellness"), ("New record", None)])


def create(request: Request):
    patient_id = request.f_int("patient_id")
    if not patient_id:
        return redirect("/wellness/new", "Select a patient first.", "danger")
    record_id = wellness.create_record(
        patient_id, request.f_int("encounter_id"), request.f_int("practitioner_id"),
        request.f_or_none("recorded_on"), request.f_or_none("source"),
        request.f_or_none("note"))
    number = db.scalar("SELECT record_no FROM wellness_record WHERE id = ?",
                       (record_id,))
    return redirect(f"/wellness/{record_id}", f"Record {number} created.")


# ---------------------------------------------------------------------------
def _section_view(rows) -> str:
    table_rows = []
    for obs in rows:
        # generated rows carry a text-only concept: no code, label in code_text
        code = obs["loinc_code"] or obs["snomed_code"]
        display = (obs["loinc_display"] or obs["snomed_display"]
                   or obs["code_text"] or "Observation")
        if obs["value_code"]:
            value = ui.esc(obs["value_display"])
        elif obs["value_quantity"] is not None:
            value = f'{obs["value_quantity"]:g} {ui.esc(obs["value_unit"] or "")}'
            chip = ui.flag_chip(obs["interpretation"], show_normal=False)
            if chip:
                value += " " + chip
        else:
            value = ui.esc(obs["value_string"] or "—")
        table_rows.append([
            ui.esc(ui.short_label(display)), value,
            f'<code class="z-codespan">{ui.esc(code)}</code>' if code
            else f'<span class="text-xs color"{st(color="var(--z-muted-f)")}>'
                 "text only</span>"])
    return ui.table(["Observation", "Value", "Code"], table_rows,
                    empty="Nothing recorded in this section.")


def _section_form(wid: int, key: str, rows) -> str:
    title, kind, _profile, value_kind = wellness.SECTIONS[key]
    current = {}
    for obs in rows:
        current[obs["loinc_code"] or obs["snomed_code"]] = obs

    fields = []
    for master in db.terms(kind):
        obs = current.get(master["code"])
        answers = wellness.answer_options(master)
        label = f'{ui.short_label(master["display"])}'
        if master["unit"]:
            label += f' ({master["unit"]})'
        help_text = f'{"SNOMED CT" if "snomed" in master["system"] else "LOINC"} ' \
                    f'{master["code"]}'
        if answers:
            control = ui.select(f'coded_{master["code"]}',
                                [(a["code"], a["display"]) for a in answers],
                                obs["value_code"] if obs else "", blank="Not recorded")
        elif value_kind == "coded-only":
            continue
        elif master["unit"]:
            value = ""
            if obs is not None and obs["value_quantity"] is not None:
                value = obs["value_quantity"]
            control = ui.text_input(f'value_{master["code"]}', value, type_="number",
                                    attrs='step="0.01"')
            if master["ref_low"] is not None or master["ref_high"] is not None:
                help_text += (f' · reference {master["ref_low"] or ""}'
                              f'–{master["ref_high"] or ""}')
        else:
            value = ""
            if obs is not None:
                value = obs["value_string"] or (obs["value_quantity"] or "")
            control = ui.text_input(f'value_{master["code"]}', value)
        fields.append(ui.field(label, control, help_text=help_text))

    return (f'<form method="post" action="/wellness/{wid}/sections/{key}">'
            + f'<div class="display-grid gap sm:grid-cols lg:grid-cols"'
            + st(gap=3, sm_grid_cols=2, lg_grid_cols=3) + ">"
            + "".join(fields) + "</div>"
            + f'<div class="display-flex justify-end"{st()}>'
            + ui.button(f"Save {title.lower()}", style="z-button-primary",
                        type_="submit", ico="save")
            + "</div></form>")


def detail(request: Request):
    wid = request.params["wid"]
    row = wellness.record(wid)
    if row is None:
        return not_found(request, "Wellness record")
    grouped = wellness.grouped(wid)
    editable = row["status"] == "draft" and not row["dialysis_session_id"]

    extra = (ui.label_chip(row["record_no"], "info")
             + ui.label_chip(*wellness.STATUS[row["status"]]))
    if row["dialysis_session_id"]:
        # Machine-written: nothing here can be hand-edited, so the only sensible
        # action is to rebuild it from the run.
        extra += ui.post_button(f"/wellness/{wid}/regenerate", "Regenerate",
                                size="z-button-small", ico="refresh-cw")
    elif editable:
        extra += ui.post_button(f"/wellness/{wid}/finalise", "Sign record",
                                style="z-button-primary", size="z-button-small",
                                ico="circle-check")
    else:
        extra += ui.post_button(f"/wellness/{wid}/reopen", "Reopen",
                                size="z-button-small", ico="rotate-ccw")

    facts = ui.card("Record", ui.dl([
        ("Record", row["record_no"]),
        ("Recorded on", row["recorded_on"]),
        ("Recorded by", row["doctor_name"]),
        ("Source", row["source"]),
        ("Encounter", row["encounter_no"]),
        ("Dialysis session", (
            f'<a class="z-link" href="/dialysis/sessions/'
            f'{row["dialysis_session_id"]}">'
            f'{ui.esc(db.scalar("SELECT session_no FROM dialysis_session WHERE id = ?", (row["dialysis_session_id"],)))}</a>')
         if row["dialysis_session_id"] else None),
        ("Observations", str(sum(len(v) for v in grouped.values()))),
        ("Note", row["note"]),
    ], cols=3))

    panes = []
    for key in wellness.SECTION_ORDER:
        title = wellness.SECTIONS[key][0]
        rows = grouped.get(key) or []
        generated = wellness.SECTIONS[key][3] == "generated"
        content = _section_view(rows)
        if generated:
            content = (f'<p class="color mb"{st(color="var(--z-muted-f)", mb=4)}>'
                       "Populated automatically from linked activity — dialysis "
                       "parameters land here because this is the one section whose "
                       "target profile accepts a concept with no bound code."
                       "</p>" + content)
        elif editable:
            content += (f'<div class="mt"{st(mt=5)}>'
                        + ui.accordion([(f"Record {title.lower()}",
                                         _section_form(wid, key, rows))],
                                       open_first=not rows) + "</div>")
        panes.append((f"{title} ({len(rows)})", ui.card(None, content)))

    body = (
        ui.patient_header(dict(row) | {"id": row["patient_id"],
                                       "name": row["patient_name"]}, extra=extra)
        + f'<div class="mt"{st(mt=5)}>' + facts + "</div>"
        + f'<div class="mt"{st(mt=5)}>' + ui.tabs(panes) + "</div>")
    return render(request, f'Wellness {row["record_no"]}', "wellness", body,
                  breadcrumb=[("Wellness", "/wellness"), (row["record_no"], None)])


# ---------------------------------------------------------------------------
def save_section(request: Request):
    wid = request.params["wid"]
    key = request.params["key"]
    if key not in wellness.SECTIONS:
        return not_found(request, "Wellness section")
    kind = wellness.SECTIONS[key][1]
    values, coded = {}, {}
    for master in db.terms(kind):
        values[master["code"]] = request.f(f'value_{master["code"]}')
        coded[master["code"]] = request.f(f'coded_{master["code"]}')
    try:
        written = wellness.save_section(wid, key, values, coded)
    except ValueError as error:
        return redirect(f"/wellness/{wid}", str(error), "danger")
    title = wellness.SECTIONS[key][0]
    return redirect(f"/wellness/{wid}",
                    f"{title}: {written} observation(s) recorded.")


def finalise(request: Request):
    wid = request.params["wid"]
    try:
        wellness.finalise(wid)
    except ValueError as error:
        return redirect(f"/wellness/{wid}", str(error), "danger")
    return redirect(f"/wellness/{wid}",
                    "Record signed. It can now be exported as a WellnessRecord.")


def reopen(request: Request):
    wid = request.params["wid"]
    try:
        wellness.reopen(wid)
    except ValueError as error:
        return redirect(f"/wellness/{wid}", str(error), "danger")
    return redirect(f"/wellness/{wid}", "Record reopened for editing.")


def regenerate(request: Request):
    wid = request.params["wid"]
    row = wellness.record(wid)
    if row is None:
        return not_found(request, "Wellness record")
    if not row["dialysis_session_id"]:
        return redirect(f"/wellness/{wid}",
                        "Only records generated from a dialysis run can be "
                        "regenerated.", "danger")
    new_id = wellness.regenerate_for_session(row["dialysis_session_id"])
    if new_id is None:
        return redirect("/wellness",
                        "That run no longer has anything to record.", "warning")
    return redirect(f"/wellness/{new_id}", "Record rebuilt from the dialysis run.")
