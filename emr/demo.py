"""Demonstration data: two patients walked through every workflow.

Running this is also the quickest end-to-end check that the FHIR export layer
produces bundles that pass the profile gate.
"""

from __future__ import annotations

from . import db, dialysis, hospital, services, wellness
from .fhir import persist_bundle


def seed_demo() -> None:
    if db.scalar("SELECT COUNT(*) FROM patient", default=0):
        print("  demo data already present — skipping")
        return

    doctors = {r["department"]: r["id"] for r in db.query(
        "SELECT id, department FROM practitioner")}
    physician = doctors.get("General Medicine")
    cardiologist = doctors.get("Cardiology")
    pathologist = doctors.get("Laboratory")

    # ---------------------------------------------------------------- OPD case
    opd_patient = services.create_patient({
        "name": "Lakshmi Narayanan", "given_name": "Lakshmi",
        "family_name": "Narayanan", "gender": "female", "birth_date": "1979-04-12",
        "phone": "+919840012345", "email": "lakshmi.n@example.in",
        "abha_number": "91-1234-5678-9012", "abha_address": "lakshmi.n@abdm",
        "marital_status_code": "M", "marital_status_display": "Married",
        "blood_group": "B+", "address_line": "18, Kamaraj Street, Adyar",
        "city": "Chennai", "district": "Chennai", "state": "Tamil Nadu",
        "postal_code": "600020", "contact_name": "Ravi Narayanan",
        "contact_relation": "Spouse", "contact_phone": "+919840099887",
    })

    visit = services.create_encounter("OPD", {
        "patient_id": opd_patient, "status": "finished", "class_code": "AMB",
        "type_code": "11429006", "type_display": "Consultation",
        "service_type_code": "394802001", "service_type_display": "General medicine",
        "priority_code": "R", "priority_display": "routine",
        "priority_system": "http://terminology.hl7.org/CodeSystem/v3-ActPriority",
        "practitioner_id": physician, "department": "General Medicine",
        "period_start": db.now_iso(), "period_end": db.now_iso(),
        "reason_code": "386661006", "reason_display": "Fever",
        "consultation_fee": 700,
    })

    _condition(opd_patient, visit, "chief-complaint", "complaint", "386661006",
               "Fever with chills for three days")
    _condition(opd_patient, visit, "chief-complaint", "complaint", "25064002",
               "Frontal headache, worse in the evening")
    _condition(opd_patient, visit, "diagnosis", "diagnosis", "44054006",
               "Type 2 diabetes mellitus, on oral hypoglycaemics")
    _condition(opd_patient, visit, "diagnosis", "diagnosis", "38341003",
               "Essential hypertension, controlled")

    services.record_vitals(opd_patient, visit, {
        "8310-5": "38.4", "8867-4": "96", "9279-1": "18", "8480-6": "138",
        "8462-4": "86", "59408-5": "97", "29463-7": "68", "8302-2": "158",
    })

    services.add_allergy(opd_patient, None, "91936005", "", "medication", "high",
                         "126485001", "Urticaria within an hour of the first dose")
    services.add_allergy(opd_patient, None, None, "Brinjal (aubergine)", "food", "low",
                         "418363000", None)
    services.add_problem(opd_patient, None, "40930008", "",
                         onset="2019-06-01", note="On thyroxine 50 mcg daily")

    _medication(opd_patient, visit, physician, "387517004", 500, "mg", 3, 5,
                "311504000")
    _medication(opd_patient, visit, physician, "372567009", 500, "mg", 2, 30,
                "311504000")

    _service_request(opd_patient, visit, physician, "58410-2")
    _service_request(opd_patient, visit, physician, "4548-4")

    services.upsert_note(visit, opd_patient, "OPD_NOTE", {
        "author_id": physician,
        "history_text": "Three days of intermittent high-grade fever with chills and "
                        "generalised myalgia. No cough, no dysuria, no rash.",
        "examination_text": "Conscious, oriented, febrile. Chest clear, S1 S2 normal, "
                            "abdomen soft and non-tender. No neck stiffness.",
        "advice_text": "Paracetamol SOS for fever above 38.5 C, oral fluids 3 L/day, "
                       "return immediately if bleeding, breathlessness or drowsiness.",
        "follow_up_date": db.today_iso(),
        "follow_up_note": "Review with CBC and HbA1c reports",
        "status": "final",
    })

    lab = services.create_lab_order(opd_patient, visit, "58410-2", pathologist, None)
    results = {r["id"]: value for r, value in zip(
        db.query("SELECT id FROM observation WHERE lab_order_id = ? ORDER BY sort_order",
                 (lab,)),
        ["11.2", "36", "4.1", "12.6", "132", "88", "29", "33"])}
    services.save_lab_results(
        lab, {f"result_{k}": v for k, v in results.items()},
        "Mild anaemia with leucocytosis and borderline thrombocytopenia; "
        "correlate clinically and repeat after 48 hours.", finalise=True)

    invoice = None  # raised after the pharmacy issues below

    # ---------------------------------------------------------------- IPD case
    ipd_patient = services.create_patient({
        "name": "Mohammed Irfan", "given_name": "Mohammed", "family_name": "Irfan",
        "gender": "male", "birth_date": "1962-11-30", "phone": "+919845567788",
        "abha_number": "91-9876-5432-1098", "abha_address": "m.irfan@abdm",
        "marital_status_code": "M", "marital_status_display": "Married",
        "blood_group": "O+", "address_line": "7/2, Nungambakkam High Road",
        "city": "Chennai", "district": "Chennai", "state": "Tamil Nadu",
        "postal_code": "600034", "contact_name": "Farah Irfan",
        "contact_relation": "Daughter", "contact_phone": "+919845511223",
    })

    admission = services.create_encounter("IPD", {
        "patient_id": ipd_patient, "status": "finished", "class_code": "IMP",
        "type_code": "32485007", "type_display": "Hospital admission",
        "service_type_code": "394579002", "service_type_display": "Cardiology",
        "priority_code": "UR", "priority_display": "urgent",
        "priority_system": "http://terminology.hl7.org/CodeSystem/v3-ActPriority",
        "practitioner_id": cardiologist, "department": "Cardiology",
        "period_start": db.now_iso(), "period_end": db.now_iso(),
        "discharge_ts": db.now_iso(),
        "reason_code": "29857009", "reason_display": "Chest pain",
        "admission_type": "Emergency", "consultation_fee": 1200,
        "discharge_disposition_code": "home", "discharge_disposition_display": "Home",
    })

    icu_bed = db.one("SELECT b.id FROM bed b JOIN ward w ON w.id = b.ward_id "
                     "WHERE w.code = 'ICU' AND b.status = 'vacant' ORDER BY b.code")
    hospital.occupy_bed(icu_bed["id"], admission, reason="Emergency admission")

    _condition(ipd_patient, admission, "chief-complaint", "complaint", "29857009",
               "Central crushing chest pain radiating to the left arm")
    _condition(ipd_patient, admission, "diagnosis", "diagnosis", "22298006",
               "Acute inferior wall myocardial infarction")
    _condition(ipd_patient, admission, "medical-history", "diagnosis", "38341003",
               "Hypertension for twelve years")

    services.record_vitals(ipd_patient, admission, {
        "8310-5": "36.8", "8867-4": "104", "9279-1": "22", "8480-6": "148",
        "8462-4": "92", "59408-5": "94", "29463-7": "81", "8302-2": "172",
    })

    services.add_allergy(ipd_patient, None, "293761005", "", "medication", "high",
                         "4386001", "Bronchospasm after a single dose")

    db.insert("procedure", {
        "patient_id": ipd_patient, "encounter_id": admission, "status": "completed",
        "snomed_code": "232717009",
        "snomed_display": "Coronary artery bypass graft",
        "category_code": "387713003", "category_display": "Surgical procedure",
        "outcome_code": "385669000", "outcome_display": "Successful",
        "performed_ts": db.now_iso(), "performer_id": cardiologist,
        "note": "Triple vessel CABG, uneventful intra-operative course.",
    })

    _medication(ipd_patient, admission, cardiologist, "387458008", 75, "mg", 1, 90,
                "311504000")
    _medication(ipd_patient, admission, cardiologist, "373444002", 40, "mg", 1, 90,
                "307165006")

    ipd_lab = services.create_lab_order(ipd_patient, admission, "24331-1", pathologist,
                                        None)
    ipd_values = dict(zip(
        [r["id"] for r in db.query(
            "SELECT id FROM observation WHERE lab_order_id = ? ORDER BY sort_order",
            (ipd_lab,))],
        ["248", "212", "31", "162"]))
    services.save_lab_results(
        ipd_lab, {f"result_{k}": v for k, v in ipd_values.items()},
        "Dyslipidaemia with elevated LDL and triglycerides; high dose statin advised.",
        finalise=True)

    services.upsert_note(admission, ipd_patient, "DISCHARGE_SUMMARY", {
        "author_id": cardiologist,
        "admission_reason": "Acute onset central chest pain with diaphoresis; ECG "
                            "showed ST elevation in the inferior leads.",
        "history_text": "Known hypertensive on irregular treatment, ex-smoker, "
                        "strong family history of coronary artery disease.",
        "examination_text": "Pulse 104/min, BP 148/92 mmHg, JVP not raised, "
                            "bilateral basal crepitations.",
        "course_in_hospital": "Thrombolysed on arrival and shifted to the ICU. "
                              "Coronary angiography showed triple vessel disease; "
                              "underwent CABG on day three. Post-operative recovery "
                              "was uneventful, mobilised from day five and stepped "
                              "down to the ward on day six.",
        "condition_at_discharge": "Haemodynamically stable, ambulant, wound healthy, "
                                  "no angina at rest or on mild exertion.",
        "discharge_instructions": "Continue medication as charted. Wound dressing on "
                                  "alternate days. Report immediately for fever, "
                                  "wound discharge, chest pain or breathlessness.",
        "diet_advice": "Low salt, low fat diet; fluid intake 2 L/day.",
        "care_plan_text": "Cardiac rehabilitation from week two, lipid profile and "
                          "ECG at four weeks, smoking cessation counselling.",
        "follow_up_date": db.today_iso(),
        "follow_up_note": "Cardiology OPD review in one week",
        "status": "final",
    })

    ipd_invoice = None  # raised after the pharmacy issues below

    # ------------------------------------------------------------- operations
    # Opening stock for every item, then the issues these two episodes consumed.
    for index, item in enumerate(db.query("SELECT * FROM stock_item ORDER BY id")):
        hospital.receive_stock(
            item["id"], f"B{2600 + index}",
            f"2027-{(index % 12) + 1:02d}-28",
            item["reorder_level"] * 3 or 100,
            item["purchase_price"], "Ellider Pharma Distributors")

    for code, qty in [("MED001", 15), ("MED006", 60), ("CON005", 4)]:
        item = db.one("SELECT id FROM stock_item WHERE code = ?", (code,))
        hospital.issue_stock(item["id"], qty, opd_patient, visit, "OPD prescription")
    for code, qty in [("MED009", 90), ("MED011", 6), ("CON001", 4), ("CON003", 10),
                      ("CON006", 20)]:
        item = db.one("SELECT id FROM stock_item WHERE code = ?", (code,))
        hospital.issue_stock(item["id"], qty, ipd_patient, admission, "Ward indent")

    hospital.release_bed(admission)

    invoice = services.create_invoice(opd_patient, visit, "03", "OPD", physician,
                                      "OPD consultation, investigations and pharmacy")
    services.save_invoice(invoice, services.suggest_invoice_lines(visit))
    hospital.mark_issues_billed(visit)
    hospital.record_payment(invoice, db.scalar(
        "SELECT total_gross FROM invoice WHERE id = ?", (invoice,)), "upi",
        "UPI/2026/88213", "Settled at the counter")

    ipd_invoice = services.create_invoice(ipd_patient, admission, "02", "IPD",
                                          cardiologist, "Inpatient episode — CABG")
    services.save_invoice(ipd_invoice, services.suggest_invoice_lines(admission))
    hospital.mark_issues_billed(admission)
    ipd_total = db.scalar("SELECT total_gross FROM invoice WHERE id = ?",
                          (ipd_invoice,)) or 0
    hospital.record_payment(ipd_invoice, round(ipd_total * 0.6, 2), "bank",
                            "NEFT/887312", "Part payment; insurance claim pending")

    for offset, (patient_id, doctor_id, dept, reason) in enumerate([
            (opd_patient, physician, "General Medicine", "Fever"),
            (ipd_patient, cardiologist, "Cardiology", "Chest pain")]):
        hospital.book_appointment(patient_id, doctor_id, dept, db.today_iso(),
                                  f"{10 + offset}:30", reason, None)

    # ------------------------------------------------------------- dialysis
    dialysis_patient = services.create_patient({
        "name": "Sarita Devi", "given_name": "Sarita", "family_name": "Devi",
        "gender": "female", "birth_date": "1968-02-19", "phone": "+919845123456",
        "abha_number": "91-5566-7788-9900", "abha_address": "sarita.d@abdm",
        "marital_status_code": "W", "marital_status_display": "Widowed",
        "blood_group": "A+", "address_line": "23, Perambur Barracks Road",
        "city": "Chennai", "district": "Chennai", "state": "Tamil Nadu",
        "postal_code": "600012", "contact_name": "Anil Kumar",
        "contact_relation": "Son", "contact_phone": "+919845765432",
    })
    nephrologist = doctors.get("Nephrology") or physician
    dialysis_visit = services.create_encounter("OPD", {
        "patient_id": dialysis_patient, "status": "in-progress", "class_code": "AMB",
        "type_code": "11429006", "type_display": "Consultation",
        "service_type_code": "394589003", "service_type_display": "Nephrology",
        "priority_code": "R", "priority_display": "routine",
        "priority_system": "http://terminology.hl7.org/CodeSystem/v3-ActPriority",
        "practitioner_id": nephrologist, "department": "Nephrology",
        "period_start": db.now_iso(), "consultation_fee": 600,
    })
    _condition(dialysis_patient, dialysis_visit, "diagnosis", "diagnosis", "40930008",
               "End stage renal disease on maintenance haemodialysis")

    course = dialysis.create_course({
        "patient_id": dialysis_patient, "encounter_id": dialysis_visit,
        "practitioner_id": nephrologist,
        "modality_code": "302497006", "modality_display": "Hemodialysis",
        "access_code": "av-fistula", "access_display": "Arteriovenous fistula",
        "access_site": "Left radiocephalic",
        "sessions_per_week": 3, "duration_minutes": 240, "dry_weight_kg": 62.5,
        "dialyser": "F7 HPS (1.6 m²)",
        "anticoagulant_code": "372877000", "anticoagulant_display": "Heparin",
        "heparin_bolus_units": 2000, "heparin_hourly_units": 1000,
        "blood_flow_rate": 300, "dialysate_flow_rate": 500,
        "dialysate_na": 138, "dialysate_k": 2, "dialysate_ca": 1.25,
        "dialysate_bicarb": 32, "session_charge": 2200,
        "note": "Maintenance haemodialysis, Mon/Wed/Fri.",
    })

    machine = db.scalar("SELECT id FROM dialysis_machine WHERE code = 'HD-01'")
    run = dialysis.schedule_session(course, machine, db.now_iso(), nephrologist)
    dialysis.start_session(run)
    dialysis.record_phase_vitals(run, "pre", {
        "8480-6": "158", "8462-4": "94", "8867-4": "88", "8310-5": "36.7",
        "9279-1": "18", "59408-5": "98"})
    dialysis.save_parameters(run, {
        "pre_weight_kg": 65.2, "uf_goal_ml": 2700, "blood_flow_rate": 320,
        "dialysate_flow_rate": 500, "dialysate_temp": 36.5, "conductivity": 14.0,
        "dialyser_reuse": 3, "duration_minutes": 240, "ktv": 1.42, "urr_pct": 68.5,
        "complication_code": "45007003", "complication_display": "Hypotension",
        "complication_note": "Symptomatic drop at 150 minutes; 200 mL saline given "
                             "and ultrafiltration held for 15 minutes.",
        "note": "Otherwise uneventful run.",
    })
    for minutes, sbp, dbp, pulse, qb, art, ven, tmp, uf in [
            (0, 158, 94, 88, 320, -80, 120, 140, 0),
            (30, 150, 90, 86, 320, -82, 124, 145, 400),
            (60, 142, 86, 84, 320, -85, 130, 150, 900),
            (120, 128, 78, 90, 320, -90, 135, 160, 1800),
            (150, 104, 62, 96, 280, -95, 140, 165, 2300),
            (180, 118, 70, 92, 300, -88, 132, 158, 2450),
            (240, 132, 80, 82, 300, -85, 125, 150, 2600)]:
        dialysis.add_reading(run, {
            "elapsed_minutes": minutes, "bp_systolic": sbp, "bp_diastolic": dbp,
            "pulse": pulse, "blood_flow_rate": qb, "arterial_pressure": art,
            "venous_pressure": ven, "tmp": tmp, "uf_volume_ml": uf,
            "note": "Saline 200 mL, UF held" if minutes == 150 else None})
    dialysis.record_phase_vitals(run, "post", {
        "8480-6": "132", "8462-4": "80", "8867-4": "82", "8310-5": "36.4",
        "9279-1": "16", "59408-5": "99"})
    dialysis.save_parameters(run, {"post_weight_kg": 62.6})
    dialysis.complete_session(run)

    dialysis_invoice = services.create_invoice(
        dialysis_patient, dialysis_visit, "03", "OPD", nephrologist,
        "Maintenance haemodialysis session")
    services.save_invoice(dialysis_invoice,
                          services.suggest_invoice_lines(dialysis_visit))
    dialysis.mark_sessions_billed(dialysis_visit)
    hospital.record_payment(dialysis_invoice, db.scalar(
        "SELECT total_gross FROM invoice WHERE id = ?", (dialysis_invoice,)),
        "cash", None, "Paid at the dialysis counter")

    # ------------------------------------------------------------- wellness
    check_up = wellness.create_record(
        opd_patient, visit, physician, db.today_iso(), "Health camp",
        "Annual preventive health check at the Adyar camp.")
    wellness.save_section(check_up, "vital-signs", {
        "61008-9": "36.6", "9279-1": "16", "8867-4": "74", "2708-6": "98",
        "85354-9": "128/82"})
    wellness.save_section(check_up, "body-measurement", {
        "29463-7": "68", "8302-2": "158", "39156-5": "27.2", "8280-0": "92",
        "56074-8": "34", "56072-2": "28"})
    wellness.save_section(check_up, "physical-activity", {
        "55423-8": "6200", "93832-4": "6.5", "41981-2": "310",
        "80493-0": "Moderate"})
    wellness.save_section(check_up, "general-assessment",
                          {"41604-0": "118", "8999-5": "1800", "9052-2": "1900"},
                          {"8693-4": "alert-and-oriented", "365275006": "fair"})
    wellness.save_section(check_up, "women-health",
                          {"8665-2": "2026-08-02", "92656-8": "12", "42798-9": "13"})
    wellness.save_section(check_up, "lifestyle", {}, {
        "365981007": "266919005", "228273003": "105542008",
        "228509002": "373067005"})
    wellness.finalise(check_up)

    # -------------------------------------------------------------- FHIR export
    built = [
        persist_bundle("OPConsultRecord", visit),
        persist_bundle("DiagnosticReportRecord", lab),
        persist_bundle("InvoiceRecord", invoice),
        persist_bundle("DischargeSummaryRecord", admission),
        persist_bundle("DiagnosticReportRecord", ipd_lab),
        persist_bundle("InvoiceRecord", ipd_invoice),
        persist_bundle("OPConsultRecord", dialysis_visit),
        persist_bundle("InvoiceRecord", dialysis_invoice),
        persist_bundle("WellnessRecord", check_up),
    ]
    for row in built:
        state = "valid" if row["valid"] else "HAS ERRORS"
        print(f"  {row['artifact']:24s} {row['bundle_identifier']:12s} "
              f"{row['resource_count']:3d} resources  {state}")
    print("  demo data seeded")


