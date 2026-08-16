"""Route modules. Each exposes ``register(app)``."""

from . import (
    appointments, assets, beds, billing, dashboard, dialysis, fhir_pages, ipd,
    lab, masters, opd, patients, pharmacy, reports, settings, wellness,
)

MODULES = (dashboard, patients, appointments, opd, beds, ipd, dialysis, lab,
           pharmacy, wellness, billing, reports, fhir_pages, masters, settings,
           assets)


def register_all(app) -> None:
    for module in MODULES:
        module.register(app)
