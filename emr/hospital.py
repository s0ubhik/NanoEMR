"""Operational services for a small hospital: beds, pharmacy, money, queue.

These are the things a 10–20 bed hospital cannot run without and that the
clinical record alone does not give you — where the patients physically are,
what came out of the drug cupboard, what has actually been paid, and who is
waiting outside the consulting room.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta
from typing import Any

from . import db

BED_STATUS = {
    "vacant": ("Vacant", "success"),
    "occupied": ("Occupied", "danger"),
    "cleaning": ("Cleaning", "warning"),
    "blocked": ("Blocked", ""),
}

PAYMENT_MODES = [("cash", "Cash"), ("card", "Card"), ("upi", "UPI"),
                 ("bank", "Bank transfer"), ("insurance", "Insurance"),
                 ("other", "Other")]


# ===========================================================================
# beds
# ===========================================================================
def ward_board() -> list[dict[str, Any]]:
    """Every ward with its beds and current occupant, for the ward board."""
    wards = db.query("SELECT * FROM ward WHERE active = 1 ORDER BY sort_order, name")
    beds = db.query(
        "SELECT b.*, w.name AS ward_name, w.tariff AS ward_tariff, "
        "       w.nursing_rate, w.class AS ward_class, "
        "       e.encounter_no, e.period_start, e.patient_id, "
        "       p.name AS patient_name, p.mrn, p.gender, "
        "       d.name AS doctor_name "
        "FROM bed b JOIN ward w ON w.id = b.ward_id "
        "LEFT JOIN encounter e ON e.id = b.encounter_id AND e.status <> 'finished' "
        "LEFT JOIN patient p ON p.id = e.patient_id "
        "LEFT JOIN practitioner d ON d.id = e.practitioner_id "
        "WHERE b.active = 1 ORDER BY w.sort_order, b.code")
    out = []
    for ward in wards:
        rows = [b for b in beds if b["ward_id"] == ward["id"]]
        out.append({
            "ward": ward,
            "beds": rows,
            "occupied": sum(1 for b in rows if b["status"] == "occupied"),
            "vacant": sum(1 for b in rows if b["status"] == "vacant"),
        })
    return out


def bed_stats() -> dict[str, int]:
    """Counts by bed status plus the occupancy percentage."""
    rows = db.query("SELECT status, COUNT(*) AS n FROM bed WHERE active = 1 "
                    "GROUP BY status")
    stats = {status: 0 for status in BED_STATUS}
    for row in rows:
        stats[row["status"]] = row["n"]
    stats["total"] = sum(stats[s] for s in BED_STATUS)
    stats["occupancy_pct"] = round(
        100 * stats["occupied"] / stats["total"]) if stats["total"] else 0
    return stats


def vacant_beds(gender: str | None = None) -> list:
    """Vacant beds, respecting each ward's gender policy."""
    rows = db.query(
        "SELECT b.*, w.name AS ward_name, w.tariff AS ward_tariff, "
        "       w.nursing_rate, w.gender_policy "
        "FROM bed b JOIN ward w ON w.id = b.ward_id "
        "WHERE b.active = 1 AND b.status = 'vacant' AND w.active = 1 "
        "ORDER BY w.sort_order, b.code")
    if gender in ("male", "female"):
        rows = [r for r in rows if r["gender_policy"] in ("any", gender)]
    return rows


def bed_tariff(bed_row) -> float:
    """The nightly rate for a bed: its own override, else the ward rate."""
    return float(bed_row["tariff"] if bed_row["tariff"] is not None
                 else bed_row["ward_tariff"])


def occupy_bed(bed_id: int, encounter_id: int, when: str | None = None,
               reason: str = "Admission") -> dict[str, Any]:
    """Assign a bed and open a movement row. Refuses an already-taken bed."""
    bed = db.one(
        "SELECT b.*, w.name AS ward_name, w.tariff AS ward_tariff, w.nursing_rate "
        "FROM bed b JOIN ward w ON w.id = b.ward_id WHERE b.id = ?", (bed_id,))
    if bed is None:
        raise ValueError("that bed does not exist")
    if bed["status"] == "occupied" and bed["encounter_id"] != encounter_id:
        raise ValueError(f'bed {bed["code"]} is already occupied')
    tariff = bed_tariff(bed)
    db.insert("bed_movement", {
        "encounter_id": encounter_id, "bed_id": bed_id,
        "from_ts": db.to_instant(when) or db.now_iso(),
        "tariff": tariff, "nursing_rate": bed["nursing_rate"], "reason": reason,
    })
    db.update("bed", bed_id, {"status": "occupied", "encounter_id": encounter_id})
    db.update("encounter", encounter_id, {
        "bed_id": bed_id, "ward": bed["ward_name"], "bed": bed["code"],
        "bed_rate": tariff,
    })
    return {"ward": bed["ward_name"], "bed": bed["code"], "tariff": tariff}


