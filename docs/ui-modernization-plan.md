# UI modernization plan

A review of the evidence-browser SPA (`agr/static/`) and a staged plan to give
it a modern, calm, data-dense look without changing what it says. The review
covers every surface in the demo store (`agr demo`): Runs, run review
(Overview · Key moments · Checks), the evidence panel, the Trace drawer,
Patterns, Compare versions, the modals, dark mode and a 390px phone width.

## Status

All phases below shipped, one commit each (plus one follow-up-fixes commit
after the Phase 3/4 review). `git log` on this branch has the full list.

| Phase | Status |
|---|---|
| 0. Quick fixes | Done |
| 1. Foundations (fonts, tokens) | Done |
| 2. Components (icon sprite, shared classes) | Done |
| 3. Shell and navigation | Done |
| 4. Run review | Done |
| — Phase 3/4 review fixes (evidence-rail collapse, verdict duplication, dense queue rows, sidebar footer/collapse, merged "⋯" menu, Runs page title) | Done |
| 5. Workspaces (Runs table, Patterns taxonomy, Compare versions) | Done |
| 6. Trace, modals, polish (a11y, mobile) | Done |
| 7. Cleanup | Done, with one target not fully reached (see below) |
| — Final review fixes (Runs "evidence-grounded finding" wording, mobile drawer closes on Esc / navigation, "⋯" menu no longer opens over the page, phone layout for the Runs table, Compare form and wide tables) | Done |

`agruisample.html` was restyled to the new tokens but keeps its original
three-pane layout (top-bar nav, separate run-list column); it is a static
mock, not the shipped shell.

Decisions taken on the plan's open questions (§ "Open questions for the
owner"):

- **Queue sidebar (Q1):** kept, not removed — slimmed to one dense row per
  run (status dot, task name, checks tally, a small review-state icon) inside
  the same left sidebar as primary nav, collapsible to a 56px icon rail. The
  full-width Runs page stays the other way to reach a run; run-to-run
  movement inside a review uses the header stepper, `N`/`J`/`K`, and "Back to
  runs", as the plan's Phase 3 section always intended.
- **Serif headline (Q2):** dropped. The app is all-sans (Inter Variable for
  UI, Geist Mono Variable for data) — no serif anywhere, including the
  Overview finding headline that was the plan's one candidate for keeping
  one.
- **Accent color (Q3):** blue (`#2f5bea` light / `#7d9bff` dark), as
  proposed — not the old rust. Rust/moss/amber survive only as the fail/
  pass/warn semantic tokens; violet (`--ai`) is reserved for AI-interpretation
  blocks.
- **Icon sprite license:** Lucide (`agr/static/icons.svg`), ISC — see
  `agr/static/ICONS-LICENSE`.

Known gap: `agr/static/app.css` is ~1,450 lines, not the ≤900 target in the
Phase 7 cleanup row. Every legacy token alias (`--paper`, `--ink`, `--muted`,
`--rust`, `--moss`, `--amber`, `--violet`, `--navy`, `--sans`, `--mono`, …)
was migrated to its canonical name and deleted, and grep-confirmed dead
selectors were removed, but the file's length is mostly real, working
component styles for a genuinely large number of distinct surfaces (Runs,
Patterns, Compare, the run-review chapters, the evidence panel, the trace
inspector, modals, the sidebar/topbar shell) — trimming comments alone
(~290 lines across 78 multi-line blocks) could not close a 550-line gap
without deleting behavior. Several of the longest rationale comments were
shortened; a full line-by-line rewrite of the remaining ones was judged not
worth the risk of quietly losing a still-relevant "why" for a line-count
target versus the actual gain.

## Constraints that shape the plan

- **No build step.** The SPA ships as package data (`index.html`, `app.css`,
  `static/js/*.js`) and must keep working from `pip install .`. Plan: stay
  vanilla JS and hand-written CSS. No React/Tailwind port. New assets (fonts,
  icon sprite) go under `agr/static/` and get added to
  `[tool.setuptools.package-data]` in `pyproject.toml`.
