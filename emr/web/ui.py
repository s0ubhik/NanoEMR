"""HTML rendering built strictly on the 0build design system (kit 0.5.4).

Class contract used here (all verified against dist/css/kit.min.css):

* components  ``z-container z-card z-button z-input z-select z-textarea z-table
  z-nav z-tab z-accordion z-alert z-badge z-label z-list z-breadcrumb z-modal
  z-h1..z-h6 z-form-*``
* utilities   one generic class per CSS property, the value supplied through a
  CSS custom property, e.g. ``class="mt" style="--mt: 4"``.
* icons       ``<z-icon icon="lucide-name">``

Nothing here uses Tailwind-style value-in-the-class-name utilities, because
0build does not ship them.
"""

from __future__ import annotations

import html as _html
from typing import Any, Iterable, Sequence

CDN = "https://cdn.jsdelivr.net/gh/0builddotdev/0build@0.5.4/dist"

BRAND = "NanoEMR"


def esc(value: Any) -> str:
    """HTML-escape a value; ``None`` becomes an empty string."""
    if value is None:
        return ""
    return _html.escape(str(value), quote=True)


def st(**variables: Any) -> str:
    """Render a 0build utility value block: ``st(mt=4, gap=2)``."""
    if not variables:
        return ""
    parts = [f"--{key.replace('__', '.').replace('_', '-')}: {value}"
             for key, value in variables.items()]
    return ' style="' + "; ".join(parts) + '"'


def icon(name: str, size: int = 4, cls: str = "") -> str:
    """A Lucide icon from the kit's ``<z-icon>`` element."""
    extra = f" {cls}" if cls else ""
    return (f'<z-icon class="size{extra}"{st(size=size)} icon="{esc(name)}" '
            f'decorative></z-icon>')


# ---------------------------------------------------------------------------
# navigation
# ---------------------------------------------------------------------------
# The sidebar is a *destination* list, not an action list. Every "New …" screen
# is reachable from a primary button on the list page it belongs to, so putting
# it here too only made the nav taller than the viewport — which pushed the
# bottom entries below the fold and reset their scroll position on every click.
NAV: list[tuple[str, list[tuple[str, str, str]]]] = [
    ("Front desk", [
        ("dashboard", "/", "layout-dashboard"),
        ("patients", "/patients", "users"),
        ("appointments", "/appointments", "calendar-clock"),
    ]),
    ("Clinical", [
        ("opd", "/opd", "stethoscope"),
        ("beds", "/beds", "layout-grid"),
        ("ipd", "/ipd", "bed"),
        ("wellness", "/wellness", "heart-pulse"),
    ]),
    ("Dialysis", [
        ("dialysis", "/dialysis", "activity"),
        ("dialysis-machines", "/dialysis/machines", "server"),
        ("dialysis-courses", "/dialysis/courses", "clipboard-list"),
        ("dialysis-sessions", "/dialysis/sessions", "list-checks"),
    ]),
    ("Diagnostics", [
        ("lab", "/lab", "flask-conical"),
        ("pharmacy", "/pharmacy", "pill"),
    ]),
    ("Finance", [
        ("billing", "/billing", "receipt-indian-rupee"),
        ("reports", "/reports", "chart-column"),
    ]),
    ("Facility", [
        ("fhir", "/fhir", "share-2"),
        ("masters", "/masters", "database"),
        ("settings", "/settings", "settings"),
    ]),
]

NAV_LABEL = {
    "dashboard": "Dashboard",
    "patients": "Patient directory",
    "appointments": "Appointments",
    "opd": "OPD visits",
    "beds": "Ward board",
    "ipd": "IPD admissions",
    "dialysis": "Dialysis unit",
    "dialysis-machines": "Machines",
    "dialysis-courses": "Courses",
    "dialysis-sessions": "Sessions",
    "lab": "Laboratory",
    "pharmacy": "Pharmacy & stock",
    "wellness": "Wellness records",
    "billing": "Billing",
    "reports": "Reports",
    "fhir": "FHIR export centre",
    "masters": "Masters",
    "settings": "Settings",
}

# A "new record" screen highlights the list it belongs to, so the sidebar still
# shows where you are after following a button off one of those pages.
NAV_ALIAS = {
    "patient-new": "patients",
    "opd-new": "opd",
    "ipd-new": "ipd",
    "dialysis-new": "dialysis-sessions",
    "wellness-new": "wellness",
}