def release_bed(encounter_id: int, when: str | None = None,
                new_status: str = "cleaning") -> None:
    """Close the open movement and free the bed (defaults to awaiting cleaning)."""
    stamp = db.to_instant(when) or db.now_iso()
    open_row = db.one(
        "SELECT * FROM bed_movement WHERE encounter_id = ? AND to_ts IS NULL "
        "ORDER BY id DESC LIMIT 1", (encounter_id,))
    if open_row:
        db.update("bed_movement", open_row["id"], {"to_ts": stamp})
    for bed in db.query("SELECT id FROM bed WHERE encounter_id = ?", (encounter_id,)):
        db.update("bed", bed["id"], {"status": new_status, "encounter_id": None})


def transfer_bed(encounter_id: int, new_bed_id: int, reason: str) -> dict[str, Any]:
    """Move a patient to another bed, closing and opening movements."""
    release_bed(encounter_id, new_status="cleaning")
    return occupy_bed(new_bed_id, encounter_id, reason=reason or "Transfer")


def set_bed_status(bed_id: int, status: str) -> None:
    """Change a bed's status. Refused while a patient occupies it."""
    if status not in BED_STATUS:
        raise ValueError("unknown bed status")
    patch: dict[str, Any] = {"status": status}
    if status in ("vacant", "blocked"):
        patch["encounter_id"] = None
    db.update("bed", bed_id, patch)


def bed_days(start: str, end: str) -> int:
    """Billable days between two instants: a part day counts as one, which is
    how hospitals bill. Unparseable input counts as a single day rather than
    zero, so a malformed timestamp can never make a stay free."""
    try:
        a = datetime.fromisoformat(db.to_instant(start))
        b = datetime.fromisoformat(db.to_instant(end))
    except (TypeError, ValueError):
        return 1
    return max(1, (b.date() - a.date()).days or 1)


def stay_charges(encounter_id: int) -> list[dict[str, Any]]:
    """Bed and nursing charges derived from the movement ledger."""
    moves = db.query(
        "SELECT m.*, b.code AS bed_code, w.name AS ward_name "
        "FROM bed_movement m JOIN bed b ON b.id = m.bed_id "
        "JOIN ward w ON w.id = b.ward_id WHERE m.encounter_id = ? ORDER BY m.from_ts",
        (encounter_id,))
    out = []
    for move in moves:
        days = bed_days(move["from_ts"], move["to_ts"] or db.now_iso())
        if move["tariff"]:
            out.append({"kind": "bed", "days": days, "rate": move["tariff"],
                        "label": f'Bed charges — {move["ward_name"]} '
                                 f'({move["bed_code"]})'})
        if move["nursing_rate"]:
            out.append({"kind": "nursing", "days": days, "rate": move["nursing_rate"],
                        "label": f'Nursing charges — {move["ward_name"]}'})
    return out


# ===========================================================================
# pharmacy
# ===========================================================================
def stock_on_hand(item_id: int) -> float:
    """Total quantity across every batch of one item."""
    return db.scalar("SELECT COALESCE(SUM(quantity), 0) FROM stock_batch "
                     "WHERE item_id = ?", (item_id,), default=0) or 0


def stock_list(search: str = "", kind: str = "") -> list[dict[str, Any]]:
    """Stock items with quantity on hand and next expiry, filterable."""
    sql = ("SELECT i.*, COALESCE(SUM(b.quantity), 0) AS on_hand, "
           "       MIN(CASE WHEN b.quantity > 0 THEN b.expiry_date END) AS next_expiry "
           "FROM stock_item i LEFT JOIN stock_batch b ON b.item_id = i.id "
           "WHERE i.active = 1")
    params: list[Any] = []
    if search:
        sql += " AND (i.name LIKE ? OR i.code LIKE ?)"
        params += [f"%{search}%", f"%{search}%"]
    if kind:
        sql += " AND i.kind = ?"
        params.append(kind)
    sql += " GROUP BY i.id ORDER BY i.kind, i.name"
    return [dict(r) for r in db.query(sql, params)]


