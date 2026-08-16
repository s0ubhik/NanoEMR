"""FHIR export centre — build, inspect, validate and download NRCES bundles."""

from __future__ import annotations

import json

from ... import db
from ...fhir import ARTIFACTS, persist_bundle
from ...terminology import PROFILE
from .. import ui
from ..common import patient_cell, not_found, render
from ..router import Request, json_response, redirect

st = ui.st

SOURCE_PICKERS = {
    "OPConsultRecord": (
        "OPD visit",
        "SELECT e.id, e.encounter_no || ' · ' || p.name AS label FROM encounter e "
        "JOIN patient p ON p.id = e.patient_id WHERE e.kind = 'OPD' ORDER BY e.id DESC"),
    "DischargeSummaryRecord": (
        "Discharged admission",
        "SELECT e.id, e.encounter_no || ' · ' || p.name AS label FROM encounter e "
        "JOIN patient p ON p.id = e.patient_id WHERE e.kind = 'IPD' "
        "AND e.status = 'finished' ORDER BY e.id DESC"),
    "DiagnosticReportRecord": (
        "Finalised lab report",
        "SELECT o.id, o.order_no || ' · ' || p.name AS label FROM lab_order o "
        "JOIN patient p ON p.id = o.patient_id WHERE o.status = 'final' "
        "ORDER BY o.id DESC"),
    "InvoiceRecord": (
        "Invoice",
        "SELECT i.id, i.invoice_no || ' · ' || p.name AS label FROM invoice i "
        "JOIN patient p ON p.id = i.patient_id ORDER BY i.id DESC"),
    "WellnessRecord": (
        "Signed wellness record",
        "SELECT w.id, w.record_no || ' · ' || p.name || ' · ' || w.recorded_on "
        "AS label FROM wellness_record w JOIN patient p ON p.id = w.patient_id "
        "WHERE w.status = 'final' ORDER BY w.id DESC"),
}


def register(app) -> None:
    app.add("GET", "/fhir", index)
    app.add("GET", "/fhir/profiles", profiles)
    app.add("GET", "/fhir/export", export)
    app.add("POST", "/fhir/export", export)
    app.add("GET", "/fhir/<int:xid>", detail)
    app.add("GET", "/fhir/<int:xid>/download", download)
    app.add("GET", "/fhir/<int:xid>/raw", raw)


# ---------------------------------------------------------------------------
def index(request: Request):
    rows = db.query(
        "SELECT x.*, p.name AS patient_name, p.mrn FROM fhir_export x "
        "JOIN patient p ON p.id = x.patient_id ORDER BY x.id DESC LIMIT 300")
    table_rows = [[
        f'<a class="z-link" href="/fhir/{r["id"]}">{ui.esc(r["artifact"])}</a>'
        f'<div class="text-xs color"{st(color="var(--z-muted-f)")}>'
        f'v{r["bundle_version"]}</div>',
        patient_cell(r),
        ui.esc(r["bundle_identifier"]),
        f'{r["resource_count"]} entries',
        (ui.label_chip("valid", "success") if r["valid"]
         else ui.label_chip(f'{len(json.loads(r["issues_json"]))} issue(s)', "danger")),
        ui.when(r["created_at"]),
        (ui.button("View", f'/fhir/{r["id"]}', size="z-button-xsmall")
         + ui.button("JSON", f'/fhir/{r["id"]}/download', size="z-button-xsmall",
                     attrs='style="margin-left:.35rem"')),
    ] for r in rows]

    pickers = []
    for artifact, (label, sql) in SOURCE_PICKERS.items():
        options = [(r["id"], r["label"]) for r in db.query(sql)]
        pickers.append(ui.card(ARTIFACTS[artifact], (
            f'<p class="text-xs color mb"{st(color="var(--z-muted-f)", mb=3)}>'
            f'{ui.esc(PROFILE[artifact])}</p>'
            + f'<form method="post" action="/fhir/export">'
            + f'<input type="hidden" name="artifact" value="{artifact}">'
            + ui.field(label, ui.select("source", options, "",
                                        blank="Select a source record", required=True))
            + ui.button("Generate bundle", style="z-button-primary", type_="submit",
                        ico="file-json")
            + "</form>")))

    body = (
        ui.card("What gets produced", (
            '<p>Every artifact is a <code class="z-codespan">Bundle</code> of type '
            '<code class="z-codespan">document</code> conforming to the NRCES '
            '<code class="z-codespan">DocumentBundle</code> profile, whose first entry '
            'is the profiled <code class="z-codespan">Composition</code>. Section codes, '
            'the Composition type coding and every mandatory element are taken straight '
            'from the published StructureDefinitions.</p>'
            f'<div class="mt"{st(mt=3)}>'
            + ui.button("Profile reference", "/fhir/profiles", ico="book-open")
            + "</div>"))
        + f'<div class="mt display-grid gap md:grid-cols"'
        + st(mt=5, gap=4, md_grid_cols=2) + ">" + "".join(pickers) + "</div>"
        + f'<div class="mt"{st(mt=5)}>'
        + ui.card(f"{len(table_rows)} generated bundle(s)", ui.table(
            ["Artifact", "Patient", "Identifier", "Size", "Validation", "Generated", ""],
            table_rows, empty="No bundles generated yet."))
        + "</div>")
    return render(request, "FHIR export centre", "fhir", body)


