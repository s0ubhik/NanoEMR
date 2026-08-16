"""Facility settings.

These fields are not cosmetic: the organization row is the `custodian` of every
FHIR document NanoEMR produces and the `Organization` resource inside it, so the
facility name, HFR ID and address travel with each exported bundle.
"""

from __future__ import annotations

from ... import db
from ...terminology import IDENTIFIER_TYPE, NDHM_IDENTIFIER_TYPE
from .. import ui
from ..common import render
from ..router import Request, redirect

st = ui.st

# The identifier types that make sense for a facility, from the NRCES
# ndhm-identifier-type-code system plus HL7 v2-0203.
FACILITY_ID_TYPES = [
    ("NHRR", "National Health Resource Repository (NHRR) ID"),
    ("ROHINI", "Registry of Hospitals in Network of Insurance (ROHINI) ID"),
    ("PMJAY", "Pradhan Mantri Jan Aarogya Yojana (PMJAY) ID"),
    ("OIN", "Other identifier"),
    ("PRN", "Provider number"),
]

ORG_TYPES = [
    ("prov", "Healthcare Provider"),
    ("dept", "Hospital Department"),
    ("team", "Organizational team"),
    ("ins", "Insurance Company"),
    ("other", "Other"),
]

STATES = [
    "Andaman and Nicobar Islands", "Andhra Pradesh", "Arunachal Pradesh", "Assam",
    "Bihar", "Chandigarh", "Chhattisgarh", "Dadra and Nagar Haveli and Daman and Diu",
    "Delhi", "Goa", "Gujarat", "Haryana", "Himachal Pradesh", "Jammu and Kashmir",
    "Jharkhand", "Karnataka", "Kerala", "Ladakh", "Lakshadweep", "Madhya Pradesh",
    "Maharashtra", "Manipur", "Meghalaya", "Mizoram", "Nagaland", "Odisha",
    "Puducherry", "Punjab", "Rajasthan", "Sikkim", "Tamil Nadu", "Telangana",
    "Tripura", "Uttar Pradesh", "Uttarakhand", "West Bengal",
]


def register(app) -> None:
    app.add("GET", "/settings", index)
    app.add("POST", "/settings", update)


