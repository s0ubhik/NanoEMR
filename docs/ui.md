# The UI layer

Server-rendered HTML built strictly from the [0build](https://0build.dev)
design system, kit 0.5.4, loaded from jsDelivr. No templates — `emr/web/ui.py`
is a set of Python functions returning HTML strings.

## The 0build contract

Two kinds of class, and it matters which is which:

* **Components** — `z-card`, `z-button`, `z-input`, `z-select`, `z-table`,
  `z-nav`, `z-tab` + `data-z-switcher`, `data-z-accordion`, `z-alert`,
  `z-label`, `z-badge`, `z-breadcrumb`, `z-avatar`, `z-h1`–`z-h6`, and the
  `<z-icon>` Lucide element.
* **Utilities** — one generic class per CSS property with the value supplied
  through a custom property, *not* Tailwind-style value-in-the-class-name:

  ```html
  <div class="display-grid gap md:grid-cols" style="--gap: 4; --md-grid-cols: 2">
  ```

  `ui.st(gap=4, md_grid_cols=2)` renders that style attribute (underscores
  become hyphens).

Colours come from the theme tokens (`--z-bg`, `--z-bg-f`, `--z-muted-f`,
`--z-border`, `--z-primary`, …), so the light/dark toggle works without bespoke
CSS. The brand is dark purple via a four-line token override in `BRAND_CSS` —
the kit defines its tokens inside `@layer theme`, so an unlayered block wins
without specificity games:

```css
:root { --z-primary: var(--color-purple-800); --z-primary-f: var(--color-purple-50); }
.dark { --z-primary: var(--color-purple-500); --z-primary-f: var(--color-purple-50); }
```

Every class emitted across all screens is audited against `dist/css/kit.min.css`
during review; the single deliberate exception is `z-form-controls`, documented
in the kit's form API but unstyled in 0.5.4.

## Traps that cost real debugging time

Learn these before touching the markup:

1. **Opacity-composed tokens.** `--z-muted` is `#000` with a *separate*
   `--z-muted-o: 4%`; `--z-border` is `#000` at 10%. The plain `bg` / `border`
   classes apply the base colour at full strength — **solid black**. Only the
   colour-mixing variants compose them: `bg/o`, `border/o`, `border-b/o`. This
   once made a code block render black-on-black and read as "missing content".
2. **`z-overflow-auto` is not an overflow utility.** It is a *modal padding*
   helper. The real utilities are `overflow-auto` / `overflow-x-auto`.
3. **The switcher hides panes through the `z-switcher` class**
   (`> :not(.z-active) { display: none }`). Putting `data-z-switcher` on the
   content container initialises a second component and hides nothing — every
   pane renders stacked and the tab bar goes inert. Correct pairing:
   `<ul data-z-tab="connect: #id">` + `<ul id="id" class="z-switcher">`.
4. **`z-nav-default` barely marks the active item** (font-weight only).
   Sidebars want `z-nav-alt`, which paints the filled pill.
5. **Raw-value utilities are bracketed**: `[max-h]` takes `--max-h` verbatim,
   while `max-h` multiplies by the spacing scale.

The self-test's *0build markup contract* section pins traps 1 and 3 so they
cannot silently return.

## The helper vocabulary

Screens are composed from `ui.py` helpers rather than raw strings. The ones a
new screen actually needs:

| Helper | Renders |
|---|---|
| `page(title, active, body, …)` | The shell: head, header, sidebar, flash, scripts |
| `card(title, body, actions=, footer=)` | `z-card` with optional header/footer |
| `stack(*blocks)` | Vertically-spaced page sections |
| `table(headers, rows, empty=, align_right=)` | Scrollable `z-table`, or an empty-state line |
| `grid(*cells, cols=)` / `field(label, control)` | Form layout |
| `text_input / textarea / select / checkbox` | Controls |
| `button(label, href=, …)` / `post_button(action, label, fields=, confirm=)` | Link buttons and one-button POST forms |
| `confirm_form(action, label, question)` | The destructive variant |
| `label_chip / badge / flag_chip(interp)` | Chips; `flag_chip` is the one H/L/N colour map |
| `when(ts)` / `muted(text)` / `sub(main, sub)` | Instant formatting and secondary lines |
| `patient_cell(row)` (in `common.py`) | Patient link + MRN sub-line |
| `dl(pairs, cols=)` | Term/value detail grids |
| `accordion(items)` / `tabs(items, active=)` | Disclosure and tab/switcher pairs |
| `repeater(key, template, rows)` / `row_shell` / `cell` | Repeating form blocks (prescriptions, invoice lines) |
| `stat(label, value, icon)` | Dashboard tiles |
| `empty_state(message, action)` | Centred placeholder |

Conventions: tables cap at ~8 columns — merge related facts into a main line
with a `muted` sub-line rather than adding columns; every state-changing action
that is not a full form is a `post_button`; destructive ones always `confirm`.