# ---------------------------------------------------------------------------
def export(request: Request):
    artifact = request.q("artifact") or request.f("artifact")
    source = request.q("source") or request.f("source")
    if artifact not in ARTIFACTS or not source:
        return redirect("/fhir", "Pick an artifact and a source record.", "danger")
    try:
        export_row = persist_bundle(artifact, int(source))
    except Exception as error:  # surfaced to the user rather than a 500 page
        return redirect("/fhir", f"Could not build the bundle: {error}", "danger")
    issues = json.loads(export_row["issues_json"])
    errors = [i for i in issues if i["severity"] == "error"]
    if errors:
        message = (f"{artifact} generated with {len(errors)} profile error(s) — "
                   "open it to see what is missing.")
        kind = "warning"
    else:
        message = f"{artifact} generated and validated against the NRCES profiles."
        kind = "success"
    return redirect(f"/fhir/{export_row['id']}", message, kind)


# ---------------------------------------------------------------------------
def detail(request: Request):
    xid = request.params["xid"]
    row = db.one("SELECT * FROM fhir_export WHERE id = ?", (xid,))
    if row is None:
        return not_found(request, "FHIR bundle")
    bundle = json.loads(row["bundle_json"])
    issues = json.loads(row["issues_json"])
    patient = db.one("SELECT * FROM patient WHERE id = ?", (row["patient_id"],))

    counts: dict[str, int] = {}
    for entry in bundle.get("entry", []):
        rtype = entry.get("resource", {}).get("resourceType", "?")
        counts[rtype] = counts.get(rtype, 0) + 1
    composition = bundle["entry"][0]["resource"] if bundle.get("entry") else {}

    summary = ui.dl([
        ("Artifact", row["artifact"]),
        ("Profile", f'<code class="z-codespan">{ui.esc(PROFILE[row["artifact"]])}</code>'),
        ("Bundle identifier", row["bundle_identifier"]),
        ("Version", f'v{row["bundle_version"]}'),
        ("Bundle type", bundle.get("type")),
        ("Timestamp", bundle.get("timestamp")),
        ("Entries", f'{row["resource_count"]} resources'),
        ("Validation", ui.label_chip("passes profile checks", "success") if row["valid"]
         else ui.label_chip(f"{len(issues)} issue(s)", "danger")),
    ], cols=3)

    composition_view = ui.dl([
        ("Title", composition.get("title")),
        ("Status", composition.get("status")),
        ("Type", json.dumps(composition.get("type", {}).get("coding",
                                                            composition.get("type", {})))),
        ("Date", composition.get("date")),
        ("Sections", ", ".join(s.get("title", "?")
                               for s in composition.get("section", [])) or "—"),
    ], cols=2)

    resource_rows = [[ui.esc(rtype), str(count),
                      f'<code class="z-codespan">{ui.esc(PROFILE.get(rtype, "—"))}</code>'
                      if rtype in PROFILE else "—"]
                     for rtype, count in sorted(counts.items())]

    if issues:
        issue_rows = [[
            (ui.label_chip("error", "danger") if i["severity"] == "error"
             else ui.label_chip("warning", "warning")),
            f'<code class="z-codespan">{ui.esc(i["path"])}</code>',
            ui.esc(i["message"]),
        ] for i in issues]
        validation = ui.table(["Severity", "Path", "Detail"], issue_rows)
    else:
        validation = (f'<div class="z-alert z-alert-success" data-z-alert>'
                      "Every mandatory element and fixed coding required by the "
                      f"NRCES {row['artifact']} and DocumentBundle profiles is present."
                      "</div>")

    sections = []
    for entry in bundle.get("entry", []):
        resource = entry.get("resource", {})
        title = (f'{resource.get("resourceType", "?")} — '
                 f'{entry.get("fullUrl", "")[:48]}')
        sections.append((title, ui.code_block(json.dumps(resource, indent=2,
                                                         ensure_ascii=False))))

    body = (
        ui.card(None, (
            f'<div class="display-flex items-center justify-between gap flex-wrap"'
            f'{st(gap=3)}>'
            f'<div><div class="z-h4">{ui.esc(row["artifact"])}</div>'
            f'<div class="mt text-sm color"{st(mt=1, color="var(--z-muted-f)")}>'
            f'{ui.esc(patient["name"])} · {ui.esc(patient["mrn"])}</div></div>'
            f'<div class="display-flex gap flex-wrap"{st(gap=2)}>'
            + ui.button("Download JSON", f"/fhir/{xid}/download", style="z-button-primary",
                        ico="download")
            + ui.button("Open raw", f"/fhir/{xid}/raw", ico="code")
            + ui.button("Back to export centre", "/fhir", style="z-button-secondary")
            + "</div></div>"))
        + f'<div class="mt"{st(mt=5)}>' + ui.card("Bundle", summary) + "</div>"
        + f'<div class="mt"{st(mt=5)}>'
        + ui.tabs([
            ("Composition", ui.card(None, composition_view)),
            ("Resources", ui.card(None, ui.table(
                ["Resource type", "Count", "NRCES profile"], resource_rows))),
            ("Profile validation", ui.card(None, validation)),
            ("Raw JSON", ui.card(None, ui.code_block(
                json.dumps(bundle, indent=2, ensure_ascii=False)))),
            ("Per-resource", ui.card(None, ui.accordion(sections, multiple=True,
                                                        open_first=False))),
        ])
        + "</div>")

    return render(request, f"{row['artifact']} · {row['bundle_identifier']}", "fhir", body,
                  breadcrumb=[("FHIR", "/fhir"), (row["bundle_identifier"], None)])