def low_stock() -> list[dict[str, Any]]:
    """Items at or below their reorder level."""
    return [i for i in stock_list() if i["on_hand"] <= i["reorder_level"]]


def expiring_batches(days: int = 90) -> list:
    """Batches with stock left that expire within ``days``."""
    limit = (date.today() + timedelta(days=days)).isoformat()
    return db.query(
        "SELECT b.*, i.name, i.code, i.unit FROM stock_batch b "
        "JOIN stock_item i ON i.id = b.item_id "
        "WHERE b.quantity > 0 AND b.expiry_date IS NOT NULL AND b.expiry_date <= ? "
        "ORDER BY b.expiry_date", (limit,))


def receive_stock(item_id: int, batch_no: str, expiry: str | None, quantity: float,
                  cost_price: float, supplier: str | None) -> int:
    """Book a batch into stock and journal the receipt."""
    if quantity <= 0:
        raise ValueError("received quantity must be greater than zero")
    batch_id = db.insert("stock_batch", {
        "item_id": item_id, "batch_no": batch_no or "-", "expiry_date": expiry,
        "quantity": quantity, "cost_price": cost_price, "supplier": supplier,
        "received_at": db.now_iso(),
    })
    db.insert("stock_txn", {
        "item_id": item_id, "batch_id": batch_id, "kind": "receipt",
        "quantity": quantity, "rate": cost_price,
        "note": f"Batch {batch_no or '-'} from {supplier or 'supplier'}",
        "created_at": db.now_iso(),
    })
    return batch_id


def issue_stock(item_id: int, quantity: float, patient_id: int | None,
                encounter_id: int | None, note: str | None = None) -> list[dict]:
    """Issue against batches first-expiry-first-out. Refuses to go negative."""
    if quantity <= 0:
        raise ValueError("issued quantity must be greater than zero")
    item = db.one("SELECT * FROM stock_item WHERE id = ?", (item_id,))
    if item is None:
        raise ValueError("unknown stock item")
    available = stock_on_hand(item_id)
    if available < quantity:
        raise ValueError(
            f'only {available:g} {item["unit"]} of {item["name"]} in stock')

    batches = db.query(
        "SELECT * FROM stock_batch WHERE item_id = ? AND quantity > 0 "
        "ORDER BY COALESCE(expiry_date, '9999-12-31'), id", (item_id,))
    remaining = quantity
    picked = []
    for batch in batches:
        if remaining <= 0:
            break
        take = min(remaining, batch["quantity"])
        db.update("stock_batch", batch["id"], {"quantity": batch["quantity"] - take})
        db.insert("stock_txn", {
            "item_id": item_id, "batch_id": batch["id"], "kind": "issue",
            "quantity": -take, "rate": item["mrp"], "patient_id": patient_id,
            "encounter_id": encounter_id, "note": note, "created_at": db.now_iso(),
        })
        picked.append({"batch_no": batch["batch_no"], "quantity": take,
                       "expiry": batch["expiry_date"]})
        remaining -= take
    return picked


def unbilled_issues(encounter_id: int) -> list[dict[str, Any]]:
    rows = db.query(
        "SELECT t.item_id, i.name, i.unit, i.gst_pct, "
        "       SUM(-t.quantity) AS qty, MAX(t.rate) AS rate "
        "FROM stock_txn t JOIN stock_item i ON i.id = t.item_id "
        "WHERE t.encounter_id = ? AND t.kind = 'issue' AND t.billed = 0 "
        "GROUP BY t.item_id HAVING qty > 0", (encounter_id,))
    return [dict(r) for r in rows]


def mark_issues_billed(encounter_id: int) -> None:
    db.execute("UPDATE stock_txn SET billed = 1 WHERE encounter_id = ? "
               "AND kind = 'issue'", (encounter_id,))


