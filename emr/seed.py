"""Master data seeding.

The `terminology` table is a *demo subset* of SNOMED CT / LOINC / ICD-10 that is
sufficient to produce well formed NRCES bundles. In a real deployment replace it
with a licensed SNOMED CT India expansion and the NRCES ValueSet expansions.
"""

from __future__ import annotations

from . import db
from .terminology import (
    DISCHARGE_DISPOSITION,
    ICD10,
    LOINC,
    NDHM_IDENTIFIER_TYPE,
    SNOMED,
    V2_0203,
    V3_MARITAL,
)

LOCAL_CS = "https://nanoemr.local/CodeSystem"


def _t(kind, code, display, *, system=SNOMED, alt=None, unit=None,
       low=None, high=None, extra=None, order=100):
    alt_system, alt_code, alt_display = alt or (None, None, None)
    return (kind, system, code, display, alt_system, alt_code, alt_display,
            unit, low, high, extra, order)


def terminology_rows() -> list[tuple]:
    rows: list[tuple] = []

    # ------------------------------------------------------------ complaints
    complaints = [
        ("386661006", "Fever"), ("49727002", "Cough"), ("25064002", "Headache"),
        ("21522001", "Abdominal pain"), ("29857009", "Chest pain"),
        ("267036007", "Dyspnea"), ("422587007", "Nausea"), ("422400008", "Vomiting"),
        ("62315008", "Diarrhea"), ("404640003", "Dizziness"), ("84229001", "Fatigue"),
        ("161891005", "Backache"), ("68962001", "Muscle pain"), ("43724002", "Chill"),
        ("57676002", "Joint pain"), ("162397003", "Sore throat"),
    ]
    for i, (c, d) in enumerate(complaints):
        rows.append(_t("complaint", c, d, order=i))

    # ------------------------------------------------------------- diagnoses
    diagnoses = [
        ("44054006", "Diabetes mellitus type 2", "E11.9", "Type 2 diabetes mellitus without complications"),
        ("38341003", "Hypertensive disorder", "I10", "Essential (primary) hypertension"),
        ("195967001", "Asthma", "J45", "Asthma"),
        ("13645005", "Chronic obstructive lung disease", "J44", "Other chronic obstructive pulmonary disease"),
        ("233604007", "Pneumonia", "J18", "Pneumonia, unspecified organism"),
        ("235595009", "Gastroesophageal reflux disease", "K21", "Gastro-oesophageal reflux disease"),
        ("82272006", "Common cold", "J00", "Acute nasopharyngitis [common cold]"),
        ("61462000", "Malaria", "B54", "Unspecified malaria"),
        ("38362002", "Dengue", "A90", "Dengue fever [classical dengue]"),
        ("68566005", "Urinary tract infectious disease", "N39.0", "Urinary tract infection, site not specified"),
        ("22298006", "Myocardial infarction", "I21.9", "Acute myocardial infarction, unspecified"),
        ("230690007", "Cerebrovascular accident", "I64", "Stroke, not specified as haemorrhage or infarction"),
        ("396275006", "Osteoarthritis", "M19.9", "Arthrosis, unspecified"),
        ("69896004", "Rheumatoid arthritis", "M06.9", "Rheumatoid arthritis, unspecified"),
        ("25374005", "Gastroenteritis", "A09", "Diarrhoea and gastroenteritis of presumed infectious origin"),
        ("74400008", "Appendicitis", "K37", "Unspecified appendicitis"),
        ("271737000", "Anemia", "D64.9", "Anaemia, unspecified"),
        ("197480006", "Anxiety disorder", "F41.9", "Anxiety disorder, unspecified"),
        ("35489007", "Depressive disorder", "F32.9", "Depressive episode, unspecified"),
        ("40930008", "Hypothyroidism", "E03.9", "Hypothyroidism, unspecified"),
    ]
    for i, (c, d, ic, idisp) in enumerate(diagnoses):
        rows.append(_t("diagnosis", c, d, alt=(ICD10, ic, idisp), order=i))

    # ---------------------------------------------------------------- vitals
    vitals = [
        ("8310-5", "Body temperature", "386725007", "Body temperature", "Cel", 36.1, 37.2),
        ("8867-4", "Heart rate", "364075005", "Heart rate", "/min", 60, 100),
        ("9279-1", "Respiratory rate", "86290005", "Respiratory rate", "/min", 12, 20),
        ("8480-6", "Systolic blood pressure", "271649006", "Systolic blood pressure", "mm[Hg], ", 90, 130),
        ("8462-4", "Diastolic blood pressure", "271650005", "Diastolic blood pressure", "mm[Hg]", 60, 85),
        ("59408-5", "Oxygen saturation in Arterial blood by Pulse oximetry", "442476006", "Arterial oxygen saturation", "%", 95, 100),
        ("29463-7", "Body weight", "27113001", "Body weight", "kg", None, None),
        ("8302-2", "Body height", "50373000", "Body height", "cm", None, None),
        ("39156-5", "Body mass index (BMI) [Ratio]", "60621009", "Body mass index", "kg/m2", 18.5, 24.9),
    ]
    for i, (lc, ld, sc, sd, unit, lo, hi) in enumerate(vitals):
        rows.append(_t("vital", lc, ld, system=LOINC, alt=(SNOMED, sc, sd),
                       unit=unit.strip().rstrip(","), low=lo, high=hi, order=i))

    # ------------------------------------------------------------- lab panels
    # extra = comma separated LOINC codes of the analytes that belong to the panel
    panels = [
        ("58410-2", "CBC panel - Blood by Automated count", "Haematology",
         "718-7,4544-3,789-8,6690-2,777-3,787-2,785-6,786-4", "119297000", "Blood specimen", 350),
        ("24331-1", "Lipid 1996 panel - Serum or Plasma", "Biochemistry",
         "2093-3,2571-8,2085-9,13457-7", "119364003", "Serum specimen", 700),
        ("24325-3", "Hepatic function 2000 panel - Serum or Plasma", "Biochemistry",
         "1975-2,1968-7,1920-8,1742-6,6768-6,2885-2,1751-7", "119364003", "Serum specimen", 650),
        ("24362-6", "Renal function 2000 panel - Serum or Plasma", "Biochemistry",
         "2160-0,3094-0,2951-2,2823-3,2075-0", "119364003", "Serum specimen", 600),
        ("4548-4", "Hemoglobin A1c/Hemoglobin.total in Blood", "Biochemistry",
         "4548-4", "119297000", "Blood specimen", 450),
        ("2345-7", "Glucose [Mass/volume] in Serum or Plasma", "Biochemistry",
         "2345-7", "119364003", "Serum specimen", 120),
        ("3016-3", "Thyrotropin [Units/volume] in Serum or Plasma", "Biochemistry",
         "3016-3,3026-2,3051-0", "119364003", "Serum specimen", 550),
        ("24356-8", "Urinalysis complete panel - Urine", "Clinical pathology",
         "5811-5,5803-2,5792-7,5804-0,5794-3,5799-2", "122575003", "Urine specimen", 250),
    ]
    for i, (code, disp, cat, analytes, spec, spec_d, price) in enumerate(panels):
        rows.append(_t("lab_panel", code, disp, system=LOINC,
                       alt=(SNOMED, spec, spec_d),
                       extra=f"{cat}|{analytes}|{price}", order=i))

    # ----------------------------------------------------------- lab analytes
    analytes = [
        ("718-7", "Hemoglobin [Mass/volume] in Blood", "g/dL", 13.0, 17.0),
        ("4544-3", "Hematocrit [Volume Fraction] of Blood by Automated count", "%", 40.0, 50.0),
        ("789-8", "Erythrocytes [#/volume] in Blood by Automated count", "10*6/uL", 4.5, 5.9),
        ("6690-2", "Leukocytes [#/volume] in Blood by Automated count", "10*3/uL", 4.0, 11.0),
        ("777-3", "Platelets [#/volume] in Blood by Automated count", "10*3/uL", 150.0, 450.0),
        ("787-2", "MCV [Entitic volume] by Automated count", "fL", 80.0, 100.0),
        ("785-6", "MCH [Entitic mass] by Automated count", "pg", 27.0, 33.0),
        ("786-4", "MCHC [Mass/volume] by Automated count", "g/dL", 32.0, 36.0),
        ("2093-3", "Cholesterol [Mass/volume] in Serum or Plasma", "mg/dL", 0.0, 200.0),
        ("2571-8", "Triglyceride [Mass/volume] in Serum or Plasma", "mg/dL", 0.0, 150.0),
        ("2085-9", "Cholesterol in HDL [Mass/volume] in Serum or Plasma", "mg/dL", 40.0, 60.0),
        ("13457-7", "Cholesterol in LDL [Mass/volume] in Serum or Plasma by calculation", "mg/dL", 0.0, 100.0),
        ("1975-2", "Bilirubin.total [Mass/volume] in Serum or Plasma", "mg/dL", 0.2, 1.2),
        ("1968-7", "Bilirubin.direct [Mass/volume] in Serum or Plasma", "mg/dL", 0.0, 0.3),
        ("1920-8", "Aspartate aminotransferase [Enzymatic activity/volume] in Serum or Plasma", "U/L", 5.0, 40.0),
        ("1742-6", "Alanine aminotransferase [Enzymatic activity/volume] in Serum or Plasma", "U/L", 7.0, 56.0),
        ("6768-6", "Alkaline phosphatase [Enzymatic activity/volume] in Serum or Plasma", "U/L", 44.0, 147.0),
        ("2885-2", "Protein [Mass/volume] in Serum or Plasma", "g/dL", 6.0, 8.3),
        ("1751-7", "Albumin [Mass/volume] in Serum or Plasma", "g/dL", 3.5, 5.0),
        ("2160-0", "Creatinine [Mass/volume] in Serum or Plasma", "mg/dL", 0.6, 1.3),
        ("3094-0", "Urea nitrogen [Mass/volume] in Serum or Plasma", "mg/dL", 7.0, 20.0),
        ("2951-2", "Sodium [Moles/volume] in Serum or Plasma", "mmol/L", 135.0, 145.0),
        ("2823-3", "Potassium [Moles/volume] in Serum or Plasma", "mmol/L", 3.5, 5.1),
        ("2075-0", "Chloride [Moles/volume] in Serum or Plasma", "mmol/L", 98.0, 107.0),
        ("2345-7", "Glucose [Mass/volume] in Serum or Plasma", "mg/dL", 70.0, 100.0),
        ("4548-4", "Hemoglobin A1c/Hemoglobin.total in Blood", "%", 4.0, 5.6),
        ("3026-2", "Thyroxine (T4) free [Mass/volume] in Serum or Plasma", "ng/dL", 0.8, 1.8),
        ("3051-0", "Triiodothyronine (T3) free [Mass/volume] in Serum or Plasma", "pg/mL", 2.3, 4.2),
        ("5811-5", "Specific gravity of Urine by Test strip", "{ratio}", 1.005, 1.03),
        ("5803-2", "pH of Urine by Test strip", "pH", 4.5, 8.0),
        ("5792-7", "Glucose [Mass/volume] in Urine by Test strip", "mg/dL", 0.0, 15.0),
        ("5804-0", "Protein [Mass/volume] in Urine by Test strip", "mg/dL", 0.0, 20.0),
        ("5794-3", "Ketones [Mass/volume] in Urine by Test strip", "mg/dL", 0.0, 5.0),
        ("5799-2", "Leukocyte esterase [Presence] in Urine by Test strip", "{presence}", None, None),
    ]
    for i, (c, d, u, lo, hi) in enumerate(analytes):
        rows.append(_t("lab_analyte", c, d, system=LOINC, unit=u, low=lo, high=hi, order=i))

    # ------------------------------------------------------------- medicines
    medicines = [
        ("387517004", "Paracetamol", "500 mg tablet"),
        ("387207008", "Ibuprofen", "400 mg tablet"),
        ("372687004", "Amoxicillin", "500 mg capsule"),
        ("387531004", "Azithromycin", "500 mg tablet"),
        ("387137007", "Omeprazole", "20 mg capsule"),
        ("372567009", "Metformin", "500 mg tablet"),
        ("386864001", "Amlodipine", "5 mg tablet"),
        ("373444002", "Atorvastatin", "10 mg tablet"),
        ("372897005", "Salbutamol", "100 mcg inhaler"),
        ("387458008", "Aspirin", "75 mg tablet"),
        ("372487007", "Ondansetron", "4 mg tablet"),
        ("7034005", "Diclofenac", "50 mg tablet"),
    ]
    for i, (c, d, form) in enumerate(medicines):
        rows.append(_t("medicine", c, d, extra=form, order=i))

    for i, (c, d) in enumerate([
        ("26643006", "Oral route"), ("47625008", "Intravenous route"),
        ("78421000", "Intramuscular route"), ("34206005", "Subcutaneous route"),
        ("6064005", "Topical route"), ("447694001", "Respiratory tract route"),
        ("37161004", "Rectal route"), ("54471007", "Buccal route"),
    ]):
        rows.append(_t("route", c, d, order=i))

    for i, (c, d) in enumerate([
        ("419652001", "Take"), ("422145002", "Inject"),
        ("421521009", "Swallow"), ("417924000", "Apply"),
    ]):
        rows.append(_t("method", c, d, order=i))

    for i, (c, d) in enumerate([
        ("311504000", "With or after food"), ("311501008", "Before food"),
        ("307165006", "At bedtime"), ("419652001", "Take as directed"),
    ]):
        rows.append(_t("dose_instruction", c, d, order=i))

    # -------------------------------------------------------------- allergies
    # AllergyIntolerance.code is 1..1 in the NRCES profile and its coding system
    # is pinned to SNOMED CT, so the picker only offers SNOMED concepts (an
    # operator can still record a text-only allergen).
    # extra = default category for the allergen
    allergens = [
        ("91936005", "Allergy to penicillin", "medication"),
        ("293585002", "Allergy to sulfonamide", "medication"),
        ("293761005", "Allergy to acetylsalicylic acid", "medication"),
        ("419199007", "Allergy to substance", "environment"),
        ("91935009", "Allergy to peanuts", "food"),
        ("91930004", "Allergy to eggs", "food"),
        ("300913006", "Shellfish allergy", "food"),
        ("425525006", "Allergy to lactose", "food"),
        ("300916003", "Allergy to latex", "environment"),
        ("418689008", "Allergy to grass pollen", "environment"),
        ("232347008", "Dust mite allergy", "environment"),
        ("232346004", "Animal dander allergy", "environment"),
    ]
    for i, (c, d, cat) in enumerate(allergens):
        rows.append(_t("allergen", c, d, extra=cat, order=i))

    for i, (c, d) in enumerate([
        ("271807003", "Eruption of skin"), ("126485001", "Urticaria"),
        ("418363000", "Itching of skin"), ("65124004", "Swelling"),
        ("267036007", "Dyspnea"), ("422587007", "Nausea"),
        ("422400008", "Vomiting"), ("62315008", "Diarrhea"),
        ("39579001", "Anaphylaxis"), ("4386001", "Bronchospasm"),
    ]):
        rows.append(_t("reaction", c, d, order=i))

    # --------------------------------------------------------------- wellness
    # Every code below is a member of the ValueSet the matching NRCES
    # Observation profile binds to, so the WellnessRecord sections are coded
    # from the published sets rather than hand-picked.
    #   kind, LOINC/SNOMED code, display, unit, low, high
    wellness = {
        "wellness_vital": [                       # ndhm-vital-signs
            ("61008-9", "Body surface temperature", "Cel", 36.1, 37.2),
            ("9279-1", "Respiratory rate", "/min", 12, 20),
            ("8867-4", "Heart rate", "/min", 60, 100),
            ("2708-6", "Oxygen saturation in Arterial blood", "%", 95, 100),
            ("85354-9", "Blood pressure panel with all children optional", "mm[Hg]",
             None, None),
        ],
        "wellness_body": [                        # ndhm-body-measurement
            ("29463-7", "Body weight", "kg", None, None),
            ("8302-2", "Body height", "cm", None, None),
            ("39156-5", "Body mass index (BMI) [Ratio]", "kg/m2", 18.5, 24.9),
            ("8280-0", "Waist Circumference at umbilicus by Tape measure", "cm",
             None, None),
            ("56074-8", "Circumference Neck", "cm", None, None),
            ("56072-2", "Circumference Mid upper arm - right", "cm", None, None),
        ],
        "wellness_activity": [                    # ndhm-physical-activity
            ("55423-8", "Number of steps in unspecified time Pedometer", "{steps}",
             5000, None),
            ("93832-4", "Sleep duration", "h", 7, 9),
            ("41981-2", "Calories burned", "kcal", None, None),
            ("80493-0", "Activity level [Acceleration]", None, None, None),
        ],
        "wellness_assessment": [                  # ndhm-general-assessment
            ("2339-0", "Glucose [Mass/volume] in Blood", "mg/dL", 70, 140),
            ("41604-0", "Fasting glucose [Mass/volume] in Capillary blood by "
                        "Glucometer", "mg/dL", 70, 100),
            ("14743-9", "Glucose [Moles/volume] in Capillary blood by Glucometer",
             "mmol/L", 3.9, 7.8),
            ("14760-3", "Glucose [Moles/volume] in Capillary blood --2 hours post "
                        "meal", "mmol/L", None, 7.8),
            ("73708-0", "Body fat [Mass] Calculated", "kg", None, None),
            ("8999-5", "Fluid intake oral Estimated", "mL", 2000, None),
            ("9052-2", "Calorie intake total", "kcal", None, None),
            ("69429-9", "Metabolic rate --resting", "kcal/d", None, None),
            ("94122-9", "Oxygen consumption (VO2)/Body weight [Volume Rate Content] "
                        "--peak during exercise", "mL/(kg.min)", None, None),
            ("34534-8", "12 lead EKG panel", None, None, None),
        ],
        "wellness_women": [                       # ndhm-women-health
            ("8665-2", "Last menstrual period start date", None, None, None),
            ("11976-8", "Ovulation date", None, None, None),
            ("92656-8", "Number of menstrual periods per year", "{count}", None, None),
            ("42798-9", "Age at menarche", "a", None, None),
            ("42802-9", "Age at menopause", "a", None, None),
        ],
    }
    for kind, entries in wellness.items():
        for i, (code, display, unit, low, high) in enumerate(entries):
            rows.append(_t(kind, code, display, system=LOINC, unit=unit,
                           low=low, high=high, order=i))
    # The two General Assessment concepts that take a coded rather than numeric
    # value. Observation.value[x] carries no binding in the profile, so the
    # answer lists are local.
    rows.append(_t("wellness_assessment", "8693-4", "Mental status", system=LOINC,
                   extra="wellness_mental", order=len(wellness["wellness_assessment"])))
    rows.append(_t("wellness_assessment", "365275006", "General well-being finding",
                   extra="wellness_wellbeing",
                   order=len(wellness["wellness_assessment"]) + 1))
    for i, d in enumerate(["Good", "Fair", "Poor"]):
        rows.append(_t("wellness_wellbeing", d.lower(), d,
                       system=f"{LOCAL_CS}/wellbeing", order=i))
    for i, d in enumerate(["Alert and oriented", "Mildly impaired",
                           "Confused", "Not assessable"]):
        rows.append(_t("wellness_mental", d.lower().replace(" ", "-"), d,
                       system=f"{LOCAL_CS}/mental-status", order=i))

    # ndhm-lifestyle: the code is the topic, the value is a coded finding.
    # extra = the `kind` holding that topic's permitted answers.
    for i, (code, display, answers) in enumerate([
        ("365981007", "Finding of tobacco smoking behavior", "lifestyle_smoking"),
        ("228273003", "Finding relating to alcohol drinking behavior",
         "lifestyle_alcohol"),
        ("228509002", "Finding relating to tobacco chewing", "lifestyle_yesno"),
        ("41829006", "Finding relating to tobacco chewing (habit)", "lifestyle_yesno"),
    ]):
        rows.append(_t("wellness_lifestyle", code, display, extra=answers, order=i))

    for kind, answers in {
        "lifestyle_smoking": [("266919005", "Never smoked tobacco"),
                              ("77176002", "Smoker"), ("8517006", "Ex-smoker")],
        "lifestyle_alcohol": [("105542008", "Non-drinker of alcohol"),
                              ("219006", "Current drinker of alcohol"),
                              ("82581004", "Ex-drinker")],
        "lifestyle_yesno": [("373067005", "No"), ("373066001", "Yes")],
    }.items():
        for i, (code, display) in enumerate(answers):
            rows.append(_t(kind, code, display, order=i))

    # --------------------------------------------------------------- dialysis
    for i, (c, d) in enumerate([
        ("302497006", "Hemodialysis"),
        ("233575001", "Hemodiafiltration"),
        ("71192002", "Peritoneal dialysis"),
        ("265764009", "Renal dialysis"),
    ]):
        rows.append(_t("dialysis_modality", c, d, order=i))

    # Vascular access types are kept in a local code system: the author is not
    # confident enough in the SNOMED concept ids to assert them as clinical codes.
    for i, (c, d) in enumerate([
        ("av-fistula", "Arteriovenous fistula"),
        ("av-graft", "Arteriovenous graft"),
        ("tunnelled-catheter", "Tunnelled central venous catheter"),
        ("temp-catheter", "Temporary central venous catheter"),
        ("peritoneal-catheter", "Peritoneal catheter"),
    ]):
        rows.append(_t("vascular_access", c, d,
                       system=f"{LOCAL_CS}/vascular-access", order=i))

    for i, (c, d) in enumerate([
        ("45007003", "Hypotension"), ("45352006", "Cramp"),
        ("422587007", "Nausea"), ("422400008", "Vomiting"),
        ("25064002", "Headache"), ("43724002", "Chill"),
        ("29857009", "Chest pain"), ("386661006", "Fever"),
        ("698247007", "Cardiac arrhythmia"), ("302866003", "Hypoglycemia"),
    ]):
        rows.append(_t("dialysis_complication", c, d, order=i))
    for i, (c, d) in enumerate([
        ("circuit-clotting", "Clotting of the extracorporeal circuit"),
        ("access-bleeding", "Bleeding from the vascular access"),
        ("access-failure", "Poor access flow / needling failure"),
        ("machine-alarm", "Machine fault or repeated alarms"),
    ], start=10):
        rows.append(_t("dialysis_complication", c, d,
                       system=f"{LOCAL_CS}/dialysis-complication", order=i))

    rows.append(_t("anticoagulant", "372877000", "Heparin", order=0))
    for i, (c, d) in enumerate([
        ("lmwh", "Low molecular weight heparin"),
        ("citrate", "Regional citrate"),
        ("none", "Heparin-free (saline flushes)"),
    ], start=1):
        rows.append(_t("anticoagulant", c, d,
                       system=f"{LOCAL_CS}/anticoagulant", order=i))

    for i, d in enumerate(["F6 HPS (1.3 m²)", "F7 HPS (1.6 m²)", "F8 HPS (1.8 m²)",
                           "High-flux 1.6 m²", "High-flux 1.8 m²", "Polysulfone 2.0 m²"]):
        rows.append(_t("dialyser", d.split()[0].lower() + str(i), d,
                       system=f"{LOCAL_CS}/dialyser", order=i))

    # -------------------------------------------------------------- specimens
    for i, (c, d) in enumerate([
        ("119297000", "Blood specimen"), ("119364003", "Serum specimen"),
        ("122575003", "Urine specimen"), ("119334006", "Sputum specimen"),
        ("119339001", "Stool specimen"), ("258580003", "Whole blood sample"),
    ]):
        rows.append(_t("specimen", c, d, order=i))

    # ------------------------------------------------------------- procedures
    for i, (c, d) in enumerate([
        ("80146002", "Appendectomy"), ("11466000", "Cesarean section"),
        ("232717009", "Coronary artery bypass graft"), ("265764009", "Renal dialysis"),
        ("116859006", "Transfusion of blood product"), ("387713003", "Surgical procedure"),
        ("103693007", "Diagnostic procedure"), ("277132007", "Therapeutic procedure"),
        ("71388002", "Procedure"), ("18286008", "Incision and drainage"),
    ]):
        rows.append(_t("procedure", c, d, order=i))

    for i, (c, d) in enumerate([
        ("387713003", "Surgical procedure"), ("103693007", "Diagnostic procedure"),
        ("277132007", "Therapeutic procedure"),
    ]):
        rows.append(_t("procedure_category", c, d, order=i))

    for i, (c, d) in enumerate([
        ("385669000", "Successful"), ("385670004", "Partially successful"),
        ("385671000", "Unsuccessful"),
    ]):
        rows.append(_t("procedure_outcome", c, d, order=i))

    # ----------------------------------------------------------- encounter md
    for i, (c, d) in enumerate([
        ("11429006", "Consultation"), ("270427003", "Patient-initiated encounter"),
        ("185349003", "Encounter for check up"), ("448337001", "Telemedicine consultation"),
        ("32485007", "Hospital admission"), ("183452005", "Emergency hospital admission"),
        ("308335008", "Patient encounter procedure"),
    ]):
        rows.append(_t("encounter_type", c, d, order=i))

    for i, (c, d) in enumerate([
        ("394802001", "General medicine"), ("394579002", "Cardiology"),
        ("394608008", "General pediatrics"), ("394801008", "Trauma and orthopedics"),
        ("394586005", "Gynecology"), ("394582007", "Dermatology"),
        ("394576009", "Accident and emergency"), ("394584008", "Gastroenterology"),
        ("394589003", "Nephrology"), ("394814009", "General practice"),
    ]):
        rows.append(_t("service_type", c, d, order=i))

    prio = "http://terminology.hl7.org/CodeSystem/v3-ActPriority"
    for i, (c, d) in enumerate([
        ("R", "routine"), ("UR", "urgent"), ("EM", "emergency"), ("EL", "elective"),
    ]):
        rows.append(_t("priority", c, d, system=prio, order=i))

    for i, (c, d) in enumerate([
        ("home", "Home"), ("alt-home", "Alternative home"),
        ("other-hcf", "Other healthcare facility"), ("hosp", "Hospice"),
        ("long", "Long-term care"), ("aadvice", "Left against advice"),
        ("exp", "Expired"), ("rehab", "Rehabilitation"), ("oth", "Other"),
    ]):
        rows.append(_t("discharge_disposition", c, d, system=DISCHARGE_DISPOSITION, order=i))

    for i, d in enumerate(["Elective", "Emergency", "Transfer", "Newborn", "Day care"]):
        rows.append(_t("admission_type", d.lower().replace(" ", "-"), d,
                       system=f"{LOCAL_CS}/admission-type", order=i))

    for i, (c, d) in enumerate([
        ("M", "Married"), ("S", "Never Married"), ("W", "Widowed"),
        ("D", "Divorced"), ("U", "unmarried"),
    ]):
        rows.append(_t("marital_status", c, d, system=V3_MARITAL, order=i))

    for i, g in enumerate(["A+", "A-", "B+", "B-", "AB+", "AB-", "O+", "O-"]):
        rows.append(_t("blood_group", g, g, system=f"{LOCAL_CS}/blood-group", order=i))

    for i, d in enumerate([
        "General Medicine", "Cardiology", "Pediatrics", "Orthopedics",
        "Obstetrics & Gynaecology", "Dermatology", "Emergency", "Gastroenterology",
        "Nephrology", "General Surgery",
    ]):
        rows.append(_t("department", d.lower().replace(" ", "-").replace("&", "and"), d,
                       system=f"{LOCAL_CS}/department", order=i))

    wards = [
        ("gen-male", "General Ward (Male)", 1500), ("gen-female", "General Ward (Female)", 1500),
        ("semi-private", "Semi Private", 3000), ("private", "Private Room", 5000),
        ("icu", "Intensive Care Unit", 12000), ("hdu", "High Dependency Unit", 8000),
        ("maternity", "Maternity Ward", 3500),
    ]
    for i, (c, d, rate) in enumerate(wards):
        rows.append(_t("ward", c, d, system=f"{LOCAL_CS}/ward", extra=str(rate), order=i))

    charges = [
        ("CONS-GEN", "General consultation", 500, "00"),
        ("CONS-SPL", "Specialist consultation", 900, "00"),
        ("CONS-FUP", "Follow-up consultation", 300, "00"),
        ("REG-OPD", "OPD registration charge", 100, "03"),
        ("BED-DAY", "Bed charges (per day)", 1500, "02"),
        ("NURS-DAY", "Nursing charges (per day)", 600, "02"),
        ("OT-MINOR", "Minor OT charges", 4500, "02"),
        ("OT-MAJOR", "Major OT charges", 18000, "02"),
        ("LAB-TEST", "Laboratory investigation", 0, "99"),
        ("DIAL-HD", "Haemodialysis session", 2200, "99"),
        ("PHARM", "Pharmacy / consumables", 0, "01"),
        ("RAD-XRAY", "X-Ray", 450, "99"),
        ("RAD-USG", "Ultrasonography", 1200, "99"),
        ("PROC-DRESS", "Dressing / minor procedure", 350, "99"),
        ("AMB", "Ambulance charges", 900, "99"),
        ("MISC", "Miscellaneous", 0, "99"),
    ]
    for i, (c, d, price, itype) in enumerate(charges):
        rows.append(_t("charge", c, d, system=f"{LOCAL_CS}/charge-codes",
                       extra=f"{price}|{itype}", order=i))

    return rows