- **Offline-first.** `agr demo` needs no network. Fonts are self-hosted
  `woff2`, not a CDN link.
- **Tests select by class name.** `tests/test_ui.py` (about 2,000 lines,
  browser-driven) queries `.run-header`, `.runs-row`, `.vs-label`,
  `.runs-controls-row .more-filter-panel`, `.queue`, `.evidence-panel` and
  others. Restyle behind the existing class names and rename nothing unless a
  test changes in the same commit.
- **Vocabulary and honesty rules stay.** Labels like "Hypothesis", "Not
  evaluated", "Demo override — contract not human-confirmed" are product
  semantics (see `docs/verdict-vs-analysis.md`). The redesign changes how they
  look, not whether they show.

## Reference direction

The Applied Compute trace-analysis page
(appliedcompute.com/platform/billion-token-scale-trace-analysis) is a good tone
reference for a trace-analysis product:

- white canvas, near-black ink (`#0e0e0a`), neutral greys, no warm beige;
- Inter for UI, Geist Mono for data, a serif (Tiempos) **only** for the page
  headline;
- hairline borders (`#ededed`), 2–4px radii, almost no shadow;
- one accent color; everything else is greyscale.

Its content also fits Patterns: failure modes shown as a **named taxonomy with
definitions and exclusions**, with calibrated confidence per label. That
informs Phase 5 below.

Target feel: Linear / Vercel / Langfuse-style developer tool. Neutral surfaces,
one accent, semantic color only for outcome (fail / warn / pass), dense but
readable tables, crisp 1px lines.

---

## Review findings

### 1. Foundations (type, color, spacing)

| # | Finding | Evidence |
|---|---------|----------|
| F1 | **Inter is declared but never loaded.** Every screen falls back to the OS sans (Arial/Liberation in CI), and Georgia is used for headlines. This alone makes the app look dated. | `--sans:Inter,…` in `app.css:11`; no `@font-face` anywhere |
| F2 | **23 distinct font sizes** from 8px to 27px, 40% of them ≤10.5px. Sizes like 8px, 8.5px and 9px are below legibility. | `grep font-size app.css` gives 8, 8.5, 9, 9.5, 10, 10.5, 11, 11.5, 12, 12.5, 13, 13.5, 14, 15, 16, 17, 18, 19, 20, 21, 22, 27 |
| F3 | **16 font weights** (400…800, including 560, 620, 670, 730, 760, 780). The fallback fonts only have 400/700, so the fine gradations don't render. | same file |
| F4 | **12 border-radius values** (3–14px, plus 999px pills). Cards at 14px sit next to chips at 5px and buttons at 8px. | same file |
| F5 | **34 uppercase, letter-spaced eyebrows.** Almost every section, label and table header shouts in small caps, so nothing stands out. | `text-transform:uppercase` ×34 |
| F6 | Warm "paper" palette (`#f3f1eb` canvas, navy chrome, rust/moss/amber). It's coherent but reads as an editorial template, not a modern tool. Rust is used both as the brand accent and as the "failed" color, so every link looks like an error. | `:root` tokens |
| F7 | **Low-contrast text.** `--faint #9aa5a9` on `--paper #fbfaf6` is about 2.5:1, used for meta lines, empty chips and captions. | tokens |
| F8 | The token block is copied three times (media query, `[data-theme=light]`, `[data-theme=dark]`), about 40 lines of duplication. | `app.css:1-41` |

### 2. Dark mode bugs

| # | Finding |
|---|---------|
| D1 | **15 dark overrides only apply with an explicit `data-theme="dark"`**, not under `prefers-color-scheme: dark`. A user whose OS is dark (no toggle clicked) gets navy-on-charcoal text: "FULL BREAKDOWN", "Show full text", the active filter chip, the evidence tab. Seen in the dark screenshot of Key moments. |
| D2 | The model chip ("AI accounts/fireworks/models/kimi-k3 · 2026-08-25") is almost invisible in dark mode. |
| D3 | The navy app bar is the same in both themes, so dark mode has no clear chrome/content split. |

### 3. Global chrome and navigation

