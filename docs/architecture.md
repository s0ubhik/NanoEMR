# Architecture

Four layers, each liftable on its own. ~12,000 lines of stdlib-only Python,
106 routes, 30 tables.

```
emr/web/routes/*      screens: one module per area, each exposing register(app)
emr/web/ui.py         HTML built from the 0build kit — no templates, no JSX
emr/web/router.py     ~250 lines of routing over http.server
      │
emr/services.py       patients, encounters, vitals, labs, invoice arithmetic
emr/hospital.py       beds, pharmacy, payments, appointments
emr/dialysis.py       courses, sessions, machines, flowsheet
emr/wellness.py       WellnessRecord capture and generation
emr/masters.py        master CRUD and the patient-data reset
      │
emr/fhir/*            row → profiled FHIR resource → document bundle → validate
      │
emr/db.py             thread-local SQLite, transactions, counters, terminology
emr/schema.sql        one file, column names aligned to the FHIR elements
```

## Conventions

These are the rules the codebase actually follows; break them knowingly or not
at all.

* **Domain modules never import the web layer.** A route reads the request,
  calls a domain function, redirects. Business rules live in `services`,
  `hospital`, `dialysis`, `wellness` and `masters`, which is why 611 checks run
  without starting a server. (The two narrative strings in `fhir/bundles.py`
  that format their own dates, rather than importing a `ui` helper, are this
  rule costing three lines — accepted.)
* **Domain functions raise `ValueError` for anything an operator did wrong** —
  a full bed, an overpayment, an empty record. Routes catch it and turn it into
  a red flash. Anything else is a bug and is allowed to 500 with a traceback.
* **Multi-row writes go inside `db.transaction()`.** A half-written clinical
  record is worse than none: it looks real, and the duplicate guards then
  protect it. Transactions nest (the outermost commits), roll back number
  allocation with everything else, and writes outside one still autocommit.
* **Terminology is data, not code.** Every picker reads `db.terms(kind)`;
  adding a concept is a row in `emr/seed.py` or the `/masters/codes` editor,
  never a screen change. Records store both the code *and* display they were
  written with, so deleting a concept only removes it from the picker — history
  is never rewritten.
* **Derived artefacts are never fatal to the fact that produced them.**
  Completing a dialysis run is the clinical fact; the wellness record derived
  from it is written in its own transaction, and a failure there is reported
  loudly, not raised.
* **The sidebar lists destinations, not actions.** Every "New …" screen is
  reached from a button on its list page; `NAV_ALIAS` keeps those screens
  highlighting their parent entry.
* **Static files are an allow-list, not a directory.** A name not in the map
  never reaches the filesystem, so traversal is impossible by construction.
* **The master/transactional split is an allow-list** (`masters.MASTER_TABLES`).
  A table added later is treated as transactional unless classified, and
  `masters.unlisted_tables()` — asserted in the suite — fails the build if a
  new table is never classified at all.

## Adding a module

The same five steps every existing area follows:

1. Tables in `emr/schema.sql`. New columns on *existing* tables also go in
   `db.MIGRATIONS` so live databases pick them up on next start.
2. Any codes it needs in `emr/seed.py` under a new terminology `kind` (and a
   label in `masters.CODE_GROUPS` so the editor can reach it).
3. A domain module holding the rules — no HTML in it.
4. `emr/web/routes/<area>.py` exposing `register(app)`; add it to
   `routes/__init__.py` and to `NAV` in `emr/web/ui.py`.
5. A `section(...)` in `selftest.py` covering the happy path *and* what must be
   refused.

To export a new FHIR artifact: a builder in `emr/fhir/bundles.py`, rules in
`emr/fhir/validator.py`, the canonical URL in `emr/terminology.py`.

## The router in one paragraph

`App.add(method, pattern, fn)` with `<int:name>`/`<str:name>` path parameters;
handlers take a `Request` (query/form accessors with trimming and numeric
coercion, cookies, headers, params) and return a `Response` (or use the
`html`/`redirect`/`json_response` helpers). Redirects carry one-shot flash
messages on a cookie. `GET`, `HEAD` (same headers, no body) and `POST` are
implemented; unknown paths 404, known paths with the wrong method 405.
Everything is dispatchable in-process — `app.dispatch(...)` — which is how the
HTTP layer is tested without sockets.

## The test suite

`selftest.py` builds a throwaway database under `/tmp`, seeds it, and runs 611
checks in named sections against one shared fixture set — later sections use
records earlier ones created, so **order matters**, and the destructive
clear-patient-data section runs last. `check()` never raises; every failure is
reported, tallied per section, and the run exits non-zero at the end.

House style for new tests: prefer a check that would have caught a real bug
over one that restates the implementation. The regression sections are all
defects that reached working code, named after what went wrong — the FK
ordering that broke the reset, the truncated-but-final generated record, the
section POST that silently deleted 27 rows.