def seed_terminology() -> None:
    db.executemany(
        "INSERT OR IGNORE INTO terminology "
        "(kind, system, code, display, alt_system, alt_code, alt_display, "
        " unit, ref_low, ref_high, extra, sort_order) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
        terminology_rows(),
    )


def seed_organization() -> None:
    if db.scalar("SELECT COUNT(*) FROM organization", default=0):
        return
    db.insert("organization", {
        "name": "KyroCare Multispeciality Hospital",
        "identifier_type_system": NDHM_IDENTIFIER_TYPE,
        "identifier_type_code": "NHRR",
        "identifier_type_display": "National Health Resource Repository (NHRR) ID",
        "identifier_system": "https://facility.abdm.gov.in",
        "identifier_value": "IN2710000123",
        "type_code": "prov",
        "type_display": "Healthcare Provider",
        "phone": "+914442228888",
        "email": "records@kyrocare.example.in",
        "address_line": "142, Anna Salai, Teynampet",
        "city": "Chennai",
        "district": "Chennai",
        "state": "Tamil Nadu",
        "postal_code": "600018",
        "country": "India",
        "gstin": "33AABCK1234M1ZQ",
        "is_default": 1,
    })


PRACTITIONERS = [
    ("Dr. Ananya Rao", "female", "HPID", "71-2233-4455-6677", "MBBS, MD (General Medicine)",
     "General Medicine", "158965000", "Medical practitioner", "394802001", "General medicine", 700),
    ("Dr. Vikram Menon", "male", "HPID", "71-3344-5566-7788", "MBBS, MD, DM (Cardiology)",
     "Cardiology", "158965000", "Medical practitioner", "394579002", "Cardiology", 1200),
    ("Dr. Priya Sharma", "female", "HPID", "71-4455-6677-8899", "MBBS, MS (Obstetrics & Gynaecology)",
     "Obstetrics & Gynaecology", "158965000", "Medical practitioner", "394586005", "Gynecology", 900),
    ("Dr. Rahul Iyer", "male", "HPID", "71-5566-7788-9900", "MBBS, MS (Orthopaedics)",
     "Orthopedics", "158965000", "Medical practitioner", "394801008", "Trauma and orthopedics", 900),
    ("Dr. Meera Krishnan", "female", "HPID", "71-6677-8899-0011", "MBBS, MD (Pediatrics)",
     "Pediatrics", "158965000", "Medical practitioner", "394608008", "General pediatrics", 800),
    ("Dr. Sameer Gupta", "male", "HPID", "71-7788-9900-1122", "MBBS, MD (Pathology)",
     "Laboratory", "159285003", "Pathologist", "394915009", "Pathology", 0),
]