def _sidebar(active: str) -> str:
    """`z-nav z-nav-alt` — the alt style is the one that paints a filled pill on
    the active item, which is what a sidebar needs; `z-nav-default` only shifts
    the font weight and reads as though nothing is selected."""
    active = NAV_ALIAS.get(active, active)
    items = []
    head_cls = "z-nav-header text-xs uppercase tracking-wide"
    for index, (heading, links) in enumerate(NAV):
        # every group but the first gets breathing room above it
        attrs = f'class="{head_cls}"' if index == 0 else f'class="{head_cls} mt"{st(mt=3)}'
        items.append(f"<li {attrs}>{esc(heading)}</li>")
        for key, href, ico in links:
            active_cls = ' class="z-active" data-nav-active' if key == active else ""
            items.append(
                f'<li{active_cls}><a href="{href}">{icon(ico)}'
                f'<span class="ml"{st(ml=3)}>{esc(NAV_LABEL[key])}</span></a></li>')
    # Tighter than the kit default so the whole nav clears the fold; the kit
    # exposes these as custom properties precisely for this.
    return ('<ul class="z-nav z-nav-alt"'
            + st(z_nav_item_padding="0.375rem 0.75rem",
                 z_nav_item_margin="0.0625rem 0")
            + ">" + "".join(items) + "</ul>")


# ---------------------------------------------------------------------------
# page shell
# ---------------------------------------------------------------------------
# Brand override. The kit defines its tokens inside `@layer theme`, so this
# unlayered block wins regardless of specificity. Dark mode gets a lighter step
# of the same hue so the filled nav pill and primary buttons stay legible.
BRAND_CSS = """
:root {
  --z-primary: var(--color-purple-800);
  --z-primary-f: var(--color-purple-50);
}
.dark {
  --z-primary: var(--color-purple-500);
  --z-primary-f: var(--color-purple-50);
}
"""

THEME_INIT = """
const htmlElement = document.documentElement;
const __Z_THEME__ = JSON.parse(localStorage.getItem("__Z_THEME__") || "{}");
if (__Z_THEME__.mode === "dark" ||
    (!__Z_THEME__.mode && window.matchMedia("(prefers-color-scheme: dark)").matches)) {
  htmlElement.classList.add("dark");
} else {
  htmlElement.classList.remove("dark");
}
htmlElement.classList.add(__Z_THEME__.layout || "z-layout-small");
"""

THEME_TOGGLE = """
function zToggleTheme() {
  const el = document.documentElement;
  const t = JSON.parse(localStorage.getItem("__Z_THEME__") || "{}");
  t.mode = el.classList.contains("dark") ? "light" : "dark";
  el.classList.toggle("dark", t.mode === "dark");
  localStorage.setItem("__Z_THEME__", JSON.stringify(t));
}
// Repeating form blocks (medications, diagnoses, invoice lines, ...).
function emrAddRow(key) {
  const tpl = document.getElementById(key + "-template");
  const host = document.getElementById(key + "-rows");
  if (tpl && host) { host.appendChild(tpl.content.cloneNode(true)); }
}
function emrDelRow(btn) {
  const row = btn.closest("[data-row]");
  if (row) { row.remove(); }
}
// If the sidebar is taller than its box on a short screen, open it at the
// selected item instead of the top — otherwise picking a bottom entry lands you
// on a nav scrolled back up with no sign of where you are.
addEventListener("DOMContentLoaded", function () {
  const active = document.querySelector("#emr-nav [data-nav-active]");
  if (!active) { return; }
  const box = active.closest(".overflow-auto");
  if (box && box.scrollHeight > box.clientHeight) {
    active.scrollIntoView({ block: "nearest" });
  }
});
"""


