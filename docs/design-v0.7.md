# v0.7 — Production surface (design spec)

The v0.6 UI proved the pipeline; v0.7 makes it a usable product. Two complaints drive
every decision here: the index dumps ~30 raw-enum filter chips and all 721 rows at once,
and the interface speaks database (`POST_COMPLETION_CHANGE`, `RECRUITING`) instead of
English. Nothing in the pipeline changes — this is the API contract and the UI layer.

Design authority: this document. The Redline brand system already in `ui/index.html`
(ink/paper/redline/meta/rule tokens, IBM Plex, red = changed record text only) is
fixed — extend it, never restyle it.

## Ownership

- **Backend agent**: `ctcm/api.py`, `tests/test_api.py`. Nothing else.
- **Frontend agent**: `ui/index.html`. Nothing else.
- The two ship against the contract below; neither edits the other's files.

## API contract changes (backend)

### 1. `GET /api/trials` — pagination

New query params: `limit` (default 50, max 200), `offset` (default 0).
Response shape stays `{rows, total, counts}` but `total` becomes the **full filtered
count** (a `COUNT(*)` over the same WHERE), while `rows` carries only the page.
Existing sort/filter params unchanged. Out-of-range offset returns empty rows, not 404.

### 2. `GET /api/meta` — one call that lets the index render without fetching rows

```json
{
  "counts": { ...same object _headline_counts returns... },
  "filters": {
    "severity":     [{"value": "SIGNAL", "count": 721}, ...],
    "changeType":   [{"value": "POST_COMPLETION_CHANGE", "count": 616}, ...],
    "sponsorClass": [{"value": "INDUSTRY", "count": 342}, ...],
    "phase":        [{"value": "PHASE3", "count": 210}, ...]
  },
  "caseStudies": [ ...top 5 SIGNAL index-rows by |days_after_primary_completion| desc,
                   same shape as /api/trials rows (incl. rationale + adjudication)... ]
}
```

Filter counts are **trial counts over the headline-finding join** (same denominator the
index table shows), sorted by count desc. Null/empty values excluded.

### 3. `GET /api/trials/{nct_id}` — headline finding on the trial payload

Add `"headlineFinding"`: the same finding `_headline_key` would rank first for this
trial (severity, change type, days after enrolment/completion, rationale, adjudication,
fromVersion/toVersion), or `null` when the trial has none. The UI's banner reads only
this field — it must not re-derive the ranking client-side.

### 4. OpenAPI polish (public-API roadmap item)

FastAPI metadata: title "Redline — Clinical Trial Registry Change Monitor API",
version "0.7", per-endpoint summaries/descriptions, response examples for /api/trials.
`/docs` stays enabled. No auth (read-only, sqlite ro).

Tests: pagination boundaries (limit cap, offset past end, total = filtered count),
/api/meta shape and count consistency vs /api/trials totals, headlineFinding presence
and null case. Keep every existing test green.

## UI (frontend)

Single self-contained `ui/index.html` stays (no build step, DEMO fallback intact).

### Vocabulary — the app speaks English everywhere

One central label map; raw enums never reach the screen (raw value goes in `title=`):

| raw | label |
|---|---|
| POST_COMPLETION_CHANGE | Post-completion change |
| PRIMARY_ADDED / _REMOVED / _REPLACED | Primary outcome added / removed / replaced |
| PRIMARY_BROADENED / _NARROWED | Primary outcome broadened / narrowed |
| PRIMARY_DEMOTED | Primary demoted to secondary |
| SECONDARY_PROMOTED | Secondary promoted to primary |
| REWORDED | Reworded |
| TIMEPOINT_CHANGED | Timepoint changed |
| TIMELINE_REVISED | Timeline revised |
| SIGNAL / CONTEXT | Signal / Context |
| RECRUITING etc. | Sentence case ("Recruiting", "Active, not recruiting") |
| INDUSTRY, NIH, FED, OTHER_GOV, NETWORK, INDIV, OTHER | Industry, NIH, US federal, Other government, Network, Individual, Other |

Timing copy: keep exact days in `title=`/mono contexts, but prose surfaces humanize:
`< 90 days` → "N days", `< 730` → "N months", else "N.N years", always with the
existing direction phrasing ("after enrolment began").

### Index view

Layout, top to bottom: masthead → notable cases → filter bar → ledger → pager → footer.

- **Masthead**: unchanged (serif thesis + red counter), now fed by `/api/meta.counts`
  so first paint needs no row fetch.
- **Notable cases** (rename from "Case studies"): the meta payload's 5, same card
  design, change-type labels humanized.
- **Filter bar** replaces the chip wall. One row of four native `<select>`s + search:
  `Severity ▾ · Change ▾ · Sponsor class ▾ · Phase ▾ · [search]`.
  Each select's first option is its placeholder ("Severity"); options read
  "Signal (721)" using meta counts. Picking an option **adds a filter token and resets
  the select to placeholder** — repeated picks accumulate (multi-select via repetition,
  no custom dropdown widget). Active tokens render on a second row as removable
  `[Signal ×]` chips (existing `.chip[aria-pressed=true]` inverted style). No tokens →
  no second row. The sponsor drill-down token keeps its current behavior, joins this row.
- **Ledger**: columns `Trial` (NCT mono + conditions), `Sponsor` (new — name, class
  beneath in meta grey), `Change` (humanized label + Signal/Context tag), `Phase`,
  `Timing` (humanized, exact days in title). Sorting unchanged. Row click unchanged.
- **Pager** under the table, mono metadata voice: `‹ Prev · 1–50 of 721 · Next ›`.
  Page size 50. Any filter/sort/search change resets to page 1. Hidden when
  total ≤ page size (DEMO mode shows no pager).
- **Footer** (all views): hairline rule, then one metadata-voice line —
  corpus caveat (from meta counts), "Redline v0.7", link to the trial registry.

### Trial view

- **Finding banner** between header and scrubber, reading `headlineFinding`: severity
  tag + humanized change type + rationale sentence + adjudication line when present
  (reuse case-card adjudication copy rules). Signal findings get the red severity tag;
  banner frame stays rule-grey (red belongs to changed text, not to chrome).
- **Diff legend**, small metadata line right-aligned above the outcome panel:
  "struck = removed · underlined = added" with an inline `<del>`/`<ins>` sample.
- **Version stepper**: `‹ v5 ›` buttons flanking the readout version so the scrubber
  isn't the only affordance; ArrowLeft/Right keeps working, add `aria-live="polite"`
  to the readout so version changes announce.
- Status pill, readout status, overall statuses: humanized.

### Analytics view

Keep the table-first design. Humanize row labels via the same map, add a one-line
metadata caption under each section title saying what the denominator is (e.g.
"Completed, results-posted trials with a headline finding"). Footer as above.

### States

Every view: a loading line in metadata voice, an error line that names the failing
endpoint and offers retry (a link that re-runs the fetch), and the existing DEMO
fallback caveat surfaced in the footer instead of the masthead tagline.

## Acceptance

- `make test` green; new API tests included.
- Index first paint: 2 requests (`/api/meta`, `/api/trials?limit=50`), not 721 rows.
- No raw enum visible anywhere in the rendered UI.
- Filter bar shows 4 selects + search on one row at desktop width; tokens removable.
- Keyboard: selects, tokens, pager, rows, scrubber all reachable and operable.
- DEMO mode (file://) still renders index + trial views with the embedded trial.