def seed_practitioners() -> None:
    if db.scalar("SELECT COUNT(*) FROM practitioner", default=0):
        return
    for name, gender, id_type, id_val, qual, dept, role_c, role_d, spec_c, spec_d, fee in PRACTITIONERS:
        db.insert("practitioner", {
            "name": name,
            "prefix": "Dr",
            "gender": gender,
            "identifier_type_system": NDHM_IDENTIFIER_TYPE,
            "identifier_type_code": id_type,
            "identifier_type_display": "Healthcare Professional ID (HPID)",
            "identifier_system": "https://doctor.abdm.gov.in",
            "identifier_value": id_val,
            "qualification_code": "BS",
            "qualification_display": qual,
            "qualification_system": V2_0203,
            "role_code": role_c,
            "role_display": role_d,
            "specialty_code": spec_c,
            "specialty_display": spec_d,
            "department": dept,
            "phone": "+914442228888",
            "email": name.split()[-1].lower() + "@kyrocare.example.in",
            "consultation_fee": fee,
            "active": 1,
        })


# ---------------------------------------------------------------- ward & beds
# An 18-bed single-block hospital: the shape this app is built for.
WARDS = [
    # code, name, class, tariff/day, nursing/day, gender policy, bed count, prefix
    ("GWM", "General Ward (Male)", "general", 1500, 400, "male", 6, "GM"),
    ("GWF", "General Ward (Female)", "general", 1500, 400, "female", 5, "GF"),
    ("SPR", "Semi Private", "semi-private", 3000, 600, "any", 2, "SP"),
    ("PVT", "Private Room", "private", 5000, 800, "any", 2, "PR"),
    ("ICU", "Intensive Care Unit", "icu", 12000, 2000, "any", 3, "IC"),
]