def page(title: str, active: str, body: str, flash: tuple[str, str] | None = None,
         breadcrumb: Sequence[tuple[str, str | None]] | None = None,
         actions: str = "", scripts: str = "") -> str:
    """The full page shell: head, header, sidebar, content and scripts."""
    from .. import db  # local import keeps this module free of a load-time cycle

    org = db.default_org()
    facility = org["name"] if org else ""
    alert = ""
    if flash:
        kind, message = flash
        style = {"success": "z-alert-success", "danger": "z-alert-danger",
                 "warning": "z-alert-warning", "info": "z-alert-info"}.get(
            kind, "z-alert-success")
        alert = (f'<div class="z-alert {style} mb" data-z-alert{st(mb=4)}>'
                 f'<a href class="z-alert-close"></a>{esc(message)}</div>')

    crumbs = ""
    if breadcrumb:
        items = []
        for label, href in breadcrumb:
            if href:
                items.append(f'<li><a href="{href}">{esc(label)}</a></li>')
            else:
                items.append(f'<li><span>{esc(label)}</span></li>')
        crumbs = ('<nav aria-label="Breadcrumb" class="mb"' + st(mb=4)
                  + '><ul class="z-breadcrumb">' + "".join(items) + "</ul></nav>")

    return f"""<!doctype html>
<html lang="en" class="antialiased">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{esc(title)} — {esc(BRAND)}</title>
<link rel="icon" type="image/png" href="/static/favicon.png">
<link rel="apple-touch-icon" href="/static/logo.png">
<link rel="stylesheet" href="{CDN}/css/kit.min.css">
<link rel="stylesheet" href="{CDN}/css/chart.min.css">
<style>{BRAND_CSS}</style>
<script>{THEME_INIT}</script>
<script>{THEME_TOGGLE}</script>
<script src="{CDN}/js/hwc/core.iife.js" type="module"></script>
</head>
<body class="bg color"{st(bg="var(--z-bg)", color="var(--z-bg-f)")}>

<header class="sticky top z bg border-b-w border-b/o"{st(
        top=0, z=20, bg="var(--z-bg)", border_b_w="1px",
        border_b="var(--z-border)", border_b_o="var(--z-border-o)")}>
  <div class="z-container z-container-expand display-flex items-center justify-between h"{st(h=14)}>
    <div class="display-flex items-center gap"{st(gap=3)}>
      <a href="/" class="display-flex items-center gap"{st(gap=3)}>
        <img src="/static/logo.png" alt="" width="256" height="256"
             class="size display-block"{st(size=9)}>
        <span>
          <span class="text-lg font-bold">{esc(BRAND)}</span>
          <span class="ml text-xs color display-hidden md:display-inline"{st(
              ml=2, color="var(--z-muted-f)")}>{esc(facility)}</span>
        </span>
      </a>
    </div>
    <form class="display-hidden md:display-flex items-center gap" action="/patients"
          method="get"{st(gap=2)}>
      <div class="z-inline">
        <span class="z-form-icon">{icon("search")}</span>
        <input class="z-input z-form-small" type="search" name="q"
               placeholder="Search name / MRN / phone" aria-label="Search patients">
      </div>
      <button class="z-button z-button-primary z-button-small" type="submit">Search</button>
    </form>
    <div class="display-flex items-center gap"{st(gap=2)}>
      <button class="z-button z-button-secondary z-button-icon z-button-small"
              type="button" onclick="zToggleTheme()" aria-label="Toggle colour scheme">
        {icon("sun-moon")}
      </button>
      <a class="z-button z-button-primary z-button-small" href="/patients/new">
        {icon("user-plus")}<span class="ml display-hidden sm:display-inline"{st(ml=2)}>Register</span>
      </a>
    </div>
  </div>
</header>

<div class="z-container z-container-expand py"{st(py=6)}>
  <div class="display-grid gap lg:[grid-cols]"{st(gap=6, lg_grid_cols="260px minmax(0, 1fr)")}>
    <aside id="emr-nav">
      <div class="z-card lg:sticky lg:top lg:[max-h] overflow-auto"{st(
            lg_top=20, lg_max_h="calc(100dvh - 6rem)")}>
        <div class="py px"{st(py=3, px=2)}>{_sidebar(active)}</div>
      </div>
    </aside>
    <main class="min-w"{st(min_w=0)}>
      {crumbs}
      <div class="display-flex items-center justify-between gap mb"{st(gap=4, mb=5)}>
        <h1 class="z-h2">{esc(title)}</h1>
        <div class="display-flex items-center gap"{st(gap=2)}>{actions}</div>
      </div>
      {alert}
      {body}
    </main>
  </div>
</div>

<script src="{CDN}/js/uikit.min.js"></script>
<script>window.zRuntime = window.zRuntime || {{}};</script>
<script src="{CDN}/js/runtime.iife.js" type="module"></script>
<script src="{CDN}/js/hwc/icon.iife.js" type="module"></script>
<script src="{CDN}/js/hwc/chart.iife.js" type="module"></script>
<script src="{CDN}/js/hwc/components.iife.js" type="module"></script>
{scripts}
</body>
</html>"""