def download(request: Request):
    row = db.one("SELECT * FROM fhir_export WHERE id = ?", (request.params["xid"],))
    if row is None:
        return not_found(request, "FHIR bundle")
    filename = f'{row["artifact"]}-{row["bundle_identifier"]}.json'.replace("/", "-")
    return json_response(json.loads(row["bundle_json"]), download=filename)


def raw(request: Request):
    row = db.one("SELECT * FROM fhir_export WHERE id = ?", (request.params["xid"],))
    if row is None:
        return not_found(request, "FHIR bundle")
    return json_response(json.loads(row["bundle_json"]))


# ---------------------------------------------------------------------------
def profiles(request: Request):
    clinical = ["OPConsultRecord", "DischargeSummaryRecord", "DiagnosticReportRecord",
                "InvoiceRecord", "WellnessRecord", "DocumentBundle"]
    rows = [[ui.esc(name),
             f'<a class="z-link" href="https://www.nrces.in/ndhm/fhir/r4/'
             f'StructureDefinition-{name}.html" target="_blank" rel="noreferrer">'
             f'<code class="z-codespan">{ui.esc(PROFILE[name])}</code></a>']
            for name in clinical]
    supporting = [n for n in PROFILE if n not in clinical]
    support_rows = [[ui.esc(name),
                     f'<code class="z-codespan">{ui.esc(PROFILE[name])}</code>']
                    for name in sorted(supporting)]

    body = (
        ui.card("Clinical artifacts produced by this EMR",
                ui.table(["Profile", "Canonical URL"], rows))
        + f'<div class="mt"{st(mt=5)}>'
        + ui.card("Supporting resource profiles referenced",
                  ui.table(["Profile", "Canonical URL"], support_rows))
        + "</div>"
        + f'<div class="mt"{st(mt=5)}>'
        + ui.card("Mapping cheatsheet", ui.accordion([
            ("Patient registration → Patient",
             "<p>MRN becomes <code class=\"z-codespan\">Patient.identifier</code> typed "
             "<code class=\"z-codespan\">MR</code> (HL7 v2-0203); ABHA number and ABHA "
             "address become identifiers typed <code class=\"z-codespan\">ABHA</code> and "
             "<code class=\"z-codespan\">HIN</code> from the NDHM identifier code system. "
             "<code class=\"z-codespan\">Patient.name.text</code> and "
             "<code class=\"z-codespan\">Patient.telecom.value</code> are mandatory.</p>"),
            ("OPD visit → Encounter + OPConsultRecord",
             "<p>The visit becomes an <code class=\"z-codespan\">Encounter</code> with "
             "class <code class=\"z-codespan\">AMB</code>. Complaints, vitals, diagnoses, "
             "prescription, investigation advice and follow-up map to the Chief "
             "complaints, Physical examination, Medical history, Medications, "
             "Investigation advice and Follow up sections.</p>"),
            ("IPD admission & discharge → DischargeSummaryRecord",
             "<p>Class <code class=\"z-codespan\">IMP</code>, ward and bed become "
             "<code class=\"z-codespan\">Encounter.location</code>, disposition becomes "
             "<code class=\"z-codespan\">Encounter.hospitalization.dischargeDisposition</code>. "
             "Finalised lab reports feed the Investigations section and the discharge "
             "narrative feeds a <code class=\"z-codespan\">CarePlan</code>.</p>"),
            ("Lab report → DiagnosticReportRecord",
             "<p>The order becomes a <code class=\"z-codespan\">DiagnosticReportLab</code> "
             "with a LOINC code, a SNOMED category, a mandatory "
             "<code class=\"z-codespan\">resultsInterpreter</code>, a mandatory "
             "<code class=\"z-codespan\">conclusion</code>, a "
             "<code class=\"z-codespan\">Specimen</code> and one "
             "<code class=\"z-codespan\">Observation</code> per analyte.</p>"),
            ("Wellness capture → WellnessRecord",
             "<p>Vitals, body measurements, activity, general assessment, women's "
             "health and lifestyle each become an Observation on its own NRCES "
             "profile. Unusually, this record slices its sections by a fixed "
             "<code class=\"z-codespan\">title</code> and puts no "
             "<code class=\"z-codespan\">code</code> on them, and the Lifestyle "
             "profile permits only a "
             "<code class=\"z-codespan\">valueCodeableConcept</code>.</p>"),
            ("Bill → InvoiceRecord",
             "<p>Each line item carries price components coded from "
             "<code class=\"z-codespan\">ndhm-price-components</code> (Rate, Discount, "
             "CGST, SGST) and the invoice type comes from "
             "<code class=\"z-codespan\">ndhm-invoice-types</code>. "
             "<code class=\"z-codespan\">Composition.type.text</code> is fixed to "
             "\"Invoice Record\".</p>"),
        ], multiple=True, open_first=False))
        + "</div>")
    return render(request, "NRCES profile reference", "fhir", body,
                  breadcrumb=[("FHIR", "/fhir"), ("Profiles", None)])