def seed_wards_and_beds() -> None:
    if db.scalar("SELECT COUNT(*) FROM ward", default=0):
        return
    for order, (code, name, klass, tariff, nursing, policy, count, prefix) in \
            enumerate(WARDS):
        ward_id = db.insert("ward", {
            "code": code, "name": name, "class": klass, "tariff": tariff,
            "nursing_rate": nursing, "gender_policy": policy, "active": 1,
            "sort_order": order,
        })
        for index in range(1, count + 1):
            db.insert("bed", {
                "ward_id": ward_id, "code": f"{prefix}-{index:02d}",
                "status": "vacant", "active": 1,
            })


# ------------------------------------------------------------------- pharmacy
# code, name, kind, form, strength, unit, snomed, hsn, mrp, cost, gst, reorder
STOCK_ITEMS = [
    ("MED001", "Paracetamol", "drug", "Tablet", "500 mg", "tablet", "387517004",
     "30049099", 2.20, 1.10, 12, 200),
    ("MED002", "Ibuprofen", "drug", "Tablet", "400 mg", "tablet", "387207008",
     "30049099", 3.40, 1.80, 12, 150),
    ("MED003", "Amoxicillin", "drug", "Capsule", "500 mg", "capsule", "372687004",
     "30042010", 9.50, 5.60, 12, 150),
    ("MED004", "Azithromycin", "drug", "Tablet", "500 mg", "tablet", "387531004",
     "30042010", 28.00, 17.00, 12, 60),
    ("MED005", "Omeprazole", "drug", "Capsule", "20 mg", "capsule", "387137007",
     "30049099", 4.60, 2.30, 12, 120),
    ("MED006", "Metformin", "drug", "Tablet", "500 mg", "tablet", "372567009",
     "30049099", 2.80, 1.30, 12, 200),
    ("MED007", "Amlodipine", "drug", "Tablet", "5 mg", "tablet", "386864001",
     "30049099", 3.10, 1.40, 12, 150),
    ("MED008", "Atorvastatin", "drug", "Tablet", "10 mg", "tablet", "373444002",
     "30049099", 7.90, 4.20, 12, 100),
    ("MED009", "Aspirin", "drug", "Tablet", "75 mg", "tablet", "387458008",
     "30049099", 1.40, 0.60, 12, 200),
    ("MED010", "Ondansetron", "drug", "Injection", "2 mg/mL", "ampoule", "372487007",
     "30049099", 18.00, 11.00, 12, 40),
    ("MED011", "Ceftriaxone", "drug", "Injection", "1 g", "vial", "372834009",
     "30042010", 62.00, 41.00, 12, 30),
    ("MED012", "Salbutamol", "drug", "Inhaler", "100 mcg", "inhaler", "372897005",
     "30049099", 185.00, 128.00, 12, 10),
    ("CON001", "IV cannula 20G", "consumable", "Cannula", "20G", "piece", None,
     "90183930", 32.00, 19.00, 12, 60),
    ("CON002", "IV set", "consumable", "Set", None, "piece", None,
     "90183930", 45.00, 27.00, 12, 40),
    ("CON003", "Normal saline 500 mL", "consumable", "IV fluid", "0.9%", "bottle",
     None, "30049099", 48.00, 30.00, 12, 60),
    ("CON004", "Ringer lactate 500 mL", "consumable", "IV fluid", None, "bottle",
     None, "30049099", 52.00, 33.00, 12, 40),
    ("CON005", "Disposable syringe 5 mL", "consumable", "Syringe", "5 mL", "piece",
     None, "90183100", 6.00, 3.20, 12, 150),
    ("CON006", "Surgical gloves (pair)", "consumable", "Gloves", "Medium", "pair",
     None, "40151900", 14.00, 8.00, 12, 200),
    ("CON007", "Sterile gauze pad", "consumable", "Dressing", None, "piece", None,
     "30051090", 7.50, 4.00, 12, 150),
    ("CON008", "Adhesive bandage roll", "consumable", "Dressing", None, "roll", None,
     "30051090", 26.00, 15.00, 12, 50),
]