# ---------------------------------------------------------------------------
# building blocks
# ---------------------------------------------------------------------------
def card(title: str | None, body: str, footer: str = "", actions: str = "",
         style: str = "", body_class: str = "z-card-body") -> str:
    """A ``z-card`` with an optional header, actions and footer."""
    head = ""
    if title:
        head = (f'<div class="z-card-header display-flex items-center justify-between gap"'
                f'{st(gap=3)}><h3 class="z-card-title">{esc(title)}</h3>'
                f'<div class="display-flex items-center gap"{st(gap=2)}>{actions}</div></div>')
    foot = f'<div class="z-card-footer">{footer}</div>' if footer else ""
    cls = f"z-card {style}".strip()
    return f'<div class="{cls}">{head}<div class="{body_class}">{body}</div>{foot}</div>'


def grid(*columns: str, cols: int = 2, gap: int = 4, breakpoint: str = "md") -> str:
    """A responsive column grid of the given cells."""
    return (f'<div class="display-grid gap {breakpoint}:grid-cols"'
            + st(**{"gap": gap, f"{breakpoint}_grid_cols": cols}) + ">"
            + "".join(columns) + "</div>")


def field(label: str, control: str, help_text: str = "", required: bool = False,
          field_id: str = "") -> str:
    """A labelled form control with optional help text."""
    cls = "z-form-label z-form-label-required" if required else "z-form-label"
    for_attr = f' for="{esc(field_id)}"' if field_id else ""
    helper = (f'<div class="z-form-help mt"{st(mt=1)}>{esc(help_text)}</div>'
              if help_text else "")
    return (f'<div class="mb"{st(mb=4)}><label class="{cls}"{for_attr}>{esc(label)}</label>'
            f'<div class="z-form-controls mt"{st(mt=1)}>{control}{helper}</div></div>')


def text_input(name: str, value: Any = "", *, type_: str = "text", placeholder: str = "",
               required: bool = False, attrs: str = "", size: str = "") -> str:
    cls = f"z-input {size}".strip()
    return (f'<input class="{cls}" id="{esc(name)}" type="{type_}" name="{esc(name)}" '
            f'value="{esc(value)}" placeholder="{esc(placeholder)}"'
            f'{" required" if required else ""} {attrs}>')


def textarea(name: str, value: Any = "", rows: int = 3, placeholder: str = "",
             required: bool = False) -> str:
    return (f'<textarea class="z-textarea" id="{esc(name)}" name="{esc(name)}" '
            f'rows="{rows}" placeholder="{esc(placeholder)}"'
            f'{" required" if required else ""}>{esc(value)}</textarea>')


def select(name: str, options: Iterable[tuple[Any, str]], value: Any = "",
           *, blank: str | None = None, required: bool = False,
           multiple: bool = False, attrs: str = "") -> str:
    opts = []
    if blank is not None:
        opts.append(f'<option value="">{esc(blank)}</option>')
    for opt_value, label in options:
        selected = " selected" if str(opt_value) == str(value) else ""
        opts.append(f'<option value="{esc(opt_value)}"{selected}>{esc(label)}</option>')
    return (f'<select class="z-select" id="{esc(name)}" name="{esc(name)}"'
            f'{" required" if required else ""}{" multiple" if multiple else ""} '
            f'{attrs}>' + "".join(opts) + "</select>")


def checkbox(name: str, label: str, checked: bool = False, value: str = "1") -> str:
    return (f'<label class="display-flex items-center gap"{st(gap=2)}>'
            f'<input class="z-checkbox" type="checkbox" name="{esc(name)}" '
            f'value="{esc(value)}"{" checked" if checked else ""}>'
            f'<span>{esc(label)}</span></label>')