# ---------------------------------------------------------------------------
def _condition(patient_id: int, encounter_id: int, category: str, kind: str,
               code: str, text: str) -> None:
    master = db.term(kind, code)
    db.insert("condition", {
        "patient_id": patient_id, "encounter_id": encounter_id, "category": category,
        "clinical_status": "active", "verification_status": "confirmed",
        "snomed_code": master["code"] if master else None,
        "snomed_display": master["display"] if master else None,
        "icd10_code": master["alt_code"] if master else None,
        "icd10_display": master["alt_display"] if master else None,
        "text": master["display"] if master else text,
        "note": text,
        "recorded_at": db.now_iso(),
    })


def _medication(patient_id: int, encounter_id: int, requester: int | None, code: str,
                dose: float, unit: str, freq: int, days: int,
                instruction: str | None) -> None:
    master = db.term("medicine", code)
    route = db.term("route", "26643006")
    extra = db.term("dose_instruction", instruction) if instruction else None
    db.insert("medication_request", {
        "patient_id": patient_id, "encounter_id": encounter_id,
        "status": "active", "intent": "order",
        "snomed_code": master["code"], "snomed_display": master["display"],
        "dose_quantity": dose, "dose_unit": unit,
        "route_code": route["code"], "route_display": route["display"],
        "frequency": freq, "period": 1, "period_unit": "d", "duration_days": days,
        "timing_text": f"{master['display']} {dose} {unit} — {freq} time(s) a day "
                       f"for {days} day(s)",
        "additional_code": extra["code"] if extra else None,
        "additional_display": extra["display"] if extra else None,
        "authored_on": db.now_iso(), "requester_id": requester,
    })


def _service_request(patient_id: int, encounter_id: int, requester: int | None,
                     panel_code: str) -> None:
    master = db.term("lab_panel", panel_code)
    db.insert("service_request", {
        "patient_id": patient_id, "encounter_id": encounter_id,
        "purpose": "investigation", "status": "active", "intent": "order",
        "code_system": master["system"], "code": master["code"],
        "display": master["display"],
        "authored_on": db.now_iso(), "requester_id": requester,
    })