# ===========================================================================
# payments
# ===========================================================================
def record_payment(invoice_id: int, amount: float, mode: str,
                   reference: str | None, note: str | None) -> int:
    invoice = db.one("SELECT * FROM invoice WHERE id = ?", (invoice_id,))
    if invoice is None:
        raise ValueError("unknown invoice")
    if amount <= 0:
        raise ValueError("payment amount must be greater than zero")
    outstanding = round(invoice["total_gross"] - paid_total(invoice_id), 2)
    if amount - outstanding > 0.005:
        raise ValueError(f"that is more than the ₹{outstanding:.2f} outstanding")
    payment_id = db.insert("payment", {
        "receipt_no": db.next_number("receipt", "RCP-"),
        "invoice_id": invoice_id, "patient_id": invoice["patient_id"],
        "amount": round(amount, 2), "mode": mode or "cash",
        "reference": reference, "note": note, "received_at": db.now_iso(),
    })
    refresh_invoice_payment(invoice_id)
    return payment_id


def paid_total(invoice_id: int) -> float:
    """Sum of receipts against one invoice."""
    return round(db.scalar("SELECT COALESCE(SUM(amount), 0) FROM payment "
                           "WHERE invoice_id = ?", (invoice_id,), default=0) or 0, 2)


def refresh_invoice_payment(invoice_id: int) -> None:
    """Recompute an invoice's paid amount and status from its receipts."""
    invoice = db.one("SELECT * FROM invoice WHERE id = ?", (invoice_id,))
    if invoice is None:
        return
    paid = paid_total(invoice_id)
    status = invoice["status"]
    if status not in ("cancelled", "draft"):
        status = "balanced" if paid >= invoice["total_gross"] - 0.005 else "issued"
    last = db.one("SELECT mode FROM payment WHERE invoice_id = ? "
                  "ORDER BY id DESC LIMIT 1", (invoice_id,))
    db.update("invoice", invoice_id, {
        "amount_paid": paid, "status": status,
        "payment_mode": last["mode"] if last else invoice["payment_mode"],
    })


def outstanding_invoices() -> list:
    """Unpaid invoices with the balance owed, oldest first."""
    return db.query(
        "SELECT i.*, p.name AS patient_name, p.mrn, p.phone, "
        "       i.total_gross - i.amount_paid AS balance "
        "FROM invoice i JOIN patient p ON p.id = i.patient_id "
        "WHERE i.status NOT IN ('cancelled') "
        "AND i.total_gross - i.amount_paid > 0.005 "
        "ORDER BY i.date")


# ===========================================================================
# appointments / OPD queue
# ===========================================================================
APPOINTMENT_STATUS = {
    "booked": ("Booked", "info"), "arrived": ("Arrived", "warning"),
    "fulfilled": ("Seen", "success"), "cancelled": ("Cancelled", "danger"),
    "no-show": ("No show", "danger"),
}


def book_appointment(patient_id: int, practitioner_id: int | None, department: str,
                     slot_date: str, slot_time: str | None, reason: str | None,
                     note: str | None) -> int:
    slot_date = slot_date or db.today_iso()
    token = (db.scalar("SELECT COALESCE(MAX(token), 0) FROM appointment "
                       "WHERE slot_date = ? AND COALESCE(practitioner_id, 0) = ?",
                       (slot_date, practitioner_id or 0), default=0) or 0) + 1
    return db.insert("appointment", {
        "appointment_no": db.next_number("appointment", "APT-"),
        "patient_id": patient_id, "practitioner_id": practitioner_id,
        "department": department or None, "slot_date": slot_date,
        "slot_time": slot_time or None, "token": token, "status": "booked",
        "reason": reason, "note": note, "created_at": db.now_iso(),
    })


def appointments_for(slot_date: str) -> list:
    """One day's appointments joined to patient and doctor."""
    return db.query(
        "SELECT a.*, p.name AS patient_name, p.mrn, p.phone, p.gender, "
        "       d.name AS doctor_name FROM appointment a "
        "JOIN patient p ON p.id = a.patient_id "
        "LEFT JOIN practitioner d ON d.id = a.practitioner_id "
        "WHERE a.slot_date = ? ORDER BY a.practitioner_id, a.token", (slot_date,))


def set_appointment_status(appointment_id: int, status: str,
                           encounter_id: int | None = None) -> None:
    """Move an appointment through the queue."""
    if status not in APPOINTMENT_STATUS:
        raise ValueError("unknown appointment status")
    patch: dict[str, Any] = {"status": status}
    if encounter_id:
        patch["encounter_id"] = encounter_id
    db.update("appointment", appointment_id, patch)