def table(headers: Sequence[str], rows: Sequence[Sequence[str]],
          empty: str = "Nothing here yet.", align_right: Sequence[int] = ()) -> str:
    """A ``z-table`` that scrolls horizontally, or an empty-state line."""
    if not rows:
        return (f'<p class="color text-sm"{st(color="var(--z-muted-f)")}>{esc(empty)}</p>')
    head = "".join(
        f'<th class="{"text-right" if i in align_right else ""}">{h}</th>'
        for i, h in enumerate(headers))
    body = []
    for row in rows:
        cells = "".join(
            f'<td class="{"text-right" if i in align_right else ""}">{c}</td>'
            for i, c in enumerate(row))
        body.append(f"<tr>{cells}</tr>")
    return ('<div class="overflow-x-auto"><table class="z-table z-table-divider '
            'z-table-hover z-table-small z-table-middle">'
            f"<thead><tr>{head}</tr></thead><tbody>{''.join(body)}</tbody>"
            "</table></div>")


def when(ts: Any, chars: int = 16) -> str:
    """A stored instant rendered for the screen: ``2026-08-16 09:30``.

    Handles ``None`` and empty strings (returns an em dash), so callers can pass
    nullable columns straight through. ``chars=10`` gives just the date.
    """
    if not ts:
        return "—"
    return str(ts)[:chars].replace("T", " ")


def short_label(display: str) -> str:
    """A terminology display trimmed for column headers and tight cells —
    everything before the first bracket or LOINC-style double dash."""
    for sep in ("[", "(", "--"):
        display = display.split(sep)[0]
    return display.strip()


def muted(text: str, size: str = "text-xs") -> str:
    """A dim secondary line, pre-escaped."""
    return (f'<div class="{size} color"{st(color="var(--z-muted-f)")}>'
            f"{esc(text)}</div>")


def sub(main: str, sub_text: Any) -> str:
    """A table cell with a main line (already HTML) over a muted sub-line."""
    return main + (muted(sub_text) if sub_text else "")


# Observation interpretation -> chip. One map for every screen that flags a
# reading, so "High is red, Low is amber" cannot drift between modules.
FLAG_CHIP = {"H": ("High", "danger"), "L": ("Low", "warning"),
             "N": ("Normal", "success"), "A": ("Abnormal", "danger")}


def flag_chip(interpretation: str | None, show_normal: bool = True) -> str:
    """The coloured chip for an H/L/N/A flag, or an empty string."""
    entry = FLAG_CHIP.get(interpretation or "")
    if entry is None or (not show_normal and interpretation == "N"):
        return ""
    return label_chip(*entry)


def post_button(action: str, label: str, *, style: str = "z-button-secondary",
                size: str = "z-button-xsmall", ico: str = "",
                fields: dict[str, Any] | None = None, confirm: str = "") -> str:
    """A one-button POST form — the idiom behind every state-changing action
    that is not a full form (check in, retire, start run, mark clean, …)."""
    hidden = "".join(
        f'<input type="hidden" name="{esc(k)}" value="{esc(v)}">'
        for k, v in (fields or {}).items())
    guard = f' onsubmit="return confirm({_js(confirm)})"' if confirm else ""
    return (f'<form method="post" action="{action}"{guard}>{hidden}'
            + button(label, style=style, size=size, ico=ico, type_="submit")
            + "</form>")


def stack(*blocks: str, gap: int = 5) -> str:
    """Vertically spaced page sections: the first flush, the rest offset."""
    parts = [b for b in blocks if b]
    if not parts:
        return ""
    return parts[0] + "".join(
        f'<div class="mt"{st(mt=gap)}>{b}</div>' for b in parts[1:])


def label_chip(text: str, kind: str = "") -> str:
    """A ``z-label`` chip."""
    cls = f"z-label z-label-{kind}" if kind else "z-label"
    return f'<span class="{cls}">{esc(text)}</span>'


def badge(text: str, kind: str = "") -> str:
    """A ``z-badge``."""
    cls = f"z-badge z-badge-{kind}" if kind else "z-badge"
    return f'<span class="{cls}">{esc(text)}</span>'


def button(label: str, href: str | None = None, *, style: str = "z-button-default",
           size: str = "z-button-small", ico: str = "", attrs: str = "",
           type_: str = "button") -> str:
    """A ``z-button``, rendered as a link when ``href`` is given."""
    inner = (icon(ico) + f'<span class="ml"{st(ml=2)}>{esc(label)}</span>') if ico \
        else esc(label)
    cls = f"z-button {style} {size}".strip()
    if href:
        return f'<a class="{cls}" href="{href}" {attrs}>{inner}</a>'
    return f'<button class="{cls}" type="{type_}" {attrs}>{inner}</button>'


