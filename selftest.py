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
        check("a path off the mount point is a 404",
              mounted.dispatch("GET", "/patients", {}, {}, SimpleCookie(), b"").status
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
          _claims.receive({"fhir": {}}, "claim", "request", "fhir") == "ignored")

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
    try:
        _claims.save_preauth(claim_id, dict(stay_dates, case_type="nonpackage"),
                             [], team, [{"code": "BED-DAY", "qty": "4"}])
        check("a preauth without a diagnosis is refused", False)
    except ValueError:
        check("a preauth without a diagnosis is refused", True)
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
    check("the care team quotes the doctor and role",
          children["care_team"][0]["practitioner_id"] == physician
          and children["care_team"][0]["role"] == "admitting")

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