| # | Finding |
|---|---------|
| N1 | The app bar holds four bordered "Runs / Patterns / Compare versions / Glossary" buttons that look like secondary actions, not navigation. There is no active-state indicator. |
| N2 | **Two competing navigation systems.** Runs appear both in the full-width Runs page *and* in a 296px left "Runs inbox" that duplicates the same filters, sort and stats while you review a run. |
| N3 | Icons are Unicode glyphs (☰ ◐ ⋯ ↗ ▸ ▾ ⌕ ‹ ›). They render at inconsistent sizes and baselines. The theme toggle shows as a half-circle sliver and the search icon is a rotated "⌕". |
| N4 | "RK" avatar is hard-coded and does nothing. |
| N5 | Glossary is a top-level nav item, but it's help content. It belongs under "?" with the shortcuts. |

### 4. Run review (Overview · Key moments · Checks)

| # | Finding |
|---|---------|
| R1 | **Header overload.** Above the tabs sit: eyebrow + stepper, title, mono id wrapping onto two lines, the verdict sentence, a FAILED badge, then **six chips over three rows** (AI-enriched review, Moments found, model+date, Demo override, a large Deterministic/AI segmented pill, Unreviewed). About 300px before any content. |
| R2 | The reviewer selector (Deterministic vs AI · model) is styled as a big floating pill that looks like the primary control on the page. It's a secondary toggle. |
| R3 | **The "main finding" headline is often machine text**, e.g. "Grounded in quoted run evidence (evt_149, evt_151, evt_160, evt_164, evt_165), a likely explanation for the outcome." rendered in 27px rust serif. The largest text on the page is a list of event ids. |
| R4 | The chapter tab row mixes three control types: dotted tabs, plain links (↗ Trace, Source) and two dropdowns (More analysis ▾, Compare ▾). |
| R5 | **Timeline bugs.** The "Submission" phase band spills past the right edge of the track. The "Execution" label is clipped to the band's top edge. The Key-moments lane is a mostly empty 566px bar with one dot. |
| R6 | The evidence panel (358px) shows only "Evidence follows the Key-moments chapter's selected moment." on Overview and Checks. A third of the screen is empty on two of three chapters. |
| R7 | Moment cards use collapsible `▸ FULL BREAKDOWN` / `▸ EVAL LESSON AVAILABLE` summaries in uppercase accent color. They read like warnings. |
| R8 | Execution-quality table on Overview: "— not evaluated" status pills in monospace wrap onto two lines. |

### 5. Runs page