def stat(label: str, value: Any, ico: str, kind: str = "") -> str:
    """A dashboard stat tile."""
    tint = f"var(--z-{kind})" if kind else "var(--z-primary)"
    return (
        '<div class="z-card z-card-body">'
        f'<div class="display-flex items-center justify-between gap"{st(gap=3)}>'
        '<div>'
        f'<div class="text-xs uppercase tracking-wide color"'
        f'{st(color="var(--z-muted-f)")}>{esc(label)}</div>'
        f'<div class="z-h3 mt"{st(mt=1)}>{esc(value)}</div>'
        "</div>"
        f'<span class="color"{st(color=tint)}>{icon(ico, 7)}</span>'
        "</div></div>")


def accordion(items: Sequence[tuple[str, str]], multiple: bool = True,
              open_first: bool = True) -> str:
    """0build accordion — ``<ul data-z-accordion>`` with title/content children."""
    options = "multiple: true" if multiple else "collapsible: true"
    parts = []
    for index, (title, content) in enumerate(items):
        cls = ' class="z-open"' if open_first and index == 0 else ""
        parts.append(
            f"<li{cls}>"
            f'<a class="z-accordion-title" href>'
            f"<span>{esc(title)}</span>"
            f'<span class="z-accordion-icon size"{st(size=4)}>'
            f'<z-icon icon="chevron-down" decorative></z-icon></span></a>'
            f'<div class="z-accordion-content">{content}</div></li>')
    return f'<ul data-z-accordion="{options}">' + "".join(parts) + "</ul>"


def tabs(items: Sequence[tuple[str, str]], active: int = 0,
         key: str = "emr-switcher") -> str:
    """0build tab bar wired to a switcher.

    The content container takes the `z-switcher` **class** — that is what carries
    `> :not(.z-active) { display: none }`. Putting `data-z-switcher` on it instead
    initialises a second switcher component and hides nothing, which leaves every
    pane stacked on the page and the tab bar inert.
    """
    heads, panes = [], []
    for index, (title, content) in enumerate(items):
        state = ' class="z-active"' if index == active else ""
        heads.append(f'<li{state}><a class="px pb pt"{st(px=4, pb=3, pt=2)} href>'
                     f"{esc(title)}</a></li>")
        pane_cls = "pt z-active" if index == active else "pt"
        panes.append(f'<li class="{pane_cls}"{st(pt=4)}>{content}</li>')
    return (f'<ul data-z-tab="connect: #{key}">' + "".join(heads) + "</ul>"
            f'<ul id="{key}" class="z-switcher">' + "".join(panes) + "</ul>")


def confirm_form(action: str, label: str, question: str,
                 size: str = "z-button-xsmall") -> str:
    """A destructive one-button form that asks before it fires."""
    return post_button(action, label, style="z-button-danger", size=size,
                       confirm=question)


def _js(value: str) -> str:
    import json as _json
    return esc(_json.dumps(value))


def repeater(key: str, template_row: str, existing_rows: Sequence[str],
             add_label: str = "Add row") -> str:
    """A repeating form block: rendered rows + a <template> the Add button clones."""
    rows = "".join(existing_rows)
    return (
        f'<div id="{key}-rows" class="display-flex flex-col gap"{st(gap=3)}>{rows}</div>'
        f'<template id="{key}-template">{template_row}</template>'
        f'<div class="mt"{st(mt=3)}>'
        f'<button class="z-button z-button-secondary z-button-small" type="button" '
        f'onclick="emrAddRow(\'{key}\')">{icon("plus")}'
        f'<span class="ml"{st(ml=2)}>{esc(add_label)}</span></button></div>')


def row_shell(inner: str) -> str:
    """One line of a repeater, with a delete affordance."""
    return (f'<div data-row class="display-flex gap items-end flex-wrap p z-rounded '
            f'border-w border/o"'
            + st(gap=2, p=3, border_w="1px", border="var(--z-border)",
                 border_o="var(--z-border-o)") + ">" + inner
            + '<button class="z-button z-button-danger z-button-icon z-button-small" '
              'type="button" aria-label="Remove row" onclick="emrDelRow(this)">'
            + icon("trash-2") + "</button></div>")


