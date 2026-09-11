#!/usr/bin/env python3
"""End-to-end self test

    python3 selftest.py          # exit code 0 when every check passes

Builds a throwaway SQLite database under /tmp, walks every workflow the app
supports, exports all five NRCES artifacts, checks them against the profile
rules, and then deliberately breaks things — bundles, writes, permissions — to
prove the guards are not vacuously passing.

**How it is organised.** Checks are grouped into named sections purely for
readable output; they all run in one process against one seeded database,
because the later sections need records the earlier ones create. `section()`
starts a group, `check()` asserts one thing. Nothing raises: every failure is
reported and tallied, and the run exits non-zero at the end.

**Adding a test.** Put it in the section it belongs to, or open a new one with
`section("...")`. Prefer a check that would have caught a real bug over one that
restates the implementation — the regression sections near the bottom are all
defects that reached working code and are named after what went wrong.
"""

from __future__ import annotations

import base64
import copy
import json
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
os.environ["NANOEMR_DB"] = os.path.join(tempfile.mkdtemp(prefix="nanoemr-test-"), "t.db")

from emr import db, dialysis, hospital, seed, services, wellness  # noqa: E402
from emr.fhir import build_bundle, validate_bundle  # noqa: E402
from emr.web import ui  # noqa: E402
from emr.web.router import App  # noqa: E402
from emr.web.routes import register_all  # noqa: E402

PASS, FAIL = "  ok  ", " FAIL "
failures = 0
current = "general"
tally: dict[str, list[int]] = {}


def section(name: str) -> None:
    """Start a named group of checks. Purely for readable output — every check
    still runs in one process against one seeded database."""
    global current
    current = name
    tally.setdefault(name, [0, 0])
    print(f"\n\u2500\u2500 {name} " + "\u2500" * max(3, 60 - len(name)))


def check(label: str, condition: bool, detail: str = "") -> None:
    """Assert one thing. Never raises: the run reports every failure at the end
    rather than stopping at the first."""
    global failures
    tally.setdefault(current, [0, 0])
    tally[current][0 if condition else 1] += 1
    if not condition:
        failures += 1
    print(f"[{PASS if condition else FAIL}] {label}" + (f" — {detail}" if detail else ""))


def _cancel_refusal(module, claim_id: int) -> str:
    """The message a cancellation is refused with, for the checks below."""
    try:
        module.cancel_preauth(claim_id, "patientrequest")
    except ValueError as error:
        return str(error)
    return ""