def index(request: Request):
    org = db.default_org()
    if org is None:
        return redirect("/", "No facility record exists yet.", "danger")

    identity = ui.grid(
        ui.field("Facility name", ui.text_input("name", org["name"], required=True,
                                                placeholder="As registered on HFR"),
                 required=True,
                 help_text="Becomes Organization.name and the document custodian."),
        ui.field("Facility type", ui.select("type_code", ORG_TYPES, org["type_code"]),
                 help_text="Organization.type, coded from the HL7 organization-type "
                           "system."),
        cols=2)

    identifiers = (
        ui.grid(
            ui.field("HFR / facility ID", ui.text_input(
                "identifier_value", org["identifier_value"], required=True,
                placeholder="e.g. IN2710000123"), required=True,
                help_text="Organization.identifier.value on every exported bundle."),
            ui.field("Identifier type", ui.select(
                "identifier_type_code", FACILITY_ID_TYPES,
                org["identifier_type_code"]),
                help_text="ndhm-identifier-type-code, or a provider number."),
            cols=2)
        + ui.grid(
            ui.field("Identifier system", ui.text_input(
                "identifier_system", org["identifier_system"],
                placeholder="https://facility.abdm.gov.in"),
                help_text="The namespace the ID above is issued in."),
            ui.field("Participant ID", ui.text_input(
                "participant_code", org["participant_code"] or "",
                placeholder="e.g. kyrocare@hcx"),
                help_text="NHCX / HCX participant code used for claims exchange."),
            cols=2))

    address = (
        ui.field("Address line", ui.text_input("address_line",
                                               org["address_line"] or ""))
        + ui.grid(
            ui.field("City / town", ui.text_input("city", org["city"] or "")),
            ui.field("District", ui.text_input("district", org["district"] or "")),
            ui.field("State", ui.select("state", [(s, s) for s in STATES],
                                        org["state"] or "", blank="Select state")),
            ui.field("PIN code", ui.text_input("postal_code",
                                               org["postal_code"] or "",
                                               placeholder="600018")),
            cols=2)
        + ui.field("Country", ui.text_input("country", org["country"] or "India")))

    contact = ui.grid(
        ui.field("Phone", ui.text_input("phone", org["phone"] or "",
                                        placeholder="+914442228888"),
                 help_text="Organization.telecom.value is mandatory when telecom "
                           "is present."),
        ui.field("Email", ui.text_input("email", org["email"] or "", type_="email")),
        ui.field("GSTIN", ui.text_input("gstin", org["gstin"] or ""),
                 help_text="Printed on invoices."),
        cols=3)

    form = (
        '<form method="post" action="/settings">'
        + ui.card("Facility", ui.accordion([
            ("Identity", identity),
            ("Registry identifiers", identifiers),
            ("Address", address),
            ("Contact & tax", contact),
        ], multiple=True), footer=(
            f'<div class="display-flex justify-between items-center gap flex-wrap"'
            f'{st(gap=2)}>'
            f'<span class="text-xs color"{st(color="var(--z-muted-f)")}>'
            "Saved values are written into the Organization resource of every "
            "bundle generated from now on."
            "</span>"
            + ui.button("Save facility settings", style="z-button-primary",
                        size="z-button-medium", type_="submit")
            + "</div>"))
        + "</form>")

    preview = ui.card("How this appears in an exported bundle", ui.dl([
        ("Organization.name", org["name"]),
        ("Organization.identifier.system", org["identifier_system"]),
        ("Organization.identifier.value", org["identifier_value"]),
        ("Organization.identifier.type", (f'{org["identifier_type_code"]} — '
                                          f'{org["identifier_type_display"]}')),
        ("Organization.type", f'{org["type_code"]} — {org["type_display"]}'),
        ("Organization.telecom", ", ".join(filter(None, [org["phone"], org["email"]]))),
        ("Organization.address", ", ".join(filter(None, [
            org["address_line"], org["city"], org["district"], org["state"],
            org["postal_code"], org["country"]]))),
        ("Participant ID (claims)", org["participant_code"]),
    ], cols=2))

    body = form + f'<div class="mt"{st(mt=5)}>' + preview + "</div>"
    return render(request, "Settings", "settings", body)


def update(request: Request):
    org = db.default_org()
    if org is None:
        return redirect("/", "No facility record exists yet.", "danger")

    name = request.f("name")
    identifier_value = request.f("identifier_value")
    if not name or not identifier_value:
        return redirect("/settings",
                        "Facility name and the HFR / facility ID are both required.",
                        "danger")

    id_code = request.f("identifier_type_code") or "NHRR"
    id_system, _code, id_display = IDENTIFIER_TYPE.get(
        id_code, (NDHM_IDENTIFIER_TYPE, id_code, dict(FACILITY_ID_TYPES).get(
            id_code, id_code)))
    type_code = request.f("type_code") or "prov"

    db.update("organization", org["id"], {
        "name": name,
        "identifier_type_system": id_system,
        "identifier_type_code": id_code,
        "identifier_type_display": dict(FACILITY_ID_TYPES).get(id_code, id_display),
        "identifier_system": request.f("identifier_system")
        or "https://facility.abdm.gov.in",
        "identifier_value": identifier_value,
        "participant_code": request.f_or_none("participant_code"),
        "type_code": type_code,
        "type_display": dict(ORG_TYPES).get(type_code, "Healthcare Provider"),
        "phone": request.f_or_none("phone"),
        "email": request.f_or_none("email"),
        "address_line": request.f_or_none("address_line"),
        "city": request.f_or_none("city"),
        "district": request.f_or_none("district"),
        "state": request.f_or_none("state"),
        "postal_code": request.f_or_none("postal_code"),
        "country": request.f("country") or "India",
        "gstin": request.f_or_none("gstin"),
    })
    return redirect("/settings", "Facility settings saved.")


# Keep the organization-type system reachable for anyone importing this module.
