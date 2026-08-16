"""Dialysis unit — courses, sessions, machines and the intra-dialytic chart.

A *course* is the standing prescription for a patient. A *session* is one run
against it on a named machine, carrying the parameters actually delivered plus
pre- and post-dialysis vitals. Completing a session writes a SNOMED-coded
`Procedure`, which is how the run reaches the discharge summary and the bill —
nothing in the FHIR layer needed to know about dialysis.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from . import db, services

MACHINE_STATUS = {
    "available": ("Available", "success"),
    "in-use": ("In use", "danger"),
    "maintenance": ("Maintenance", "warning"),
    "retired": ("Retired", ""),
}

SESSION_STATUS = {
    "planned": ("Planned", "info"),
    "in-progress": ("Running", "warning"),
    "completed": ("Completed", "success"),
    "abandoned": ("Abandoned", "danger"),
}

COURSE_STATUS = {
    "active": ("Active", "success"),
    "completed": ("Completed", "info"),
    "cancelled": ("Cancelled", "danger"),
}

# The pre/post flowsheet reuses the ordinary vitals master so the readings stay
# LOINC-coded, range-flagged and exportable.
FLOWSHEET_VITALS = ["8480-6", "8462-4", "8867-4", "8310-5", "9279-1", "59408-5"]

# SNOMED procedure recorded when a session completes.
PROCEDURE_CATEGORY = ("277132007", "Therapeutic procedure")


# ===========================================================================
# machines
# ===========================================================================
def machines(include_retired: bool = False) -> list:
    """Every machine with its current occupant, if any."""
    sql = ("SELECT m.*, s.id AS session_id, s.session_no, p.name AS patient_name, "
           "       p.id AS patient_id "
           "FROM dialysis_machine m "
           "LEFT JOIN dialysis_session s ON s.machine_id = m.id "
           "     AND s.status = 'in-progress' "
           "LEFT JOIN patient p ON p.id = s.patient_id WHERE 1 = 1")
    if not include_retired:
        sql += " AND m.active = 1 AND m.status <> 'retired'"
    return db.query(sql + " ORDER BY m.code")


def available_machines() -> list:
    """Machines free to take a patient right now."""
    return db.query(
        "SELECT * FROM dialysis_machine WHERE active = 1 AND status = 'available' "
        "ORDER BY code")


def set_machine_status(machine_id: int, status: str, note: str | None = None) -> None:
    """Change a machine's status. Refused while it is running."""
    if status not in MACHINE_STATUS:
        raise ValueError("unknown machine status")
    busy = db.one("SELECT session_no FROM dialysis_session WHERE machine_id = ? "
                  "AND status = 'in-progress'", (machine_id,))
    if busy and status != "in-use":
        raise ValueError(f'that machine is running session {busy["session_no"]}')
    patch: dict[str, Any] = {"status": status}
    if note is not None:
        patch["note"] = note or None
    db.update("dialysis_machine", machine_id, patch)


# ===========================================================================
# courses
# ===========================================================================
def create_course(values: dict[str, Any]) -> int:
    """Open a course and allocate its number."""
    values = dict(values)
    values["course_no"] = db.next_number("dialysis_course", "DLC-")
    values["created_at"] = db.now_iso()
    values.setdefault("started_on", db.today_iso())
    values.setdefault("status", "active")
    return db.insert("dialysis_course", values)


def course(course_id: int):
    """One course joined to its patient and nephrologist."""
    return db.one(
        "SELECT c.*, p.name AS patient_name, p.mrn, p.gender, p.birth_date, "
        "       p.age_years, p.phone, d.name AS doctor_name "
        "FROM dialysis_course c JOIN patient p ON p.id = c.patient_id "
        "LEFT JOIN practitioner d ON d.id = c.practitioner_id WHERE c.id = ?",
        (course_id,))


def courses(status: str = "") -> list:
    """Courses with their completed-session count and last run."""
    sql = ("SELECT c.*, p.name AS patient_name, p.mrn, d.name AS doctor_name, "
           "       (SELECT COUNT(*) FROM dialysis_session s WHERE s.course_id = c.id "
           "        AND s.status = 'completed') AS done, "
           "       (SELECT MAX(s.started_at) FROM dialysis_session s "
           "        WHERE s.course_id = c.id AND s.status = 'completed') AS last_run "
           "FROM dialysis_course c JOIN patient p ON p.id = c.patient_id "
           "LEFT JOIN practitioner d ON d.id = c.practitioner_id")
    params: list[Any] = []
    if status:
        sql += " WHERE c.status = ?"
        params.append(status)
    return db.query(sql + " ORDER BY c.status <> 'active', c.id DESC", params)