def seed_stock() -> None:
    if db.scalar("SELECT COUNT(*) FROM stock_item", default=0):
        return
    for (code, name, kind, form, strength, unit, snomed, hsn, mrp, cost, gst,
         reorder) in STOCK_ITEMS:
        master = db.term("medicine", snomed) if snomed else None
        db.insert("stock_item", {
            "code": code, "name": name, "kind": kind, "form": form,
            "strength": strength, "unit": unit,
            "snomed_code": snomed,
            "snomed_display": master["display"] if master else None,
            "hsn_code": hsn, "mrp": mrp, "purchase_price": cost, "gst_pct": gst,
            "reorder_level": reorder, "active": 1,
        })


DIALYSIS_MACHINES = [
    ("HD-01", "Fresenius 4008S", "Fresenius Medical Care", "Dialysis unit"),
    ("HD-02", "Fresenius 4008S", "Fresenius Medical Care", "Dialysis unit"),
    ("HD-03", "Nipro Surdial X", "Nipro", "Dialysis unit"),
    ("HD-04", "B.Braun Dialog+", "B. Braun", "Dialysis unit"),
    ("HD-05", "Fresenius 4008S", "Fresenius Medical Care", "Isolation bay"),
]


def seed_dialysis_machines() -> None:
    if db.scalar("SELECT COUNT(*) FROM dialysis_machine", default=0):
        return
    for index, (code, model, maker, location) in enumerate(DIALYSIS_MACHINES):
        db.insert("dialysis_machine", {
            "code": code, "model": model, "manufacturer": maker,
            "serial_no": f"SN{2026000 + index}", "location": location,
            "status": "available", "last_service": "2026-07-01", "active": 1,
        })


def bootstrap() -> None:
    db.init_db()
    seed_terminology()
    seed_organization()
    seed_practitioners()
    seed_wards_and_beds()
    seed_stock()
    seed_dialysis_machines()
