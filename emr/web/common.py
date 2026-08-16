"""Shared helpers for the route modules."""

from __future__ import annotations

from typing import Any

from .. import db
from . import ui
from .router import Request, Response, html

CLINICAL_TABLES = {
    "condition": "condition",
    "observation": "observation",
    "medication_request": "medication_request",
    "procedure": "procedure",
    "service_request": "service_request",
    "allergy": "allergy",
}


def render(request: Request, title: str, active: str, body: str, **kwargs: Any) -> Response:
    """Render a page, passing the request's flash message through."""
    return html(ui.page(title, active, body, flash=request.flash(), **kwargs))


def term_options(kind: str, label_with_code: bool = True) -> list[tuple[str, str]]:
    """Select options for one terminology kind."""
    rows = db.terms(kind)
    if label_with_code:
        return [(r["code"], f"{r['display']} ({r['code']})") for r in rows]
    return [(r["code"], r["display"]) for r in rows]


def doctor_options(department: str | None = None) -> list[tuple[str, str]]:
    """Select options for the active practitioners."""
    sql = "SELECT id, name, department FROM practitioner WHERE active = 1"
    params: list[Any] = []
    if department:
        sql += " AND department = ?"
        params.append(department)
    sql += " ORDER BY name"
    return [(r["id"], f"{r['name']} — {r['department']}") for r in db.query(sql, params)]


def patient_options(limit: int = 500) -> list[tuple[str, str]]:
    """Select options for the most recently registered patients."""
    rows = db.query("SELECT id, name, mrn, phone FROM patient ORDER BY id DESC LIMIT ?",
                    (limit,))
    return [(r["id"], f"{r['name']} · {r['mrn']} · {r['phone']}") for r in rows]


def get_patient(patient_id: int) -> dict[str, Any] | None:
    """One patient as a plain dict, or ``None``."""
    row = db.one("SELECT * FROM patient WHERE id = ?", (patient_id,))
    return dict(row) if row else None


def not_found(request: Request, what: str) -> Response:
    """A rendered "not found" page for a missing record."""
    return render(request, "Not found", "dashboard",
                  ui.empty_state(f"{what} could not be found.",
                                 ui.button("Back to dashboard", "/",
                                           style="z-button-primary")))


def status_label(status: str) -> str:
    """A colour-coded chip for a status string."""
    kind = {"finished": "success", "in-progress": "info", "planned": "warning",
            "cancelled": "danger", "arrived": "info", "final": "success",
            "registered": "warning", "preliminary": "info", "issued": "info",
            "balanced": "success", "draft": "warning"}.get(status, "")
    return ui.label_chip(status.replace("-", " "), kind)


def patient_cell(row) -> str:
    return (f'<a class="z-link" href="/patients/{row["patient_id"]}">'
            f'{ui.esc(row["patient_name"])}</a>'
            f'<div class="text-xs color"{ui.st(color="var(--z-muted-f)")}>'
            f'{ui.esc(row["mrn"])}</div>')



def collect_rows(request: Request, prefix: str, fields: list[str]) -> list[dict[str, str]]:
    """Read a repeating form block posted as ``prefix_field`` arrays."""
    columns = {field: request.f_all(f"{prefix}_{field}") for field in fields}
    length = max((len(v) for v in columns.values()), default=0)
    rows = []
    for index in range(length):
        row = {field: (values[index] if index < len(values) else "")
               for field, values in columns.items()}
        if any(row.values()):
            rows.append(row)
    return rows