def close_course(course_id: int, status: str = "completed") -> None:
    """Close a course. Refused while one of its runs is in progress."""
    if status not in COURSE_STATUS:
        raise ValueError("unknown course status")
    running = db.one("SELECT session_no FROM dialysis_session WHERE course_id = ? "
                     "AND status = 'in-progress'", (course_id,))
    if running:
        raise ValueError(f'session {running["session_no"]} is still running')
    db.update("dialysis_course", course_id,
              {"status": status, "ended_on": db.today_iso()})


# ===========================================================================
# sessions
# ===========================================================================
def schedule_session(course_id: int, machine_id: int | None, scheduled_at: str | None,
                     practitioner_id: int | None) -> int:
    row = db.one("SELECT * FROM dialysis_course WHERE id = ?", (course_id,))
    if row is None:
        raise ValueError("unknown dialysis course")
    if row["status"] != "active":
        raise ValueError("that course is closed")
    seq = (db.scalar("SELECT COALESCE(MAX(seq), 0) FROM dialysis_session "
                     "WHERE course_id = ?", (course_id,), default=0) or 0) + 1
    # The session inherits the course prescription; the nurse edits what was
    # actually delivered on the session screen.
    return db.insert("dialysis_session", {
        "session_no": db.next_number("dialysis_session", "DLS-"),
        "course_id": course_id,
        "patient_id": row["patient_id"],
        "encounter_id": row["encounter_id"],
        "machine_id": machine_id,
        "practitioner_id": practitioner_id or row["practitioner_id"],
        "seq": seq,
        "status": "planned",
        "scheduled_at": db.to_instant(scheduled_at) or db.now_iso(),
        "duration_minutes": row["duration_minutes"],
        "access_code": row["access_code"],
        "access_display": row["access_display"],
        "dialyser": row["dialyser"],
        "blood_flow_rate": row["blood_flow_rate"],
        "dialysate_flow_rate": row["dialysate_flow_rate"],
        "dialysate_na": row["dialysate_na"],
        "dialysate_k": row["dialysate_k"],
        "dialysate_ca": row["dialysate_ca"],
        "dialysate_bicarb": row["dialysate_bicarb"],
        "anticoagulant_code": row["anticoagulant_code"],
        "anticoagulant_display": row["anticoagulant_display"],
        "heparin_bolus_units": row["heparin_bolus_units"],
        "heparin_hourly_units": row["heparin_hourly_units"],
        "dry_weight_kg": row["dry_weight_kg"],
        "created_at": db.now_iso(),
    })


def session(session_id: int):
    """One session joined to its course, patient, machine and doctor."""
    return db.one(
        "SELECT s.*, c.course_no, c.modality_display, c.sessions_per_week, "
        "       c.session_charge, c.duration_minutes AS prescribed_minutes, "
        "       p.name AS patient_name, p.mrn, p.gender, p.birth_date, p.age_years, "
        "       p.phone, m.code AS machine_code, m.model AS machine_model, "
        "       d.name AS doctor_name "
        "FROM dialysis_session s JOIN dialysis_course c ON c.id = s.course_id "
        "JOIN patient p ON p.id = s.patient_id "
        "LEFT JOIN dialysis_machine m ON m.id = s.machine_id "
        "LEFT JOIN practitioner d ON d.id = s.practitioner_id WHERE s.id = ?",
        (session_id,))


def sessions_for_course(course_id: int) -> list:
    """Every session of one course, newest first."""
    return db.query(
        "SELECT s.*, m.code AS machine_code FROM dialysis_session s "
        "LEFT JOIN dialysis_machine m ON m.id = s.machine_id "
        "WHERE s.course_id = ? ORDER BY s.seq DESC", (course_id,))


def sessions_on(day: str) -> list:
    """Every session scheduled on one day."""
    return db.query(
        "SELECT s.*, c.course_no, c.modality_display, p.name AS patient_name, p.mrn, "
        "       m.code AS machine_code FROM dialysis_session s "
        "JOIN dialysis_course c ON c.id = s.course_id "
        "JOIN patient p ON p.id = s.patient_id "
        "LEFT JOIN dialysis_machine m ON m.id = s.machine_id "
        "WHERE date(s.scheduled_at) = ? ORDER BY s.scheduled_at, s.id", (day,))