def main() -> int:
    seed.bootstrap()
    check("terminology seeded",
          db.scalar("SELECT COUNT(*) FROM terminology", default=0) > 150)
    check("organization seeded", db.default_org() is not None)
    check("practitioners seeded",
          db.scalar("SELECT COUNT(*) FROM practitioner", default=0) == 6)

    physician = db.scalar("SELECT id FROM practitioner WHERE department='General Medicine'")
    pathologist = db.scalar("SELECT id FROM practitioner WHERE department='Laboratory'")

    section("registration")
    patient = services.create_patient({
        "name": "Selftest Patient", "gender": "female", "birth_date": "1985-06-01",
        "phone": "+919000000000", "abha_number": "91-0000-1111-2222",
        "abha_address": "selftest@abdm", "city": "Chennai", "state": "Tamil Nadu",
        "address_line": "1 Test Road", "postal_code": "600001",
    })
    mrn = db.scalar("SELECT mrn FROM patient WHERE id = ?", (patient,))
    check("patient gets an MRN", mrn == "MRN00001", mrn)

    section("OPD visit")
    visit = services.create_encounter("OPD", {
        "patient_id": patient, "status": "finished", "class_code": "AMB",
        "type_code": "11429006", "type_display": "Consultation",
        "service_type_code": "394802001", "service_type_display": "General medicine",
        "priority_code": "R", "priority_display": "routine",
        "priority_system": "http://terminology.hl7.org/CodeSystem/v3-ActPriority",
        "practitioner_id": physician, "department": "General Medicine",
        "period_start": db.now_iso(), "consultation_fee": 700,
    })
    db.insert("condition", {
        "patient_id": patient, "encounter_id": visit, "category": "chief-complaint",
        "clinical_status": "active", "verification_status": "confirmed",
        "snomed_code": "386661006", "snomed_display": "Fever", "text": "Fever",
        "recorded_at": db.now_iso()})
    db.insert("condition", {
        "patient_id": patient, "encounter_id": visit, "category": "diagnosis",
        "clinical_status": "active", "verification_status": "confirmed",
        "snomed_code": "38362002", "snomed_display": "Dengue",
        "icd10_code": "A90", "icd10_display": "Dengue fever [classical dengue]",
        "text": "Dengue", "recorded_at": db.now_iso()})
    recorded = services.record_vitals(patient, visit, {
        "8310-5": "38.9", "8867-4": "104", "8480-6": "118", "8462-4": "76",
        "39156-5": ""})
    check("vitals recorded and flagged", recorded == 4, f"{recorded} observations")
    flags = {r["loinc_code"]: r["interpretation"] for r in db.query(
        "SELECT loinc_code, interpretation FROM observation WHERE encounter_id = ?",
        (visit,))}
    check("temperature flagged high", flags.get("8310-5") == "H", str(flags))
    check("systolic BP flagged normal", flags.get("8480-6") == "N", str(flags))

    db.insert("medication_request", {
        "patient_id": patient, "encounter_id": visit, "status": "active",
        "intent": "order", "snomed_code": "387517004", "snomed_display": "Paracetamol",
        "dose_quantity": 500, "dose_unit": "mg", "route_code": "26643006",
        "route_display": "Oral route", "frequency": 3, "period": 1, "period_unit": "d",
        "duration_days": 5, "timing_text": "Paracetamol 500 mg thrice daily",
        "authored_on": db.now_iso(), "requester_id": physician})
    services.upsert_note(visit, patient, "OPD_NOTE", {
        "author_id": physician, "history_text": "Fever for three days.",
        "examination_text": "Febrile, no rash.", "advice_text": "Fluids.",
        "follow_up_date": db.today_iso(), "status": "final"})

    section("chart-level capture")
    services.record_vitals(patient, None, {"29463-7": "62", "8302-2": "160"},
                           effective_ts="2026-08-01T09:30", note="pre-visit weigh-in")
    standalone = [b for b in services.vital_sets(patient) if b["encounter_id"] is None]
    check("vitals can be captured without an encounter", len(standalone) == 1)
    bmi = standalone[0]["readings"].get("39156-5")
    check("BMI is derived from height and weight",
          bmi is not None and abs(bmi["value_quantity"] - 24.2) < 0.1,
          str(bmi["value_quantity"]) if bmi else "missing")
    check("vitals batches are grouped by capture time",
          len(services.vital_sets(patient)) == 2,
          str(len(services.vital_sets(patient))))
    check("the encounter batch keeps its encounter link",
          services.latest_vitals(patient)["encounter_id"] in (None, visit))

    services.add_allergy(patient, None, "91936005", "", "medication", "high",
                         "126485001", "Urticaria within an hour")
    services.add_allergy(patient, None, None, "Brinjal", "food", "low", None, None)
    allergy_rows = db.query("SELECT * FROM allergy WHERE patient_id = ?", (patient,))
    check("two allergies recorded", len(allergy_rows) == 2)
    coded = [a for a in allergy_rows if a["snomed_code"]][0]
    check("a coded allergen takes its display from the master",
          coded["text"] == "Allergy to penicillin" and coded["criticality"] == "high")
    check("a text-only allergen is accepted",
          any(a["snomed_code"] is None and a["text"] == "Brinjal" for a in allergy_rows))
    try:
        services.add_allergy(patient, None, None, "", None, None, None, None)
        check("an empty allergy is rejected", False)
    except ValueError:
        check("an empty allergy is rejected", True)

    services.add_problem(patient, None, "40930008", "", onset="2019-06-01",
                         note="On thyroxine")
    chart_problem = db.one(
        "SELECT * FROM condition WHERE patient_id = ? AND encounter_id IS NULL",
        (patient,))
    check("a chart-level problem carries both codings",
          chart_problem["snomed_code"] == "40930008"
          and chart_problem["icd10_code"] == "E03.9")

    section("lab")
    order = services.create_lab_order(patient, visit, "58410-2", pathologist, None)
    analytes = db.query(
        "SELECT id FROM observation WHERE lab_order_id = ? ORDER BY sort_order", (order,))
    check("panel expanded into analytes", len(analytes) == 8, f"{len(analytes)}")
    values = dict(zip([f"result_{r['id']}" for r in analytes],
                      ["9.8", "31", "3.9", "13.4", "88", "78", "24", "30"]))
    services.save_lab_results(order, values, "", finalise=True)
    lab = db.one("SELECT * FROM lab_order WHERE id = ?", (order,))
    check("abnormal results drive the conclusion code",
          lab["conclusion_code"] == "263654008", str(lab["conclusion_display"]))
    check("conclusion is never empty", bool(lab["conclusion"]))

    section("billing")
    invoice = services.create_invoice(patient, visit, "03", "OPD", physician, None)
    services.save_invoice(invoice, [
        {"description": "Consultation", "charge_system": "x", "charge_code": "CONS-SPL",
         "charge_display": "Specialist consultation", "quantity": 1, "unit_price": 1000,
         "discount_pct": 10, "cgst_pct": 9, "sgst_pct": 9},
    ])
    inv = db.one("SELECT * FROM invoice WHERE id = ?", (invoice,))
    check("invoice net applies the discount", inv["total_net"] == 900, str(inv["total_net"]))
    check("invoice gross applies CGST + SGST", inv["total_gross"] == 1062,
          str(inv["total_gross"]))

    section("IPD")
    admission = services.create_encounter("IPD", {
        "patient_id": patient, "status": "finished", "class_code": "IMP",
        "type_code": "32485007", "type_display": "Hospital admission",
        "practitioner_id": physician, "department": "General Medicine",
        "period_start": db.now_iso(), "discharge_ts": db.now_iso(),
        "ward": "General Ward (Female)", "bed": "GW-2", "bed_rate": 1500,
        "discharge_disposition_code": "home", "discharge_disposition_display": "Home"})
    db.insert("condition", {
        "patient_id": patient, "encounter_id": admission, "category": "diagnosis",
        "clinical_status": "active", "verification_status": "confirmed",
        "snomed_code": "233604007", "snomed_display": "Pneumonia",
        "icd10_code": "J18", "icd10_display": "Pneumonia, unspecified organism",
        "text": "Pneumonia", "recorded_at": db.now_iso()})
    ipd_lab = services.create_lab_order(patient, admission, "24362-6", pathologist, None)
    ipd_analytes = db.query(
        "SELECT id FROM observation WHERE lab_order_id = ? ORDER BY sort_order", (ipd_lab,))
    services.save_lab_results(
        ipd_lab, {f"result_{r['id']}": v for r, v in
                  zip(ipd_analytes, ["1.0", "14", "140", "4.0", "102"])},
        "", finalise=True)
    services.upsert_note(admission, patient, "DISCHARGE_SUMMARY", {
        "author_id": physician, "admission_reason": "Breathlessness and fever.",
        "course_in_hospital": "IV antibiotics for five days, afebrile from day three.",
        "condition_at_discharge": "Stable.", "discharge_instructions": "Complete course.",
        "care_plan_text": "Chest x-ray in two weeks.", "status": "final"})
    ipd_invoice = services.create_invoice(patient, admission, "02", "IPD", physician, None)
    services.save_invoice(ipd_invoice, services.suggest_invoice_lines(admission))
    check("IPD bill auto-suggests bed and nursing lines",
          db.scalar("SELECT COUNT(*) FROM invoice_line WHERE invoice_id = ?",
                    (ipd_invoice,), default=0) >= 2)

    section("FHIR export")
    artifacts = [
        ("OPConsultRecord", visit),
        ("DischargeSummaryRecord", admission),
        ("DiagnosticReportRecord", order),
        ("DiagnosticReportRecord", ipd_lab),
        ("InvoiceRecord", invoice),
        ("InvoiceRecord", ipd_invoice),
    ]
    bundles = {}
    for artifact, source in artifacts:
        bundle = build_bundle(artifact, source)
        issues = validate_bundle(artifact, bundle)
        errors = [i for i in issues if i["severity"] == "error"]
        bundles[(artifact, source)] = bundle
        check(f"{artifact} (#{source}) passes the NRCES profile gate", not errors,
              "; ".join(f'{i["path"]}: {i["message"]}' for i in errors[:3]))
        check(f"{artifact} (#{source}) is a document bundle",
              bundle["type"] == "document"
              and bundle["entry"][0]["resource"]["resourceType"] == "Composition")
        check(f"{artifact} (#{source}) round-trips through JSON",
              json.loads(json.dumps(bundle)) == bundle)

    op = bundles[("OPConsultRecord", visit)]
    op_sections = {s["code"]["coding"][0]["code"]: s
                   for s in op["entry"][0]["resource"]["section"]}
    check("OP consult carries the chief-complaint section", "422843007" in op_sections)
    check("OP consult carries the medications section", "721912009" in op_sections)
    check("captured allergies reach the Allergies section",
          len(op_sections.get("722446000", {}).get("entry", [])) == 2)
    check("captured vitals reach the Physical examination section",
          len(op_sections.get("425044008", {}).get("entry", [])) >= 4)

    allergy_resources = [e["resource"] for e in op["entry"]
                         if e["resource"]["resourceType"] == "AllergyIntolerance"]
    check("a coded allergy uses the SNOMED system",
          any(a.get("code", {}).get("coding", [{}])[0].get("system")
              == "http://snomed.info/sct" for a in allergy_resources))
    check("a text-only allergy still satisfies code 1..1",
          any("coding" not in a.get("code", {}) and a["code"].get("text")
              for a in allergy_resources))

    history_entries = op_sections.get("371529009", {}).get("entry", [])
    check("a chart-level problem reaches the Medical history section",
          len(history_entries) >= 2, f"{len(history_entries)} entries")

    ds = bundles[("DischargeSummaryRecord", admission)]
    ds_codes = [s["code"]["coding"][0]["code"]
                for s in ds["entry"][0]["resource"]["section"]]
    check("discharge summary carries the investigations section", "721981007" in ds_codes)
    check("discharge summary carries the care plan section", "734163000" in ds_codes)

    inv_bundle = bundles[("InvoiceRecord", invoice)]
    inv_res = next(e["resource"] for e in inv_bundle["entry"]
                   if e["resource"]["resourceType"] == "Invoice")
    component_codes = {c["code"]["coding"][0]["code"]
                       for li in inv_res["lineItem"] for c in li["priceComponent"]}
    check("invoice uses ndhm-price-components 01/02/03/04",
          {"01", "02", "03", "04"} <= component_codes, str(sorted(component_codes)))
    check("InvoiceRecord fixes Composition.type.text",
          inv_bundle["entry"][0]["resource"]["type"]["text"] == "Invoice Record")

    # deterministic ids: rebuilding must produce byte-identical resource ids
    again = build_bundle("OPConsultRecord", visit)
    check("resource identity is stable across rebuilds",
          [e["fullUrl"] for e in again["entry"]] == [e["fullUrl"] for e in op["entry"]])

    section("negative testing")
    mutations = [
        ("missing Bundle.timestamp", lambda b: b.pop("timestamp", None)),
        ("Bundle.type not document", lambda b: b.update(type="collection")),
        ("missing Bundle.meta.versionId", lambda b: b["meta"].pop("versionId", None)),
        ("wrong Composition profile",
         lambda b: b["entry"][0]["resource"]["meta"].update(profile=["http://x"])),
        ("missing Composition.author",
         lambda b: b["entry"][0]["resource"].pop("author", None)),
        ("dangling reference",
         lambda b: b["entry"][0]["resource"]["subject"].update(reference="urn:uuid:0")),
    ]
    for label, mutate in mutations:
        broken = copy.deepcopy(op)
        mutate(broken)
        errors = [i for i in validate_bundle("OPConsultRecord", broken)
                  if i["severity"] == "error"]
        check(f"validator catches: {label}", bool(errors))

    section("ward and beds")
    check("ward and bed master seeded",
          db.scalar("SELECT COUNT(*) FROM bed WHERE active = 1", default=0) == 18,
          str(db.scalar("SELECT COUNT(*) FROM bed", default=0)))
    stats = hospital.bed_stats()
    check("every bed starts vacant", stats["vacant"] == 18 and stats["occupied"] == 0)

    icu = db.one("SELECT b.id, b.code FROM bed b JOIN ward w ON w.id = b.ward_id "
                 "WHERE w.code = 'ICU' AND b.status = 'vacant' ORDER BY b.code")
    placed = hospital.occupy_bed(icu["id"], admission, reason="Admission")
    check("admitting occupies a bed", placed["bed"] == icu["code"]
          and hospital.bed_stats()["occupied"] == 1)
    check("the encounter records ward and tariff",
          db.scalar("SELECT bed_rate FROM encounter WHERE id = ?", (admission,)) == 12000)

    second = db.one("SELECT id, code FROM bed WHERE status = 'vacant' "
                    "AND ward_id = (SELECT id FROM ward WHERE code = 'GWM') LIMIT 1")
    try:
        hospital.occupy_bed(icu["id"], visit)
        check("an occupied bed cannot be double-booked", False)
    except ValueError:
        check("an occupied bed cannot be double-booked", True)

    hospital.transfer_bed(admission, second["id"], "Stepped down from ICU")
    moves = db.query("SELECT * FROM bed_movement WHERE encounter_id = ? ORDER BY id",
                     (admission,))
    check("a transfer closes the old movement and opens a new one",
          len(moves) == 2 and moves[0]["to_ts"] and moves[1]["to_ts"] is None)
    check("the vacated bed goes to cleaning",
          db.scalar("SELECT status FROM bed WHERE id = ?", (icu["id"],)) == "cleaning")

    charges = hospital.stay_charges(admission)
    check("both wards bill at their own tariff",
          {c["rate"] for c in charges if c["kind"] == "bed"} == {12000.0, 1500.0},
          str(sorted({c["rate"] for c in charges if c["kind"] == "bed"})))

    hospital.release_bed(admission)
    check("discharge frees the bed",
          db.scalar("SELECT COUNT(*) FROM bed WHERE status = 'occupied'", default=0) == 0)

    section("pharmacy")
    check("stock master seeded",
          db.scalar("SELECT COUNT(*) FROM stock_item", default=0) == 20)
    para = db.one("SELECT * FROM stock_item WHERE code = 'MED001'")
    check("a new item starts with no stock", hospital.stock_on_hand(para["id"]) == 0)
    check("an empty item is flagged for reorder",
          any(i["id"] == para["id"] for i in hospital.low_stock()))

    hospital.receive_stock(para["id"], "OLD1", "2026-09-30", 40, 1.0, "Supplier A")
    hospital.receive_stock(para["id"], "NEW1", "2027-06-30", 100, 1.1, "Supplier B")
    check("goods receipt adds stock", hospital.stock_on_hand(para["id"]) == 140)

    picked = hospital.issue_stock(para["id"], 50, patient, visit, "OPD prescription")
    check("issue consumes the nearest expiry first",
          picked[0]["batch_no"] == "OLD1" and picked[0]["quantity"] == 40
          and picked[1]["batch_no"] == "NEW1" and picked[1]["quantity"] == 10,
          str(picked))
    check("stock falls by the issued quantity",
          hospital.stock_on_hand(para["id"]) == 90)
    try:
        hospital.issue_stock(para["id"], 10_000, patient, visit, None)
        check("issuing more than is in stock is refused", False)
    except ValueError:
        check("issuing more than is in stock is refused", True)
    # A batch is only "expiring" while it still holds stock — OLD1 was fully
    # consumed above, so use a fresh near-expiry batch to prove the alert.
    hospital.receive_stock(para["id"], "SOON1", "2026-09-15", 25, 1.0, "Supplier A")
    soon = hospital.expiring_batches(60)
    check("expiring batches with stock are surfaced",
          any(b["batch_no"] == "SOON1" for b in soon),
          str([b["batch_no"] for b in soon]))
    check("exhausted batches drop off the expiry alert",
          not any(b["batch_no"] == "OLD1" for b in hospital.expiring_batches(400)))

    unbilled = hospital.unbilled_issues(visit)
    check("unbilled issues are collected for the bill",
          len(unbilled) == 1 and unbilled[0]["qty"] == 50, str(unbilled))

    section("money and queue")
    ipd_bill = services.create_invoice(patient, admission, "02", "IPD", physician, None)
    services.save_invoice(ipd_bill, services.suggest_invoice_lines(admission))
    ipd_lines = db.query("SELECT * FROM invoice_line WHERE invoice_id = ?", (ipd_bill,))
    check("the IPD bill is built from the bed ledger",
          any("Intensive Care" in l["description"] for l in ipd_lines)
          and any("General Ward (Male)" in l["description"] for l in ipd_lines),
          str([l["description"] for l in ipd_lines][:4]))

    pharmacy_bill = services.create_invoice(patient, visit, "03", "OPD", physician, None)
    services.save_invoice(pharmacy_bill, services.suggest_invoice_lines(visit))
    pharm_line = [l for l in db.query(
        "SELECT * FROM invoice_line WHERE invoice_id = ?", (pharmacy_bill,))
        if "Paracetamol" in l["description"]]
    check("pharmacy issues flow onto the bill with GST split",
          len(pharm_line) == 1 and pharm_line[0]["cgst_pct"] == 6
          and pharm_line[0]["sgst_pct"] == 6, str(pharm_line and dict(pharm_line[0])))
    hospital.mark_issues_billed(visit)
    check("billed issues are not billed twice", hospital.unbilled_issues(visit) == [])

    total = db.scalar("SELECT total_gross FROM invoice WHERE id = ?", (pharmacy_bill,))
    hospital.record_payment(pharmacy_bill, round(total / 2, 2), "cash", None, None)
    invoice_now = db.one("SELECT * FROM invoice WHERE id = ?", (pharmacy_bill,))
    check("a part payment leaves the invoice open",
          invoice_now["status"] == "issued"
          and abs(invoice_now["amount_paid"] - round(total / 2, 2)) < 0.01)
    try:
        hospital.record_payment(pharmacy_bill, total, "cash", None, None)
        check("overpayment is refused", False)
    except ValueError:
        check("overpayment is refused", True)
    hospital.record_payment(pharmacy_bill, round(total - round(total / 2, 2), 2),
                            "upi", "UPI/1", None)
    check("settling the balance closes the invoice",
          db.scalar("SELECT status FROM invoice WHERE id = ?",
                    (pharmacy_bill,)) == "balanced")
    check("receipts are numbered",
          db.scalar("SELECT COUNT(*) FROM payment WHERE invoice_id = ?",
                    (pharmacy_bill,), default=0) == 2)
    unpaid = [r["id"] for r in hospital.outstanding_invoices()]
    check("an unpaid invoice appears in the dues list", ipd_bill in unpaid, str(unpaid))
    check("a settled invoice drops off the dues list", pharmacy_bill not in unpaid)

    appt = hospital.book_appointment(patient, physician, "General Medicine",
                                     db.today_iso(), "10:30", "Fever", None)
    appt2 = hospital.book_appointment(patient, physician, "General Medicine",
                                      db.today_iso(), "10:45", "Review", None)
    tokens = [a["token"] for a in hospital.appointments_for(db.today_iso())]
    check("tokens are issued in order per doctor", tokens == [1, 2], str(tokens))
    hospital.set_appointment_status(appt, "arrived")
    check("appointment status moves through the queue",
          db.scalar("SELECT status FROM appointment WHERE id = ?", (appt,)) == "arrived")
    hospital.set_appointment_status(appt2, "cancelled")
    check("a cancelled slot is kept for the record",
          db.scalar("SELECT status FROM appointment WHERE id = ?",
                    (appt2,)) == "cancelled")

    section("0build markup contract")
    # The switcher hides panes through the `z-switcher` CLASS; using the
    # `data-z-switcher` attribute on the content container instead leaves every
    # pane visible and the tab bar inert, so pin the contract down here.
    rendered = ui.tabs([("One", "<p>1</p>"), ("Two", "<p>2</p>"),
                        ("Three", "<p>3</p>")], active=1)
    check("tab nav connects to the switcher by id",
          'data-z-tab="connect: #emr-switcher"' in rendered)
    check("switcher container carries the z-switcher class",
          '<ul id="emr-switcher" class="z-switcher">' in rendered)
    check("switcher container does NOT carry data-z-switcher",
          "data-z-switcher" not in rendered)
    check("exactly one pane is active",
          rendered.count('class="pt z-active"') == 1
          and rendered.count('class="pt"') == 2)
    check("the active pane matches the active tab",
          rendered.index("<p>2</p>") > rendered.index('class="pt z-active"')
          > rendered.index("<p>1</p>"))
    check("the active nav item is marked",
          rendered.count('<li class="z-active">') == 1)

    # `--z-muted` / `--z-border` are #000 with a separate opacity token, so they
    # only work through the colour-mixing `bg/o` and `border/o` classes — the
    # plain `bg` class painted the raw-JSON block solid black over black text.
    block = ui.code_block('{"resourceType": "Bundle"}')
    check("code blocks compose the muted background with its opacity token",
          "bg/o" in block and "--bg-o: var(--z-muted-o)" in block
          and 'class="overflow-auto' in block)
    check("code blocks never use the bare bg/border classes",
          ' bg"' not in block and ' border"' not in block)
    check("code blocks scroll instead of using the modal padding helper",
          "z-overflow-auto" not in block)
    check("code blocks are height-capped", "[max-h]" in block and "--max-h" in block)
    check("wide tables scroll horizontally",
          'class="overflow-x-auto"' in ui.table(["A"], [["1"]]))

    check("when() renders instants for the screen and survives None",
          ui.when("2026-08-16T09:30:00+05:30") == "2026-08-16 09:30"
          and ui.when(None) == "—" and ui.when("2026-08-16", 10) == "2026-08-16")
    check("flag_chip maps interpretations to one shared palette",
          "z-label-danger" in ui.flag_chip("H")
          and ui.flag_chip("N", show_normal=False) == ""
          and ui.flag_chip(None) == "")
    pb = ui.post_button("/x/1/go", "Go", fields={"status": "done"}, confirm="Sure?")
    check("post_button renders a guarded one-button form",
          pb.startswith('<form method="post" action="/x/1/go"')
          and 'name="status" value="done"' in pb and "confirm(" in pb
          and pb.count("<form") == 1)
    check("stack spaces sections and drops empties",
          ui.stack("a", "", "b") == 'a<div class="mt" style="--mt: 5">b</div>')
    check("sub() joins a cell with its muted second line",
          ui.sub("<b>x</b>", "y") == '<b>x</b>' + ui.muted("y")
          and ui.sub("<b>x</b>", None) == "<b>x</b>")
    check("short_label trims brackets and LOINC dashes",
          ui.short_label("Glucose [Mass/volume] in Blood") == "Glucose"
          and ui.short_label("Metabolic rate --resting") == "Metabolic rate")

    accordion = ui.accordion([("A", "<p>a</p>"), ("B", "<p>b</p>")], open_first=True)
    check("accordion uses the documented component markup",
          "data-z-accordion" in accordion
          and 'class="z-accordion-title"' in accordion
          and 'class="z-accordion-content"' in accordion)
    check("the first accordion item opens on load",
          accordion.count('<li class="z-open">') == 1)

    section("sidebar navigation")
    from emr.web import ui as _ui
    nav_keys = [k for _h, links in _ui.NAV for k, _u, _i in links]
    check("the sidebar fits in a laptop viewport",
          len(nav_keys) + len(_ui.NAV) <= 26,
          f"{len(_ui.NAV)} headers + {len(nav_keys)} items")
    check("every nav key has a label",
          all(k in _ui.NAV_LABEL for k in nav_keys))
    check("nav keys are unique", len(nav_keys) == len(set(nav_keys)))
    check("no 'new record' screen sits in the sidebar",
          not any(k.endswith("-new") for k in nav_keys), str(nav_keys))

    # Every screen must light up an entry, directly or through an alias, or the
    # operator loses track of where they are.
    import glob as _glob
    import re as _re
    used = set()
    for path in _glob.glob("emr/web/routes/*.py"):
        for m in _re.finditer(r'render\([^,]+,\s*[^,]+,\s*"([a-z-]+)"',
                              open(path).read()):
            used.add(m.group(1))
    unresolved = sorted(k for k in used
                        if k not in nav_keys and k not in _ui.NAV_ALIAS)
    check("every screen highlights a sidebar entry", not unresolved,
          str(unresolved))
    for alias, target in _ui.NAV_ALIAS.items():
        check(f"alias {alias} resolves to a real entry", target in nav_keys)

    rendered = _ui.page("Settings", "settings", "<p>x</p>")
    sidebar = _re.search(r"<aside id=\"emr-nav\">.*?</aside>", rendered, _re.S)
    check("exactly one entry is marked in the sidebar markup",
          sidebar is not None and sidebar.group().count("data-nav-active") == 1,
          str(sidebar.group().count("data-nav-active")) if sidebar else "no aside")
    check("the scroll-into-view script looks for that marker",
          'querySelector("#emr-nav [data-nav-active]")' in rendered)
    check("a 'new' screen marks its parent entry",
          'data-nav-active><a href="/patients"'
          in _ui.page("Register", "patient-new", "<p>x</p>"))
    check("the nav is tightened below the kit default",
          "--z-nav-item-padding" in rendered)

    section("routing")
    app = App()
    register_all(app)
    routes = {(m, p.pattern) for m, p, _, _ in app.routes}
    check("all route modules registered", len(routes) >= 30, f"{len(routes)} routes")
    paths = {pat for _m, pat in routes}
    check("the terminology browser is gone", "^/terminology$" not in paths)
    check("the ABHA services screen is gone",
          not any("abha" in p for p in paths), str([p for p in paths if "abha" in p]))
    check("the FHIR export centre is still reachable", "^/fhir$" in paths)
    check("the settings screen is registered", "^/settings$" in paths)
    for route in ("^/dialysis$", "^/dialysis/machines$", "^/dialysis/courses$",
                  "^/dialysis/courses/new$", "^/dialysis/sessions$",
                  "^/dialysis/sessions/new$"):
        check(f"dialysis route {route[1:-1]} is registered", route in paths)
    check("creating a session posts to its own endpoint",
          ("POST", "^/dialysis/sessions$") in routes)
    check("the old per-course session endpoint is gone",
          not any("courses/(?P<cid>[0-9]+)/sessions" in pat for pat in paths))

    section("dialysis")
    check("dialysis machines seeded",
          db.scalar("SELECT COUNT(*) FROM dialysis_machine", default=0) == 5)
    course = dialysis.create_course({
        "patient_id": patient, "encounter_id": visit, "practitioner_id": physician,
        "modality_code": "302497006", "modality_display": "Hemodialysis",
        "access_code": "av-fistula", "access_display": "Arteriovenous fistula",
        "sessions_per_week": 3, "duration_minutes": 240, "dry_weight_kg": 62.5,
        "blood_flow_rate": 300, "dialysate_flow_rate": 500, "dialysate_k": 2,
        "anticoagulant_code": "372877000", "anticoagulant_display": "Heparin",
        "heparin_bolus_units": 2000, "session_charge": 2200,
        "dialyser": "F7 HPS (1.6 m2)"})
    course_row = db.one("SELECT * FROM dialysis_course WHERE id = ?", (course,))
    check("a course gets a number and starts active",
          course_row["course_no"] == "DLC-00001"
          and course_row["status"] == "active")

    machine = db.scalar("SELECT id FROM dialysis_machine WHERE code = 'HD-01'")
    run = dialysis.schedule_session(course, machine, db.now_iso(), physician)
    run_row = dialysis.session(run)
    check("a session inherits the course prescription",
          run_row["blood_flow_rate"] == 300 and run_row["dialysate_k"] == 2
          and run_row["dry_weight_kg"] == 62.5 and run_row["seq"] == 1)

    dialysis.start_session(run)
    check("starting a run takes the machine",
          db.scalar("SELECT status FROM dialysis_machine WHERE id = ?",
                    (machine,)) == "in-use")
    second = dialysis.schedule_session(course, machine, db.now_iso(), physician)
    try:
        dialysis.start_session(second)
        check("a busy machine cannot run two patients", False)
    except ValueError:
        check("a busy machine cannot run two patients", True)
    try:
        dialysis.set_machine_status(machine, "maintenance")
        check("a running machine cannot be sent to service", False)
    except ValueError:
        check("a running machine cannot be sent to service", True)

    dialysis.record_phase_vitals(run, "pre", {
        "8480-6": "158", "8462-4": "94", "8867-4": "88", "9279-1": "18",
        "59408-5": "98", "8310-5": "36.7"})
    dialysis.record_phase_vitals(run, "post", {
        "8480-6": "132", "8462-4": "80", "8867-4": "82", "9279-1": "16",
        "59408-5": "99", "8310-5": "36.4"})
    flow = dialysis.phase_vitals(run)
    check("pre and post vitals are kept apart",
          flow["pre"]["8480-6"]["value_quantity"] == 158
          and flow["post"]["8480-6"]["value_quantity"] == 132)
    check("the whole flowsheet is captured",
          len(flow["pre"]) == 6 and len(flow["post"]) == 6,
          f'{len(flow["pre"])}/{len(flow["post"])}')
    check("flowsheet vitals are ordinary flagged observations",
          flow["pre"]["8480-6"]["interpretation"] == "H"
          and flow["post"]["8462-4"]["interpretation"] == "N")
    dialysis.record_phase_vitals(run, "pre", {"8480-6": "160"})
    check("re-saving a phase replaces rather than duplicates",
          len(dialysis.phase_vitals(run)["pre"]) == 1)
    dialysis.record_phase_vitals(run, "pre", {
        "8480-6": "158", "8462-4": "94", "8867-4": "88", "9279-1": "18",
        "59408-5": "98", "8310-5": "36.7"})
    try:
        dialysis.record_phase_vitals(run, "mid", {"8480-6": "1"})
        check("only pre and post phases are accepted", False)
    except ValueError:
        check("only pre and post phases are accepted", True)

    dialysis.save_parameters(run, {"pre_weight_kg": 65.2, "uf_goal_ml": 2700,
                                   "ktv": 1.42, "conductivity": 14.0,
                                   "complication_code": "45007003",
                                   "complication_display": "Hypotension"})
    dialysis.save_parameters(run, {"post_weight_kg": 62.6})
    saved = dialysis.session(run)
    check("ultrafiltration is derived from the weights",
          saved["uf_achieved_ml"] == 2600, str(saved["uf_achieved_ml"]))
    dialysis.save_parameters(run, {"uf_achieved_ml": 2450})
    check("a typed ultrafiltration figure wins over the derived one",
          dialysis.session(run)["uf_achieved_ml"] == 2450)

    for minutes, sbp in [(0, 158), (60, 142), (150, 104), (240, 132)]:
        dialysis.add_reading(run, {"elapsed_minutes": minutes, "bp_systolic": sbp,
                                   "pulse": 88, "venous_pressure": 130})
    chart = dialysis.readings(run)
    check("the monitoring chart keeps its order",
          [r["elapsed_minutes"] for r in chart] == [0, 60, 150, 240])
    check("the intra-dialytic nadir is retrievable",
          min(r["bp_systolic"] for r in chart) == 104)

    # The list tables were trimmed to reviewable widths; pin the budget so
    # columns do not quietly creep back.
    import inspect as _inspect
    import re as _re2
    import emr.web.routes.dialysis as _dr
    _headers = _re2.findall(r'ui\.table\(\[(.*?)\]', _inspect.getsource(_dr), _re2.S)
    _widest = max(h.count('"') // 2 for h in _headers)
    check("no dialysis table is wider than eight columns", _widest <= 8,
          f"widest has {_widest}")

    dialysis.complete_session(run)
    completed = dialysis.session(run)
    check("completing a run frees the machine",
          db.scalar("SELECT status FROM dialysis_machine WHERE id = ?",
                    (machine,)) == "available")
    proc = db.one("SELECT * FROM procedure WHERE dialysis_session_id = ?", (run,))
    check("a completed run is written as a SNOMED procedure",
          proc is not None and proc["snomed_code"] == "302497006")
    check("a managed complication downgrades rather than fails the run",
          proc["outcome_code"] == "385670004", proc["outcome_display"])
    dialysis.complete_session(run)
    check("completing twice does not duplicate the procedure",
          db.scalar("SELECT COUNT(*) FROM procedure WHERE dialysis_session_id = ?",
                    (run,), default=0) == 1)

    dial_bill = services.create_invoice(patient, visit, "03", "OPD", physician, None)
    services.save_invoice(dial_bill, services.suggest_invoice_lines(visit))
    dial_lines = db.query("SELECT * FROM invoice_line WHERE invoice_id = ?",
                          (dial_bill,))
    check("a completed session bills once from the course tariff",
          sum(1 for l in dial_lines if l["unit_price"] == 2200) == 1)
    check("the dialysis procedure does not also attract a theatre charge",
          not any("Hemodialysis" in l["description"] and l["unit_price"] == 4500
                  for l in dial_lines),
          str([l["description"] for l in dial_lines]))
    dialysis.mark_sessions_billed(visit)
    check("billed sessions are not billed twice",
          dialysis.unbilled_sessions(visit) == [])

    try:
        dialysis.close_course(course)
        check("a course with a planned session still closes", True)
    except ValueError as error:
        check("a course with a planned session still closes", False, str(error))
    try:
        dialysis.schedule_session(course, machine, db.now_iso(), physician)
        check("a closed course accepts no new sessions", False)
    except ValueError:
        check("a closed course accepts no new sessions", True)

    # the courses page partitions on exactly this split
    everything = dialysis.courses()
    active_only = [c for c in everything if c["status"] == "active"]
    closed_only = [c for c in everything if c["status"] != "active"]
    check("courses partition cleanly into active and closed",
          len(active_only) + len(closed_only) == len(everything)
          and course in [c["id"] for c in closed_only])
    check("the course list carries the counts the page shows",
          all(c["done"] is not None for c in everything))

    db.update("dialysis_course", course, {"status": "active", "ended_on": None})
    check("a reopened course accepts sessions again",
          dialysis.schedule_session(course, None, db.now_iso(), physician) > 0)

    running = db.one("SELECT id FROM dialysis_session WHERE course_id = ? "
                     "AND status = 'planned' ORDER BY id DESC LIMIT 1", (course,))
    dialysis.start_session(running["id"], machine)
    try:
        dialysis.close_course(course)
        check("a course cannot close with a run in progress", False)
    except ValueError:
        check("a course cannot close with a run in progress", True)
    dialysis.complete_session(running["id"], abandoned=True)
    check("an abandoned run writes no procedure",
          db.scalar("SELECT COUNT(*) FROM procedure WHERE dialysis_session_id = ?",
                    (running["id"],), default=0) == 0)

    section("wellness")
    for kind, expected in [("wellness_vital", 5), ("wellness_body", 6),
                           ("wellness_activity", 4), ("wellness_women", 5),
                           ("wellness_lifestyle", 4)]:
        check(f"{kind} seeded from the published ValueSet",
              len(db.terms(kind)) == expected, str(len(db.terms(kind))))

    well = wellness.create_record(patient, visit, physician, db.today_iso(),
                                  "Health camp", "Annual check")
    check("a wellness record starts as a draft",
          db.scalar("SELECT status FROM wellness_record WHERE id = ?",
                    (well,)) == "draft")
    try:
        wellness.finalise(well)
        check("an empty wellness record cannot be signed", False)
    except ValueError:
        check("an empty wellness record cannot be signed", True)

    wellness.save_section(well, "vital-signs", {
        "61008-9": "36.6", "8867-4": "74", "85354-9": "128/82"})
    wellness.save_section(well, "body-measurement",
                          {"29463-7": "68", "8302-2": "158", "39156-5": "27.2"})
    wellness.save_section(well, "physical-activity",
                          {"55423-8": "6200", "93832-4": "6.5"})
    wellness.save_section(well, "general-assessment", {"41604-0": "118"},
                          {"365275006": "fair"})
    wellness.save_section(well, "women-health", {"8665-2": "2026-08-02"})
    wellness.save_section(well, "lifestyle", {},
                          {"365981007": "266919005", "228273003": "105542008"})
    counts = wellness.summary(well)
    check("every wellness section captures",
          counts == {"vital-signs": 3, "body-measurement": 3,
                     "physical-activity": 2, "general-assessment": 2,
                     "women-health": 1, "lifestyle": 2, "other": 0}, str(counts))

    vitals_rows = wellness.observations(well, "vital-signs")
    numeric = [o for o in vitals_rows if o["value_quantity"] is not None]
    strings = [o for o in vitals_rows if o["value_string"]]
    check("a numeric wellness reading is flagged against its range",
          any(o["interpretation"] for o in numeric))
    check("a non-numeric reading falls back to valueString",
          len(strings) == 1 and strings[0]["value_string"] == "128/82")
    life = wellness.observations(well, "lifestyle")
    check("lifestyle observations carry only a coded value",
          all(o["value_code"] and o["value_quantity"] is None for o in life))
    check("a lifestyle answer outside its list is refused",
          wellness.save_section(well, "lifestyle", {},
                                {"365981007": "not-a-code"}) == 0)
    wellness.save_section(well, "lifestyle", {},
                          {"365981007": "266919005", "228273003": "105542008"})

    wellness.save_section(well, "vital-signs", {"8867-4": "80"})
    check("re-saving a section replaces it",
          len(wellness.observations(well, "vital-signs")) == 1)
    wellness.save_section(well, "vital-signs", {
        "61008-9": "36.6", "8867-4": "74", "85354-9": "128/82"})

    wellness.finalise(well)
    check("a populated record signs",
          db.scalar("SELECT status FROM wellness_record WHERE id = ?",
                    (well,)) == "final")
    try:
        wellness.save_section(well, "vital-signs", {"8867-4": "70"})
        check("a signed record refuses edits", False)
    except ValueError:
        check("a signed record refuses edits", True)
    wellness.reopen(well)
    wellness.save_section(well, "vital-signs", {
        "61008-9": "36.6", "8867-4": "74", "85354-9": "128/82"})
    wellness.finalise(well)

    wb = build_bundle("WellnessRecord", well)
    wb_issues = [i for i in validate_bundle("WellnessRecord", wb)
                 if i["severity"] == "error"]
    check("the wellness bundle passes the profile gate", not wb_issues,
          "; ".join(i["message"] for i in wb_issues[:2]))
    wcomp = wb["entry"][0]["resource"]
    check("WellnessRecord fixes Composition.type.text",
          wcomp["type"] == {"text": "Wellness Record"}, str(wcomp["type"]))
    titles = [s["title"] for s in wcomp["section"]]
    check("sections are sliced by their fixed titles",
          titles == ["Vital Signs", "Body Measurement", "Physical Activity",
                     "General Assessment", "Women Health", "Lifestyle"],
          str(titles))
    check("wellness sections carry no section code",
          all("code" not in s for s in wcomp["section"]))
    profiles = {e["resource"]["meta"]["profile"][0].rsplit("/", 1)[1]
                for e in wb["entry"]
                if e["resource"]["resourceType"] == "Observation"}
    check("each section uses its own NRCES Observation profile",
          profiles == {"ObservationVitalSigns", "ObservationBodyMeasurement",
                       "ObservationPhysicalActivity", "ObservationGeneralAssessment",
                       "ObservationWomenHealth", "ObservationLifestyle"},
          str(sorted(profiles)))
    life_res = [e["resource"] for e in wb["entry"]
                if e["resource"]["meta"]["profile"][0].endswith("ObservationLifestyle")]
    check("the Lifestyle profile emits valueCodeableConcept only",
          all("valueCodeableConcept" in r and "valueQuantity" not in r
              for r in life_res))

    broken = copy.deepcopy(wb)
    broken["entry"][0]["resource"]["section"][0]["title"] = "Vitals"
    check("the validator rejects a section title outside the slice list",
          any(i["severity"] == "error"
              for i in validate_bundle("WellnessRecord", broken)))
    broken2 = copy.deepcopy(wb)
    broken2["entry"][0]["resource"]["type"] = {"text": "Wellness"}
    check("the validator rejects the wrong Composition.type.text",
          any(i["severity"] == "error"
              for i in validate_bundle("WellnessRecord", broken2)))

    section("a wellness record per dialysis session")
    gen = wellness.record_for_session(run)
    check("completing a dialysis run generates a wellness record", gen is not None)
    check("the generated record is signed and traceable to the session",
          gen["status"] == "final" and gen["dialysis_session_id"] == run
          and "DLS-" in (gen["source"] or ""), str(dict(gen))[:120])
    check("it belongs to the same patient and encounter",
          gen["patient_id"] == patient and gen["encounter_id"] == visit)

    gen_counts = wellness.summary(gen["id"])
    check("post-dialysis vitals reach the Vital Signs section",
          gen_counts["vital-signs"] == 5, str(gen_counts))
    check("the whole dialysis parameter set reaches Other Observations",
          gen_counts["other"] >= 15, str(gen_counts["other"]))

    labels = {o["code_text"] for o in wellness.observations(gen["id"], "other")}
    for wanted in ("Blood flow rate (Qb)", "Dialysate flow rate (Qd)",
                   "Ultrafiltration achieved", "Dialysis adequacy Kt/V",
                   "Dialysate conductivity", "Dialyser", "Dialysis machine",
                   "Vascular access used", "Anticoagulant"):
        check(f"parameter carried: {wanted}", wanted in labels)

    gen_vitals = wellness.observations(gen["id"], "vital-signs")
    codes = {o["loinc_code"] for o in gen_vitals}
    check("SpO2 is remapped to the code the ValueSet binds",
          "2708-6" in codes and "59408-5" not in codes, str(sorted(codes)))
    check("blood pressure becomes the bound panel code as a string",
          any(o["loinc_code"] == "85354-9" and o["value_string"] for o in gen_vitals))

    gen_bundle = build_bundle("WellnessRecord", gen["id"])
    gen_errors = [i for i in validate_bundle("WellnessRecord", gen_bundle)
                  if i["severity"] == "error"]
    check("the generated wellness record exports and validates", not gen_errors,
          "; ".join(i["message"] for i in gen_errors[:2]))
    gen_sections = [s["title"] for s in gen_bundle["entry"][0]["resource"]["section"]]
    check("dialysis parameters land in the Other Observations section",
          "Other Observations" in gen_sections, str(gen_sections))
    other_res = [e["resource"] for e in gen_bundle["entry"]
                 if e["resource"]["resourceType"] == "Observation"
                 and "coding" not in e["resource"].get("code", {})]
    check("unbound parameters are text-only CodeableConcepts, never a local coding",
          other_res and all(r["code"].get("text") for r in other_res))
    check("they carry the base NRCES Observation profile",
          all(r["meta"]["profile"][0].endswith("/Observation") for r in other_res))

    dialysis.complete_session(run)
    check("re-completing does not generate a second wellness record",
          db.scalar("SELECT COUNT(*) FROM wellness_record "
                    "WHERE dialysis_session_id = ?", (run,), default=0) == 1)

    abandoned = dialysis.schedule_session(course, None, db.now_iso(), physician)
    dialysis.start_session(abandoned, machine)
    dialysis.complete_session(abandoned, abandoned=True)
    check("an abandoned run generates no wellness record",
          wellness.record_for_session(abandoned) is None)

    op_after = build_bundle("OPConsultRecord", visit)
    exam = next((s for s in op_after["entry"][0]["resource"]["section"]
                 if s["title"] == "Physical examination"), {"entry": []})
    wellness_urls = {R_url for R_url in
                     [e["fullUrl"] for e in wb["entry"]
                      if e["resource"]["resourceType"] == "Observation"]}
    check("wellness observations stay out of the encounter document",
          not any(e["reference"] in wellness_urls for e in exam.get("entry", [])))

    section("search, numbering and derived values")
    found = services.search_patients("Selftest")
    check("patients are searchable by name", any(p["id"] == patient for p in found))
    check("patients are searchable by MRN",
          any(p["id"] == patient for p in services.search_patients(mrn)))
    check("patients are searchable by phone",
          any(p["id"] == patient
              for p in services.search_patients("+919000000000")))
    check("patients are searchable by ABHA",
          any(p["id"] == patient
              for p in services.search_patients("91-0000-1111-2222")))
    check("an empty search lists recent patients",
          len(services.search_patients("")) > 0)
    check("a search that matches nothing returns nothing",
          services.search_patients("zzzz-no-such-patient") == [])

    check("encounter numbers are sequential per kind",
          db.scalar("SELECT encounter_no FROM encounter WHERE id = ?",
                    (visit,)) == "OPD-00001")
    check("IPD numbering runs on its own counter",
          db.scalar("SELECT encounter_no FROM encounter WHERE id = ?",
                    (admission,)) == "IPD-00001")

    check("BMI is left alone when one measurement is missing",
          "39156-5" not in services.derive_bmi({"29463-7": "70"}))
    check("BMI is not derived from a zero height",
          "39156-5" not in services.derive_bmi({"29463-7": "70", "8302-2": "0"}))
    check("BMI is not derived from junk",
          "39156-5" not in services.derive_bmi({"29463-7": "x", "8302-2": "170"}))
    check("a typed BMI is never overwritten",
          services.derive_bmi({"29463-7": "70", "8302-2": "170",
                               "39156-5": "99"})["39156-5"] == "99")

    check("a reading below the range flags low",
          services.interpret(50, 60, 100) == "L")
    check("a reading above the range flags high",
          services.interpret(120, 60, 100) == "H")
    check("a reading inside the range flags normal",
          services.interpret(80, 60, 100) == "N")
    check("no range means no flag", services.interpret(80, None, None) is None)

    section("bed allocation rules")
    male_beds = hospital.vacant_beds("male")
    female_beds = hospital.vacant_beds("female")
    check("a male patient is not offered the female ward",
          not any(b["ward_name"] == "General Ward (Female)" for b in male_beds))
    check("a female patient is not offered the male ward",
          not any(b["ward_name"] == "General Ward (Male)" for b in female_beds))
    check("mixed wards are offered to everyone",
          any(b["ward_name"] == "Private Room" for b in male_beds)
          and any(b["ward_name"] == "Private Room" for b in female_beds))
    check("no gender given means no filtering",
          len(hospital.vacant_beds()) >= len(male_beds))
    blocked = hospital.vacant_beds()[0]
    hospital.set_bed_status(blocked["id"], "blocked")
    check("a blocked bed leaves the vacant list",
          blocked["id"] not in [b["id"] for b in hospital.vacant_beds()])
    hospital.set_bed_status(blocked["id"], "vacant")
    try:
        hospital.set_bed_status(blocked["id"], "not-a-status")
        check("an unknown bed status is refused", False)
    except ValueError:
        check("an unknown bed status is refused", True)

    section("pharmacy edge cases")
    multi = db.one("SELECT * FROM stock_item WHERE code = 'MED003'")
    hospital.receive_stock(multi["id"], "C", "2027-12-31", 10, 1.0, "S")
    hospital.receive_stock(multi["id"], "A", "2026-10-31", 10, 1.0, "S")
    hospital.receive_stock(multi["id"], "B", "2026-11-30", 10, 1.0, "S")
    picked = hospital.issue_stock(multi["id"], 25, patient, visit, None)
    check("an issue walks batches in expiry order, oldest first",
          [p["batch_no"] for p in picked] == ["A", "B", "C"], str(picked))
    check("the last batch is only partly drawn down",
          picked[-1]["quantity"] == 5 and hospital.stock_on_hand(multi["id"]) == 5)
    try:
        hospital.issue_stock(multi["id"], 0, patient, visit, None)
        check("a zero-quantity issue is refused", False)
    except ValueError:
        check("a zero-quantity issue is refused", True)
    try:
        hospital.receive_stock(multi["id"], "D", None, -5, 1.0, "S")
        check("a negative goods receipt is refused", False)
    except ValueError:
        check("a negative goods receipt is refused", True)
    check("a batch with no expiry never appears in the expiry alert",
          all(b["expiry_date"] for b in hospital.expiring_batches(3650)))

    section("appointment queue rules")
    other_doctor = db.scalar("SELECT id FROM practitioner WHERE department='Cardiology'")
    day = "2026-09-01"
    a1 = hospital.book_appointment(patient, physician, "General Medicine", day,
                                   "09:00", "Fever", None)
    a2 = hospital.book_appointment(patient, physician, "General Medicine", day,
                                   "09:15", "Review", None)
    b1 = hospital.book_appointment(patient, other_doctor, "Cardiology", day,
                                   "10:00", "Chest pain", None)
    tokens = {r["id"]: r["token"] for r in hospital.appointments_for(day)}
    check("tokens count up per doctor", tokens[a1] == 1 and tokens[a2] == 2)
    check("a second doctor starts its own token run", tokens[b1] == 1)
    check("another day starts again at one",
          db.scalar("SELECT token FROM appointment WHERE id = ?",
                    (hospital.book_appointment(patient, physician, "General Medicine",
                                               "2026-09-02", None, None, None),)) == 1)
    try:
        hospital.set_appointment_status(a1, "teleported")
        check("an unknown appointment status is refused", False)
    except ValueError:
        check("an unknown appointment status is refused", True)

    section("generated-record integrity (regressions)")
    gen = wellness.record_for_session(run)

    # Regression: reopening a generated record used to strand it in draft with
    # no way back to final, silently dropping it out of the export picker.
    try:
        wellness.reopen(gen["id"])
        check("a generated record cannot be reopened", False)
    except ValueError as error:
        check("a generated record cannot be reopened", "regenerate" in str(error))
    check("it stays final and exportable",
          db.scalar("SELECT status FROM wellness_record WHERE id = ?",
                    (gen["id"],)) == "final")

    # Regression: posting to the generated section deleted every parameter.
    before = wellness.summary(gen["id"])["other"]
    try:
        wellness.save_section(gen["id"], "other", {})
        check("the generated section cannot be posted to", False)
    except ValueError:
        check("the generated section cannot be posted to", True)
    try:
        wellness.save_section(gen["id"], "vital-signs", {"8867-4": "70"})
        check("a generated record cannot be hand-edited", False)
    except ValueError:
        check("a generated record cannot be hand-edited", True)
    check("the parameter set survives both attempts",
          wellness.summary(gen["id"])["other"] == before and before > 0,
          f"{before} parameters")

    # Regression: a mid-write failure left a truncated record marked final that
    # the duplicate guard then protected forever.
    victim = dialysis.schedule_session(course, None, db.now_iso(), physician)
    dialysis.start_session(victim, machine)
    dialysis.record_phase_vitals(victim, "post", {"8867-4": "80", "9279-1": "16"})
    dialysis.save_parameters(victim, {"blood_flow_rate": 300, "ktv": 1.4,
                                      "post_weight_kg": 62})
    counter_before = db.scalar("SELECT value FROM counter WHERE name = 'wellness'",
                               default=0)
    real_insert = db.insert
    calls = {"n": 0}

    def flaky(table, values):
        if table == "observation":
            calls["n"] += 1
            if calls["n"] == 4:
                raise RuntimeError("simulated write failure")
        return real_insert(table, values)

    db.insert = flaky
    try:
        dialysis.complete_session(victim)
        check("a failed generation does not break session completion", True)
    except Exception as error:            # noqa: BLE001 - that is the bug
        check("a failed generation does not break session completion", False,
              str(error))
    finally:
        db.insert = real_insert
    check("nothing partial is left behind",
          wellness.record_for_session(victim) is None)
    check("the run still completes",
          db.scalar("SELECT status FROM dialysis_session WHERE id = ?",
                    (victim,)) == "completed")
    check("the rolled-back attempt does not burn a record number",
          db.scalar("SELECT value FROM counter WHERE name = 'wellness'",
                    default=0) == counter_before)

    recovered = wellness.regenerate_for_session(victim)
    check("a failed generation can be recovered", recovered is not None)
    check("regenerating replaces rather than duplicating",
          db.scalar("SELECT COUNT(*) FROM wellness_record "
                    "WHERE dialysis_session_id = ?", (victim,), default=0) == 1)
    wellness.regenerate_for_session(victim)
    check("regenerating twice is still one record",
          db.scalar("SELECT COUNT(*) FROM wellness_record "
                    "WHERE dialysis_session_id = ?", (victim,), default=0) == 1)

    # Regression: a non-numeric blood pressure crashed the vitals mapping.
    odd = dialysis.schedule_session(course, None, db.now_iso(), physician)
    dialysis.start_session(odd, machine)
    dialysis.record_phase_vitals(odd, "post",
                                 {"8480-6": "unrecordable", "8462-4": "80"})
    dialysis.save_parameters(odd, {"blood_flow_rate": 300})
    dialysis.complete_session(odd)
    odd_record = wellness.record_for_session(odd)
    check("a non-numeric reading does not break generation",
          odd_record is not None)
    check("the unusable blood pressure is simply left out",
          odd_record is not None
          and not any(o["loinc_code"] == "85354-9"
                      for o in wellness.observations(odd_record["id"],
                                                     "vital-signs")))

    section("HTTP layer")
    from emr.web.router import App as _App, Request, redirect as _redirect
    from http.cookies import SimpleCookie

    probe = _App()
    seen = {}

    @probe.get("/thing/<int:tid>/part/<str:name>")
    def _thing(request):
        seen.update(request.params)
        return _redirect("/done", "saved", "success")

    @probe.post("/thing")
    def _create(request):
        seen["form"] = (request.f("a"), request.f_int("n"), request.f_float("x"),
                        request.f_all("multi"), request.f_or_none("blank"))
        return _redirect("/ok")

    empty = SimpleCookie()
    response = probe.dispatch("GET", "/thing/42/part/left-arm", {}, {}, empty, b"")
    check("integer path parameters are converted",
          seen.get("tid") == 42 and isinstance(seen.get("tid"), int))
    check("string path parameters are captured", seen.get("name") == "left-arm")
    check("a redirect returns 303 with a Location",
          response.status == 303
          and any(k == "Location" for k, _ in response.headers))
    check("a flash message rides on a cookie",
          any("flash=" in v for k, v in response.headers if k == "Set-Cookie"))

    probe.dispatch("POST", "/thing", {},
                   {"a": ["hi "], "n": ["7"], "x": ["1.5"],
                    "multi": ["p", "q"], "blank": [""]}, empty, b"")
    check("form values are trimmed", seen["form"][0] == "hi")
    check("integers and floats are coerced",
          seen["form"][1] == 7 and seen["form"][2] == 1.5)
    check("repeated fields come back as a list", seen["form"][3] == ["p", "q"])
    check("a blank field reads as None", seen["form"][4] is None)
    check("a non-numeric integer field falls back to the default",
          Request("GET", "/", {}, {"n": ["abc"]}, empty, {}, b"")
          .f_int("n", 99) == 99)

    check("an unknown path is a 404",
          probe.dispatch("GET", "/nope", {}, {}, empty, b"").status == 404)
    check("a known path with the wrong method is a 405",
          probe.dispatch("DELETE", "/thing", {}, {}, empty, b"").status == 405)
    check("a path parameter of the wrong type does not match",
          probe.dispatch("GET", "/thing/abc/part/x", {}, {},
                         empty, b"").status == 404)

    @probe.post("/door/<path:route>")
    def _door(request):
        seen["route"] = request.params["route"]
        return _redirect("/ok")

    probe.dispatch("POST", "/door/v1/coverageeligibility/on_check", {}, {},
                   empty, b"")
    check("a path parameter swallows the whole tail, slashes included",
          seen.get("route") == "v1/coverageeligibility/on_check")
    check("a path parameter still needs something to match",
          probe.dispatch("POST", "/door/", {}, {}, empty, b"").status == 404)

    flash_cookie = SimpleCookie()
    flash_cookie.load("flash=success%7Csaved%20it")
    check("a flash cookie round-trips",
          Request("GET", "/", {}, {}, flash_cookie, {}, b"").flash()
          == ("success", "saved it"))
    check("no cookie means no flash",
          Request("GET", "/", {}, {}, empty, {}, b"").flash() is None)
    check("query parameters are read with a default",
          Request("GET", "/", {"q": ["x"]}, {}, empty, {}, b"").q("missing", "d") == "d")

    section("mount prefix")
    from emr.web import router as _router

    check("no prefix is the default", _router.base() == "")
    check("a path is untouched without a prefix", _router.url("/patients") == "/patients")
    for given, expected in (("/emr", "/emr"), ("emr", "/emr"), ("/emr/", "/emr"),
                            ("https://hospital.example/emr/", "/emr"),
                            ("/a/b", "/a/b"), ("", ""), ("/", "")):
        check(f"{given!r} normalises to {expected!r}",
              _router.set_base(given) == expected)
    for bad in ("/emr space", "/emr?x=1", "/emr#top", "/emr/../etc"):
        try:
            _router.set_base(bad)
            check(f"{bad!r} is refused", False)
        except ValueError:
            check(f"{bad!r} is refused", True)

    try:
        _router.set_base("/emr")
        check("the prefix is what the app reports", _router.base() == "/emr")
        check("url() prefixes an app path", _router.url("/patients") == "/emr/patients")
        check("url() does not leave a bare trailing slash", _router.url("/") == "/emr")

        from emr.app import _error_page

        mounted = App()
        register_all(mounted)
        mounted.error_page = _error_page  # wired exactly as create_app() does
        home = mounted.dispatch("GET", "/emr/", {}, {}, SimpleCookie(), b"")
        check("the mounted root routes to the dashboard", home.status == 200)
        page_html = home.body.decode("utf-8")
        check("every link in the page carries the prefix",
              '="/emr/patients"' in page_html
              and not _re.search(r'(?:href|action|src)="/(?!emr/)', page_html))
        check("the static assets move with the app",
              'href="/emr/static/favicon.png"' in page_html)
        check("the CDN is left alone", f'href="{ui.CDN}/css/kit.min.css"' in page_html)
        check("the prefix without a trailing slash is still the dashboard",
              mounted.dispatch("GET", "/emr", {}, {}, SimpleCookie(), b"").status == 200)
        # A proxy that strips the prefix before forwarding (nginx's
        # `proxy_pass http://host:port/;`) hands the app a bare path; it is
        # served, and the page it returns still links with the prefix, so the
        # browser keeps asking for the mounted URLs.
        stripped = mounted.dispatch("GET", "/patients", {}, {}, SimpleCookie(), b"")
        check("a prefix-stripping proxy is served too", stripped.status == 200)
        check("a stripped request still answers with mounted links",
              '="/emr/patients"' in stripped.body.decode("utf-8"))
        stripped_asset = mounted.dispatch("GET", "/static/logo.png", {}, {},
                                          SimpleCookie(), b"")
        check("a stripped asset request is served",
              stripped_asset.status == 200
              and stripped_asset.body[:8] == b"\x89PNG\r\n\x1a\n")
        check("a path that is no route either way is still a 404",
              mounted.dispatch("GET", "/nope", {}, {}, SimpleCookie(), b"").status
              == 404)
        check("the 404 page's own links are mounted too",
              '="/emr/"' in mounted.dispatch("GET", "/nope", {}, {}, SimpleCookie(),
                                             b"").body.decode("utf-8"))

        # A redirect that lost the prefix would send the browser off the mount
        # point and 404 after every successful save.
        bounced = _App()
        bounced.add("POST", "/thing", lambda request: _redirect("/done", "saved"))
        landed = dict(bounced.dispatch("POST", "/emr/thing", {}, {}, SimpleCookie(),
                                       b"").headers)
        check("a redirect Location is mounted", landed["Location"] == "/emr/done")
        check("the flash cookie is scoped to the mount point",
              "Path=/emr/" in landed["Set-Cookie"])

        served_asset = mounted.dispatch("GET", "/emr/static/logo.png", {}, {},
                                        SimpleCookie(), b"")
        check("a mounted binary response is served untouched",
              served_asset.status == 200
              and served_asset.body[:8] == b"\x89PNG\r\n\x1a\n")
        exported = mounted.dispatch("GET", "/emr/fhir", {}, {}, SimpleCookie(), b"")
        check("the FHIR centre renders under the prefix", exported.status == 200)
    finally:
        # Every later section addresses the app from the root.
        _router.set_base("")
    check("the prefix is cleared for the rest of the suite", _router.base() == "")

    section("static assets")
    from emr.web.routes import assets as _assets
    from http.cookies import SimpleCookie as _Cookie

    for name in ("logo.png", "favicon.png"):
        loaded = _assets.load(name)
        check(f"{name} ships with the package and loads", loaded is not None)
        if loaded:
            body, content_type, etag = loaded
            check(f"{name} is a real PNG", body[:8] == b"\x89PNG\r\n\x1a\n")
            check(f"{name} is served as an image", content_type == "image/png")
            check(f"{name} is small enough to serve on every page",
                  len(body) < 60_000, f"{len(body):,} bytes")
            check(f"{name} has a content-derived ETag",
                  etag.startswith('"') and len(etag) > 8)

    check("an unknown asset name is refused", _assets.load("secrets.db") is None)
    check("a traversal attempt is refused",
          _assets.load("../../../etc/passwd") is None)
    check("the allow-list is what is served",
          set(_assets.ASSETS) == {"logo.png", "favicon.png"})

    asset_app = App()
    register_all(asset_app)
    empty_cookies = _Cookie()
    served = asset_app.dispatch("GET", "/static/logo.png", {}, {},
                                empty_cookies, b"")
    header_map = dict(served.headers)
    check("the asset route returns the file", served.status == 200
          and served.body[:8] == b"\x89PNG\r\n\x1a\n")
    check("it is cached for a week",
          "max-age=604800" in header_map.get("Cache-Control", ""))
    missing = asset_app.dispatch("GET", "/static/nope.png", {}, {},
                                 empty_cookies, b"")
    check("a missing asset is a 404, not a traceback", missing.status == 404)

    revalidated = asset_app.dispatch(
        "GET", "/static/logo.png", {}, {}, empty_cookies, b"",
        {"If-None-Match": header_map["ETag"]})
    check("a matching ETag returns 304 with no body",
          revalidated.status == 304 and revalidated.body == b"")
    stale = asset_app.dispatch("GET", "/static/logo.png", {}, {}, empty_cookies,
                               b"", {"If-None-Match": '"stale"'})
    check("a stale ETag returns the file again", stale.status == 200)
    check("request headers are matched case-insensitively",
          Request("GET", "/", {}, {}, empty_cookies, {}, b"",
                  {"If-None-Match": "x"}).header("if-none-match") == "x")

    shell = ui.page("Dashboard", "dashboard", "<p>x</p>")
    check("the page links the favicon",
          'rel="icon" type="image/png" href="/static/favicon.png"' in shell)
    brand = _re.search(r"<header.*?</header>", shell, _re.S)
    check("the brand mark is the logo, not a stock icon",
          brand is not None and "/static/logo.png" in brand.group()
          and "<z-icon" not in brand.group().split("</a>")[0])
    check("the logo declares its intrinsic size so the header does not reflow",
          'width="256" height="256"' in shell)

    section("wellness export gating")
    draft = wellness.create_record(patient, None, physician, db.today_iso(),
                                   "Clinic", None)
    wellness.save_section(draft, "vital-signs", {"8867-4": "72"})
    finals = [r["id"] for r in wellness.records("final")]
    drafts = [r["id"] for r in wellness.records("draft")]
    check("a draft is listed as a draft", draft in drafts and draft not in finals)
    wellness.finalise(draft)
    check("signing moves it to the final list",
          draft in [r["id"] for r in wellness.records("final")])
    wellness.reopen(draft)
    check("a hand-entered record can be reopened",
          db.scalar("SELECT status FROM wellness_record WHERE id = ?",
                    (draft,)) == "draft")
    wellness.save_section(draft, "vital-signs", {"8867-4": "76"})
    check("and edited again after reopening",
          wellness.observations(draft, "vital-signs")[0]["value_quantity"] == 76)
    try:
        wellness.save_section(draft, "not-a-section", {})
        check("an unknown section is refused", False)
    except ValueError:
        check("an unknown section is refused", True)

    section("NHCX claims")
    from emr import claims as _claims

    selected = {
        "member_id": "MD5SLS4X5", "name": "PALLVI",
        "policy_code": "PMJAY/HP/S/G", "payer_id": "1518",
        "payer_name": "Nhcx Pmjay", "product_name": "PMJAY for Himachal",
        "abha_number": "91-7034-1237-4240", "mobile_number": "9818512600",
        "raw": {"memberid": "MD5SLS4X5", "payerid": "1518"},
    }
    claim_id = _claims.create_claim(selected, "MemberId", "MD5SLS4X5")
    claim_row = _claims.claim(claim_id)
    check("a claim opens as a draft with a CLM number",
          claim_row["status"] == "draft"
          and claim_row["claim_no"].startswith("CLM-"))
    check("the selected policy is copied onto the claim",
          claim_row["member_id"] == "MD5SLS4X5"
          and claim_row["policy_code"] == "PMJAY/HP/S/G"
          and json.loads(claim_row["policy_json"])["memberid"] == "MD5SLS4X5")
    check("the claim ledger lists it",
          any(r["id"] == claim_id for r in _claims.claims_list("draft")))
    try:
        _claims.create_claim({"name": "No Member"}, "MobileNo", "9818512600")
        check("a policy without a member ID is refused", False)
    except ValueError:
        check("a policy without a member ID is refused", True)
    try:
        _claims.run_check(claim_id, "validation", "", "MD5SLS4X5")
        check("a validation check without a policy code is refused", False)
    except ValueError:
        check("a validation check without a policy code is refused", True)
    try:
        _claims.run_check(claim_id, "validation", "PMJAY/HP/S/G", "MD5SLS4X5")
        check("a check before the NHCX participant code is set is refused",
              False)
    except ValueError as error:
        check("a check before the NHCX participant code is set is refused",
              "participant code" in str(error))

    # The payer's on_check reply, reduced to the resources the parser reads.
    on_check = {"resourceType": "Bundle", "type": "collection", "entry": [
        {"resource": {
            "resourceType": "CoverageEligibilityResponse",
            "outcome": "complete",
            "disposition": "Policy is currently in-force",
            "insurance": [{"inforce": True, "item": [{
                "authorizationRequired": True,
                "benefit": [{
                    "allowedMoney": {"currency": "INR", "value": 500000},
                    "usedMoney": {"currency": "INR", "value": 120000}}]}]}]}},
        {"resource": {
            "resourceType": "Patient", "gender": "female",
            "birthDate": "2002-01-01",
            "name": [{"text": "PALLVI"}],
            "identifier": [{"type": {"coding": [{"code": "ABHA"}]},
                            "value": "91-7034-1237-4240"}],
            "address": [{"line": ["ISPUR"], "district": "UNA",
                         "state": "HIMACHAL PRADESH",
                         "postalCode": "177202"}]}},
        {"resource": {
            "resourceType": "Coverage",
            "class": [{"name": "PMJAY for Himachal"}],
            "period": {"start": "2023-04-02T00:00:00+05:30",
                       "end": "2028-04-02T00:00:00+05:30"},
            "relationship": {"coding": [{"code": "child",
                                         "display": "Child"}]}}},
    ]}
    parsed = _claims.parse_validation_bundle(on_check)
    check("the verdict is flattened from the response bundle",
          parsed["inforce"] == 1 and parsed["allowed_amount"] == 500000
          and parsed["used_amount"] == 120000 and parsed["auth_required"] == 1)
    check("the payer-enriched beneficiary profile is captured",
          parsed["patient_address"] == "ISPUR, UNA, HIMACHAL PRADESH, 177202"
          and parsed["plan_name"] == "PMJAY for Himachal"
          and parsed["relationship"] == "Child")
    _claims.apply_response(claim_id, parsed, on_check)
    claim_row = _claims.claim(claim_id)
    check("an in-force reply settles the claim as eligible",
          claim_row["status"] == "eligible"
          and _claims.balance(claim_row) == 380000)
    try:
        _claims.parse_validation_bundle({"resourceType": "Bundle", "entry": []})
        check("a reply without a verdict resource is refused", False)
    except ValueError:
        check("a reply without a verdict resource is refused", True)

    # A 404 from txn/related means hcxkit's ledger lost the transaction (its
    # database was reset): the claim must settle as an error so the operator
    # gets "Check again", not an endless "could not poll" note.
    stuck = _claims.create_claim(selected, "MemberId", "MD5SLS4X5")
    db.update("claim", stuck, {"status": "checking", "txn_id": "01GONE",
                               "checked_at": db.now_iso()})
    real_api = _claims._api

    def _gone(path, payload=None, **kw):
        raise _claims.GatewayError(
            f"hcxkit {path} returned 404: transaction not found", 404)

    _claims._api = _gone
    try:
        settled = _claims.poll_response(stuck)
    finally:
        _claims._api = real_api
    stuck_row = _claims.claim(stuck)
    check("a forgotten transaction settles the claim as an error",
          settled and stuck_row["status"] == "error"
          and "ledger was reset" in (stuck_row["error_message"] or ""))

    db.update("claim", stuck, {"status": "checking"})

    def _down(path, payload=None, **kw):
        raise _claims.GatewayError("hcxkit gateway unreachable at test")

    _claims._api = _down
    try:
        _claims.poll_response(stuck)
        check("a transient gateway failure still surfaces, not settles", False)
    except ValueError:
        check("a transient gateway failure still surfaces, not settles",
              _claims.claim(stuck)["status"] == "checking")
    finally:
        _claims._api = real_api
    check("GatewayError is still caught as ValueError by the routes",
          issubclass(_claims.GatewayError, ValueError))

    # The push half of the same exchange: hcxkit POSTs the inbound envelope to
    # the callback URL, and the worker reads the reply as a delivery outcome.
    pushed = _claims.create_claim(selected, "MemberId", "MD5SLS4X5")
    db.update("claim", pushed, {"status": "checking", "txn_id": "01PUSH",
                                "correlation_id": "corr-push-1",
                                "checked_at": db.now_iso()})
    envelope = {"jwe_headers": {"x-hcx-correlation_id": "corr-push-1"},
                "fhir": on_check}
    check("a pushed on_check settles the claim without polling",
          _claims.receive(envelope, "coverage", "on_request", "fhir") == "settled"
          and _claims.claim(pushed)["status"] == "eligible")
    check("redelivering the same callback does not reopen the claim",
          _claims.receive(envelope, "coverage", "on_request", "fhir") == "ignored"
          and _claims.claim(pushed)["status"] == "eligible")
    check("a callback for somebody else's correlation id is ignored",
          _claims.receive({"jwe_headers": {"x-hcx-correlation_id": "corr-none"},
                           "fhir": on_check}, "coverage", "on_request",
                          "fhir") == "unmatched")
    check("a message type this EMR does not answer is acknowledged, not read",
          _claims.receive({"fhir": {}}, "communication", "request", "fhir")
          == "ignored"
          and _claims.receive({"fhir": {}}, "search", "request",
                              "fhir") == "ignored"
          and _claims.receive({"jwe_headers": {"x-hcx-correlation_id": "st-none"},
                               "fhir": {}}, "status", "request",
                              "fhir") == "unmatched")

    rejected = _claims.create_claim(selected, "MemberId", "MD5SLS4X5")
    db.update("claim", rejected, {"status": "checking",
                                  "correlation_id": "corr-push-2",
                                  "checked_at": db.now_iso()})
    _claims.receive({"jwe_headers": {"x-hcx-correlation_id": "corr-push-2"},
                     "fhir": {"type": "ProtocolResponse",
                              "x-hcx-status": "response.error",
                              "x-hcx-error_details": {"code": "PAYR-1008",
                                                      "message": "HFR ID mismatch"}}},
                    "coverage", "on_request", "fhir")
    rejected_row = _claims.claim(rejected)
    check("a pushed gateway rejection settles the claim as an error",
          rejected_row["status"] == "error"
          and "PAYR-1008" in (rejected_row["error_message"] or ""))

    # The door itself. hcxkit appends the message's own inbound route to the
    # callback base, so the on_check arrives on a path rather than a flat URL
    # — and on whichever path that kit's inMap spells, which is why the whole
    # /callback family answers.
    from http.cookies import SimpleCookie as _Cookies

    door = App()
    register_all(door)

    def _deliver(path, correlation, body=None):
        payload = body if body is not None else {
            "jwe_headers": {"x-hcx-correlation_id": correlation},
            "fhir": on_check}
        return door.dispatch("POST", path, {}, {}, _Cookies(),
                             json.dumps(payload).encode(),
                             {"X-Hcxkit-Type": "coverage",
                              "X-Hcxkit-Flow": "on_request",
                              "X-Hcxkit-Payload-Kind": "fhir"})

    def _awaiting(correlation):
        cid = _claims.create_claim(selected, "MemberId", "MD5SLS4X5")
        db.update("claim", cid, {"status": "checking",
                                 "correlation_id": correlation,
                                 "checked_at": db.now_iso()})
        return cid

    on_route = _awaiting("corr-door-1")
    delivered = _deliver("/callback/v1/coverageeligibility/on_check",
                         "corr-door-1")
    check("the on_check route hcxkit posts to settles the claim",
          delivered.status == 200
          and _claims.claim(on_route)["status"] == "eligible")

    other_route = _awaiting("corr-door-2")
    _deliver("/callback/v1/coverageeligibility/check", "corr-door-2")
    check("a differently spelled inMap route lands too, not a 404",
          _claims.claim(other_route)["status"] == "eligible")

    flat = _awaiting("corr-door-3")
    _deliver("/nhcx/callback", "corr-door-3")
    check("the flat callback URL still answers",
          _claims.claim(flat)["status"] == "eligible")

    _awaiting("corr-door-4")
    check("an unreadable callback body is dead-lettered, not retried",
          _deliver("/callback/v1/coverageeligibility/on_check", "corr-door-4",
                   {"jwe_headers": {"x-hcx-correlation_id": "corr-door-4"}}
                   ).status == 400)

    section("NHCX insurance plan")
    # Step 5 of the exchange: an InsurancePlan discovery Task out, the payer's
    # package master back. Offline — the gateway is stubbed throughout.
    plan_claim = _claims.create_claim(selected, "MemberId", "MD5SLS4X5")
    plan_claim_no = _claims.claim(plan_claim)["claim_no"]
    try:
        _claims.request_plan(plan_claim)
        check("a plan fetch before the participant code is set is refused",
              False)
    except ValueError as error:
        check("a plan fetch before the participant code is set is refused",
              "participant code" in str(error))

    facility = db.default_org()
    db.update("organization", facility["id"],
              {"participant_code": "1000003463@hcx"})

    task_bundle = _claims.build_plan_request("CLM-00042", "PMJAY/HP/S/G",
                                             "IN2710000123", "KyroCare",
                                             "+914442228888")
    task = next(e["resource"] for e in task_bundle["entry"]
                if e["resource"]["resourceType"] == "Task")
    provider_entry = next(e for e in task_bundle["entry"]
                          if e["resource"]["resourceType"] == "Organization")
    task_inputs = {i["type"]["coding"][0]["code"]: i["valueString"]
                   for i in task["input"]}
    check("the discovery Task asks by policy number and provider id",
          task_inputs == {"policyNumber": "PMJAY/HP/S/G",
                          "providerId": "IN2710000123"})
    check("it is a requested poll carrying the case number",
          task["status"] == "requested"
          and task["code"]["coding"][0]["code"] == "poll"
          and task_bundle["type"] == "collection"
          and task_bundle["identifier"]["value"] == "CLM-00042")
    check("the Task requester resolves to the provider Organization",
          task["requester"]["reference"] == provider_entry["fullUrl"]
          and provider_entry["resource"]["identifier"][0]["value"] == "IN2710000123"
          and provider_entry["resource"]["identifier"][0]["type"]["coding"][0]["code"]
          == "NPI")
    check("plan discovery carries no clinical content at all",
          {e["resource"]["resourceType"] for e in task_bundle["entry"]}
          == {"Task", "Organization"})
    try:
        _claims.build_plan_request("CLM-1", "", "", "KyroCare")
        check("a Task with neither input is refused", False)
    except ValueError:
        check("a Task with neither input is refused", True)

    posted = {}

    def _ack(path, payload=None, **kw):
        posted["path"], posted["payload"] = path, payload
        return {"txn_id": "01PLAN", "correlation_id": "corr-plan-1"}

    _claims._api = _ack
    try:
        _claims.request_plan(plan_claim)
    finally:
        _claims._api = real_api
    plan_state = _claims.plan(plan_claim)
    check("the Task is queued to the insuranceplan route, not coverage's",
          posted["path"] == "/fhir/out/v1/insuranceplan/request"
          and posted["payload"]["jwe_headers"]["x-hcx-workflow_id"] == plan_claim_no
          and posted["payload"]["jwe_headers"]["x-hcx-sender_code"]
          == "1000003463@hcx")
    check("the claim starts awaiting the payer's plan",
          plan_state["status"] == "fetching" and plan_state["txn_id"] == "01PLAN"
          and plan_state["provider_id"] == "IN2710000123"
          and plan_state["policy_code"] == "PMJAY/HP/S/G")

    # The payer's on_request reply — PMJAY's package-master shape, reduced to
    # what the parser reads.
    # Extension urls and cost codings exactly as the live PMJAY sandbox sends
    # them: a condition's own child url names it, and the cost coded
    # `Procedure` is the package rate.
    sd = "https://nrces.in/ndhm/fhir/r4/StructureDefinition"
    plan_cs = "https://www.nrces.in/ndhm/fhir/r4/CodeSystem/ndhm-plan-type"

    stg_url = "https://payer.gov.in/policy/stgquestionnaire/105315"

    def _stg_form():
        # The payer writes the question on `prefix`, not `text`, and answers
        # as plain strings rather than codings.
        return {"resourceType": "Questionnaire", "id": "105315",
                "url": stg_url, "title": "STG Questionnaire",
                "status": "active",
                "item": [
                    {"linkId": "112408", "type": "choice", "required": True,
                     "prefix": "Still image of the patient undergoing the "
                               "procedure",
                     "answerOption": [{"valueString": "Yes"},
                                      {"valueString": "No"}]},
                    {"linkId": "112409", "type": "attachment",
                     "text": "Post treatment photo"}]}

    def _cost(code, amount, qualifier=None, qualifier_code=None):
        cost = {"type": {"coding": [{"system": plan_cs, "code": code}]},
                "value": {"unit": "INR", "value": amount}}
        if qualifier:
            cost["qualifiers"] = [{"coding": [{"code": qualifier_code,
                                               "display": qualifier}]}]
        return cost
    package_master = {"resourceType": "Bundle", "type": "collection", "entry": [
        {"resource": {
            "resourceType": "InsurancePlan",
            "id": "PMJAY-HP",
            "identifier": [{"value": "PMJAY/HP/S/G"}],
            "name": "PMJAY for Himachal",
            # Requirements on the resource itself apply to every claim under
            # the policy, and use the payer's other child-url spelling.
            "extension": [
                {"url": f"{sd}/Claim-SupportingInfoRequirement",
                 "extension": [
                     {"url": "SupportInfoCategory", "valueCodeableConcept": {
                         "coding": [{"code": "POI",
                                     "display": "Proof of identity"}]}},
                     {"url": "SupportInfoCode", "valueCodeableConcept": {
                         "coding": [{"code": "ADN",
                                     "display": "Aadhaar Number"}]}}]}],
            "plan": [{
                "type": {"coding": [{"code": "PMJAY",
                                     "display": "Ayushman Bharat"}]},
                "generalCost": [{"cost": {"value": 500000, "currency": "INR"}}],
                "specificCost": [{
                    "category": {"coding": [{"code": "GM",
                                             "display": "General Medicine"}]},
                    "benefit": [
                        {"type": {"coding": [{"code": "BM001",
                                              "display": "Dengue fever"}]},
                         "extension": [
                             {"url": f"{sd}/Claim-Condition", "extension": [
                                 {"url": f"{sd}/Claim-Condition/IsDayCare",
                                  "valueString": "N"},
                                 {"url": f"{sd}/Claim-Condition/ImplantApplicable",
                                  "valueString": "Y"}]},
                             {"url": f"{sd}/Claim-SupportingInfoRequirement/BM001",
                              "extension": [
                                  {"url": "category", "valueCodeableConcept": {
                                      "coding": [{"code": "DIA",
                                                  "display": "Diagnostic report"}]}},
                                  {"url": "code", "valueCodeableConcept": {
                                      "coding": [{"code": "MAND0409",
                                                  "display": "any investigations done"}]}},
                                  {"url": "documentationUrl", "valueReference": {
                                      "reference": stg_url}}]}],
                         # The ward/ICU tiers are money paid *over* the package
                         # rate, not the rate — reading one as the rate is the
                         # bug this pins.
                         "cost": [_cost("Procedure", 27000),
                                  _cost("Stratification", 4500,
                                        "ICU - With Ventilator")]},
                        {"type": {"coding": [{"code": "SE012A",
                                              "display": "Corneal Grafting"}]},
                         # The other spelling the guides use, and the explicit
                         # code/value child shape.
                         "extension": [
                             {"url": f"{sd}/claimCondition", "extension": [
                                 {"url": "code", "valueString": "GovtReserved"},
                                 {"url": "value", "valueString": "N"}]}],
                         "cost": [_cost("Procedure", 41000),
                                  _cost("Implant", 1800, "Fibrin Glue",
                                        "IMP0026")]}]}]}],
            # The live payer sends the same packages a second time under
            # `coverage`; merging on the code must not double them, and a
            # coverage-only package must still come through.
            "coverage": [{
                "type": {"coding": [{"code": "GM",
                                     "display": "General Medicine"}]},
                "benefit": [
                    {"type": {"coding": [{"code": "BM001",
                                          "display": "Dengue fever"}]},
                     "limit": [{"code": {"coding": [{"code": "BM001"}]},
                                "value": {"unit": "INR", "value": 27000}}]},
                    {"type": {"coding": [{"code": "BM009",
                                          "display": "Enteric fever"}]},
                     "limit": [{"code": {"coding": [{"code": "BM009"}]},
                                "value": {"unit": "INR", "value": 9000}},
                               {"code": {"coding": [{"code": "STRAT006b",
                                                     "display": "HDU"}]},
                                "value": {"unit": "INR", "value": 2700}}]},
                    # The implant again, this time as an entry of its own —
                    # which is the only place its own rate and rules live.
                    {"type": {"coding": [{"code": "IMP0026",
                                          "display": "Fibrin Glue"}]},
                     "limit": [{"code": {"coding": [{"code": "IMP0026"}]},
                                "value": {"unit": "INR", "value": 1800}}]}]}]}},
        {"resource": {"resourceType": "Organization", "name": "Nhcx Pmjay"}},
        {"resource": _stg_form()},
        # The same form again, exactly as the payer repeats it per benefit.
        {"resource": _stg_form()},
    ]}
    parsed_plan = _claims.parse_plan_bundle(package_master)
    check("the plan header carries the overall sum insured",
          parsed_plan["values"]["sum_insured"] == 500000
          and parsed_plan["values"]["plan_title"] == "PMJAY for Himachal"
          and parsed_plan["values"]["plan_identifier"] == "PMJAY/HP/S/G")
    by_code = {b["code"]: b for b in parsed_plan["benefits"]}
    dengue = by_code["BM001"]
    check("the package rate is the Procedure cost, not a ward tier",
          dengue["rate"] == 27000 and dengue["cost_type"] == "Procedure"
          and dengue["category_display"] == "General Medicine")
    check("the ward/ICU tiers are kept as money paid over that rate",
          json.loads(dengue["extras"])
          == [{"type": "Stratification", "code": None,
               "label": "ICU - With Ventilator",
               "rate": 4500, "currency": "INR"}])
    check("claim conditions are read off the benefit's extensions",
          json.loads(dengue["conditions"])
          == {"IsDayCare": "N", "ImplantApplicable": "Y"})
    check("the other condition spelling and code/value shape read too",
          json.loads(by_code["SE012A"]["conditions"]) == {"GovtReserved": "N"})
    check("required documents are kept out of the conditions",
          json.loads(dengue["supporting_info"])
          == [{"category": "DIA", "category_display": "Diagnostic report",
               "code": "MAND0409", "display": "any investigations done",
               "form": stg_url}])
    check("policy-wide requirements are read off the plan resource itself",
          json.loads(parsed_plan["values"]["policy_documents"])
          == [{"category": "POI", "category_display": "Proof of identity",
               "code": "ADN", "display": "Aadhaar Number"}])
    check("a repeated questionnaire is collected once, by url",
          [f["url"] for f in parsed_plan["forms"]] == [stg_url]
          and parsed_plan["forms"][0]["kind"] == "stgquestionnaire"
          and parsed_plan["forms"][0]["form_id"] == "105315")
    questions = json.loads(parsed_plan["forms"][0]["items"])
    check("a question reads from prefix, then text, with its options",
          [q["text"] for q in questions]
          == ["Still image of the patient undergoing the procedure",
              "Post treatment photo"]
          and questions[0]["options"] == ["Yes", "No"]
          and questions[0]["required"] and not questions[1]["required"]
          and questions[1]["type"] == "attachment")
    check("the two shapes merge on the package code instead of doubling",
          sorted(by_code) == ["BM001", "BM009", "IMP0026", "SE012A"]
          and by_code["BM009"]["rate"] == 9000
          and json.loads(by_code["BM009"]["extras"])[0]["label"] == "HDU")
    check("a benefit is typed by what its code names",
          {b["code"]: b["kind"] for b in parsed_plan["benefits"]}
          == {"BM001": "Procedure", "SE012A": "Procedure",
              "BM009": "Procedure", "IMP0026": "Implant"})

    _claims.apply_plan(plan_state["id"], parsed_plan, package_master)
    plan_state = _claims.plan(plan_claim)
    check("the package master settles as ready",
          plan_state["status"] == "ready" and plan_state["fetched_at"]
          and plan_state["sum_insured"] == 500000)
    offered = _claims.packages(plan_claim)
    check("the payer's packages replace the local HBP master",
          sorted(p["code"] for p in offered)
          == ["BM001", "BM009", "IMP0026", "SE012A"]
          and offered[0]["source"] == "payer" and offered[0]["rate"] == 27000)
    check("a claim without a plan still gets the local master",
          _claims.packages(claim_id)[0]["source"] == "local")
    check("the packages group by speciality for the filter",
          [(c["category_code"], c["n"])
           for c in _claims.plan_categories(plan_state["id"])] == [("GM", 4)])

    # A payer master runs to a thousand packages, so the operator searches.
    def _found(**kw):
        return sorted(b["code"] for b in
                      _claims.plan_benefits(plan_state["id"], **kw))

    check("searching by name matches part of it, either case",
          _found(search="dengue") == ["BM001"]
          and _found(search="GRAFT") == ["SE012A"])
    check("searching by code matches too",
          _found(search="BM00") == ["BM001", "BM009"])
    check("the speciality and type filters narrow the same list",
          _found(category="GM") == ["BM001", "BM009", "IMP0026", "SE012A"]
          and _found(kind="Implant") == ["IMP0026"]
          and _found(kind="Procedure") == ["BM001", "BM009", "SE012A"]
          and _found(category="GM", search="corneal") == ["SE012A"])
    check("a search matching nothing is empty, not everything",
          _found(search="zzz") == [])
    detail_row = _claims.plan_benefit(
        _claims.plan_benefits(plan_state["id"], search="dengue")[0]["id"])
    stored_forms = _claims.plan_forms(plan_state["id"])
    check("the forms are stored once per plan and read back by url",
          [f["form_id"] for f in stored_forms] == ["105315"]
          and [f["url"] for f in _claims.plan_forms(plan_state["id"],
                                                    [stg_url, stg_url, ""])]
          == [stg_url]
          and _claims.plan_forms(plan_state["id"], []) == [])
    check("a package's document requirement resolves to its form",
          _claims.plan_forms(plan_state["id"], [
              d.get("form") for d in _claims.plan_documents(
                  _claims.plan_benefits(plan_state["id"],
                                        search="dengue")[0])])[0]["title"]
          == "STG Questionnaire")
    check("forms are searchable by name and by id",
          [f["form_id"] for f in _claims.search_forms(plan_state["id"], "stg")]
          == ["105315"]
          and [f["form_id"] for f in _claims.search_forms(plan_state["id"],
                                                          "1053")] == ["105315"]
          and _claims.search_forms(plan_state["id"], "zzz") == [])
    # An implant is named twice — as a qualifier on the package that may use
    # it, and as an entry of its own — and the item view has to join the two.
    grafting = _claims.plan_benefits(plan_state["id"], search="corneal")[0]
    allowed = _claims.benefit_implants(grafting)
    fibrin = _claims.plan_benefits(plan_state["id"], kind="Implant")[0]
    check("a package's implants resolve to their own entry in the plan",
          [(i["code"], i["display"], i["rate"], i["id"]) for i in allowed]
          == [("IMP0026", "Fibrin Glue", 1800, fibrin["id"])])
    check("and the relation reads backwards from the implant",
          [p["code"] for p in _claims.benefit_procedures(
              _claims.plan_benefit(fibrin["id"]))] == ["SE012A"])
    check("a procedure has no packages listed against it that way",
          _claims.benefit_procedures(
              _claims.plan_benefit(grafting["id"])) == []
          and _claims.benefit_implants(_claims.plan_benefit(fibrin["id"])) == [])
    check("the ward tiers and the implants are separate money",
          [t["type"] for t in _claims.plan_extras(grafting)] == ["Implant"]
          and [t["type"] for t in _claims.plan_extras(
              _claims.plan_benefits(plan_state["id"], search="dengue")[0])]
          == ["Stratification"])
    check("the policy-wide documents read back off the plan row",
          [d["code"] for d in _claims.policy_documents(plan_state)] == ["ADN"])
    check("one package reads back with its claim and everything on it",
          detail_row["claim_id"] == plan_claim
          and detail_row["kind"] == "Procedure"
          and _claims.plan_extras(detail_row)[0]["label"]
          == "ICU - With Ventilator"
          and _claims.plan_documents(detail_row)[0]["code"] == "MAND0409"
          and _claims.plan_condition_map(detail_row)["IsDayCare"] == "N")

    # The second published shape: coverage -> benefit -> limit. A provider
    # parser has to take whichever the payer sends.
    indemnity = {"resourceType": "Bundle", "type": "collection", "entry": [
        {"resource": {
            "resourceType": "InsurancePlan", "name": "Group Mediclaim",
            "coverage": [{
                "type": {"coding": [{"code": "IP", "display":
                                     "In-Patient Hospitalization"}]},
                "benefit": [{
                    "type": {"coding": [{"code": "ICU",
                                         "display": "ICU Charges"}]},
                    "requirement": "Pre-authorization required",
                    "limit": [{"value": {"value": 500000, "unit": "INR"},
                               "code": {"text": "Per year"}}]}]}]}}]}
    indemnity_benefits = _claims.parse_plan_bundle(indemnity)["benefits"]
    check("the indemnity shape parses to the same benefit rows",
          len(indemnity_benefits) == 1
          and indemnity_benefits[0]["code"] == "ICU"
          and indemnity_benefits[0]["rate"] == 500000
          and indemnity_benefits[0]["requirement"] == "Pre-authorization required"
          and indemnity_benefits[0]["category_display"]
          == "In-Patient Hospitalization")

    empty_plan = {"resourceType": "Bundle", "type": "collection", "entry": [
        {"resource": {"resourceType": "InsurancePlan",
                      "name": "PMJAY for Himachal"}}]}
    _claims.apply_plan(plan_state["id"],
                       _claims.parse_plan_bundle(empty_plan), empty_plan)
    emptied = _claims.plan(plan_claim)
    check("an empty plan is a business outcome, not a transport failure",
          emptied["status"] == "empty"
          and _claims.plan_benefits(emptied["id"]) == [])
    check("an empty plan falls back to the local package master",
          _claims.packages(plan_claim)[0]["source"] == "local")
    try:
        _claims.parse_plan_bundle({"resourceType": "Bundle", "entry": []})
        check("a reply without an InsurancePlan is refused", False)
    except ValueError:
        check("a reply without an InsurancePlan is refused", True)

    # The push half. hcxkit labels an insurance reply flow "request" where a
    # coverage reply is "on_request" — its inMap spells the two directions the
    # opposite way round — so the flow must not be what routes this.
    pushed_claim = _claims.create_claim(selected, "MemberId", "MD5SLS4X5")
    db.insert("claim_plan", {"claim_id": pushed_claim, "status": "fetching",
                             "txn_id": "01PUSHPLAN",
                             "correlation_id": "corr-plan-2",
                             "requested_at": db.now_iso()})
    delivered_plan = door.dispatch(
        "POST", "/callback/v1/insuranceplan/on_request", {}, {}, _Cookies(),
        json.dumps({"jwe_headers": {"x-hcx-correlation_id": "corr-plan-2"},
                    "fhir": package_master}).encode(),
        {"X-Hcxkit-Type": "insurance", "X-Hcxkit-Flow": "request",
         "X-Hcxkit-Payload-Kind": "fhir"})
    pushed_state = _claims.plan(pushed_claim)
    check("the payer's plan lands on the callback despite the flow label",
          delivered_plan.status == 200 and pushed_state["status"] == "ready"
          and len(_claims.plan_benefits(pushed_state["id"])) == 4)
    check("redelivering the plan does not refetch it",
          _claims.receive({"jwe_headers": {"x-hcx-correlation_id": "corr-plan-2"},
                           "fhir": package_master}, "insurance", "request",
                          "fhir") == "ignored")
    check("a plan callback nobody is waiting for is unmatched, not an error",
          _claims.receive({"jwe_headers": {"x-hcx-correlation_id": "corr-none"},
                           "fhir": package_master}, "insurance", "request",
                          "fhir") == "unmatched")

    rejected_claim = _claims.create_claim(selected, "MemberId", "MD5SLS4X5")
    rejected_plan = db.insert("claim_plan", {
        "claim_id": rejected_claim, "status": "fetching",
        "correlation_id": "corr-plan-3", "requested_at": db.now_iso()})
    _claims.receive({"jwe_headers": {"x-hcx-correlation_id": "corr-plan-3"},
                     "fhir": {"type": "ProtocolResponse",
                              "x-hcx-status": "response.error",
                              "x-hcx-error_details": {
                                  "code": "PAYR-1008",
                                  "message": "HFR ID mismatch"}}},
                    "insurance", "request", "fhir")
    rejected_state = db.one("SELECT * FROM claim_plan WHERE id = ?",
                            (rejected_plan,))
    check("a gateway rejection settles the plan as an error",
          rejected_state["status"] == "error"
          and "PAYR-1008" in (rejected_state["error_message"] or ""))

    _claims._api = _ack
    try:
        _claims.request_plan(pushed_claim)
    finally:
        _claims._api = real_api
    refetched = _claims.plan(pushed_claim)
    check("a refetch clears the stale master and waits again",
          refetched["status"] == "fetching"
          and refetched["fetched_at"] is None
          and _claims.plan_benefits(refetched["id"]) == [])

    section("NHCX preauth")
    # The beneficiary registered without the dashes the payer writes, and
    # currently admitted — the digits-only ABHA match must still find them.
    beneficiary = services.create_patient({
        "name": "PALLVI", "gender": "female", "birth_date": "2002-01-01",
        "phone": "+919818512600", "abha_number": "91703412374240"})
    stay = services.create_encounter("IPD", {
        "patient_id": beneficiary, "status": "in-progress", "class_code": "IMP",
        "practitioner_id": physician, "department": "General Medicine",
        "period_start": "2026-08-18T10:00:00+05:30",
        "ward": "General Ward (Female)", "bed": "GW-3", "bed_rate": 1500})
    claim_row = _claims.claim(claim_id)
    matches = _claims.linkable_admissions(claim_row)
    check("the current IPD stay is found by ABHA digits despite the dashes",
          [m["encounter_id"] for m in matches] == [stay])
    check("a finished stay is not offered for linking",
          admission not in [m["encounter_id"] for m in matches])
    try:
        _claims.link_admission(claim_id, admission)
        check("linking a non-matching admission is refused", False)
    except ValueError:
        check("linking a non-matching admission is refused", True)
    draft_claim = _claims.create_claim(selected, "MemberId", "MD5SLS4X5")
    try:
        _claims.link_admission(draft_claim, stay)
        check("linking before the claim is eligible is refused", False)
    except ValueError:
        check("linking before the claim is eligible is refused", True)
    _claims.link_admission(claim_id, stay)
    # The stay records its diagnosis, as an IPD admission does; the
    # pre-authorisation reads it from there.
    db.insert("condition", {
        "patient_id": beneficiary, "encounter_id": stay, "category": "diagnosis",
        "clinical_status": "active", "verification_status": "confirmed",
        "snomed_code": "233604007", "snomed_display": "Pneumonia",
        "icd10_code": "J18", "icd10_display": "Pneumonia, unspecified organism",
        "text": "Pneumonia", "recorded_at": db.now_iso()})
    claim_row = _claims.claim(claim_id)
    check("linking stores the patient, the stay and a default admission date",
          claim_row["patient_id"] == beneficiary
          and claim_row["encounter_id"] == stay
          and claim_row["admission_date"] == "2026-08-18")

    # --- the preauth draft
    dx = [{"code": "233604007"}]                      # Pneumonia -> ICD-10 J18
    team = [{"doctor": str(physician), "role": "admitting"}]
    stay_dates = {"admission_date": "2026-08-18",
                  "expected_discharge_date": "2026-08-22"}
    try:
        _claims.save_preauth(draft_claim, dict(stay_dates, case_type="nonpackage"),
                             dx, team, [{"code": "BED-DAY", "qty": "4"}])
        check("a preauth without a linked admission is refused", False)
    except ValueError:
        check("a preauth without a linked admission is refused", True)
    # The admission recorded pneumonia and is under the physician, so the
    # form does not have to say so — and cannot say otherwise.
    _claims.save_preauth(claim_id, dict(stay_dates, case_type="nonpackage"),
                         [], [], [{"code": "BED-DAY", "qty": "4"}])
    from_ipd = _claims.preauth_children(claim_id)
    check("the diagnosis comes off the admission when the form has none",
          [d["icd10_code"] for d in from_ipd["diagnoses"]] == ["J18"])
    check("and so does the treating doctor",
          [(t["practitioner_id"], t["role"]) for t in from_ipd["care_team"]]
          == [(physician, "treating")])
    _claims.save_preauth(claim_id, dict(stay_dates, case_type="nonpackage"),
                         [{"code": "44054006"}],
                         [{"doctor": str(physician), "role": "surgeon"}],
                         [{"code": "BED-DAY", "qty": "4"}])
    from_ipd = _claims.preauth_children(claim_id)
    check("what the form says cannot overrule the admission's record",
          [d["icd10_code"] for d in from_ipd["diagnoses"]] == ["J18"]
          and from_ipd["care_team"][0]["role"] == "treating")
    check("an admission without them falls back to the form",
          _claims.admission_dossier(None) == {"diagnoses": [], "team": []})
    try:
        _claims.save_preauth(claim_id, dict(stay_dates, case_type="nonpackage"),
                             dx, team, [])
        check("a non-package case without items is refused", False)
    except ValueError:
        check("a non-package case without items is refused", True)
    try:
        _claims.save_preauth(claim_id, dict(stay_dates, case_type="nonpackage"),
                             dx, team, [{"code": "BED-DAY", "qty": "0"}])
        check("an item without a quantity is refused", False)
    except ValueError:
        check("an item without a quantity is refused", True)
    try:
        _claims.save_preauth(
            claim_id, {"admission_date": "2026-08-18",
                       "expected_discharge_date": "2026-08-01",
                       "case_type": "package", "package_code": "SA001A"},
            dx, team, [])
        check("a discharge date before admission is refused", False)
    except ValueError:
        check("a discharge date before admission is refused", True)

    _claims.save_preauth(claim_id, dict(stay_dates, case_type="nonpackage"),
                         dx, team, [{"code": "BED-DAY", "qty": "4"},
                                    {"code": "OT-MAJOR", "qty": "1"}])
    claim_row = _claims.claim(claim_id)
    children = _claims.preauth_children(claim_id)
    check("a non-package estimate is master price times quantity",
          claim_row["preauth_total"] == 4 * 1500 + 18000
          and [i["amount"] for i in children["items"]] == [6000, 18000])
    check("the diagnosis is stored with its ICD-10 coding",
          children["diagnoses"][0]["icd10_code"] == "J18"
          and children["diagnoses"][0]["snomed_code"] == "233604007")
    check("the care team is the admission's consultant as treating doctor",
          children["care_team"][0]["practitioner_id"] == physician
          and children["care_team"][0]["role"] == "treating")

    _claims.save_preauth(
        claim_id, dict(stay_dates, case_type="package", package_code="SA001A"),
        dx, team, [])
    claim_row = _claims.claim(claim_id)
    children = _claims.preauth_children(claim_id)
    check("switching to a package takes the rate from the package master",
          claim_row["package_name"] == "Appendicectomy (open)"
          and claim_row["preauth_total"] == 27000)
    check("re-saving replaces the children instead of stacking them",
          len(children["diagnoses"]) == 1 and children["items"] == [])

    # With the payer's package master fetched for this claim, the estimate has
    # to come from *that* master — the local HBP list no longer applies.
    payer_master = db.insert("claim_plan", {
        "claim_id": claim_id, "status": "ready", "correlation_id": "corr-pa-1",
        "requested_at": db.now_iso(), "fetched_at": db.now_iso()})
    db.insert("claim_plan_benefit", {
        "plan_id": payer_master, "seq": 1, "category_code": "GM",
        "category_display": "General Medicine", "code": "BM001",
        "kind": "Procedure", "display": "Dengue fever", "rate": 27000,
        "currency": "INR"})
    try:
        _claims.save_preauth(
            claim_id, dict(stay_dates, case_type="package",
                           package_code="SA001A"), dx, team, [])
        check("a package case with nothing quoted is refused", False)
    except ValueError as error:
        check("a package case with nothing quoted is refused",
              "at least one procedure" in str(error))
    _claims.add_line(claim_id, "Procedure", "BM001")
    _claims.save_preauth(
        claim_id, dict(stay_dates, case_type="package"), dx, team, [])
    claim_row = _claims.claim(claim_id)
    check("a package case is priced from the lines quoted off the plan",
          claim_row["package_code"] == "BM001"
          and claim_row["package_name"] == "Dengue fever"
          and claim_row["preauth_total"] == 27000)
    db.execute("DELETE FROM claim_line WHERE claim_id = ?", (claim_id,))
    db.execute("DELETE FROM claim_plan WHERE id = ?", (payer_master,))
    # Without a plan the local HBP list is still the picker, unchanged.
    _claims.save_preauth(
        claim_id, dict(stay_dates, case_type="package", package_code="SA001A"),
        dx, team, [])
    check("with no plan fetched the local HBP master still prices the case",
          _claims.claim(claim_id)["preauth_total"] == 27000
          and _claims.claim(claim_id)["package_name"]
          == "Appendicectomy (open)")

    # --- supporting documents
    doc_id = _claims.add_document(claim_id, "admission-note.pdf",
                                  "application/pdf", b"%PDF-1.4 test", "Note")
    docs = _claims.documents(claim_id)
    check("a PDF attaches with its size recorded",
          [d["id"] for d in docs] == [doc_id]
          and docs[0]["size"] == len(b"%PDF-1.4 test"))
    try:
        _claims.add_document(claim_id, "virus.exe", "application/octet-stream",
                             b"MZ")
        check("a non PDF/image upload is refused", False)
    except ValueError:
        check("a non PDF/image upload is refused", True)
    try:
        _claims.add_document(claim_id, "huge.png", "image/png",
                             b"x" * (_claims.MAX_DOCUMENT_BYTES + 1))
        check("an oversized upload is refused", False)
    except ValueError:
        check("an oversized upload is refused", True)
    _claims.delete_document(doc_id)
    check("a document can be removed", _claims.documents(claim_id) == [])

    section("NHCX payer adapters")
    from emr import masters as _masters
    from emr import payers as _payers

    check("the seeded configuration maps PMJAY and the IRDAI payer onto "
          "their adapters",
          [(r["participant_code"], r["adapter"]["key"])
           for r in _payers.configured()]
          == [("1518@hcx", "pmjay"), ("1000004805@hcx", "kyrocare")])
    check("a participant code matches with or without the @hcx suffix",
          _payers.adapter_for("1518")["key"] == "pmjay"
          and _payers.adapter_for("1518@HCX")["key"] == "pmjay")
    check("an unconfigured payer falls back to the generic adapter, not PMJAY",
          _payers.adapter_for("9999@hcx")["key"] == "generic"
          and _payers.adapter_for("")["key"] == "generic"
          and _payers.adapter_for(None)["key"] == "generic")
    check("the generic adapter claims none of PMJAY's extras",
          _payers.GENERIC["program_code"] is None
          and _payers.GENERIC["auth_requirements"] is False
          and _payers.PMJAY["program_code"] == "AB-PMJAY"
          and _payers.PMJAY["auth_requirements"] is True)
    # The IRDAI payer speaks the PMJAY dialect — it answers the
    # auth-requirements check and publishes a package master — without the
    # scheme programme code, which an indemnity policy has no claim to.
    check("the Dummy IRDAI Payer answers auth-requirements under its own "
          "namespace and no programme code",
          _payers.adapter_for("1000004805")["key"] == "kyrocare"
          and _payers.KYROCARE["auth_requirements"] is True
          and _payers.KYROCARE["package_master"] is True
          and _payers.KYROCARE["program_code"] is None
          and _payers.KYROCARE["payer_system"] == "https://kyro.care/fhir")
    check("the mapping is configuration an operator can edit",
          "payer_adapter" in _masters.CODE_LABEL
          and db.term("payer_adapter", "1518@hcx")["extra"] == "pmjay")
    db.insert("terminology", {"kind": "payer_adapter", "system": "local",
                              "code": "2000@hcx", "display": "Some TPA",
                              "extra": "", "sort_order": 50})
    check("a payer configured with no adapter key is generic, not broken",
          _payers.adapter_for("2000@hcx")["key"] == "generic")
    db.execute("DELETE FROM terminology WHERE kind = 'payer_adapter' "
               "AND code = '2000@hcx'")

    # PMJAY hides two facts in one free-text field; the adapter is where that
    # string work lives, so the exchange never has to know about it.
    doc_pre = _payers.supporting_entry(_payers.PMJAY, {
        "coding": [{"code": "MAND0671", "display": "Clinical notes"}],
        "text": "Type: pre\n Procedure Code:SE020A"})
    doc_post = _payers.supporting_entry(_payers.PMJAY, {
        "coding": [{"code": "MAND9", "display": "Discharge summary"}],
        "text": "Type: post\n Procedure Code:SE020A"})
    form = _payers.supporting_entry(_payers.PMJAY, {
        "coding": [{"code": "100008", "display": "General Findings"}],
        "text": "fullUrl: https://payer.gov.in/policy/questionnaire/100008"})
    check("a supporting document is read with its stage and its procedure",
          doc_pre == {"kind": "document", "code": "MAND0671",
                      "display": "Clinical notes", "form_url": None,
                      "stage": "pre", "for_code": "SE020A",
                      "at_preauth": True})
    check("a stage the payer defers is not asked for at preauth",
          doc_post["stage"] == "post" and doc_post["at_preauth"] is False)
    check("a questionnaire is read as a form, by its fullUrl",
          form["kind"] == "form" and form["at_preauth"] is True
          and form["form_url"]
          == "https://payer.gov.in/policy/questionnaire/100008")
    # The IRDAI payer writes the same two facts the same way, under its own
    # document taxonomy, and "post" defers a document to the claim.
    kyro_doc = _payers.supporting_entry(_payers.KYROCARE, {
        "coding": [{"system": "https://kyro.care/fhir/CodeSystem/nhcx-document-type",
                    "code": "HDS", "display": "Hospital Discharge Summary"}],
        "text": "Type: post\nProcedure Code: PROC-KNEE-01"})
    kyro_form = _payers.supporting_entry(_payers.KYROCARE, {
        "coding": [{"code": "STG", "display": "Total Knee Replacement — STG"}],
        "text": "fullUrl: https://kyro.care/fhir/Questionnaire/PROC-KNEE-01"})
    check("the IRDAI payer's ruling reads with the same adapter code",
          kyro_doc == {"kind": "document", "code": "HDS",
                       "display": "Hospital Discharge Summary",
                       "form_url": None, "stage": "post",
                       "for_code": "PROC-KNEE-01", "at_preauth": False}
          and kyro_form["kind"] == "form"
          and kyro_form["form_url"]
          == "https://kyro.care/fhir/Questionnaire/PROC-KNEE-01")

    section("NHCX preauth submission")
    # The preauth quotes the payer's own packages, so `claim_id` gets the
    # package master fetched earlier back — this is the same plan the
    # insurance-plan section parsed.
    quoted = db.insert("claim_plan", {
        "claim_id": claim_id, "status": "ready", "correlation_id": "corr-pa-2",
        "requested_at": db.now_iso(), "fetched_at": db.now_iso(),
        "plan_title": "PMJAY for Himachal"})
    for seq, (kind, code, name, rate, extras, docs) in enumerate([
        ("Procedure", "BM001", "Dengue fever", 27000,
         [{"type": "Stratification", "code": "STRAT006a",
           "label": "Routine Ward", "rate": 1800, "currency": "INR"},
          {"type": "Implant", "code": "IMP0026", "label": "Fibrin Glue",
           "rate": 1800, "currency": "INR"}],
         [{"code": "MAND0409", "display": "any investigations done",
           "category": "DIA", "form": stg_url}]),
        ("Implant", "IMP0026", "Fibrin Glue", 1800, [], []),
    ], 1):
        db.insert("claim_plan_benefit", {
            "plan_id": quoted, "seq": seq, "kind": kind, "code": code,
            "display": name, "rate": rate, "currency": "INR",
            "category_code": "GM", "category_display": "General Medicine",
            "extras": json.dumps(extras) if extras else None,
            "supporting_info": json.dumps(docs) if docs else None})
    db.insert("claim_plan_form", {
        "plan_id": quoted, "url": stg_url, "form_id": "105315",
        "kind": "stgquestionnaire", "title": "STG Questionnaire",
        "items": json.dumps([
            {"linkId": "112408", "type": "choice", "required": True,
             "text": "Still image of the patient", "options": ["Yes", "No"],
             "depth": 0},
            {"linkId": "112409", "type": "attachment", "required": False,
             "text": "Post treatment photo", "options": [], "depth": 0}])})

    try:
        _claims.add_line(claim_id, "Procedure", "NOPE")
        check("a code the plan does not carry is refused", False)
    except ValueError:
        check("a code the plan does not carry is refused", True)
    try:
        _claims.add_line(claim_id, "Wardboy", "BM001")
        check("a line kind a preauth cannot quote is refused", False)
    except ValueError:
        check("a line kind a preauth cannot quote is refused", True)

    _claims.add_line(claim_id, "Procedure", "BM001")
    quoted_lines = _claims.lines(claim_id)
    check("a procedure is priced from the plan, not the form",
          len(quoted_lines) == 1 and quoted_lines[0]["unit_price"] == 27000
          and quoted_lines[0]["amount"] == 27000
          and quoted_lines[0]["category_display"] == "General Medicine")
    try:
        _claims.add_line(claim_id, "Procedure", "BM001")
        check("the same package cannot be quoted twice", False)
    except ValueError:
        check("the same package cannot be quoted twice", True)

    # The whole point of the package master: the payer already said which
    # implants and wards go with the procedure just chosen.
    suggested = _claims.line_suggestions(claim_id)
    check("the plan suggests the implants and tiers for that procedure",
          [i["code"] for i in suggested["implants"]] == ["IMP0026"]
          and [(t["code"], t["parent"]) for t in suggested["tiers"]]
          == [("STRAT006a", "BM001")])
    _claims.add_line(claim_id, "Implant", "IMP0026")
    _claims.add_line(claim_id, "Stratification", "STRAT006a", "BM001")
    check("a ward tier is priced through the procedure that offers it",
          [(r["kind"], r["unit_price"]) for r in _claims.lines(claim_id)]
          == [("Implant", 1800), ("Procedure", 27000),
              ("Stratification", 1800)])
    check("what is already quoted drops out of the suggestions",
          _claims.line_suggestions(claim_id) == {"implants": [], "tiers": []})
    try:
        _claims.add_line(claim_id, "Stratification", "STRAT006a", "BM009")
        check("a tier the named procedure does not offer is refused", False)
    except ValueError:
        check("a tier the named procedure does not offer is refused", True)

    ward = next(r for r in _claims.lines(claim_id)
                if r["kind"] == "Stratification")
    _claims.save_quantities(claim_id, {ward["id"]: "3"})
    check("quantity multiplies the plan's rate, server side",
          db.one("SELECT * FROM claim_line WHERE id = ?",
                 (ward["id"],))["amount"] == 5400
          and _claims.lines_total(claim_id) == 27000 + 1800 + 5400)
    try:
        _claims.save_quantities(claim_id, {ward["id"]: "0"})
        check("a zero quantity is refused", False)
    except ValueError:
        check("a zero quantity is refused",
              _claims.lines_total(claim_id) == 34200)
    try:
        _claims.save_quantities(claim_id, {ward["id"]: "1.5"})
        check("a fractional quantity is refused", False)
    except ValueError as error:
        check("a fractional quantity is refused", "whole number" in str(error))
    _claims.save_quantities(claim_id, {ward["id"]: "2.0"})
    check("a whole-number quantity written with a decimal point is taken",
          db.one("SELECT quantity FROM claim_line WHERE id = ?",
                 (ward["id"],))["quantity"] == 2)
    _claims.save_quantities(claim_id, {ward["id"]: "3"})

    # --- a price is the plan's, unless the plan has none
    package = next(r for r in _claims.lines(claim_id) if r["kind"] == "Procedure")
    check("a priced package takes no price from the form",
          not _claims.price_is_open(claim_id, package))
    _claims.save_quantities(claim_id, {}, {package["id"]: "99"})
    check("and keeps the plan's rate whatever was typed",
          db.one("SELECT unit_price FROM claim_line WHERE id = ?",
                 (package["id"],))["unit_price"] == 27000)
    db.insert("claim_plan_benefit", {
        "plan_id": quoted, "seq": 99, "kind": "Procedure", "code": "BM000",
        "display": "Unpriced package", "category_code": "BM",
        "category_display": "Burns", "rate": 0, "currency": "INR"})
    _claims.add_line(claim_id, "Procedure", "BM000")
    free = next(r for r in _claims.lines(claim_id) if r["code"] == "BM000")
    check("a package the plan prices at zero is open to the hospital's price",
          _claims.price_is_open(claim_id, free) and free["unit_price"] == 0)
    _claims.save_quantities(claim_id, {free["id"]: "2"}, {free["id"]: "1250.50"})
    free = db.one("SELECT * FROM claim_line WHERE id = ?", (free["id"],))
    check("the hospital's price is kept and multiplied by the quantity",
          free["unit_price"] == 1250.5 and free["amount"] == 2501)
    try:
        _claims.save_quantities(claim_id, {}, {free["id"]: "-1"})
        check("a negative price is refused", False)
    except ValueError:
        check("a negative price is refused", True)
    _claims.remove_line(claim_id, free["id"])
    check("the unpriced line is gone again", _claims.lines_total(claim_id) == 34200)

    # --- the forms those lines drag in
    needed = _claims.required_forms(claim_id)
    check("the chosen lines pull in the forms the payer wants answered",
          [f["form_id"] for f in needed] == ["105315"])
    _claims.save_answers(claim_id, stg_url, {"112408": "Yes", "112409": ""})
    check("an answer is stored and a blank one is not",
          _claims.answers(claim_id) == {f"{stg_url}|112408": "Yes"})
    _claims.save_answers(claim_id, stg_url, {"112408": ""})
    check("clearing an answer removes it", _claims.answers(claim_id) == {})
    _claims.save_answers(claim_id, stg_url, {"112408": "Yes"})

    # --- a question is answered in the shape its declared type asks for
    typed_url = "https://payer.gov.in/policy/questionnaire/100005"
    db.insert("claim_plan_form", {
        "plan_id": quoted, "url": typed_url, "form_id": "100005",
        "kind": "questionnaire", "title": "Discharge Information",
        "items": json.dumps([
            {"linkId": "d1", "type": "dateTime", "required": True,
             "text": "Surgery date", "options": [], "depth": 0},
            {"linkId": "d2", "type": "string", "required": False,
             "text": "Temperature", "options": [], "depth": 0},
            {"linkId": "d3", "type": "attachment", "required": True,
             "text": "Post treatment photo", "options": [], "depth": 0},
            {"linkId": "d4", "type": "choice", "required": True,
             "text": "Discharge stage", "options": ["Before", "After"],
             "initial": "After", "depth": 0},
        ])})
    # The line has to actually need the form, or it is not one to answer.
    bm001 = db.one("SELECT * FROM claim_plan_benefit WHERE plan_id = ? "
                   "AND code = 'BM001'", (quoted,))
    original_support = bm001["supporting_info"]
    db.update("claim_plan_benefit", bm001["id"], {
        "supporting_info": json.dumps(
            json.loads(original_support) + [
                {"code": "ODN", "display": "Discharge Information",
                 "category": "INF", "form": typed_url}])})
    typed_form = _claims.plan_forms(quoted, [typed_url])[0]
    typed_questions = _claims.form_questions(typed_form)
    check("a pre-selected option is kept as the question's default",
          typed_questions[3]["initial"] == "After")
    photo_id = _claims.save_answer_file(
        claim_id, typed_url, "d3", "Post treatment photo", "photo.png",
        "image/png", b"\x89PNG\r\n\x1a\nDATA")
    check("a file answer attaches to the claim and records which document",
          _claims.answers(claim_id, typed_url)[f"{typed_url}|d3"]
          == str(photo_id)
          and _claims.answer_document(str(photo_id))["filename"] == "photo.png")
    _claims.save_answers(claim_id, typed_url, {
        "d1": "2026-08-20T09:30", "d2": "38.4", "d4": "After"})

    typed_out = next(r for r in _claims.build_questionnaire_responses(claim_id)
                     if r["resource"]["questionnaire"] == typed_url)
    by_link = {i["linkId"]: i["answer"][0] for i in typed_out["resource"]["item"]}
    check("a dateTime answer goes as valueDateTime with the IST offset",
          by_link["d1"] == {"valueDateTime": "2026-08-20T09:30:00+05:30"})
    check("a string answer stays a valueString",
          by_link["d2"] == {"valueString": "38.4"}
          and by_link["d4"] == {"valueString": "After"})
    check("a file answer goes as a valueAttachment carrying the bytes",
          by_link["d3"]["valueAttachment"]["contentType"] == "image/png"
          and by_link["d3"]["valueAttachment"]["title"]
          == "Post treatment photo"
          and base64.b64decode(by_link["d3"]["valueAttachment"]["data"])
          == b"\x89PNG\r\n\x1a\nDATA")
    check("a numeric type is coerced, and a broken one falls back to text",
          _claims._answer_value({"type": "integer"}, "4")
          == {"valueInteger": 4}
          and _claims._answer_value({"type": "decimal"}, "1.5")
          == {"valueDecimal": 1.5}
          and _claims._answer_value({"type": "integer"}, "n/a")
          == {"valueString": "n/a"}
          and _claims._answer_value({"type": "boolean"}, "Yes")
          == {"valueBoolean": True})
    _claims.delete_document(photo_id)
    db.execute("DELETE FROM claim_form_answer WHERE claim_id = ? "
               "AND form_url = ?", (claim_id, typed_url))
    db.execute("DELETE FROM claim_plan_form WHERE url = ?", (typed_url,))
    db.update("claim_plan_benefit", bm001["id"],
              {"supporting_info": original_support})

    # --- validating the procedure set before sending it
    auth_sent = {}

    def _auth_ack(path, payload=None, **kw):
        auth_sent["path"], auth_sent["body"] = path, payload
        return {"txn_id": "01AUTH", "correlation_id": "corr-auth-1"}

    _claims._api = _auth_ack
    try:
        _claims.request_auth(claim_id)
    finally:
        _claims._api = real_api
    auth_bundle = auth_sent["body"]["fhir"]
    cer = next(e["resource"] for e in auth_bundle["entry"]
               if e["resource"]["resourceType"] == "CoverageEligibilityRequest")
    check("the procedure set goes as an auth-requirements coverage check",
          auth_sent["path"] == "/fhir/out/v1/coverageeligibility/check"
          and cer["purpose"] == ["auth-requirements"]
          and [i["productOrService"]["coding"][0]["code"] for i in cer["item"]]
          == ["IMP0026", "BM001", "STRAT006a"])
    check("it carries the same item shape the Claim will",
          cer["item"][1]["net"]["value"] == 27000
          and cer["item"][1]["quantity"]["value"] == 1
          and cer["item"][1]["programCode"][0]["coding"][0]["code"]
          == "AB-PMJAY")
    check("the claim now waits on the payer's ruling",
          _claims.auth(claim_id)["status"] == "checking"
          and _claims.auth(claim_id)["txn_id"] == "01AUTH")

    # The payer's ruling, shaped as the live sample sends it: a verdict per
    # line, and the supporting requirements with their stage buried in text.
    auth_response = {"resourceType": "Bundle", "type": "collection", "entry": [
        {"resource": {
            "resourceType": "CoverageEligibilityResponse",
            "outcome": "complete", "purpose": ["auth-requirements"],
            "disposition": "Policy is currently in-force",
            "insurance": [{"inforce": True, "item": [
                {"authorizationRequired": True, "excluded": False,
                 "category": {"coding": [{"code": "GM"}]},
                 "productOrService": {"coding": [
                     {"code": "BM001", "display": "Dengue fever"}]},
                 "benefit": [{"type": {"coding": [{"code": "Procedure"}]},
                              "allowedMoney": {"currency": "INR",
                                               "value": 27000}}],
                 "authorizationSupporting": [
                     {"coding": [{"code": "MAND0671",
                                  "display": "Clinical notes"}],
                      "text": "Type: pre\n Procedure Code:BM001"},
                     {"coding": [{"code": "MAND9",
                                  "display": "Discharge summary"}],
                      "text": "Type: post\n Procedure Code:BM001"},
                     {"coding": [{"code": "105315",
                                  "display": "STG Questionnaire"}],
                      "text": f"fullUrl: {stg_url}"},
                 ]},
                {"authorizationRequired": True, "excluded": True,
                 "productOrService": {"coding": [
                     {"code": "IMP0026", "display": "Fibrin Glue"}]},
                 "benefit": [{"type": {"coding": [{"code": "Implant"}]},
                              "allowedMoney": {"value": 0}}]},
            ]}]}},
    ]}
    check("the ruling lands on the coverage callback, matched by correlation",
          _claims.receive({"jwe_headers": {"x-hcx-correlation_id": "corr-auth-1"},
                           "fhir": auth_response},
                          "coverage", "on_request", "fhir") == "settled")
    ruling = _claims.auth(claim_id)
    check("the ruling settles with the payer's disposition",
          ruling["status"] == "ready" and ruling["inforce"] == 1
          and ruling["disposition"] == "Policy is currently in-force")
    ruled = _claims.auth_items(ruling["id"])
    check("each quoted line gets its own verdict",
          [(r["code"], r["auth_required"], r["excluded"], r["benefit_type"],
            r["allowed_amount"]) for r in ruled]
          == [("BM001", 1, 0, "Procedure", 27000),
              ("IMP0026", 1, 1, "Implant", 0)])
    now = _claims.auth_requirements(ruling["id"], at_preauth=True)
    later = _claims.auth_requirements(ruling["id"], at_preauth=False)
    check("what the payer wants now is split from what it wants at claim",
          sorted((n["kind"], n["code"]) for n in now)
          == [("document", "MAND0671"), ("form", "105315")]
          and [(n["kind"], n["code"], n["stage"]) for n in later]
          == [("document", "MAND9", "post")])
    check("the documents to attach now are the payer's, not a guess",
          [d["code"] for d in _claims.required_documents(claim_id)]
          == ["MAND0671"])
    check("the ruling narrows the forms to answer to the ones it named",
          [f["form_id"] for f in _claims.required_forms(claim_id)] == ["105315"])
    # --- the ruling is about the set as it was
    check("the ruling matches the set it was asked about",
          not _claims.ruling_is_stale(claim_id))
    _ruled_plan = _claims.plan(claim_id)["id"]
    db.insert("claim_plan_benefit", {
        "plan_id": _ruled_plan, "seq": 97, "kind": "Procedure", "code": "BM003",
        "display": "Something else", "category_code": "GM",
        "category_display": "General Medicine", "rate": 100, "currency": "INR"})
    _claims.add_line(claim_id, "Procedure", "BM003")
    check("a line added since makes the ruling stale",
          _claims.ruling_is_stale(claim_id))
    _claims.remove_line(claim_id, next(r["id"] for r in _claims.lines(claim_id)
                                       if r["code"] == "BM003"))
    db.execute("DELETE FROM claim_plan_benefit WHERE plan_id = ? AND code = 'BM003'",
               (_ruled_plan,))
    check("and taking it off again makes it current",
          not _claims.ruling_is_stale(claim_id))
    # --- the documents the ruling named are attached against its own codes
    try:
        _claims.attach_required_document(claim_id, "MAND9", "late.pdf",
                                         "application/pdf", b"%PDF late")
        check("a document the preauth was not asked for is refused", False)
    except ValueError:
        check("a document the preauth was not asked for is refused", True)
    notes_id = _claims.attach_required_document(
        claim_id, "MAND0671", "notes.pdf", "application/pdf", b"%PDF notes")
    filed = _claims.document_for(claim_id, "MAND0671")
    check("a required document files under the payer's own code",
          filed["id"] == notes_id and filed["code"] == "MAND0671"
          and filed["label"] == "Clinical notes")
    replaced = _claims.attach_required_document(
        claim_id, "MAND0671", "notes-v2.pdf", "application/pdf", b"%PDF v2")
    check("choosing another file replaces it rather than sending both",
          _claims.document_for(claim_id, "MAND0671")["id"] == replaced
          and [d["id"] for d in _claims.documents(claim_id)
               if d["code"] == "MAND0671"] == [replaced])
    wire = {d["code"]: d for d in _claims.preauth_documents(claim_id)}
    check("the preauth quotes that code back, not a generic one",
          "MAND0671" in wire and wire["MAND0671"]["label"] == "Clinical notes"
          and wire["MAND0671"]["category"] == "INV")
    plain = _claims.add_document(claim_id, "other.pdf", "application/pdf",
                                 b"%PDF other", "Something else")
    check("a file nobody asked for by name still files as ODN",
          "ODN" in {d["code"] for d in _claims.preauth_documents(claim_id)})
    _claims.delete_document(replaced)
    _claims.delete_document(plain)

    check("a redelivered ruling does not reopen it",
          _claims.receive({"jwe_headers": {"x-hcx-correlation_id": "corr-auth-1"},
                           "fhir": auth_response},
                          "coverage", "on_request", "fhir") == "ignored")
    check("a coverage reply for nobody is still unmatched",
          _claims.receive({"jwe_headers": {"x-hcx-correlation_id": "corr-nope"},
                           "fhir": auth_response},
                          "coverage", "on_request", "fhir") == "unmatched")

    bare_for_auth = _claims.create_claim(selected, "MemberId", "MD5SLS4X5")
    try:
        _claims.build_auth_bundle(bare_for_auth)
        check("validating a set with nothing quoted is refused", False)
    except ValueError as error:
        check("validating a set with nothing quoted is refused",
              "line items" in str(error))
    # A payer nobody has characterised gets the generic adapter, which does
    # not claim to answer a PMJAY-shaped check.
    db.update("claim", bare_for_auth, {"payer_id": "9999@hcx"})
    try:
        _claims.build_auth_bundle(bare_for_auth)
        check("a payer whose adapter does not answer the check is refused",
              False)
    except ValueError as error:
        check("a payer whose adapter does not answer the check is refused",
              "does not answer" in str(error))

    # --- the bundle that goes on the wire
    doc_for_wire = _claims.add_document(claim_id, "scan.pdf",
                                        "application/pdf", b"%PDF-1.4 wire",
                                        "Investigation")
    preauth_bundle = _claims.build_preauth_bundle(claim_id)
    resources = {}
    for entry in preauth_bundle["entry"]:
        resources.setdefault(entry["resource"]["resourceType"], []).append(entry)
    claim_res = resources["Claim"][0]["resource"]
    check("the preauth is a collection Bundle carrying one Claim",
          preauth_bundle["type"] == "collection"
          and preauth_bundle["identifier"]["value"] == claim_row["claim_no"]
          and claim_res["use"] == "preauthorization"
          and claim_res["status"] == "active")
    check("every quoted line becomes a Claim item priced from the plan",
          [(i["productOrService"]["coding"][0]["code"], i["quantity"]["value"],
            i["net"]["value"]) for i in claim_res["item"]]
          == [("IMP0026", 1, 1800), ("BM001", 1, 27000),
              ("STRAT006a", 3, 5400)]
          and claim_res["total"]["value"] == 34200)
    check("only the procedures become Procedure resources",
          [r["resource"]["code"]["coding"][0]["code"]
           for r in resources["Procedure"]] == ["BM001"]
          and claim_res["procedure"][0]["procedureReference"]["reference"]
          == resources["Procedure"][0]["fullUrl"])
    check("the item links back to the care team, diagnosis and procedure",
          claim_res["item"][0]["careTeamSequence"] == [1]
          and claim_res["item"][0]["diagnosisSequence"] == [1]
          and claim_res["item"][0]["procedureSequence"] == [1])
    check("the parties are referenced by their own anchors",
          claim_res["patient"]["reference"] == resources["Patient"][0]["fullUrl"]
          and claim_res["insurer"]["reference"]
          == resources["Organization"][1]["fullUrl"]
          and claim_res["provider"]["reference"]
          == resources["Organization"][0]["fullUrl"]
          and claim_res["insurance"][0]["coverage"]["reference"]
          == resources["Coverage"][0]["fullUrl"])
    check("the care team is one Practitioner per member",
          len(resources["Practitioner"]) == len(claim_res["careTeam"]) == 1
          and claim_res["careTeam"][0]["provider"]["reference"]
          == resources["Practitioner"][0]["fullUrl"])
    supporting = claim_res["supportingInfo"]
    kinds = [next(k for k in ("valueAttachment", "valueString",
                              "valueReference") if k in si) for si in supporting]
    check("supporting info carries the documents, the dates and the answers",
          kinds == ["valueAttachment", "valueString", "valueString",
                    "valueReference"]
          and supporting[0]["valueAttachment"]["contentType"] == "application/pdf"
          and supporting[3]["valueReference"]["reference"]
          == resources["QuestionnaireResponse"][0]["fullUrl"])
    answered = resources["QuestionnaireResponse"][0]["resource"]
    check("only the answered questions ride in the QuestionnaireResponse",
          answered["questionnaire"] == stg_url
          and answered["status"] == "completed"
          and [(i["linkId"], i["answer"][0]["valueString"])
               for i in answered["item"]] == [("112408", "Yes")])
    check("the sequence numbering is 1-based and contiguous",
          [i["sequence"] for i in claim_res["item"]] == [1, 2, 3]
          and [si["sequence"] for si in supporting] == [1, 2, 3, 4])

    # --- a quote first: the same bundle, use=predetermination, answered at
    # once and binding nobody
    asked_payload = {}

    def _quote_ack(path, payload=None, **kw):
        asked_payload["path"], asked_payload["body"] = path, payload
        return {"txn_id": "01QUOTE", "correlation_id": "corr-quote-1"}

    _claims._api = _quote_ack
    try:
        quote_id = _claims.ask_predetermination(claim_id)
    finally:
        _claims._api = real_api
    quote_claim = next(e["resource"] for e in asked_payload["body"]["fhir"]["entry"]
                       if e["resource"]["resourceType"] == "Claim")
    quote_row = _claims.predetermination(quote_id)
    check("a predetermination rides the preauth route with use changed",
          asked_payload["path"] == "/fhir/out/v1/preauth/submit"
          and quote_claim["use"] == "predetermination"
          and quote_row["status"] == "asking"
          and quote_row["requested_amount"] == _claims.lines_total(claim_id))
    check("the pre-authorisation itself is untouched by the quote",
          _claims.preauth(claim_id) is None
          and json.loads(quote_row["request_json"])["entry"][0]["resource"]["use"]
          == "predetermination")
    try:
        _claims.ask_predetermination(claim_id)
        check("a second quote waits for the first to be answered", False)
    except ValueError:
        check("a second quote waits for the first to be answered", True)

    quote_reply = {"resourceType": "Bundle", "entry": [{"resource": {
        "resourceType": "ClaimResponse", "use": "predetermination",
        "outcome": "complete", "disposition": "Quoted at the package rate",
        "adjudication": [{"category": {"coding": [{"code": "status"}]},
                          "reason": {"coding": [{"code": "approved"}]}}],
        "total": [{"category": {"coding": [{"code": "benefit"}]},
                   "amount": {"value": 34200}}]}}]}
    ledger = {"01QUOTE": [{"id": "01QUOTE", "direction": "out"},
                          {"id": "01QUOTE-IN", "direction": "in"}]}

    def _quote_poll(path, payload=None, **kw):
        if path == "/internal/txn/related":
            return ledger[payload["txnId"]]
        if path == "/internal/txn/fhir":
            return {"fhir": quote_reply, "jwe_headers": {}}
        raise AssertionError(path)

    _claims._api = _quote_poll
    try:
        polled = _claims.poll_predetermination(claim_id)
    finally:
        _claims._api = real_api
    quote_row = _claims.predetermination(quote_id)
    check("the quote is read off the ledger with the verdict parser",
          polled and quote_row["status"] == "answered"
          and quote_row["allowed_amount"] == 34200
          and quote_row["outcome"] == "complete"
          and quote_row["adjudication"] == "approved")
    check("an answered quote is not polled again",
          _claims.poll_predetermination(claim_id) is False)
    check("the push half lands the same way when no pre-auth matches",
          _claims.receive({"fhir": quote_reply, "jwe_headers": {
              "x-hcx-correlation_id": "corr-quote-1"}}, "preauth", "response")
          in ("ignored", "unmatched")
          and _claims.predetermination(quote_id)["status"] == "answered")

    # --- send it
    sent_payload = {}

    def _submit_ack(path, payload=None, **kw):
        sent_payload["path"], sent_payload["body"] = path, payload
        return {"txn_id": "01PREAUTH", "correlation_id": "corr-preauth-1"}

    _claims._api = _submit_ack
    try:
        _claims.submit_preauth(claim_id)
    finally:
        _claims._api = real_api
    submission = _claims.preauth(claim_id)
    # The preauth's x-hcx-workflow_id is pinned to a fixed value on purpose
    # (see the literal in claims.submit_preauth), so this checks the envelope
    # it can speak for: the route, the parties and the amount.
    headers = sent_payload["body"]["jwe_headers"]
    check("the preauth goes to the preauth route and waits",
          sent_payload["path"] == "/fhir/out/v1/preauth/submit"
          and headers["x-hcx-recipient_code"] == "1518"
          and headers["x-hcx-sender_code"] == "1000003463@hcx"
          and submission["status"] == "submitting"
          and submission["requested_amount"] == 34200)

    # --- the payer's answer
    claim_response = {"resourceType": "Bundle", "type": "collection", "entry": [
        {"resource": {
            "resourceType": "ClaimResponse", "status": "active",
            "use": "preauthorization", "outcome": "complete",
            "disposition": "Pre-authorisation approved",
            "preAuthRef": "PA-2026-0001",
            "total": [{"category": {"coding": [{"code": "benefit"}]},
                       "amount": {"currency": "INR", "value": 30600}}]}}]}
    check("the payer's approval settles the preauth",
          _claims.receive({"jwe_headers": {"x-hcx-correlation_id":
                                           "corr-preauth-1"},
                           "fhir": claim_response},
                          "preauth", "request", "fhir") == "settled")
    settled = _claims.preauth(claim_id)
    check("the verdict is flattened onto the preauth row",
          settled["status"] == "approved"
          and settled["preauth_ref"] == "PA-2026-0001"
          and settled["approved_amount"] == 30600
          and settled["disposition"] == "Pre-authorisation approved")

    # --- coming back for more: a line added after the approval is an enhancement
    check("nothing added, nothing to enhance",
          not _claims.enhancement_pending(claim_id))
    snapshot = dict(settled)
    plan_id = _claims.plan(claim_id)["id"]
    db.insert("claim_plan_benefit", {
        "plan_id": plan_id, "seq": 98, "kind": "Procedure", "code": "BM002",
        "display": "Dengue with warning signs", "category_code": "GM",
        "category_display": "General Medicine", "rate": 5000, "currency": "INR"})
    _claims.add_line(claim_id, "Procedure", "BM002")
    check("a line added after the decision is an enhancement waiting to go",
          [r["code"] for r in _claims.enhancement_lines(claim_id)] == ["BM002"]
          and _claims.enhancement_pending(claim_id))
    sent_out = []
    previous_api = _claims._api

    def _ack_more(path, payload=None, **kwargs):
        sent_out.append((path, payload))
        return {"txn_id": "01ENH1", "correlation_id": "corr-enh-1"}

    _claims._api = _ack_more
    try:
        _claims.submit_preauth(claim_id)
    finally:
        _claims._api = previous_api
    enhanced = _claims.preauth(claim_id)
    sent_claim = next(e["resource"] for e in sent_out[0][1]["fhir"]["entry"]
                      if e["resource"]["resourceType"] == "Claim")
    check("the enhancement carries only the new line, against the prior reference",
          sent_out[0][0] == "/fhir/out/v1/preauth/submit"
          and [i["productOrService"]["coding"][0]["code"]
               for i in sent_claim["item"]] == ["BM002"]
          and sent_claim["total"]["value"] == 5000
          and sent_claim["related"][0]["reference"]["value"] == "PA-2026-0001"
          and sent_claim["related"][0]["relationship"]["coding"][0]["code"] == "prior")
    check("the row is a second round on the same pre-authorisation",
          enhanced["status"] == "submitting" and enhanced["enhancement_no"] == 1
          and enhanced["preauth_ref"] == "PA-2026-0001"
          and enhanced["requested_amount"] == 5000
          and enhanced["correlation_id"] == "corr-enh-1")
    check("what went before counts as sent, so nothing is pending now",
          set(_claims.preauth_sent_lines(enhanced)) == {"BM001", "IMP0026", "STRAT006a", "BM002"})
    # back to the approved state the checks below build on
    _claims.remove_line(claim_id, next(r["id"] for r in _claims.lines(claim_id)
                                       if r["code"] == "BM002"))
    db.execute("DELETE FROM claim_plan_benefit WHERE plan_id = ? AND code = 'BM002'",
               (plan_id,))
    db.update("claim_preauth", enhanced["id"], {
        k: snapshot[k] for k in ("status", "correlation_id", "txn_id",
                                 "requested_amount", "request_json",
                                 "settled_at", "preauth_ref", "approved_amount",
                                 "disposition", "outcome", "adjudication",
                                 "response_json", "api_call_id")} | {"enhancement_no": 0})

    # --- the small exchanges: where does it stand, what would you pay, look again
    asked_out = []

    def _ack_ask(path, payload=None, **kwargs):
        asked_out.append((path, payload))
        return {"txn_id": f"01ASK{len(asked_out)}",
                "correlation_id": f"corr-ask-{len(asked_out)}"}

    _claims._api = _ack_ask
    try:
        st_id = _claims.ask_status(claim_id, "preauth")
    finally:
        _claims._api = previous_api
    st_task = next(e["resource"] for e in asked_out[-1][1]["fhir"]["entry"]
                   if e["resource"]["resourceType"] == "Task")
    check("a status enquiry is a Task coded status naming the claim, on the task route",
          asked_out[-1][0] == _claims.STATUS_PATH == "/fhir/out/v1/task/submit"
          and st_task["code"]["coding"][0]["code"] == "status"
          and st_task["input"][0]["valueString"] == settled["claim_ref"]
          and asked_out[-1][1]["jwe_headers"]["x-hcx-workflow_id"]
          == settled["correlation_id"]
          and _claims.enquiry(st_id)["status"] == "asking")
    st_reply = {"resourceType": "Bundle", "type": "collection", "entry": [
        {"resource": {"resourceType": "Task", "status": "completed",
                      "description": "Where the case stands",
                      "output": [{"type": {"coding": [{"code": "claimStatus"}]},
                                  "valueString": "claim-approved"}]}}]}
    check("the payer's on_status reply settles the enquiry from the protocol header",
          _claims.receive({"jwe_headers": {"x-hcx-correlation_id": "corr-ask-1",
                                           "x-hcx-status_response": {
                                               "entity_status": "preauth-approved",
                                               "stage": "claim", "outcome": "approved"}},
                           "fhir": st_reply}, "status", "request", "fhir") == "settled"
          and _claims.enquiry(st_id)["answer"] == "preauth-approved"
          and "stage: claim" in _claims.enquiry(st_id)["detail"])
    check("a header-less reply falls back to the Task's claimStatus",
          _claims._status_answer({}, st_reply)["answer"] == "claim-approved")
    check("a redelivered answer is ignored",
          _claims.receive({"jwe_headers": {"x-hcx-correlation_id": "corr-ask-1"},
                           "fhir": st_reply}, "status", "request", "fhir") == "ignored")

    try:
        _claims.ask_reprocess(claim_id, "Please look again")
        check("a claim nobody has decided cannot be sent back", False)
    except ValueError:
        check("a claim nobody has decided cannot be sent back", True)
    had_submission = _claims.submission(claim_id) is not None
    state = _claims._submission_row(claim_id)
    db.update("claim_submission", state["id"], {
        "status": "rejected", "claim_ref": _claims.claim(claim_id)["claim_no"],
        "correlation_id": "corr-claim-x"})
    try:
        _claims.ask_reprocess(claim_id, "  ")
        check("a reprocess request has to say why", False)
    except ValueError:
        check("a reprocess request has to say why", True)
    _claims._api = _ack_ask
    try:
        rp_id = _claims.ask_reprocess(claim_id, "The admission was an emergency.")
    finally:
        _claims._api = previous_api
    rp_task = next(e["resource"] for e in asked_out[-1][1]["fhir"]["entry"]
                   if e["resource"]["resourceType"] == "Task")
    check("an appeal is a Task coded reprocess with the reason, on the claim's thread",
          asked_out[-1][0] == "/fhir/out/v1/task/submit"
          and rp_task["code"]["coding"][0]["code"] == "reprocess"
          and rp_task["description"] == "The admission was an emergency."
          and asked_out[-1][1]["jwe_headers"]["x-hcx-workflow_id"] == "corr-claim-x")
    rp_reply = {"resourceType": "Bundle", "type": "collection", "entry": [
        {"resource": {"resourceType": "Task", "status": "completed",
                      "description": "Claim reopened for reprocessing (round 1)"}}]}
    check("the payer's acceptance reopens the claim: the verdict is awaited again",
          _claims.receive({"jwe_headers": {"x-hcx-correlation_id": "corr-ask-2"},
                           "fhir": rp_reply}, "task", "request", "fhir") == "settled"
          and _claims.enquiry(rp_id)["answer"] == "reopened"
          and _claims.submission(claim_id)["status"] == "submitting")
    check("the asks are listed newest first, by kind",
          [e["kind"] for e in _claims.enquiries(claim_id)] == ["reprocess", "status"]
          and [e["id"] for e in _claims.enquiries(claim_id, "status", "preauth")] == [st_id])
    # The screens those asks live on, with every kind on record.
    from emr.web.router import App as _EApp
    from emr.web.routes import register_all as _e_register
    from http.cookies import SimpleCookie as _ECookie
    _eapp = _EApp()
    _e_register(_eapp)
    db.update("claim_submission", state["id"], {"status": "rejected"})
    e_pages = {tab: _eapp.dispatch("GET", f"/claims/{claim_id}", {"tab": tab},
                                   {}, _ECookie(), b"")
               for tab in ("lines", "preauth", "claim", "validate")}
    e_bodies = {tab: r.body.decode() for tab, r in e_pages.items()}
    e_state = _eapp.dispatch("GET", f"/claims/{claim_id}/state", {}, {},
                             _ECookie(), b"")
    e_json = json.loads(e_state.body.decode())
    check("the claim's state is served as JSON with every leg on it",
          e_state.status == 200
          and e_json["claim"]["claim_no"] == _claims.claim(claim_id)["claim_no"]
          and e_json["preauth"]["status"] == "approved"
          and [e["kind"] for e in e_json["enquiries"]] == ["reprocess", "status"]
          and e_json["submission"]["status"] == "rejected"
          and isinstance(e_json["lines"], list) and e_json["ruling"] is not None)
    check("the pre-auth and claim tabs render the asks and their answers",
          all(r.status == 200 for r in e_pages.values())
          and "Ask where it stands" in e_bodies["preauth"]
          and "preauth-approved" in e_bodies["preauth"]
          and "reprocess" in e_bodies["claim"].lower()
          and "reopened" in e_bodies["claim"],
          str({tab: r.status for tab, r in e_pages.items()}))
    db.execute("DELETE FROM claim_enquiry WHERE claim_id = ?", (claim_id,))
    if not had_submission:
        db.execute("DELETE FROM claim_submission WHERE id = ?", (state["id"],))
    else:
        db.update("claim_submission", state["id"], {"status": "draft",
                                                    "correlation_id": None})
    check("redelivering the approval does not reopen it",
          _claims.receive({"jwe_headers": {"x-hcx-correlation_id":
                                           "corr-preauth-1"},
                           "fhir": claim_response},
                          "preauth", "request", "fhir") == "ignored")
    try:
        _claims.parse_claim_response({"resourceType": "Bundle", "entry": []})
        check("a reply without a ClaimResponse is refused", False)
    except ValueError:
        check("a reply without a ClaimResponse is refused", True)

    # --- the payer answers more than once, and `outcome` alone lies
    def _response(outcome, reason, benefit=None, note=None):
        verdict = {"resourceType": "ClaimResponse", "outcome": outcome,
                   "disposition": "…",
                   "adjudication": [{
                       "category": {"coding": [{"code": "status"}]},
                       "reason": {"coding": [{"code": reason}]}}]}
        if benefit is not None:
            verdict["total"] = [
                {"category": {"coding": [{"code": "submitted"}]},
                 "amount": {"value": 8175}},
                {"category": {"coding": [{"code": "benefit"}]},
                 "amount": {"value": benefit}}]
        if note:
            verdict["item"] = [{"adjudication": [
                {"category": {"coding": [{"code": "reason"}]},
                 "reason": {"coding": [{"display": note}]}}]}]
        return {"resourceType": "Bundle", "entry": [{"resource": verdict}]}

    check("the four documented verdicts are read from outcome AND reason",
          [_claims.verdict_status(_claims.parse_claim_response(
              _response(o, r))) for o, r in [
                  ("complete", "approved"), ("partial", "approved"),
                  ("partial", "queried"), ("complete", "cancelled")]]
          == ["approved", "partial", "queried", "rejected"])
    check("a rejection is not read as an approval just because it completed",
          _claims.verdict_status(_claims.parse_claim_response(
              _response("complete", "cancelled"))) == "rejected")
    check("an acknowledgement is not a decision",
          _claims.verdict_status(_claims.parse_claim_response(
              _response("queued", "submitted"))) == "submitting")
    check("totals are matched by category, never by position",
          _claims.parse_claim_response(
              _response("complete", "approved", benefit=5175)
          )["approved_amount"] == 5175)
    check("the payer's query trail is kept verbatim, colon prefix trimmed",
          _claims.parse_claim_response(
              _response("partial", "queried", note=" : USER1~02/26/2026~"
                                                    "other~need docs~PPD")
          )["query_note"] == "USER1~02/26/2026~other~need docs~PPD")

    # A preauth is answered several times on one correlation id: refusing
    # anything after the first threw the actual approval away.
    stages = _claims.create_claim(selected, "MemberId", "MD5SLS4X5")
    stage_id = db.insert("claim_preauth", {
        "claim_id": stages, "status": "submitting", "txn_id": "01SEQ",
        "correlation_id": "corr-seq", "submitted_at": db.now_iso()})

    def _deliver_reply(outcome, reason, api_call, benefit=None):
        return _claims.receive(
            {"jwe_headers": {"x-hcx-correlation_id": "corr-seq",
                             "x-hcx-api_call_id": api_call},
             "fhir": _response(outcome, reason, benefit)},
            "preauth", "request", "fhir")

    check("an acknowledgement leaves the preauth waiting, not settled",
          _deliver_reply("queued", "submitted", "call-1") == "settled"
          and db.one("SELECT * FROM claim_preauth WHERE id = ?",
                     (stage_id,))["status"] == "submitting")
    check("a query on the same correlation is taken in, not refused",
          _deliver_reply("partial", "queried", "call-2") == "settled"
          and db.one("SELECT * FROM claim_preauth WHERE id = ?",
                     (stage_id,))["status"] == "queried")
    check("and the approval that follows it settles the preauth",
          _deliver_reply("complete", "approved", "call-3", 8175) == "settled"
          and db.one("SELECT * FROM claim_preauth WHERE id = ?",
                     (stage_id,))["status"] == "approved"
          and db.one("SELECT * FROM claim_preauth WHERE id = ?",
                     (stage_id,))["approved_amount"] == 8175)
    check("a redelivery of one of them is told apart by its api_call_id",
          _deliver_reply("complete", "approved", "call-3", 8175) == "ignored")

    # --- withdrawing it again
    try:
        _claims.build_cancel_bundle(claim_id, "nonsense")
        check("a cancellation without a known reason is refused", False)
    except ValueError:
        check("a cancellation without a known reason is refused", True)
    try:
        _claims.build_cancel_bundle(claim_id, "other", "  ")
        check("“Other reason” without a note is refused", False)
    except ValueError as error:
        check("“Other reason” without a note is refused",
              "only thing the payer can read" in str(error))

    cancel_bundle = _claims.build_cancel_bundle(
        claim_id, "treatmentplanchanged")
    cancel_task = next(e["resource"] for e in cancel_bundle["entry"]
                       if e["resource"]["resourceType"] == "Task")
    cancel_inputs = {i["type"]["coding"][0]["code"]: i["valueString"]
                     for i in cancel_task["input"]}
    check("the cancellation is a Task coded cancel, naming the claim",
          cancel_task["status"] == "requested"
          and cancel_task["intent"] == "order"
          and cancel_task["code"]["coding"][0]["code"] == "cancel"
          and cancel_inputs["claimNumber"] == claim_row["claim_no"]
          and cancel_bundle["identifier"]["value"] == claim_row["claim_no"])
    check("both spellings of the intimation number go, as the payer reads one",
          cancel_inputs["initimationNumber"] == claim_row["claim_no"]
          and cancel_inputs["intimationNumber"] == claim_row["claim_no"])
    def _org_anchor(kind):
        for entry in cancel_bundle["entry"]:
            resource = entry["resource"]
            if resource["resourceType"] != "Organization":
                continue
            if resource["type"][0]["coding"][0]["code"] == kind:
                return entry["fullUrl"]
        return None

    check("the reason is coded, and the two parties are the Task's ends",
          cancel_task["reasonCode"]["coding"][0]["code"]
          == "treatmentplanchanged"
          and cancel_task["requester"]["reference"] == _org_anchor("prov")
          and cancel_task["owner"]["reference"] == _org_anchor("pay")
          and _org_anchor("prov") != _org_anchor("pay"))

    cancel_sent = {}

    def _cancel_ack(path, payload=None, **kw):
        cancel_sent["path"], cancel_sent["body"] = path, payload
        return {"txn_id": "01CANCEL", "correlation_id": "corr-cancel-1"}

    _claims._api = _cancel_ack
    try:
        _claims.cancel_preauth(claim_id, "patientrequest", "Left against advice")
    finally:
        _claims._api = real_api
    cancelling = _claims.preauth(claim_id)
    check("the cancellation goes to task/submit under its own workflow id",
          cancel_sent["path"] == "/fhir/out/v1/task/submit"
          and cancel_sent["body"]["jwe_headers"]["x-hcx-workflow_id"]
          == _claims.CANCEL_WORKFLOW_ID == "11")
    check("the claim waits on the cancellation, keeping the submission's ids",
          cancelling["status"] == "cancelling"
          and cancelling["cancel_txn_id"] == "01CANCEL"
          and cancelling["txn_id"] == "01PREAUTH"
          and cancelling["cancel_reason"] == "patientrequest"
          and cancelling["cancel_note"] == "Left against advice")
    try:
        _claims.cancel_preauth(claim_id, "patientrequest")
        check("a cancellation already in flight is not sent twice", False)
    except ValueError:
        check("a cancellation already in flight is not sent twice", True)

    # The payer answers a Task with a Task, the adjudication nested inside it.
    task_reply = {"resourceType": "Bundle", "type": "collection", "entry": [
        {"fullUrl": "urn:uuid:resp-1", "resource": {
            "resourceType": "ClaimResponse", "outcome": "complete",
            "disposition": "Pre-authorisation cancelled",
            "preAuthRef": "PA-2026-0001"}},
        {"fullUrl": "urn:uuid:task-1", "resource": {
            "resourceType": "Task", "status": "completed",
            "output": [{"type": {"coding": [{"code": "ClaimResponse"}]},
                        "valueReference": {"reference": "urn:uuid:resp-1"}}]}},
    ]}
    check("the payer's Task reply settles the cancellation",
          _claims.receive({"jwe_headers": {"x-hcx-correlation_id":
                                           "corr-cancel-1"},
                           "fhir": task_reply}, "task", "request", "fhir")
          == "settled")
    cancelled = _claims.preauth(claim_id)
    check("the ClaimResponse is dug out of Task.output, not the bundle root",
          cancelled["status"] == "cancelled"
          and cancelled["disposition"] == "Pre-authorisation cancelled")
    renumbered = _claims.claim(claim_id)
    check("an accepted cancellation retires the claim number",
          renumbered["claim_no"] != claim_row["claim_no"]
          and renumbered["claim_no"].startswith("CLM-")
          and cancelled["claim_ref"] == claim_row["claim_no"])
    check("the withdrawn number is not handed to anybody else",
          db.scalar("SELECT COUNT(*) FROM claim WHERE claim_no = ?",
                    (claim_row["claim_no"],), 0) == 0)
    claim_row = renumbered
    # A refused cancellation must leave the number alone: the payer still
    # holds the preauth under it.
    refused_claim = _claims.create_claim(selected, "MemberId", "MD5SLS4X5")
    before = _claims.claim(refused_claim)["claim_no"]
    refused_id = db.insert("claim_preauth", {
        "claim_id": refused_claim, "status": "cancelling",
        "claim_ref": before, "cancel_correlation_id": "corr-cancel-2",
        "cancel_requested_at": db.now_iso(), "cancel_reason": "patientrequest"})
    _claims.receive({"jwe_headers": {"x-hcx-correlation_id": "corr-cancel-2"},
                     "fhir": {"resourceType": "Bundle", "entry": [
                         {"resource": {"resourceType": "Task",
                                       "status": "rejected"}}]}},
                    "task", "request", "fhir")
    refused_row = db.one("SELECT * FROM claim_preauth WHERE id = ?",
                         (refused_id,))
    check("a cancellation the payer refuses keeps the number and the preauth",
          refused_row["status"] == "approved"
          and _claims.claim(refused_claim)["claim_no"] == before)

    check("a cancelled preauth cannot be cancelled again",
          "cancelled" in _cancel_refusal(_claims, claim_id))
    check("redelivering the Task reply does not reopen it",
          _claims.receive({"jwe_headers": {"x-hcx-correlation_id":
                                           "corr-cancel-1"},
                           "fhir": task_reply}, "task", "request", "fhir")
          == "ignored")
    try:
        _claims.parse_task_response({"resourceType": "Bundle", "entry": []})
        check("a Task reply without a Task is refused", False)
    except ValueError:
        check("a Task reply without a Task is refused", True)
    _claims.delete_document(doc_for_wire)

    # --- the refusals that fire before any bundle is built
    bare = _claims.create_claim(selected, "MemberId", "MD5SLS4X5")
    db.update("claim", bare, {"status": "eligible"})
    for expected in ("Link the admitted patient",):
        try:
            _claims.build_preauth_bundle(bare)
            check("a preauth without a linked admission is refused", False)
        except ValueError as error:
            check("a preauth without a linked admission is refused",
                  expected in str(error))
    db.update("claim", bare, {"patient_id": beneficiary, "encounter_id": stay})
    try:
        _claims.build_preauth_bundle(bare)
        check("a preauth without an admission date is refused", False)
    except ValueError as error:
        check("a preauth without an admission date is refused",
              "admission date" in str(error))
    db.update("claim", bare, {"admission_date": "2026-08-18"})
    try:
        _claims.build_preauth_bundle(bare)
        check("a preauth without a diagnosis is refused", False)
    except ValueError as error:
        check("a preauth without a diagnosis is refused",
              "diagnosis" in str(error))
    try:
        _claims.add_line(bare, "Procedure", "BM001")
        check("quoting before the plan is fetched is refused", False)
    except ValueError as error:
        check("quoting before the plan is fetched is refused",
              "insurance plan" in str(error))
    db.update("claim", claim_id, {"status": "draft"})
    try:
        _claims.submit_preauth(claim_id)
        check("submitting before the policy is eligible is refused", False)
    except ValueError as error:
        check("submitting before the policy is eligible is refused",
              "eligible" in str(error))
    db.update("claim", claim_id, {"status": "eligible"})

    # A stalled submission whose ledger row is gone must settle, not spin.
    db.update("claim_preauth", settled["id"],
              {"status": "submitting", "txn_id": "01GONE"})
    _claims._api = _gone
    try:
        moved = _claims.poll_preauth(claim_id)
    finally:
        _claims._api = real_api
    check("a forgotten preauth transaction settles as an error",
          moved and _claims.preauth(claim_id)["status"] == "error"
          and "ledger was reset" in
          (_claims.preauth(claim_id)["error_message"] or ""))

    # --- the multipart parser the upload rides on
    from emr.web.router import parse_multipart
    multipart_body = (b"--BOUND\r\n"
                      b'Content-Disposition: form-data; name="label"\r\n\r\n'
                      b"ID card\r\n"
                      b"--BOUND\r\n"
                      b'Content-Disposition: form-data; name="file"; '
                      b'filename="card.png"\r\n'
                      b"Content-Type: image/png\r\n\r\n"
                      b"\x89PNG\r\n\x1a\nBINARY\r\n"
                      b"--BOUND--\r\n")
    mp_form, mp_files = parse_multipart(
        multipart_body, 'multipart/form-data; boundary=BOUND')
    check("multipart text fields parse", mp_form.get("label") == ["ID card"])
    check("multipart file parts keep their bytes exactly",
          mp_files["file"][0]["filename"] == "card.png"
          and mp_files["file"][0]["content_type"] == "image/png"
          and mp_files["file"][0]["data"] == b"\x89PNG\r\n\x1a\nBINARY")
    check("an unused file input posts no file",
          parse_multipart(
              b"--B\r\nContent-Disposition: form-data; name=\"file\"; "
              b"filename=\"\"\r\nContent-Type: application/octet-stream"
              b"\r\n\r\n\r\n--B--\r\n",
              "multipart/form-data; boundary=B")[1] == {})

    section("NHCX claim submission")
    # The claim goes in once the patient has left, against an approved
    # pre-authorisation. `claim_id` already has one from the section above.
    db.update("claim_preauth",
              _claims.preauth(claim_id)["id"], {"status": "approved"})
    try:
        _claims.build_claim_bundle(claim_id)
        check("a claim without a recorded discharge is refused", False)
    except ValueError as error:
        check("a claim without a recorded discharge is refused",
              "discharged" in str(error))
    for values, why in [
            ({"discharge_mode": "", "discharge_stage": "After Surgery",
              "discharge_date": "2026-08-22"}, "how the patient"),
            ({"discharge_mode": "normal", "discharge_stage": "",
              "discharge_date": "2026-08-22"}, "before, during or after"),
            ({"discharge_mode": "normal", "discharge_stage": "After Surgery",
              "discharge_date": ""}, "discharge date"),
            ({"discharge_mode": "normal", "discharge_stage": "After Surgery",
              "discharge_date": "2026-08-01"}, "cannot be before"),
            ({"discharge_mode": "death", "discharge_stage": "After Surgery",
              "discharge_date": "2026-08-22"}, "date and time of death"),
            ({"discharge_mode": "death", "discharge_stage": "After Surgery",
              "discharge_date": "2026-08-22", "death_date": "2026-08-22"},
             "time of death")]:
        try:
            _claims.save_discharge(claim_id, values)
            check(f"a discharge missing {why!r} is refused", False)
        except ValueError as error:
            check(f"a discharge missing {why!r} is refused", why in str(error))

    _claims.save_discharge(claim_id, {
        "discharge_mode": "normal", "discharge_stage": "After Surgery",
        "discharge_date": "2026-08-22", "surgery_date": "2026-08-20"})
    discharged = _claims.submission(claim_id)
    check("a normal discharge is recorded with its stage and dates",
          discharged["discharge_mode"] == "normal"
          and discharged["discharge_stage"] == "After Surgery"
          and discharged["discharge_date"] == "2026-08-22"
          and discharged["surgery_date"] == "2026-08-20"
          and discharged["death_date"] is None)
    check("a normal discharge claims exactly what was quoted",
          [l["code"] for l in _claims.claim_lines(claim_id)]
          == [l["code"] for l in _claims.lines(claim_id)]
          and _claims.claim_total(claim_id) == _claims.lines_total(claim_id)
          and not _claims.lama_dama_only(discharged))

    # LAMA/DAMA before or during surgery collapses the claim to LM100 — the
    # payer disqualifies every approved item and takes only that one.
    db.insert("claim_plan_benefit", {
        "plan_id": quoted, "seq": 9, "kind": "Procedure",
        "code": _claims.LAMA_DAMA_CODE, "display": "LAMA DAMA Procedure",
        "rate": 5000, "currency": "INR", "category_code": "GM",
        "category_display": "General Medicine"})
    _claims.save_discharge(claim_id, {
        "discharge_mode": "lama", "discharge_stage": "During Surgery",
        "discharge_date": "2026-08-22"})
    lama = _claims.submission(claim_id)
    check("LAMA during surgery collapses the claim to the LAMA/DAMA code",
          _claims.lama_dama_only(lama)
          and [(l["code"], l["amount"])
               for l in _claims.claim_lines(claim_id)]
          == [(_claims.LAMA_DAMA_CODE, 5000)]
          and _claims.claim_total(claim_id) == 5000)
    check("the lines the preauth quoted are untouched by that",
          len(_claims.lines(claim_id)) == 3)
    _claims.save_discharge(claim_id, {
        "discharge_mode": "lama", "discharge_stage": "After Surgery",
        "discharge_date": "2026-08-22"})
    check("LAMA after surgery claims the quoted lines as normal",
          not _claims.lama_dama_only(_claims.submission(claim_id))
          and len(_claims.claim_lines(claim_id)) == 3)

    _claims.save_discharge(claim_id, {
        "discharge_mode": "death", "discharge_stage": "After Surgery",
        "discharge_date": "2026-08-22", "death_date": "2026-08-22T14:35"})
    _claims.attach_discharge_summary(claim_id, "summary.pdf",
                                     "application/pdf", b"%PDF discharge")
    claim_bundle = _claims.build_claim_bundle(claim_id)
    claim_res = next(e["resource"] for e in claim_bundle["entry"]
                     if e["resource"]["resourceType"] == "Claim")
    check("the claim is the preauth bundle with use=claim and a billable period",
          claim_bundle["id"] == "CLAIM"
          and claim_res["use"] == "claim"
          and claim_res["billablePeriod"]["start"].startswith("2026-08-18")
          and claim_res["billablePeriod"]["end"].startswith("2026-08-22"))
    by_category = {}
    for si in claim_res["supportingInfo"]:
        by_category.setdefault(si["category"]["coding"][0]["code"], []).append(si)
    check("the discharge summary rides as HDS, coded with the mode",
          by_category["HDS"][0]["code"]["coding"][0]["code"] == "(Death)"
          and by_category["HDS"][0]["valueAttachment"]["contentType"]
          == "application/pdf"
          and base64.b64decode(
              by_category["HDS"][0]["valueAttachment"]["data"])
          == b"%PDF discharge")
    check("the disposition is the DIS category, coded by discharge type",
          by_category["DIS"][0]["code"]["coding"][0]["code"] == "DTM"
          and by_category["DIS"][0]["valueString"] == "After Surgery")
    check("admission, discharge and death dates each get their own category",
          by_category["ADMD"][0]["valueString"].startswith("2026-08-18")
          and by_category["DSCHD"][0]["valueString"].startswith("2026-08-22")
          and by_category["ONS"][0]["code"]["coding"][0]["code"] == "DTM"
          and "SURD" not in by_category)
    check("the preauth's own documents do not ride on the claim",
          all(si["code"]["coding"][0]["code"] != "MAND0671"
              for si in claim_res["supportingInfo"]))

    claim_sent = {}

    def _claim_ack(path, payload=None, **kw):
        claim_sent["path"], claim_sent["body"] = path, payload
        return {"txn_id": "01CLAIM", "correlation_id": "corr-claim-1"}

    _claims._api = _claim_ack
    try:
        _claims.submit_claim(claim_id)
    finally:
        _claims._api = real_api
    check("the claim goes to claim/submit under its own workflow id",
          claim_sent["path"] == "/fhir/out/v1/claim/submit"
          and claim_sent["body"]["jwe_headers"]["x-hcx-workflow_id"]
          == _claims.CLAIM_WORKFLOW_ID == "15"
          and _claims.submission(claim_id)["status"] == "submitting")

    claim_verdict = {"resourceType": "Bundle", "type": "collection", "entry": [
        {"resource": {"resourceType": "ClaimResponse", "outcome": "complete",
                      "disposition": "Claim approved",
                      "total": [{"category": {"coding": [{"code": "benefit"}]},
                                 "amount": {"currency": "INR",
                                            "value": 31000}}]}}]}
    check("the payer's verdict settles the claim",
          _claims.receive({"jwe_headers": {"x-hcx-correlation_id":
                                           "corr-claim-1"},
                           "fhir": claim_verdict}, "claim", "request", "fhir")
          == "settled")
    settled_claim = _claims.submission(claim_id)
    check("the verdict is flattened onto the claim row",
          settled_claim["status"] == "approved"
          and settled_claim["approved_amount"] == 31000
          and settled_claim["disposition"] == "Claim approved")
    check("redelivering the claim verdict does not reopen it",
          _claims.receive({"jwe_headers": {"x-hcx-correlation_id":
                                           "corr-claim-1"},
                           "fhir": claim_verdict}, "claim", "request", "fhir")
          == "ignored")
    try:
        _claims.save_discharge(claim_id, {
            "discharge_mode": "normal", "discharge_stage": "After Surgery",
            "discharge_date": "2026-08-22"})
        check("the discharge is editable again once the payer has answered",
              _claims.submission(claim_id)["discharge_mode"] == "normal")
    except ValueError:
        check("the discharge is editable again once the payer has answered",
              False)

    section("PMJAY adjudicator")
    from emr import adjudicator as _adj

    # The workflow comes from hcxkit so the two cannot drift; stub it, since
    # the point of the checks is what this EMR does with it.
    _real_gateway = _claims.gateway
    adjudicator_calls = []

    def _adj_gateway(path, payload=None, **kw):
        adjudicator_calls.append((path, payload))
        if path.endswith("/adjudicator/workflow"):
            return {"enabled": True, "payerCode": "1518", "steps": [
                {"step": 0, "stage": "PREAUTH", "role": "PPD-Trust",
                 "actions": ["Approve", "Reject", "Query"],
                 "usecase": "PREAUTH"},
                {"step": 2, "stage": "CLAIM", "role": "CPD-Trust",
                 "actions": ["cpdApprove", "cpdReject", "Pending"],
                 "usecase": "CLAIM"}]}
        if path.endswith("/adjudicator/role"):
            return {"role": "PPD-Trust", "status": 200,
                    "step": {"stage": "PREAUTH", "usecase": "PREAUTH"},
                    "response": {"currentuserrole": "PPD-Trust"}}
        return {"success": True, "status": 200, "role": "PPD-Trust",
                "action": payload["action"], "usecase": "PREAUTH",
                "correlationId": payload["correlationId"],
                "response": {"message": "processed"}}

    _claims.gateway = _adj_gateway
    try:
        check("the workflow is read from hcxkit, not a second copy here",
              _adj.actions_for("PPD-Trust") == ["Approve", "Reject", "Query"]
              and _adj.actions_for("ppd-trust")
              == ["Approve", "Reject", "Query"]
              and _adj.actions_for("Nobody") == [])

        listed = _adj.cases()
        by_stage = {(c["claim_id"], c["stage"]): c for c in listed}
        check("every leg this EMR sent is a case, with its correlation id",
              (claim_id, "preauth") in by_stage
              and by_stage[(claim_id, "preauth")]["correlation_id"]
              == "corr-preauth-1"
              and by_stage[(claim_id, "preauth")]["stage_label"]
              == "Pre-authorisation")
        check("a claim leg is listed separately from its pre-authorisation",
              (claim_id, "claim") in by_stage
              and by_stage[(claim_id, "claim")]["correlation_id"]
              == "corr-claim-1")
        check("the case number is the one that leg went out under",
              by_stage[(claim_id, "preauth")]["case_number"]
              == _claims.preauth(claim_id)["claim_ref"])

        adjudicator_calls.clear()
        reply = _adj.process(claim_id, "preauth", "PPD-Trust", "Approve",
                             "looks fine")
        sent = next(p for path, p in adjudicator_calls
                    if path.endswith("/adjudicator/process"))
        check("the decision carries the correlation id off our own record",
              reply["success"]
              and sent["correlationId"] == "corr-preauth-1"
              and sent["caseNumber"]
              == by_stage[(claim_id, "preauth")]["case_number"]
              and sent["remarks"] == "looks fine")
        check("it is recorded here, because NHCX keeps no trace of it",
              [(d["action"], d["role"], bool(d["success"]))
               for d in _adj.decisions(claim_id, "preauth")]
              == [("Approve", "PPD-Trust", True)])

        # The refusals that fire before the payer service is troubled.
        adjudicator_calls.clear()
        for role, action, why in [
                ("PPD-Trust", "cpdApprove", "not"),
                ("Nobody", "Approve", "not a role"),
                ("", "Approve", "Read who is holding")]:
            try:
                _adj.process(claim_id, "preauth", role, action)
                check(f"{role or 'no role'} taking {action!r} is refused",
                      False)
            except ValueError as error:
                check(f"{role or 'no role'} taking {action!r} is refused",
                      why in str(error))
        check("and none of those reached the payer service",
              not any(p.endswith("/adjudicator/process")
                      for p, _ in adjudicator_calls))
        try:
            _adj.process(claim_id, "nonsense", "PPD-Trust", "Approve")
            check("a stage this EMR never sent is refused", False)
        except ValueError as error:
            check("a stage this EMR never sent is refused",
                  "never sent from here" in str(error))
    finally:
        _claims.gateway = _real_gateway

    section("NHCX payments")
    # The one leg the payer starts: it posts a notice when money moves and
    # expects an acknowledgement straight back.
    paid_claim = claim_row["claim_no"]

    def _notice(reference, disposition, status, amount, details=(), utr=None):
        return {"resourceType": "Bundle", "type": "collection",
                "id": reference, "entry": [
                    {"resource": {
                        "resourceType": "Task", "status": "requested",
                        "intent": "order", "description": disposition}},
                    {"resource": {
                        "resourceType": "PaymentNotice", "status": "active",
                        "identifier": [{
                            "type": {"coding": [{"code": "CLN",
                                                 "display": "Claim number"}]},
                            "value": reference}],
                        "created": "2026-08-23T17:27:41+05:30",
                        "amount": {"value": amount, "currency": "INR"},
                        "paymentStatus": {"coding": [{"code": status,
                                                      "display": status}]}}},
                    {"resource": {
                        "resourceType": "PaymentReconciliation",
                        "status": "active", "disposition": disposition,
                        "paymentDate": "2026-08-23",
                        "paymentAmount": {"value": amount, "currency": "INR"},
                        "paymentIdentifier": {
                            "type": {"coding": [{"code": "UTR"}]},
                            "value": utr or "PMJAY/HP/S/2024/R2/0001/Normal"},
                        "detail": [
                            {"id": ref, "type": {"coding": [{"code": kind}]},
                             "date": "2026-08-23", "amount": {"value": value}}
                            for ref, kind, value in details]}},
                ]}

    ack_calls = []

    def _ack_ok(path, payload=None, **kw):
        ack_calls.append((path, payload))
        return {"txn_id": f"01ACK{len(ack_calls)}",
                "correlation_id": f"ack-{len(ack_calls)}"}

    _claims._api = _ack_ok
    try:
        first_notice = _claims.receive(
            {"jwe_headers": {"x-hcx-correlation_id": "pay-1"},
             "fhir": _notice(paid_claim, "Payment initiated", "issued", 2160,
                             [("1652/RF", "RF", 540),
                              ("1652/N", "Payment", 2160)])},
            "paymentnotice", "request", "fhir")
    finally:
        _claims._api = real_api
    check("a payment notice is matched by the CLN inside it, not a correlation",
          first_notice == "settled"
          and len(_claims.payments(claim_id)) == 1)
    money = _claims.payments(claim_id)[0]
    check("the notice is flattened from all three of its resources",
          money["disposition"] == "Payment initiated"
          and money["payment_status"] == "issued"
          and money["amount"] == 2160
          and money["payment_date"] == "2026-08-23"
          and money["utr"] == "PMJAY/HP/S/2024/R2/0001/Normal")
    check("the reconciliation breakdown is kept line by line",
          [(d["reference"], d["type_code"], d["amount"])
           for d in _claims.payment_details(money["id"])]
          == [("1652/RF", "RF", 540), ("1652/N", "Payment", 2160)])
    check("it is acknowledged the moment it lands, without being asked",
          money["ack_status"] == "sent"
          and money["acknowledged_at"]
          and ack_calls[0][0] == "/fhir/out/v1/paymentnotice/on_request")
    ack_task = next(e["resource"] for e in ack_calls[0][1]["fhir"]["entry"]
                    if e["resource"]["resourceType"] == "Task")
    ack_output = {o["type"]["coding"][0]["code"]: o for o in ack_task["output"]}
    check("the acknowledgement is a completed Task saying paymentack",
          ack_task["status"] == "completed"
          and ack_task["code"]["coding"][0]["code"] == "status"
          and ack_output["status"]["valueCodeableConcept"]["coding"][0]["code"]
          == "paymentack"
          and ack_output["claimNumber"]["valueString"] == paid_claim)
    check("only settled money counts towards what has been paid",
          _claims.paid_total(claim_id) == 0)

    # The second notice: the same claim, money actually moved.
    _claims._api = _ack_ok
    try:
        _claims.receive({"jwe_headers": {"x-hcx-correlation_id": "pay-2"},
                         "fhir": _notice(paid_claim, "Payment cleared",
                                         "paid", 2160)},
                        "paymentnotice", "request", "fhir")
    finally:
        _claims._api = real_api
    check("a second notice is another card, not an overwrite",
          [(n["disposition"], n["payment_status"])
           for n in _claims.payments(claim_id)]
          == [("Payment cleared", "paid"), ("Payment initiated", "issued")]
          and _claims.paid_total(claim_id) == 2160)

    # PMJAY stamps its "initiated" notice `paid` too, carrying the full
    # amount — counting both notices would double the money.
    _claims._api = _ack_ok
    try:
        _claims.receive({"jwe_headers": {"x-hcx-correlation_id": "pay-2b"},
                         "fhir": _notice(paid_claim, "Payment settled",
                                         "paid", 2160)},
                        "paymentnotice", "request", "fhir")
    finally:
        _claims._api = real_api
    check("two notices about one payment count once, by UTR",
          len(_claims.payments(claim_id)) == 3
          and _claims.paid_total(claim_id) == 2160)
    _claims._api = _ack_ok
    try:
        _claims.receive({"jwe_headers": {"x-hcx-correlation_id": "pay-2c"},
                         "fhir": _notice(paid_claim, "Second instalment",
                                         "paid", 840, utr="UTR/SECOND")},
                        "paymentnotice", "request", "fhir")
    finally:
        _claims._api = real_api
    check("a genuinely separate payment does add up",
          _claims.paid_total(claim_id) == 3000)
    check("a redelivered notice is held once and says so",
          _claims.receive({"jwe_headers": {"x-hcx-correlation_id": "pay-2"},
                           "fhir": _notice(paid_claim, "Payment cleared",
                                           "paid", 2160)},
                          "paymentnotice", "request", "fhir") == "ignored"
          and len(_claims.payments(claim_id)) == 4)
    check("a notice for a claim number nobody has is unmatched, not an error",
          _claims.receive({"jwe_headers": {"x-hcx-correlation_id": "pay-9"},
                           "fhir": _notice("CLM-NOPE", "Payment initiated",
                                           "issued", 100)},
                          "paymentnotice", "request", "fhir") == "unmatched")

    # A CLN naming a number the claim used to carry still finds it.
    historic = _claims.claim(claim_id)["claim_no"]
    db.update("claim", claim_id, {"claim_no": db.next_number("claim", "CLM-")})
    db.update("claim_submission", _claims.submission(claim_id)["id"],
              {"claim_ref": historic})
    _claims._api = _ack_ok
    try:
        _claims.receive({"jwe_headers": {"x-hcx-correlation_id": "pay-3"},
                         "fhir": _notice(historic, "Payment cleared", "paid",
                                         500)},
                        "paymentnotice", "request", "fhir")
    finally:
        _claims._api = real_api
    check("a notice naming a retired claim number still finds its claim",
          len(_claims.payments(claim_id)) == 5)

    # A failed acknowledgement must not reject the notice — it did arrive.
    def _ack_down(path, payload=None, **kw):
        raise _claims.GatewayError("hcxkit gateway unreachable at test")

    _claims._api = _ack_down
    try:
        outcome = _claims.receive(
            {"jwe_headers": {"x-hcx-correlation_id": "pay-4"},
             "fhir": _notice(_claims.claim(claim_id)["claim_no"],
                             "Payment initiated", "issued", 900)},
            "paymentnotice", "request", "fhir")
    finally:
        _claims._api = real_api
    unacked = _claims.payments(claim_id)[0]
    check("a notice whose acknowledgement fails is still kept, and flagged",
          outcome == "settled" and unacked["ack_status"] == "error"
          and "unreachable" in (unacked["ack_error"] or ""))
    _claims._api = _ack_ok
    try:
        _claims.acknowledge_payment(unacked["id"])
    finally:
        _claims._api = real_api
    check("and can be acknowledged again by hand",
          _claims.payment(unacked["id"])["ack_status"] == "sent")
    # The live payer types none of its identifiers and puts a message uuid on
    # the bundle — reading the claim number off the typed `CLN` alone missed
    # every real notice.
    untyped = {"resourceType": "Bundle", "type": "collection",
               "identifier": {"system": "https://dummypayer.nha.gov.in",
                              "value": "9bd29a8d-059f-4018-bc12-7cafeff96b5d"},
               "entry": [
                   {"resource": {
                       "resourceType": "Task", "status": "completed",
                       "identifier": [{
                           "system": "https://dummypayer.nha.gov.in/Task/"
                                     + paid_claim, "value": paid_claim}]}},
                   {"resource": {
                       "resourceType": "PaymentNotice", "status": "active",
                       "identifier": [{
                           "system": "https://dummypayer.nha.gov.in/"
                                     "PaymentNotice/" + paid_claim,
                           "value": paid_claim}],
                       "created": "2026-08-24T04:21:32+05:30",
                       "amount": {"value": 780847, "currency": "INR"},
                       "paymentStatus": {"coding": [
                           {"code": "paid", "display": "Paid"}]}}},
                   {"resource": {
                       "resourceType": "PaymentReconciliation",
                       "status": "active", "outcome": "complete",
                       "paymentDate": "2026-08-24",
                       "paymentAmount": {"value": 780847,
                                         "currency": "INR"}}},
               ]}
    check("an untyped identifier still yields the claim number",
          _claims.parse_payment_notice(untyped)["claim_ref"] == paid_claim)
    check("and the bundle's own message uuid is never mistaken for one",
          _claims.parse_payment_notice(
              {"resourceType": "Bundle",
               "identifier": {"value": "9bd29a8d-not-a-claim"},
               "entry": [{"resource": {"resourceType": "PaymentNotice",
                                       "amount": {"value": 5}}}]}
          )["claim_ref"] is None)
    check("with nothing else to go on, the first entry's identifier is tried",
          _claims.parse_payment_notice(
              {"resourceType": "Bundle", "entry": [
                  {"resource": {"resourceType": "Task",
                                "identifier": [{"value": paid_claim}]}},
                  {"resource": {"resourceType": "PaymentNotice",
                                "amount": {"value": 5}}}]}
          )["claim_ref"] == paid_claim)
    check("a notice with no words of its own is titled by its status",
          _claims.parse_payment_notice(untyped)["disposition"] == "Paid")

    _claims._api = _ack_ok
    try:
        _claims.receive({"jwe_headers": {"x-hcx-correlation_id": "pay-5",
                                         "x-hcx-sender_code": "1000003538@hcx",
                                         "x-hcx-workflow_id": "31"},
                         "fhir": untyped}, "paymentnotice", "request", "fhir")
    finally:
        _claims._api = real_api
    routed = _claims.payments(claim_id)[0]
    check("the acknowledgement goes back to whoever sent the notice",
          routed["sender_code"] == "1000003538@hcx"
          and ack_calls[-1][1]["jwe_headers"]["x-hcx-recipient_code"]
          == "1000003538@hcx"
          and ack_calls[-1][1]["jwe_headers"]["x-hcx-workflow_id"] == "31"
          and ack_calls[-1][1]["jwe_headers"]["x-hcx-correlation_id"]
          == "pay-5")

    try:
        _claims.parse_payment_notice({"resourceType": "Bundle", "entry": []})
        check("a bundle with no payment resources is refused", False)
    except ValueError:
        check("a bundle with no payment resources is refused", True)

    # --- one payment, two notices: initiated, then the UTR
    def _two_step(disposition, utr, correlation):
        reconciliation = {"resourceType": "PaymentReconciliation",
                          "status": "active", "disposition": disposition,
                          "paymentDate": "2026-08-27",
                          "paymentAmount": {"value": 2160, "currency": "INR"}}
        if utr:
            reconciliation["paymentIdentifier"] = {
                "type": {"coding": [{"code": "UTR"}]}, "value": utr}
        return {"jwe_headers": {"x-hcx-correlation_id": correlation},
                "fhir": {"resourceType": "Bundle", "type": "collection",
                         "entry": [
                             {"resource": {
                                 "resourceType": "PaymentNotice",
                                 "id": "notice-PAY-77", "status": "active",
                                 "identifier": [{"type": {"coding": [
                                     {"code": "CLN"}]}, "value": paid_claim}],
                                 "created": "2026-08-27T10:00:00+05:30",
                                 "amount": {"value": 2160, "currency": "INR"},
                                 "paymentStatus": {"coding": [
                                     {"code": "paid", "display": "Paid"}]}}},
                             {"resource": reconciliation}]}}

    before_rows = len(_claims.payments(claim_id))
    before_paid = _claims.paid_total(claim_id)
    _claims._api = _ack_ok
    try:
        first = _claims.receive(_two_step(
            "Payment initiated; the transfer has not completed yet.", None,
            "pay-step-1"), "paymentnotice", "request", "fhir")
        opened = _claims.payments(claim_id)
        initiated = next(r for r in opened if r["notice_id"] == "notice-PAY-77")
        check("a notice that says the payment is initiated is filed as such",
              first == "settled" and len(opened) == before_rows + 1
              and _claims.payment_initiated_only(initiated))
        check("and is not counted as money received",
              _claims.paid_total(claim_id) == before_paid)
        acked_once = len(ack_calls)
        second = _claims.receive(_two_step(
            "Rs 2,160 paid against Rs 2,160 approved.", "UTR-STEP-2",
            "pay-step-2"), "paymentnotice", "request", "fhir")
        cleared = _claims.payments(claim_id)
        done = next(r for r in cleared if r["notice_id"] == "notice-PAY-77")
        check("the notice with the UTR updates that same payment rather than "
              "adding a second", second == "settled"
              and len(cleared) == before_rows + 1
              and done["utr"] == "UTR-STEP-2"
              and done["correlation_id"] == "pay-step-2")
        check("the money now counts, once",
              _claims.paid_total(claim_id) == round(before_paid + 2160, 2))
        check("and the second notice is acknowledged on its own thread",
              len(ack_calls) == acked_once + 1
              and done["ack_status"] == "sent")
    finally:
        _claims._api = real_api

    section("NHCX communication (the query loop)")
    # An IRDAI payer asks for more on a thread of its own — a
    # CommunicationRequest on communication/request — and expects a
    # Communication back on that thread. PMJAY has no such leg: it queries
    # inside the ClaimResponse and is answered by resubmitting.
    queried_claim = _claims.claim(claim_id)

    def _query_bundle(about, request_id="req-1", payload=None, cln=None):
        req = {"resourceType": "CommunicationRequest", "id": request_id,
               "status": "active",
               "about": [{"reference": f"Claim/{a}", "display": a}
                         for a in about],
               "payload": [{"contentString": t} for t in (payload or [])],
               "reasonCode": [{"coding": [{"code": "line-query"}],
                               "text": t} for t in (payload or [])],
               "authoredOn": "2026-08-26T10:00:00+05:30"}
        if cln:
            req["identifier"] = [{"type": {"coding": [{"code": "CLN"}]},
                                  "value": cln}]
        return {"resourceType": "Bundle", "type": "collection", "entry": [
            {"resource": req},
            {"resource": {"resourceType": "Organization", "name": "Payer"}}]}

    asked = _query_bundle(["CLM-PAYER-77", queried_claim["claim_no"]],
                          payload=["Operative notes are missing.",
                                   "PROC-KNEE-01 Knee: send the implant "
                                   "invoice."])
    check("a query naming our claim number is filed against the claim",
          _claims.receive({"jwe_headers": {"x-hcx-correlation_id": "q-1",
                                           "x-hcx-sender_code":
                                           "1000004805@hcx",
                                           "x-hcx-workflow_id": "corr-claim-1"},
                           "fhir": asked}, "communication", "request", "fhir")
          == "settled")
    open_query = _claims.queries(claim_id)[0]
    check("it lands on the leg the workflow id names, open, with every "
          "question",
          open_query["stage"] == "claim"
          and open_query["status"] == "open"
          and open_query["correlation_id"] == "q-1"
          and open_query["request_id"] == "req-1"
          and open_query["sender_code"] == "1000004805@hcx"
          and _claims.query_questions(open_query)
          == ["Operative notes are missing.",
              "PROC-KNEE-01 Knee: send the implant invoice."])
    check("a redelivery of the same query is ignored, not filed twice",
          _claims.receive({"jwe_headers": {"x-hcx-correlation_id": "q-1"},
                           "fhir": asked}, "communication", "request", "fhir")
          == "ignored" and len(_claims.queries(claim_id)) == 1)
    check("a query naming nothing this EMR knows is unmatched",
          _claims.receive({"jwe_headers": {"x-hcx-correlation_id": "q-2"},
                           "fhir": _query_bundle(["CLM-NOBODY"])},
                          "communication", "request", "fhir") == "unmatched")
    check("a query matched by the payer's CLN alone still finds the claim, "
          "on the pre-auth leg when the workflow id names it",
          _claims.receive({"jwe_headers": {"x-hcx-correlation_id": "q-3",
                                           "x-hcx-workflow_id": "corr-preauth-1"},
                           "fhir": _query_bundle(
                               ["CLM-PAYER-77"],
                               request_id="req-3",
                               cln=queried_claim["claim_no"])},
                          "communication", "request", "fhir") == "settled"
          and _claims.queries(claim_id, "preauth")[0]["request_id"] == "req-3")
    check("a Communication on this route is an answer, not a question, "
          "and is left alone",
          _claims.receive({"jwe_headers": {"x-hcx-correlation_id": "q-4"},
                           "fhir": {"resourceType": "Bundle", "entry": [
                               {"resource": {"resourceType": "Communication"}}
                           ]}}, "communication", "request", "fhir")
          == "ignored")

    # The reply: text plus the claim's own documents, on the request's thread.
    invoice_id = _claims.add_document(
        claim_id, "implant-invoice.pdf", "application/pdf", b"%PDF-1.4 inv",
        label="Implant invoice", code="IMP", stage="claim")
    try:
        _claims.answer_query(open_query["id"], "   ", [])
        check("an empty reply is refused before any HTTP call", False)
    except ValueError as error:
        check("an empty reply is refused before any HTTP call",
              "empty answer" in str(error))
    reply_calls = []

    def _reply_ack(path, payload=None, **kw):
        reply_calls.append((path, payload))
        return {"txn_id": "txn-reply-1", "correlation_id": "q-1"}

    _claims._api = _reply_ack
    try:
        _claims.answer_query(open_query["id"],
                             "Implant invoice attached; notes to follow.",
                             [invoice_id])
    finally:
        _claims._api = real_api
    answered = _claims.query(open_query["id"])
    sent_path, sent_payload = reply_calls[-1]
    sent_headers = sent_payload["jwe_headers"]
    communication = sent_payload["fhir"]["entry"][0]["resource"]
    check("the reply goes out on communication/on_request, back to the "
          "asker, on the request's own thread",
          sent_path == "/fhir/out/v1/communication/on_request"
          and sent_headers["x-hcx-recipient_code"] == "1000004805@hcx"
          and sent_headers["x-hcx-correlation_id"] == "q-1"
          and sent_headers["x-hcx-workflow_id"] == "corr-claim-1")
    attachment = communication["payload"][1]
    check("the Communication names the request and the claim",
          communication["resourceType"] == "Communication"
          and communication["status"] == "completed"
          and communication["basedOn"][0]["reference"]
          == "CommunicationRequest/req-1"
          and communication["about"][0]["reference"] == "Claim/CLM-PAYER-77")
    check("it carries the text first",
          communication["payload"][0]["contentString"]
          == "Implant invoice attached; notes to follow.")
    check("and the file, under its document code, in the payer's namespace",
          attachment["contentAttachment"]["contentType"] == "application/pdf"
          and attachment["contentAttachment"]["title"] == "Implant invoice"
          and base64.b64decode(attachment["contentAttachment"]["data"])
          == b"%PDF-1.4 inv"
          and attachment["extension"][0]["valueString"] == "IMP"
          # The extension lives under the payer's own namespace — the
          # adapter's, so the IRDAI payer reads it under kyro.care/fhir.
          and attachment["extension"][0]["url"]
          == _payers.for_claim(queried_claim)["payer_system"]
          + "/StructureDefinition/document-type")
    # New files chosen on the reply itself are filed on the claim at the
    # queried leg's stage and travel with the reply.
    reply_calls.clear()
    second_query = _claims.queries(claim_id, "preauth")[0]
    _claims._api = _reply_ack
    try:
        _claims.answer_query(second_query["id"], "Notes attached.", [], [
            {"filename": "ot-notes.pdf", "content_type": "application/pdf",
             "data": b"%PDF-1.4 notes", "label": "OT notes", "code": "OTR"},
            {"filename": "xray.png", "content_type": "image/png",
             "data": b"\x89PNGxray"}])
    finally:
        _claims._api = real_api
    with_files = _claims.query(second_query["id"])
    filed = _claims.query_reply_documents(with_files)
    sent_payloads = reply_calls[-1][1]["fhir"]["entry"][0]["resource"]["payload"]
    check("files added on the reply are filed on the claim at that leg's "
          "stage and sent with it",
          with_files["status"] == "answered"
          and [(d["filename"], d["code"], d["stage"]) for d in filed]
          == [("ot-notes.pdf", "OTR", "preauth"), ("xray.png", None, "preauth")]
          and len(sent_payloads) == 3
          and sent_payloads[1]["extension"][0]["valueString"] == "OTR"
          and "extension" not in sent_payloads[2]
          and sent_payloads[2]["contentAttachment"]["contentType"]
          == "image/png")
    try:
        _claims.answer_query(second_query["id"], "Again.", [], [
            {"filename": "x.txt", "content_type": "text/plain", "data": b"x"}])
        check("a file the claim cannot hold refuses the reply before sending",
              False)
    except ValueError:
        check("a file the claim cannot hold refuses the reply before sending",
              True)

    # The screen itself, with a ruling on the claim naming documents and a
    # query open on each leg — the "Filed as" pickers read the ruling's rows.
    from emr.web.router import App as _QApp
    from emr.web.routes import register_all as _q_register
    from http.cookies import SimpleCookie as _QCookie
    _qapp = _QApp()
    _q_register(_qapp)
    pages = {tab: _qapp.dispatch("GET", f"/claims/{claim_id}", {"tab": tab},
                                 {}, _QCookie(), b"")
             for tab in ("communication", "claim", "preauth", "validate")}
    check("the claim screen renders every tab with queries open and a "
          "ruling on record",
          all(r.status == 200 for r in pages.values())
          and "Payer queries" in pages["communication"].body.decode(),
          str({tab: r.status for tab, r in pages.items()}))

    check("the reply is recorded on the query",
          answered["status"] == "answered"
          and answered["reply_txn_id"] == "txn-reply-1"
          and answered["reply_text"]
          == "Implant invoice attached; notes to follow."
          and [d["id"] for d in _claims.query_reply_documents(answered)]
          == [invoice_id])
    try:
        _claims.answer_query(_claims.queries(claim_id, "preauth")[0]["id"],
                             "x", [10 ** 6])
        check("a document that is not on this claim is refused", False)
    except ValueError as error:
        check("a document that is not on this claim is refused",
              "not on this claim" in str(error))

    def _reply_down(path, payload=None, **kw):
        raise _claims.GatewayError("hcxkit gateway unreachable at nowhere")

    _claims._api = _reply_down
    try:
        _claims.answer_query(_claims.queries(claim_id, "preauth")[0]["id"],
                             "Sending the ID proof.", [])
        check("a failed send raises", False)
    except ValueError:
        check("a failed send raises", True)
    finally:
        _claims._api = real_api
    failed = _claims.queries(claim_id, "preauth")[0]
    check("a failed reply is kept on the row, with its text, to send again",
          failed["status"] == "error"
          and "unreachable" in (failed["error_message"] or "")
          and failed["reply_text"] == "Sending the ID proof.")

    # --- the reply on a gateway both parties share
    section("NHCX shared-gateway polling")
    real_api = _claims._api

    def _shared(path, payload=None, **kwargs):
        if path == "/internal/txn/related":
            return [{"id": "OUT-1", "direction": "out"},
                    {"id": "IN-OUR-COPY", "direction": "in"},
                    {"id": "IN-REPLY", "direction": "in"},
                    {"id": "IN-REDELIVERY", "direction": "in"}]
        if path == "/internal/txn/fhir":
            if payload["txnId"] == "IN-REPLY":
                return {"jwe_headers": {"x-hcx-api_call_id": "A-REPLY"},
                        "fhir": {"entry": [{"resource": {
                            "resourceType": "ClaimResponse"}}]}}
            return {"jwe_headers": {},
                    "fhir": {"entry": [{"resource": {"resourceType": "Claim"}}]}}
        raise AssertionError(path)

    _claims._api = _shared
    try:
        related = _claims._api("/internal/txn/related")
        env, bundle = _claims._latest_reply(related, "ClaimResponse")
        check("the reply is the newest inbound that carries a ClaimResponse, "
              "not the payer's copy of our own Claim",
              bundle is not None
              and env["jwe_headers"]["x-hcx-api_call_id"] == "A-REPLY")
        env, bundle = _claims._latest_reply(
            [{"id": "IN-OUR-COPY", "direction": "in"}], "ClaimResponse")
        check("and nothing while no inbound carries one", bundle is None)
        env, bundle = _claims._latest_reply(related, "Task")
        check("a cancellation looks for its Task the same way", bundle is None)
        check("nothing is said of the other side while its answer is fine",
              _claims._peer_dispatch_error(related, "OUT-1") is None)
    finally:
        _claims._api = real_api

    def _refused(path, payload=None, **kwargs):
        if path == "/internal/txn/dispatch":
            return {"status": "errored", "errorMessage":
                    "[GATEWAY_HTTP_400] NHCX-1010 No Data with given "
                    "Correlation id for call back request"}
        raise AssertionError(path)

    _claims._api = _refused
    try:
        note = _claims._peer_dispatch_error(
            [{"id": "OUT-OURS", "direction": "out", "status": "completed"},
             {"id": "OUT-PAYER", "direction": "out", "status": "errored"}],
            "OUT-OURS")
        check("the payer's refused answer is reported with NHCX's reason",
              note is not None and "NHCX-1010" in note
              and "submit again" in note)
        check("our own errored send is not mistaken for the payer's",
              _claims._peer_dispatch_error(
                  [{"id": "OUT-OURS", "direction": "out", "status": "errored"}],
                  "OUT-OURS") is None)
    finally:
        _claims._api = real_api

    section("master data")
    from emr import masters as _m
    from emr.web.common import doctor_options, term_options

    check("every table is classified as master or transactional",
          _m.unlisted_tables() == [], str(_m.unlisted_tables()))
    check("masters and cleared tables do not overlap",
          not (_m.MASTER_TABLES & set(_m.CLEAR_ORDER)))
    all_kinds = {r["kind"] for r in db.query("SELECT DISTINCT kind FROM terminology")}
    check("every code set is reachable from the code master",
          not (all_kinds - set(_m.code_kinds())),
          str(sorted(all_kinds - set(_m.code_kinds()))))
    check("every listed code set has a label",
          all(k in _m.CODE_LABEL for k in _m.code_kinds()))

    # --- practitioners
    new_doc = _m.save_practitioner(None, {
        "name": "Dr. Master Test", "department": "Nephrology",
        "identifier_type_system": "x", "identifier_type_code": "HPID",
        "identifier_type_display": "HPID", "identifier_system": "x",
        "identifier_value": "71-0000-0000-0001", "consultation_fee": 900,
        "active": 1})
    check("a practitioner can be added", new_doc > 0)
    try:
        _m.save_practitioner(None, {"name": "", "identifier_value": "x"})
        check("a nameless practitioner is refused", False)
    except ValueError:
        check("a nameless practitioner is refused", True)
    try:
        _m.save_practitioner(None, {"name": "X", "identifier_value": ""})
        check("a practitioner without an identifier is refused", False)
    except ValueError:
        check("a practitioner without an identifier is refused", True)
    _m.set_practitioner_active(new_doc, False)
    check("a retired practitioner leaves the pickers",
          new_doc not in [i for i, _l in doctor_options()])
    check("but is kept on the master list",
          any(r["id"] == new_doc for r in _m.staff()))
    _m.set_practitioner_active(new_doc, True)
    check("and can be reinstated",
          new_doc in [i for i, _l in doctor_options()])

    # --- clinical codes
    code_id = _m.add_code("medicine", {"code": "TESTMED1", "display": "Testolol",
                                       "extra": "10 mg tablet"})
    check("a concept can be added to a picker",
          db.term("medicine", "TESTMED1")["display"] == "Testolol")
    try:
        _m.add_code("medicine", {"code": "TESTMED1", "display": "Dupe"})
        check("a duplicate code is refused", False)
    except ValueError:
        check("a duplicate code is refused", True)
    try:
        _m.add_code("medicine", {"code": "", "display": "No code"})
        check("a concept without a code is refused", False)
    except ValueError:
        check("a concept without a code is refused", True)
    _m.update_code(code_id, {"display": "Testolol XR"})
    check("a concept can be renamed",
          db.term("medicine", "TESTMED1")["display"] == "Testolol XR")
    check("a new concept appears in the picker it belongs to",
          "TESTMED1" in [c for c, _l in term_options("medicine")])
    _m.delete_code(code_id)
    check("a concept can be removed", db.term("medicine", "TESTMED1") is None)

    panel = _m.add_code("lab_panel", {
        "code": "TESTPANEL", "display": "Test panel", "system": "http://loinc.org",
        "extra": "Biochemistry|2160-0,3094-0|450"})
    order_id = services.create_lab_order(patient, None, "TESTPANEL", pathologist, None)
    check("a lab panel added to the master is immediately orderable",
          db.scalar("SELECT price FROM lab_order WHERE id = ?",
                    (order_id,)) == 450)
    check("and its analyte list expands",
          db.scalar("SELECT COUNT(*) FROM observation WHERE lab_order_id = ?",
                    (order_id,), default=0) == 2)
    _m.delete_code(panel)

    # --- pharmacy catalogue
    item = _m.save_stock_item(None, {
        "code": "TESTITEM", "name": "Test syrup", "kind": "drug", "unit": "bottle",
        "mrp": 55.0, "purchase_price": 30.0, "gst_pct": 12, "reorder_level": 10,
        "active": 1})
    check("a stock item can be added", item > 0)
    try:
        _m.save_stock_item(None, {"code": "TESTITEM", "name": "Clash"})
        check("a duplicate item code is refused", False)
    except ValueError:
        check("a duplicate item code is refused", True)
    check("a new item is in the catalogue but not issuable without stock",
          any(i["id"] == item for i in hospital.stock_list())
          and hospital.stock_on_hand(item) == 0)
    _m.set_item_active(item, False)
    check("a retired item leaves the catalogue",
          not any(i["id"] == item for i in hospital.stock_list()))
    _m.set_item_active(item, True)

    # --- wards and beds
    ward_id = _m.save_ward(None, {"code": "TESTW", "name": "Test Ward",
                                  "class": "general", "tariff": 900,
                                  "nursing_rate": 200, "gender_policy": "female",
                                  "active": 1})
    bed_id = _m.add_bed(ward_id, "TW-01")
    check("a ward and bed can be added",
          any(b["id"] == bed_id for b in hospital.vacant_beds()))
    check("the ward gender policy is honoured immediately",
          not any(b["id"] == bed_id for b in hospital.vacant_beds("male"))
          and any(b["id"] == bed_id for b in hospital.vacant_beds("female")))
    try:
        _m.add_bed(ward_id, "TW-01")
        check("a duplicate bed code is refused", False)
    except ValueError:
        check("a duplicate bed code is refused", True)
    _m.retire_bed(bed_id)
    check("a retired bed leaves the vacant list",
          not any(b["id"] == bed_id for b in hospital.vacant_beds()))
    _m.restore_bed(bed_id)
    hospital.occupy_bed(bed_id, admission)
    try:
        _m.retire_bed(bed_id)
        check("an occupied bed cannot be retired", False)
    except ValueError:
        check("an occupied bed cannot be retired", True)
    hospital.release_bed(admission)
    hospital.set_bed_status(bed_id, "vacant")

    section("database transactions")
    baseline = db.scalar("SELECT COUNT(*) FROM patient", default=0)
    try:
        with db.transaction():
            services.create_patient({"name": "Rollback One", "gender": "male",
                                     "phone": "+910000000001"})
            services.create_patient({"name": "Rollback Two", "gender": "male",
                                     "phone": "+910000000002"})
            raise RuntimeError("boom")
    except RuntimeError:
        pass
    check("a failed transaction rolls every write back",
          db.scalar("SELECT COUNT(*) FROM patient", default=0) == baseline)
    with db.transaction():
        services.create_patient({"name": "Committed", "gender": "male",
                                 "phone": "+910000000003"})
    check("a clean transaction commits",
          db.scalar("SELECT COUNT(*) FROM patient", default=0) == baseline + 1)
    with db.transaction():
        with db.transaction():
            services.create_patient({"name": "Nested", "gender": "male",
                                     "phone": "+910000000004"})
    check("transactions nest, the outermost commits",
          db.scalar("SELECT COUNT(*) FROM patient", default=0) == baseline + 2)
    check("writes outside a transaction still commit on their own",
          not db.in_transaction())

    section("facility settings")
    org = db.default_org()
    db.update("organization", org["id"], {
        "name": "Sunrise Community Hospital", "identifier_value": "IN3110009988",
        "identifier_type_code": "ROHINI",
        "identifier_type_display": "Registry of Hospitals in Network of Insurance "
                                   "(ROHINI) ID",
        "participant_code": "sunrise@hcx", "state": "Tamil Nadu",
        "city": "Coimbatore", "address_line": "44 Gandhi Road",
        "postal_code": "641001"})
    refreshed = build_bundle("OPConsultRecord", visit)
    org_res = next(e["resource"] for e in refreshed["entry"]
                   if e["resource"]["resourceType"] == "Organization")
    check("facility settings reach the exported Organization",
          org_res["name"] == "Sunrise Community Hospital"
          and org_res["identifier"][0]["value"] == "IN3110009988"
          and org_res["identifier"][0]["type"]["coding"][0]["code"] == "ROHINI")
    check("the configured address reaches the bundle",
          "Coimbatore" in org_res["address"][0]["text"]
          and "641001" in org_res["address"][0]["text"])
    check("the facility is the document custodian",
          refreshed["entry"][0]["resource"]["custodian"]["reference"]
          == next(e["fullUrl"] for e in refreshed["entry"]
                  if e["resource"]["resourceType"] == "Organization"))
    check("a reconfigured facility still exports a valid bundle",
          not [i for i in validate_bundle("OPConsultRecord", refreshed)
               if i["severity"] == "error"])
    check("dashboard route present", any(m == "GET" and pat == "^/$"
                                         for m, pat in routes))

    # Runs last on purpose: it deletes the records every section above
    # depends on, so anything placed after it would have no fixtures.
    section("clearing patient data")
    before_masters = _m.counts()
    before_rows = sum(_m.transactional_counts().values())
    check("there is patient data to clear", before_rows > 0, f"{before_rows} rows")

    deleted = _m.clear_transactional_data()
    check("the reset completes without a foreign-key failure", sum(deleted.values()) > 0,
          f"{sum(deleted.values())} rows across {len(deleted)} tables")
    check("no patient data survives",
          sum(_m.transactional_counts().values()) == 0,
          str({k: v for k, v in _m.transactional_counts().items() if v}))
    check("every master survives untouched", _m.counts() == before_masters,
          f"{_m.counts()} vs {before_masters}")

    check("beds are freed rather than deleted",
          db.scalar("SELECT COUNT(*) FROM bed WHERE active = 1 "
                    "AND status <> 'vacant'", default=0) == 0)
    check("no bed still points at a deleted encounter",
          db.scalar("SELECT COUNT(*) FROM bed WHERE encounter_id IS NOT NULL",
                    default=0) == 0)
    check("dialysis machines are freed rather than deleted",
          db.scalar("SELECT COUNT(*) FROM dialysis_machine "
                    "WHERE status <> 'available'", default=0) == 0)
    check("numbering restarts from one",
          db.scalar("SELECT COUNT(*) FROM counter", default=0) == 0)

    # the clinic has to be usable immediately afterwards
    fresh = services.create_patient({"name": "After Reset", "gender": "female",
                                     "phone": "+919000022222"})
    check("a patient can be registered straight after a reset",
          db.scalar("SELECT mrn FROM patient WHERE id = ?", (fresh,)) == "MRN00001")
    fresh_visit = services.create_encounter("OPD", {
        "patient_id": fresh, "status": "in-progress", "class_code": "AMB",
        "practitioner_id": physician, "period_start": db.now_iso()})
    check("and an encounter opened, numbering from one",
          db.scalar("SELECT encounter_no FROM encounter WHERE id = ?",
                    (fresh_visit,)) == "OPD-00001")
    fresh_and_new = db.scalar("SELECT COUNT(*) FROM patient", default=0)
    wiped_again = _m.clear_transactional_data()
    check("clearing again removes only what was added since",
          sum(wiped_again.values()) >= fresh_and_new
          and sum(_m.transactional_counts().values()) == 0,
          str(wiped_again))

    print("\n" + "\u2550" * 64)
    total = sum(ok + bad for ok, bad in tally.values())
    for name, (ok, bad) in tally.items():
        flag = "FAIL" if bad else "ok"
        print(f"  {name:44s} {ok:3d} passed  {bad:2d} failed  [{flag}]")
    print("\u2550" * 64)
    if failures:
        print(f"{failures} of {total} check(s) FAILED")
        return 1
    print(f"all {total} checks passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