| # | Finding |
|---|---------|
| U1 | The table sits in a card inside a card (the meta row "27 runs · Sorted by…" repeats the stat line above it). |
| U2 | The "Main finding" column repeats the machine text from R3, so rows are hard to scan. |
| U3 | Review status is a bordered pill in every row, all "Unreviewed". That's noise when every value is the same. |
| U4 | No row density control, no sticky header, no column for model/harness (it's buried in grey meta text). |
| U5 | The copy-id glyph (⧉) sits between the id and model with no hover affordance. |

### 6. Patterns and Compare versions

| # | Finding |
|---|---------|
| P1 | Patterns opens with an Execution-quality table whose first two rows are "0 of 27 evaluated — no runs". The page leads with a dead table. |
| P2 | Stat tiles (6 / 6 / 0) are small centered numbers in wide empty boxes. |
| P3 | Explanatory paragraphs are indented to about 270px for no structural reason, which leaves a ragged left edge. |
| P4 | Pattern rows are not presented as a taxonomy (name → definition → examples → confidence). They are a flat table with an expand arrow. |
| C1 | Compare versions: the match keys are dark navy pills that look like buttons but are static labels. |
| C2 | The "Not a matched comparison" error sits in its own card with a red box inside a white box. |
| C3 | The match report shows seven identical stat boxes in a row with equal weight. The two numbers that matter (matched pairs, exclusions) don't stand out. |

### 7. Trace drawer

| # | Finding |
|---|---------|
| T1 | **Bug:** step rows are `<button>`s without `border:0`, so every step gets the browser's thick 2px button frame. It's the most visibly broken screen. |
| T2 | No step is selected on open, so all four detail panes say "Select a step." |
| T3 | The drawer is 820px max but the step list is a narrow column. Detail panes are 2×2 small boxes rather than one readable inspector. |

### 8. Mobile (390px)

| # | Finding |
|---|---------|
| M1 | Run header: the id wraps onto four lines of mono. The FAILED badge sits beside the title and squeezes it to about 150px. |
| M2 | The reviewer segmented pill wraps the model name onto two lines. |
| M3 | Runs table drops to two columns but keeps card padding inside card padding, so about 40px of the 390px is lost to nesting. |

### 9. Code health (affects how fast we can restyle)

- `app.css` is 1,280 lines. A large share is historical-rationale comments
  ("Redesign follow-up…", "U1:", "T2:").
- The `summary ▸/▾` disclosure pattern is re-implemented about 8 times
  (`.moment-detail`, `.moment-lesson`, `.evidence-neighbors`, `.caps-disclosure`,
  `.vs-drill`, `.evidence-provenance`, `.coverage-detail`, `.fleet-example-raw`).
- Runs-table column widths are hand-tuned for four `has-duration` / `has-cost`
  combinations (20 lines).
- There are no spacing, type-scale or radius tokens; every value is literal.

---

## Design system (target)

### Tokens

```css
@layer tokens {
  :root {
    /* neutrals — light */
    --bg:        #ffffff;   /* app canvas */
    --bg-subtle: #fafafa;   /* sidebars, table header, hover */
    --bg-muted:  #f4f4f5;   /* chips, code, inputs */
    --border:    #ececec;   /* hairline */
    --border-strong: #dedede;
    --fg:        #0e0e0a;   /* ink */
    --fg-muted:  #5c5c58;   /* secondary text  (≥ 7:1) */
    --fg-subtle: #85857f;   /* meta, captions (≥ 4.5:1) */

    /* one accent (interactive: links, focus, active tab, primary button) */
    --accent:    #2f5bea;   --accent-soft: #eef2fe;

    /* semantic — outcome only */
    --fail: #d6402f; --fail-soft: #fdf0ee;
    --warn: #b7791f; --warn-soft: #fdf6e7;
    --pass: #2f8a4c; --pass-soft: #edf7f0;
    --ai:   #7a5af8; --ai-soft:   #f3f0ff;   /* model-interpretation blocks */

    /* type */
    --font-sans: "Inter Variable", Inter, ui-sans-serif, system-ui, sans-serif;
    --font-mono: "Geist Mono", ui-monospace, "SFMono-Regular", monospace;
    --text-xs: 11px; --text-sm: 12.5px; --text-base: 14px;
    --text-lg: 16px; --text-xl: 20px; --text-2xl: 26px;
    --weight-normal: 400; --weight-medium: 500; --weight-semibold: 600;

    /* space (4px grid) + radius + elevation */
    --space-1: 4px; --space-2: 8px; --space-3: 12px; --space-4: 16px;
    --space-5: 20px; --space-6: 24px; --space-8: 32px;
    --radius-sm: 4px; --radius: 6px; --radius-lg: 10px; --radius-full: 999px;
    --shadow-pop: 0 8px 24px rgb(0 0 0 / .08), 0 0 0 1px var(--border);
  }
  /* dark: defined ONCE, applied for both OS preference and explicit toggle */
  @media (prefers-color-scheme: dark) { :root:not([data-theme="light"]) { /* dark values */ } }
  :root[data-theme="dark"] { /* same dark values */ }
}
```

Rules:

- **Six type sizes, three weights.** Nothing below 11px.
- Uppercase eyebrows are kept only for table headers and the sidebar group
  labels (11px, `--fg-subtle`, `letter-spacing:.04em`). Everything else uses
  sentence case.
- The serif is dropped from UI chrome. Optional: keep one serif (e.g. Source
  Serif 4, self-hosted) for the run's finding headline only, following the
  reference page.
- **Color means something.** Red/amber/green only for outcome and severity.
  Violet only for AI interpretation. Blue only for interactive elements.
  Everything else is grey.
- Borders over shadows. A shadow appears only on floating layers (menus,
  drawer, modal, toast).
- All dark overrides live in the token block. Component rules never contain
  `:root[data-theme="dark"] .x` (this fixes D1 by construction).

### Components (one definition each)

`button` (primary / secondary / ghost / icon), `badge` (outcome, neutral,
ai), `chip` (filter toggle with count), `tabs` (underline), `segmented`
(small), `disclosure` (one `details > summary` style with an SVG chevron),
`table` (sticky header, 36/44px row density, hover row, right-aligned
numerics in mono), `stat` (label over number, left-aligned), `callout`
(info / warn / fail, left border and tinted background, no nested card),
`kbd`, `tooltip`, `menu`, `drawer`, `modal`, `toast`, `empty-state`.

### Icons

Replace the Unicode glyphs with an inline SVG sprite
(`agr/static/icons.svg`) of about 20 Lucide icons (MIT): menu, search,
chevron-right/down/left, sun/moon, more-horizontal, copy, check,
external-link, list-tree, git-compare, layers, book-open, keyboard, x,
alert-triangle, circle-check, circle-x, sparkles (AI). Add a helper
`icon(name)` in `ui-utils.js` that returns `<svg><use href="/static/icons.svg#name"/></svg>`.

---

## Layout (target)

```
┌──────────┬────────────────────────────────────────────┬───────────────┐
│ ▣ AGR    │  Runs / make-mips-interpreter      ‹ 5/27 › │               │
│          ├────────────────────────────────────────────┤               │
│ Runs   27│  make-mips-interpreter        [Failed 0/1] │  Evidence     │
│ Patterns │  stealth/ox-alpha · harbor · 165 steps · ⧉ │  (only when a │
│ Compare  │  Review: [Deterministic | AI ▾]  ⚠ Demo     │   moment is   │
│          │  ──────────────────────────────────────── │   selected;   │
│ ──────── │  Overview  Moments 1  Checks  Trace ⋯      │   collapsible │
│ Failed 10│                                            │   otherwise)  │
│ Passed 17│  content                                   │               │
│ Unrev. 27│                                            │               │
│          │                                            │               │
│ ? ◐      │                                            │               │
└──────────┴────────────────────────────────────────────┴───────────────┘
```

- **Left sidebar replaces the navy app bar** (224px, collapsible to 56px
  icons). It holds the product mark, primary nav (Runs · Patterns · Compare
  versions) with an active indicator and counts, saved views (Failed /
  Unreviewed as one-click filters), and help/theme at the bottom. This gives
  one navigation system (fixes N1, N2, N5).
- **The 296px run-queue sidebar is removed.** Run-to-run movement stays via
  the header stepper (‹ 5/27 ›), `N` / `J` / `K` keys, and a "Back to runs"
  breadcrumb that restores scroll and the active row (logic already exists in
  `runs.js`). Optional: a ⌘K command palette to jump to a run by name.
- **Thin top bar** inside the main column: breadcrumb and run stepper only.
- **The evidence panel shows only when it has something to show** (Key
  moments with a selected moment, or a clicked evidence link). Otherwise it
  collapses to a 40px rail with an "Evidence" label. The reading column gets
  the space back on Overview and Checks (fixes R6).
- Main reading column stays at about 880px for prose. Workspace pages (Runs,
  Patterns, Compare) go full width up to 1400px as today.

---

## Surface-by-surface changes

### Run review header (R1, R2, M1, M2)
- One title line: task name at `--text-xl` semibold. The outcome badge sits
  inline after the title, not as a floating box.
- One meta line: model · harness · steps · short id with a copy icon. The full
  id goes in a tooltip, not wrapped mono.
- One controls row: small segmented `Deterministic | AI` with the model name
  in a dropdown or tooltip, then the status chips (Demo override, Unreviewed)
  as compact neutral badges. "AI-enriched review" and "Moments found" move
  into the tooltip of the review toggle, because they describe it.
- Target: header ≤ 140px at 1440px wide, ≤ 220px at 390px.

### Chapter navigation (R4)
- Plain underline tabs: Overview · Key moments (count badge) · Checks.
- Trace and Source become icon buttons on the right. "More analysis" and
  "Compare" merge into one "⋯" menu.

### Overview (R3, R8)
- **Finding card:** show the model's one-line claim as the headline. When the
  headline is machine boilerplate ("Grounded in quoted run evidence (evt_…)"),
  lead with the verdict sentence instead, and render event ids as small mono
  links under it. This is a display rule in `chapters.js`; the stored review
  is not changed.
- Headline at `--text-2xl`, `--fg` color. Outcome color only on a 3px left
  border and the badge, not on 27px text.
- Execution-quality table: status as a small dot + label ("Not evaluated",
  "Healthy") with no mono pill. Rows that are "not evaluated" go into one
  collapsed "2 dimensions not measurable — missing generation_usage,
  generation_timestamps" line.

### Key moments (R5, R7)
- Timeline: fix the band overflow (clamp width to the track, `overflow:hidden`
  on `.lane-track`) and label clipping (vertically center the label). Make
  lanes 20px tall with an 11px label inside. Moment markers become numbered
  8px dots with hover tooltips.
- Moment card: a kind badge (e.g. `Strategy drift`, sentence case), headline,
  the quoted observation as a styled blockquote, evidence ids as mono links.
  The disclosure rows ("Full breakdown", "Eval lesson") use the shared
  disclosure style, sentence case and muted, with a chevron.
- Interpretation blocks keep the violet tint plus a small "AI" badge (the
  fact vs. interpretation distinction is a product rule).

### Evidence panel
- Header: "Evidence" with a count, and Evidence grade / Cause as two inline
  label:value rows (not boxed tiles).
- Step selector as a segmented tab strip. Source content in a code block with
  line wrap and a "Show full text" button that is visible in both themes.

### Runs page (U1–U5)
- Single toolbar: search (with SVG icon and `/` shortcut hint) · filter chips
  with counts · "More filters" · sort on the right. Drop the outer card.
- Table flush in one bordered container with a sticky header, 44px rows, and
  columns: Task (name + id/model meta) · Outcome (badge + checks) · Finding
  (1 line, ellipsis, full text on hover) · Status · Duration (right-aligned
  mono).
- Status column: show a pill only when not "Unreviewed". Unreviewed is the
  implied default, shown as a small empty circle.
- Replace the 20 lines of per-combination column widths with
  `table-layout:auto` plus `min-width`/`max-width` on Finding.

### Patterns (P1–P4)
- Reorder: stat row → pattern taxonomy → execution quality (collapsed when
  mostly unevaluated).
- Stat row: left-aligned number at `--text-2xl` with the label beneath, in
  one bordered strip split by dividers (not three separate boxes).
- **Taxonomy view** (from the reference page): each pattern row shows name,
  one-line definition, affected runs / episodes, recovery split as a small
  stacked bar (confirmed / plausible / unrecovered), and severity as a left
  stripe (kept). Expanding shows examples and evidence limits.
- Drop the 270px paragraph indents. Helper text goes under the section title
  at `--fg-muted`.

### Compare versions (C1–C3)
- Match keys become neutral outline chips (`--bg-muted`, mono), clearly static.
- The "not matched" state is one fail callout directly under the title, with
  no extra card.
- Match report: highlight "Matched run pairs" and "Excluded" as the two
  headline stats. The other five sit in a compact key:value grid.

### Trace drawer (T1–T3)
- Fix the step button border (`border:0` in the shared button reset).
- Auto-select the first step (or the step in `?evidence=`) on open.
- Layout: step list (virtualized-ish; already one column) + a single
  inspector pane with tabs (Messages · Tool I/O · Environment · Artifacts ·
  Verifier) instead of five small boxes. Empty tabs show as disabled.
- Widen to `min(1100px, 96vw)`.

### Modals, toast, empty states
- Modals: 12px radius, `--shadow-pop`, title at `--text-lg` sans (no
  Georgia), footer actions right-aligned.
- Shortcuts modal: two columns of `kbd` + label. Glossary becomes a tab in
  the same help dialog.
- Empty states: icon + one line + one action (e.g. "No evidence yet — select
  a moment").

### Motion
- 120–160ms ease-out on hover, menus, drawer and tab underline. Everything is
  already behind `prefers-reduced-motion`; keep that.

---

## Phased delivery

Each phase is one PR, green on `pytest` (including `tests/test_ui.py`) and
visually checked with screenshots of the seven surfaces × light/dark × 1440/390.

| Phase | Scope | Risk | Size |
|------|-------|------|------|
| **0. Quick fixes** | T1 trace step borders; D1 move the 15 `data-theme`-only dark rules into the token block so OS-dark works; R5 timeline overflow and label clipping; F7 raise `--faint` contrast. No visual redesign. | Low | S |
| **1. Foundations** | Self-host Inter Variable + Geist Mono (`agr/static/fonts/*.woff2`, add to package-data, OFL license files). New token layer (color, type, space, radius). Map old tokens to new ones (`--paper` becomes `--bg`, etc.) so existing rules keep working. Collapse the 3× token duplication. Introduce `@layer tokens, base, components, surfaces, utilities`. | Med: every screen shifts | M |
| **2. Components** | Unify button / badge / chip / tabs / segmented / disclosure / table / callout / stat. Replace 8 disclosure copies with one. Add the SVG icon sprite and an `icon()` helper; swap Unicode glyphs in `index.html` and the JS templates. | Med | M |
| **3. Shell and navigation** | Left nav sidebar replaces the app bar. Remove the run-queue sidebar (keep the stepper and J/K/N). Collapsible evidence rail. Thin top bar with breadcrumb. Update `panels.js` resizer logic and `body.workspace-mode`. Update `test_ui.py` assertions that expect `.queue` to be visible. | **High**: layout and tests | L |
| **4. Run review** | Header compaction, tab row cleanup, finding-card headline rule, moment card, evidence panel, execution-quality collapse. | Med | M |
| **5. Workspaces** | Runs table, Patterns taxonomy reorder, Compare versions report. | Med | M |
| **6. Trace, modals, polish** | Trace inspector with tabs and auto-select; help dialog merges shortcuts and glossary; empty states; motion; mobile pass (390px); a11y audit (contrast ≥ 4.5:1, focus rings, real checkboxes). | Low–Med | M |
| **7. Cleanup** | Delete dead CSS and legacy token aliases. Move rationale comments out of CSS into this doc or commit messages. Target `app.css` ≤ 800 lines. Refresh `agruisample.html` and `site/index.html`, which copy the old design system. | Low | S |

Phases 0–2 can ship on their own and already change how the app looks
(fonts, neutral palette, icons). Phase 3 is the only structural change; it
should get a quick look from the user before merge.

## Verification per phase

- `python -m pytest` (full suite; `tests/test_ui.py` drives the SPA in
  Chromium).
- Screenshot script (Playwright, `/opt/pw-browsers` Chromium) over:
  `?view=runs`, `?run=…&chapter=overview|moments|checks`, `&trace=1`,
  `?view=fleet`, `?view=versions`, in light, dark (OS), dark (toggle), at
  1440×900 and 390×844. Before/after pairs go in each PR description.
- Contrast check on the token pairs (fg / fg-muted / fg-subtle on bg and
  bg-subtle, and semantic colors on their soft backgrounds).
- `bash scripts/verify-five-minute-path.sh`, to confirm the fonts and icons
  ship in the wheel.

## Open questions for the owner

1. **Queue sidebar removal (Phase 3).** Reviewers who triage many runs in a
   row may like the always-visible list. The alternative is to keep it, but
   slim (name + status dot only, 240px, collapsible).
2. **Serif headline.** Keep one serif for the finding headline (editorial
   feel, like the reference page) or go all-sans?
3. **Accent color.** Blue (`#2f5bea`) as proposed, or keep a warm brand accent
   (the current rust) separate from the "failed" red?
