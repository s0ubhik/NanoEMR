"""Ward board — where every patient physically is."""

from __future__ import annotations

from ... import db, hospital
from .. import ui
from ..common import not_found, render
from ..router import Request, redirect

st = ui.st


def register(app) -> None:
    app.add("GET", "/beds", index)
    app.add("POST", "/beds/<int:bid>/status", set_status)
    app.add("POST", "/beds/transfer/<int:eid>", transfer)


def index(request: Request):
    board = hospital.ward_board()
    stats = hospital.bed_stats()

    tiles = "".join([
        ui.stat("Total beds", stats["total"], "bed"),
        ui.stat("Occupied", stats["occupied"], "bed-single", "danger"),
        ui.stat("Vacant", stats["vacant"], "bed-double", "success"),
        ui.stat("Awaiting cleaning", stats["cleaning"], "spray-can", "warning"),
        ui.stat("Occupancy", f'{stats["occupancy_pct"]}%', "percent", "info"),
    ])

    ward_cards = []
    for group in board:
        ward = group["ward"]
        cells = []
        for bed in group["beds"]:
            label, kind = hospital.BED_STATUS[bed["status"]]
            tint = {"success": "var(--z-success)", "danger": "var(--z-danger)",
                    "warning": "var(--z-warning)"}.get(kind, "var(--z-muted-f)")
            if bed["status"] == "occupied" and bed["patient_name"]:
                inner = (
                    f'<a class="z-link" href="/ipd/{bed["encounter_id"]}">'
                    f'{ui.esc(bed["patient_name"])}</a>'
                    f'<div class="text-xs color"{st(color="var(--z-muted-f)")}>'
                    f'{ui.esc(bed["mrn"])} · since '
                    f'{ui.when(bed["period_start"], 10)}</div>'
                    + ui.muted(bed["doctor_name"] or "—"))
                actions = ui.button("Open", f'/ipd/{bed["encounter_id"]}',
                                    size="z-button-xsmall")
            else:
                inner = (f'<div class="text-sm color"{st(color="var(--z-muted-f)")}>'
                         f"{ui.esc(label)}</div>")
                actions = _status_actions(bed)
            cells.append(
                '<div class="z-card z-card-body border-w border/o"'
                + st(border_w="2px", border=tint, border_o="100%") + ">"
                + f'<div class="display-flex items-center justify-between gap"{st(gap=2)}>'
                + f'<strong>{ui.esc(bed["code"])}</strong>'
                + ui.label_chip(label, kind) + "</div>"
                + f'<div class="mt"{st(mt=2)}>{inner}</div>'
                + (f'<div class="mt"{st(mt=3)}>{actions}</div>' if actions else "")
                + "</div>")
        ward_cards.append(ui.card(
            f'{ward["name"]} — {group["occupied"]}/{len(group["beds"])} occupied',
            f'<div class="display-grid gap sm:grid-cols lg:grid-cols"'
            + st(gap=3, sm_grid_cols=2, lg_grid_cols=4) + ">" + "".join(cells) + "</div>",
            actions=(ui.label_chip(f'₹{ward["tariff"]:.0f}/day')
                     + ui.label_chip(ward["class"], "info")
                     + (ui.label_chip(ward["gender_policy"], "warning")
                        if ward["gender_policy"] != "any" else ""))))

    body = (
        f'<div class="display-grid gap sm:grid-cols lg:grid-cols"'
        + st(gap=4, sm_grid_cols=2, lg_grid_cols=5) + ">" + tiles + "</div>"
        + f'<div class="mt display-flex flex-col gap"{st(mt=5, gap=4)}>'
        + "".join(ward_cards) + "</div>")
    return render(request, "Ward board", "beds", body,
                  actions=ui.button("Admit a patient", "/ipd/new",
                                    style="z-button-primary", ico="hospital"))


def _status_actions(bed) -> str:
    buttons = []
    if bed["status"] == "cleaning":
        buttons.append(("vacant", "Mark clean", "z-button-primary"))
    elif bed["status"] == "vacant":
        buttons.append(("blocked", "Block", "z-button-secondary"))
    elif bed["status"] == "blocked":
        buttons.append(("vacant", "Release", "z-button-primary"))
    return "".join(
        ui.post_button(f'/beds/{bed["id"]}/status', label, style=style,
                       fields={"status": status})
        for status, label, style in buttons)


def set_status(request: Request):
    try:
        hospital.set_bed_status(request.params["bid"], request.f("status"))
    except ValueError as error:
        return redirect("/beds", str(error), "danger")
    return redirect("/beds", "Bed updated.")


def transfer(request: Request):
    eid = request.params["eid"]
    enc = db.one("SELECT * FROM encounter WHERE id = ?", (eid,))
    if enc is None:
        return not_found(request, "Admission")
    bed_id = request.f_int("bed_id")
    if not bed_id:
        return redirect(f"/ipd/{eid}", "Pick a bed to transfer to.", "danger")
    try:
        moved = hospital.transfer_bed(eid, bed_id, request.f("reason"))
    except ValueError as error:
        return redirect(f"/ipd/{eid}", str(error), "danger")
    return redirect(f"/ipd/{eid}",
                    f'Transferred to {moved["ward"]} / {moved["bed"]}.')