def cell(label: str, control: str, width: str = "12rem") -> str:
    return (f'<div style="flex:1 1 {width}; min-width:{width}">'
            f'<label class="z-form-label text-xs">{esc(label)}</label>'
            f'<div class="mt"{st(mt=1)}>{control}</div></div>')


def dl(pairs: Sequence[tuple[str, Any]], cols: int = 2) -> str:
    """Definition-style detail grid used on the record screens."""
    cells = []
    for term, value in pairs:
        if value in (None, ""):
            value = "—"
        cells.append(
            '<div>'
            f'<div class="text-xs uppercase tracking-wide color"'
            f'{st(color="var(--z-muted-f)")}>{esc(term)}</div>'
            f'<div class="mt"{st(mt=1)}>{value if str(value).startswith("<") else esc(value)}</div>'
            "</div>")
    return (f'<div class="display-grid gap sm:grid-cols"'
            + st(gap=4, sm_grid_cols=cols) + ">" + "".join(cells) + "</div>")



def empty_state(message: str, action: str = "") -> str:
    """A centred placeholder for a screen with nothing to show."""
    return ('<div class="z-card z-card-body text-center py"' + st(py=10) + ">"
            + f'<div class="display-flex justify-center color"{st(color="var(--z-muted-f)")}>'
            + icon("inbox", 10) + "</div>"
            + f'<p class="mt color"{st(mt=3, color="var(--z-muted-f)")}>{esc(message)}</p>'
            + (f'<div class="mt"{st(mt=4)}>{action}</div>' if action else "")
            + "</div>")


def code_block(text: str, max_height: str = "34rem") -> str:
    """Scrollable, height-capped preformatted block.

    The kit's `--z-muted` is `#000` with a separate `--z-muted-o: 4%`, so it
    only works through the `bg/o` class that colour-mixes the two; the plain
    `bg` class paints it solid black, which hid this block's own text. Same
    story for `border` vs `border/o`.
    """
    return ('<pre class="overflow-auto [max-h] p bg/o z-rounded border-w '
            'border/o text-xs"'
            + st(p=4, max_h=max_height, bg="var(--z-muted)",
                 bg_o="var(--z-muted-o)", border_w="1px",
                 border="var(--z-border)", border_o="var(--z-border-o)")
            + f"><code>{esc(text)}</code></pre>")


def avatar(patient: dict[str, Any], size: str = "3.5rem") -> str:
    """A ``z-avatar`` showing the patient's initials."""
    return (f'<div class="z-avatar z-avatar-rounded"' + st(z_avatar_size=size)
            + f'><div class="z-avatar-text">{esc(_initials(patient.get("name")))}'
            + "</div></div>")


def _initials(name: str | None) -> str:
    parts = [p for p in (name or "?").split() if p]
    if not parts:
        return "?"
    return (parts[0][0] + (parts[-1][0] if len(parts) > 1 else "")).upper()


def patient_header(patient: dict[str, Any], extra: str = "") -> str:
    """The patient identity banner shown above a record."""
    from ..services import display_age  # local import avoids a cycle

    bits = [
        label_chip(patient["mrn"], "info"),
        label_chip(patient["gender"].title()),
        label_chip(display_age(patient)),
    ]
    if patient.get("blood_group"):
        bits.append(label_chip(patient["blood_group"], "danger"))
    if patient.get("abha_number"):
        bits.append(label_chip("ABHA " + patient["abha_number"], "success"))
    return card(None, (
        f'<div class="display-flex items-center justify-between gap flex-wrap"{st(gap=4)}>'
        f'<div class="display-flex items-center gap"{st(gap=4)}>'
        + avatar(patient)
        + "<div>"
        f'<div class="z-h4"><a class="z-link" href="/patients/{patient["id"]}">'
        f'{esc(patient["name"])}</a></div>'
        f'<div class="display-flex items-center gap mt flex-wrap"{st(gap=2, mt=2)}>'
        + "".join(bits) + "</div>"
        f'<div class="mt text-sm color"{st(mt=2, color="var(--z-muted-f)")}>'
        f'{icon("phone", 3)} {esc(patient["phone"])}</div>'
        "</div></div>"
        f'<div class="display-flex items-center gap flex-wrap"{st(gap=2)}>{extra}</div>'
        "</div>"))