def start_session(session_id: int, machine_id: int | None = None) -> None:
    """Begin a run, claiming the machine for the duration."""
    row = db.one("SELECT * FROM dialysis_session WHERE id = ?", (session_id,))
    if row is None:
        raise ValueError("unknown session")
    if row["status"] == "completed":
        raise ValueError("that session is already completed")
    machine_id = machine_id or row["machine_id"]
    if not machine_id:
        raise ValueError("assign a machine before starting the session")
    machine = db.one("SELECT * FROM dialysis_machine WHERE id = ?", (machine_id,))
    if machine is None:
        raise ValueError("unknown machine")
    if machine["status"] == "maintenance":
        raise ValueError(f'machine {machine["code"]} is under maintenance')
    clash = db.one("SELECT session_no FROM dialysis_session WHERE machine_id = ? "
                   "AND status = 'in-progress' AND id <> ?", (machine_id, session_id))
    if clash:
        raise ValueError(f'machine {machine["code"]} is running '
                         f'session {clash["session_no"]}')
    db.update("dialysis_session", session_id, {
        "status": "in-progress", "machine_id": machine_id,
        "started_at": db.now_iso()})
    db.update("dialysis_machine", machine_id, {"status": "in-use"})


def save_parameters(session_id: int, values: dict[str, Any]) -> None:
    """Write the delivered parameters.

    Every key given is written, `None` included, so a field cleared on the form
    is actually cleared. Ultrafiltration is derived from the weights — which may
    arrive in this call or already be on the row — unless the unit typed a figure
    of its own.
    """
    row = db.one("SELECT * FROM dialysis_session WHERE id = ?", (session_id,))
    if row is None:
        raise ValueError("unknown session")
    values = dict(values)
    pre = values.get("pre_weight_kg", row["pre_weight_kg"])
    post = values.get("post_weight_kg", row["post_weight_kg"])
    if not values.get("uf_achieved_ml") and pre and post:
        # Fluid removed is the weight the patient actually lost, in millilitres.
        values["uf_achieved_ml"] = round(max(0.0, pre - post) * 1000)
    db.update("dialysis_session", session_id, values)


def complete_session(session_id: int, abandoned: bool = False) -> None:
    """End a run, free the machine and record the derived artefacts."""
    row = session(session_id)
    if row is None:
        raise ValueError("unknown session")
    ended = db.now_iso()
    duration = row["duration_minutes"]
    if row["started_at"]:
        try:
            delta = (datetime.fromisoformat(ended)
                     - datetime.fromisoformat(db.to_instant(row["started_at"])))
            duration = max(0, int(delta.total_seconds() // 60)) or duration
        except (TypeError, ValueError):
            pass
    db.update("dialysis_session", session_id, {
        "status": "abandoned" if abandoned else "completed",
        "ended_at": ended, "duration_minutes": duration})
    if row["machine_id"]:
        db.update("dialysis_machine", row["machine_id"], {"status": "available"})
    if not abandoned:
        _record_procedure(session_id)
        _generate_wellness_record(session_id)


def _generate_wellness_record(session_id: int) -> None:
    """One wellness record per completed run, carrying the parameter set.

    Completing the run is the primary clinical fact; the wellness record is
    derived from it. A failure here must not roll the completion back or blow up
    the caller — the record is written in its own transaction, so a failure
    leaves nothing behind and the operator can regenerate it from the session
    screen.
    """
    from . import wellness

    try:
        wellness.create_from_dialysis_session(session_id)
    except Exception as error:              # noqa: BLE001 - derived, never fatal
        print(f"  ! wellness record for session {session_id} could not be "
              f"generated: {error}")


def _record_procedure(session_id: int) -> None:
    """Write the completed run as a SNOMED Procedure so it reaches the clinical
    document and the bill through the paths that already exist."""
    row = session(session_id)
    if row is None or not row["encounter_id"]:
        return
    existing = db.one("SELECT id FROM procedure WHERE dialysis_session_id = ?",
                      (session_id,))
    if existing:
        return
    course_row = db.one("SELECT * FROM dialysis_course WHERE id = ?",
                        (row["course_id"],))
    detail = [f'Session {row["seq"]} of course {row["course_no"]} '
              f'({row["session_no"]})']
    if row["machine_code"]:
        detail.append(f'machine {row["machine_code"]}')
    if row["duration_minutes"]:
        detail.append(f'{row["duration_minutes"]} minutes')
    if row["uf_achieved_ml"]:
        detail.append(f'UF {row["uf_achieved_ml"]:.0f} mL')
    if row["ktv"]:
        detail.append(f'Kt/V {row["ktv"]:g}')
    db.insert("procedure", {
        "patient_id": row["patient_id"], "encounter_id": row["encounter_id"],
        "status": "completed",
        "snomed_code": course_row["modality_code"],
        "snomed_display": course_row["modality_display"],
        "category_code": PROCEDURE_CATEGORY[0],
        "category_display": PROCEDURE_CATEGORY[1],
        # A managed intra-dialytic complication does not make the run a failure;
        # it downgrades it to partially successful.
        "outcome_code": "385670004" if row["complication_code"] else "385669000",
        "outcome_display": ("Partially successful" if row["complication_code"]
                            else "Successful"),
        "performed_ts": row["ended_at"] or db.now_iso(),
        "performer_id": row["practitioner_id"],
        "dialysis_session_id": session_id,
        "note": ", ".join(detail),
    })


# ===========================================================================
# flowsheet: pre/post vitals and the intra-dialytic chart
# ===========================================================================
def record_phase_vitals(session_id: int, phase: str, readings: dict[str, str]) -> int:
    """Replace one phase of the pre/post flowsheet."""
    if phase not in ("pre", "post"):
        raise ValueError("phase must be 'pre' or 'post'")
    row = db.one("SELECT * FROM dialysis_session WHERE id = ?", (session_id,))
    if row is None:
        raise ValueError("unknown session")
    db.execute("DELETE FROM observation WHERE dialysis_session_id = ? AND phase = ?",
               (session_id, phase))
    return services.record_vitals(
        row["patient_id"], row["encounter_id"], readings,
        note=f"{phase.title()}-dialysis, session {row['session_no']}",
        extra={"dialysis_session_id": session_id, "phase": phase})


def phase_vitals(session_id: int) -> dict[str, dict[str, Any]]:
    """A session's flowsheet as ``{'pre': {...}, 'post': {...}}``."""
    out: dict[str, dict[str, Any]] = {"pre": {}, "post": {}}
    for obs in db.query("SELECT * FROM observation WHERE dialysis_session_id = ?",
                        (session_id,)):
        if obs["phase"] in out:
            out[obs["phase"]][obs["loinc_code"]] = obs
    return out


def add_reading(session_id: int, values: dict[str, Any]) -> int:
    """Append a row to the intra-dialytic monitoring chart."""
    values = dict(values)
    values["session_id"] = session_id
    values["recorded_at"] = db.now_iso()
    values.setdefault("elapsed_minutes", 0)
    return db.insert("dialysis_reading", values)


def readings(session_id: int) -> list:
    """One session's monitoring chart in time order."""
    return db.query(
        "SELECT * FROM dialysis_reading WHERE session_id = ? "
        "ORDER BY elapsed_minutes, id", (session_id,))


def delete_reading(reading_id: int, session_id: int) -> None:
    """Remove one monitoring row."""
    db.execute("DELETE FROM dialysis_reading WHERE id = ? AND session_id = ?",
               (reading_id, session_id))


# ===========================================================================
# billing
# ===========================================================================
def unbilled_sessions(encounter_id: int) -> list:
    """Completed runs on an encounter that have not been billed."""
    return db.query(
        "SELECT s.*, c.session_charge, c.modality_display FROM dialysis_session s "
        "JOIN dialysis_course c ON c.id = s.course_id "
        "WHERE s.encounter_id = ? AND s.status = 'completed' AND s.billed = 0 "
        "ORDER BY s.seq", (encounter_id,))


def mark_sessions_billed(encounter_id: int) -> None:
    """Mark an encounter's completed runs as billed."""
    db.execute("UPDATE dialysis_session SET billed = 1 WHERE encounter_id = ? "
               "AND status = 'completed'", (encounter_id,))


def unit_stats(day: str) -> dict[str, Any]:
    """Machine counts and the day's session tallies for the unit view."""
    today = sessions_on(day)
    machine_rows = db.query("SELECT status, COUNT(*) AS n FROM dialysis_machine "
                            "WHERE active = 1 GROUP BY status")
    stats = {s: 0 for s in MACHINE_STATUS}
    for row in machine_rows:
        stats[row["status"]] = row["n"]
    return {
        "machines": stats,
        "machines_total": sum(stats.values()),
        "scheduled": len(today),
        "running": sum(1 for s in today if s["status"] == "in-progress"),
        "completed": sum(1 for s in today if s["status"] == "completed"),
        "active_courses": db.scalar(
            "SELECT COUNT(*) FROM dialysis_course WHERE status = 'active'", default=0),
    }
