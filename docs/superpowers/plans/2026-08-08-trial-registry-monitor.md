# Clinical Trial Registry Change Monitor — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Ingest every version of every trial registration in a working corpus, detect and classify post-enrolment outcome changes, adjudicate them, benchmark against Holst et al. ground truth, and publish a timeline-scrubber UI + aggregate index.

**Architecture:** Deterministic ingestion → extraction → normalisation → matching cascade (T0 exact → T1 lexical similarity → T2 rule discriminator → T3 LLM) → deterministic timeline classification → multi-agent LLM adjudication → sqlite → FastAPI → static single-file UI. Everything that can be deterministic is deterministic.

**Tech Stack:** Python 3.12, uv venv, httpx, pydantic, FastAPI+uvicorn, sqlite3 (stdlib), `claude -p` CLI as the LLM tier, vanilla HTML/JS UI.

## Global Constraints

- Never use the words "fraud" or "misconduct" in any product output (PRD §6.3).
- Deterministic components stay deterministic — no model calls in ingestion, extraction, timeline classification (TECH §2).
- Always use **earliest recorded** `start_date` / `primary_completion_date` across all fetched versions (TECH §6.2).
- Only `ACTUAL` dates are trustworthy; track `ESTIMATED→ACTUAL` transitions.
- Every finding must link to two registry versions (deep links: `https://clinicaltrials.gov/study/{nct}?tab=history` and per-version compare pages).
- Cache everything keyed `(nct_id, version)`; re-runs are free; ingestion resumable.
- API endpoints (verified 2026-08-08): version list `GET https://clinicaltrials.gov/api/int/studies/{nct}/history`; snapshot `GET .../history/{v}` (returns `{study:{protocolSection...}}` or bare `protocolSection`); discovery `GET https://clinicaltrials.gov/api/v2/studies` with `pageToken` pagination.
- Working corpus (bounded for this run, expandable): interventional, completed, results posted, start ≥2008. Config `CORPUS_LIMIT` caps trial count; pipeline must run identically at any N.
- Deviations from TECH-PRD §9.3 stack, all with upgrade paths noted in code as `ponytail:` comments: sqlite3 for DuckDB; lexical T1 + rule T2 for PubMedBERT/cross-encoder; `claude -p` for API LLM; vanilla JS for React/Vite.

## Repo layout

```
ctcm/                 # package
  __init__.py
  config.py           # paths, corpus filters, constants
  db.py               # sqlite schema + connection
  ingest.py           # discovery + version history fetch → cache
  extract.py          # cache → trials/versions/outcomes/timeline_facts
  normalize.py        # norm(), ABBREV, split_timepoint()
  match.py            # the cascade; match_outcomes()
  timeline.py         # earliest_dates(), anchors, TIMELINE_REVISED
  classify.py         # taxonomy → findings
  adjudicate.py       # defence/prosecution/judge via claude -p
  api.py              # FastAPI
scripts/
  run_ingest.py  run_pipeline.py  show_history.py  headline.py  run_adjudicate.py
benchmark/
  fetch_holst.py  evaluate.py
tests/
  test_normalize.py  test_match.py  test_timeline.py  test_classify.py  test_extract.py
ui/index.html         # scrubber + aggregate index (static, served by API)
data/                 # cache/ + ctcm.db  (gitignored)
Makefile  README.md  pyproject.toml
```

---

## v0.1 — Corpus

### Task 1: Scaffold + DB schema

**Files:** Create `pyproject.toml`, `.gitignore` (`data/`, `.venv/`, `__pycache__/`), `ctcm/__init__.py`, `ctcm/config.py`, `ctcm/db.py`, `Makefile`.

**Interfaces (Produces):**
- `config.DATA_DIR`, `config.CACHE_DIR`, `config.DB_PATH` (pathlib.Path); `config.CORPUS_LIMIT: int` (env-overridable, default 800)
- `db.connect() -> sqlite3.Connection` (row_factory=Row, WAL mode, creates schema idempotently)

Schema = TECH-PRD §3.4 translated to sqlite (TEXT[] → JSON text, VECTOR dropped, findings gets INTEGER PK + adjudication columns added later by Task 10):

```sql
CREATE TABLE IF NOT EXISTS trials(nct_id TEXT PRIMARY KEY, study_type TEXT, phase TEXT,
  overall_status TEXT, lead_sponsor TEXT, sponsor_class TEXT, conditions TEXT,
  enrolment_count INT, first_posted_date TEXT, version_count INT);
CREATE TABLE IF NOT EXISTS versions(nct_id TEXT, version_no INT, version_date TEXT,
  module_labels TEXT, content_hash TEXT, PRIMARY KEY(nct_id, version_no));
CREATE TABLE IF NOT EXISTS outcomes(nct_id TEXT, version_no INT, outcome_type TEXT,
  ordinal INT, measure TEXT, description TEXT, time_frame TEXT, measure_norm TEXT,
  PRIMARY KEY(nct_id, version_no, outcome_type, ordinal));
CREATE TABLE IF NOT EXISTS timeline_facts(nct_id TEXT, version_no INT, start_date TEXT,
  start_date_type TEXT, primary_completion_date TEXT, primary_completion_type TEXT,
  completion_date TEXT, PRIMARY KEY(nct_id, version_no));
CREATE TABLE IF NOT EXISTS findings(finding_id INTEGER PRIMARY KEY, nct_id TEXT,
  from_version INT, to_version INT, change_type TEXT, severity TEXT,
  before_measure TEXT, after_measure TEXT, days_after_enrolment INT,
  days_after_primary_completion INT, confidence REAL, resolved_by TEXT, rationale TEXT);
```

- [ ] Write files; `uv venv && uv pip install httpx pydantic fastapi uvicorn pytest`
- [ ] `python -c "from ctcm import db; db.connect()"` creates data/ctcm.db with tables
- [ ] Commit

### Task 2: Ingestion

**Files:** Create `ctcm/ingest.py`, `scripts/run_ingest.py`.

**Interfaces:**
- `discover(limit: int) -> list[str]` — NCT IDs via v2 `/studies?filter.overallStatus=COMPLETED&query.term=AREA[StudyType]INTERVENTIONAL AND AREA[ResultsFirstPostDate]RANGE[2008-01-01,MAX]&fields=NCTId&pageSize=1000` with pageToken pagination. (Exact filter syntax: verify empirically; `aggFilters=results:with studyType:int status:com` is the fallback.)
- `fetch_trial(client, nct) -> None` — writes `data/cache/{nct}/history.json` (version list) and `v{n}.json.gz` for: version 0, every version whose moduleLabels intersect {"Outcome Measures","Study Status"}∩Outcome-related, and the last version. **Fetch rule:** v0 + all versions with "Outcome Measures" in moduleLabels + final version. `# ponytail: skips non-outcome versions; date-revision detection limited to fetched set — fetch all versions if TIMELINE_REVISED recall matters`
- Async httpx, semaphore ≤ 8, retry w/ exponential backoff on 429/5xx (max 5), skip files already cached (resumability). Progress line every 50 trials.
- `scripts/run_ingest.py [--limit N]` — CLI entry.

- [ ] Implement; run against 5 known trials incl. NCT04280705 first; inspect cache
- [ ] P3 check: fetch one pre-2017 trial (e.g. NCT00000620, started 1999) and confirm the int API serves it in the modern JSON schema; note result in README
- [ ] Start full corpus ingest **in background**; build later tasks against partial data
- [ ] Commit

### Task 3: Extraction

**Files:** Create `ctcm/extract.py`, `tests/test_extract.py`.

**Interfaces:**
- `extract_snapshot(raw: dict) -> Snapshot` — pydantic model: `outcomes: list[OutcomeRec(outcome_type, ordinal, measure, description, time_frame)]`, `timeline: TimelineRec(start_date, start_date_type, primary_completion_date, primary_completion_type, completion_date)`, `meta` (phase, sponsor, sponsor_class, conditions, enrolment_count, overall_status). Handles both `{study:{...}}` and bare `{protocolSection:...}` shapes; missing modules → empty lists/None, never KeyError.
- `load_corpus() -> None` — walk cache, upsert all tables, content_hash = sha256 of canonical outcomes JSON. Idempotent (INSERT OR REPLACE).

- [ ] Test: extract_snapshot on a saved ACTT-1 v0 fixture returns the ordinal-scale primary outcome and 2020-03-12/ESTIMATED start
- [ ] Implement; run `load_corpus()` on partial cache; row counts sane
- [ ] Commit

### Task 4: Normalisation

**Files:** Create `ctcm/normalize.py`, `tests/test_normalize.py`.

**Interfaces:**
- `norm(s: str) -> str` — lowercase, strip punctuation, collapse whitespace, expand abbreviations (word-boundary regex over ABBREV), so downstream compares `norm(a) == norm(b)`.
- `ABBREV: dict[str,str]` — ≥40 curated entries: ham-d→hamilton depression rating scale, hamd, madrs, phq-9, os→overall survival, pfs, orr, dfs, efs, ttp, 6mwd/6mwt, hba1c, fev1, bmi, dbp/sbp, vas, sae/ae, cgi, ymrs, panss, auc, cmax, dlt, mtd, egfr, ldl, hdl, crp, nyha, acr20, pasi, edss, mmse, adas-cog, updrs, qol, sf-36, eq-5d, hr/rr (context-safe ones only).
- `split_timepoint(measure: str) -> tuple[str, str|None]` — regex `(at|after|through|during)\s+((day|week|month|year)s?\s*\d+|\d+\s*(day|week|month|year)s?)` tail-split.
- Test cases (exact, from TECH §5.1): `norm("Change in HAM-D from baseline to week 6")` and `norm("Hamilton Depression Rating Scale improvement at 6 weeks")` share the expanded scale name; `norm("All-cause mortality at 12 months") != norm("Cardiovascular mortality at 12 months")`.

- [ ] Tests → implement → pass; run over corpus to fill `measure_norm`; commit

### v0.1 CHECKPOINT
`python scripts/show_history.py NCT04280705` prints every fetched version of the primary outcome with dates — correct and complete (v0 ordinal scale → v14 time to recovery). Report ingest progress numbers.

---

## v0.2 — First signal

### Task 5: Timeline anchors

**Files:** Create `ctcm/timeline.py`, `tests/test_timeline.py`.

**Interfaces:**
- `anchors(nct_id, conn) -> Anchors(start: date|None, start_type, pcd: date|None, pcd_type, revisions: list[Revision])` — **earliest recorded** value across all fetched versions for start_date and primary_completion_date; prefer ACTUAL type flag of the *matching* record; `revisions` lists every version where either date moved (old, new, version_no) → later becomes `TIMELINE_REVISED` findings.
- `position(change_date: date, a: Anchors) -> tuple[int|None, int|None]` — days after enrolment / after primary completion.
- Test: version dates [v0 start=2020-03-12 EST, v9 start=2020-02-21 ACTUAL] → anchors.start == 2020-02-21; a fabricated forward-shift [v0 2020-01-01, v3 2020-06-01] → earliest wins + one revision recorded.

- [ ] Tests → implement → pass → commit

### Task 6: T0 diff + taxonomy + findings

**Files:** Create `ctcm/classify.py`, `tests/test_classify.py`, `scripts/run_pipeline.py`, `scripts/headline.py`.

**Interfaces:**
- `diff_pair(before: list[OutcomeRow], after: list[OutcomeRow], matcher) -> list[RawChange]` — consumes `match.match_outcomes` (Task 7; for v0.2 pass `t0_matcher` = exact `measure_norm` equality). RawChange = (kind ∈ {ADDED, REMOVED, REPLACED, DEMOTED, PROMOTED, TIMEPOINT, REWORDED}, before_rec, after_rec).
- `classify(nct, vfrom, vto, vdate, changes, anchors_obj) -> list[Finding]` — maps to codes `PRIMARY_REPLACED, PRIMARY_DEMOTED, SECONDARY_PROMOTED, PRIMARY_NARROWED, TIMEPOINT_CHANGED, POST_COMPLETION_CHANGE, PRIMARY_ADDED, PRIMARY_REMOVED, TIMELINE_REVISED`; severity: post-enrolment primary changes → SIGNAL, POST_COMPLETION_CHANGE overrides as highest; pre-enrolment or REWORDED → CONTEXT; non-outcome → not emitted (NOISE suppressed by construction — we only diff outcome rows).
- `run_pipeline.py` — for each trial: anchors → consecutive version pairs → diff → classify → findings table (delete+reinsert per trial, idempotent).
- `headline.py` — prints: N trials with ≥1 post-enrolment primary change; N with post-completion change; breakdown by change_type/severity/sponsor_class.
- Tests: demote (primary X → secondary X post-enrolment) → PRIMARY_DEMOTED/SIGNAL; same change pre-enrolment → CONTEXT; ADDED+REMOVED same version collapse → PRIMARY_REPLACED.

- [ ] Tests → implement → pass; run pipeline on ingested corpus
- [ ] Commit

### v0.2 CHECKPOINT
`headline.py` output — the defensible raw count on the ingested corpus, ACTT-1 flagged with PRIMARY_REPLACED. Decision note: breadth vs depth story.

---

## v0.3 — Semantic matching

### Task 7: Matching cascade

**Files:** Create `ctcm/match.py`, `tests/test_match.py`.

**Interfaces:**
- `match_outcomes(before: list[OutcomeRow], after: list[OutcomeRow]) -> MatchResult(pairs: list[Pair], tier_counts: dict)`; Pair = (b_idx|None, a_idx|None, relation ∈ {SAME, REWORDED, NARROWED, BROADENED, TIMEPOINT_CHANGED, DIFFERENT}, tier ∈ {T0,T1,T2,T3}, score)
- T0: `measure_norm` byte equality → SAME.
- T1: token-set Jaccard on expanded norms + greedy best-first assignment; ≥0.9 → REWORDED, ≤0.35 → DIFFERENT, else escalate. `# ponytail: lexical Jaccard stands in for PubMedBERT embeddings; swap in sentence-transformers if T1 accuracy caps out`
- T2 rule discriminator, runs on escalated band **and** on high-similarity pairs before accepting SAME/REWORDED: (a) timepoint differs but stem identical → TIMEPOINT_CHANGED; (b) qualifier-narrowing lexicon (all-cause→cause-specific: cardiovascular/cancer/…; any inserted restrictive qualifier on identical head noun) → NARROWED (reverse → BROADENED). This catches "all-cause vs cardiovascular mortality" **deterministically** — high Jaccard must not yield SAME when heads differ on the qualifier lexicon.
- T3: residual ambiguous pairs → `claude -p` (haiku), prompt returns strict JSON `{relation, confidence, reasoning}`; response cached in sqlite table `llm_cache(key TEXT PRIMARY KEY, response TEXT)` keyed by sha256(prompt); on CLI failure → relation=DIFFERENT tier=T3_FALLBACK conservative. `# ponytail: claude CLI as T3; fine-tuned cross-encoder is the scale upgrade`
- Required tests (TECH §5.1 table, verbatim): HAM-D pair → SAME-ish (REWORDED) not at T3; all-cause vs cardiovascular mortality → NARROWED/DIFFERENT never SAME; "Overall survival" vs "Overall survival at 24 months" → TIMEPOINT_CHANGED.

- [ ] Tests → implement → pass
- [ ] Wire into `diff_pair` (replace t0_matcher); rerun pipeline; record tier attribution counts in findings.resolved_by
- [ ] Commit

### v0.3 CHECKPOINT
Canonical pairs resolve correctly; tier mix printed (expect ≥85% decided ≤T1 per TECH §8.4).

---

## v0.4 — Judgement

### Task 8: Multi-agent adjudication

**Files:** Create `ctcm/adjudicate.py`, `scripts/run_adjudicate.py`.

**Interfaces:**
- `adjudicate(finding_row, conn) -> Adjudication(severity_confirmed: str, confidence: float, rationale: str, defence: str, prosecution: str)` — three `claude -p` calls (defence → prosecution → judge, judge sees both), strict JSON out, every prompt+response appended to `data/adjudication_log.jsonl`, results into findings columns (`ALTER TABLE findings ADD COLUMN defence TEXT/prosecution TEXT` + update confidence/rationale). SIGNAL findings only; cap via `--limit`; cached by finding content hash.
- Prompts must never invite or produce the words "fraud"/"misconduct"; judge outputs concern ∈ {low, moderate, high} + confidence + rationale referencing the dates.

- [ ] Implement; adjudicate ACTT-1's finding end-to-end; inspect rationale quality
- [ ] Run over SIGNAL findings (bounded); commit

### v0.4 CHECKPOINT
Every flagged trial in the corpus carries rationale + counter-argument; show ACTT-1's.

---

## v0.5 — Validated

### Task 9: Benchmark vs Holst

**Files:** Create `benchmark/fetch_holst.py`, `benchmark/evaluate.py`.

- Fetch Holst et al. 2023 data: try `https://github.com/Martin-R-H/InvisibleOutcomeChanges` (raw CSVs), fallback OSF `osf.io/e2uct`. Inspect columns; map their outcome-change labels to our change_type codes (document mapping in benchmark/README.md).
- Split trial IDs 50/50 dev/held-out with fixed seed. **Iterate only on dev.** One final held-out run.
- `evaluate.py` — run our pipeline on the Holst trial IDs (ingest them if absent), join predictions to labels, report per change_type precision/recall/F1, overall FPR, tier attribution. Output `benchmark/results.md`.
- Human κ inter-rater set (TECH §8.3) requires human coders → out of machine scope; state as limitation in results.md.

- [ ] Fetch + inspect + map; ingest Holst trials; dev eval; iterate if mapping bugs (not model overfitting); single held-out run; write results.md as-is
- [ ] Commit

### v0.5 CHECKPOINT
`benchmark/results.md` with per-type P/R/F1 + FPR, held-out discipline intact, limitations stated.

---

## v0.6 — Public

### Task 10: API

**Files:** Create `ctcm/api.py`.

- FastAPI: `GET /api/trials?severity=&change_type=&sponsor_class=&phase=&q=&sort=` (aggregate index rows + headline counts), `GET /api/trials/{nct}` (versions, outcomes per version, timeline anchors, findings w/ adjudication), `GET /` serves `ui/index.html`. CORS open. `uvicorn ctcm.api:app`.

- [ ] Implement; curl both endpoints; commit

### Task 11: Timeline scrubber UI + aggregate index

**Files:** Create `ui/index.html` (single file, vanilla JS, no build step).

- Index view: headline counter ("N trials changed their primary outcome after enrolment began"), filter chips (severity, change type, sponsor class, phase), sortable table → click → trial view.
- Trial view (the scrubber, PRD §9): horizontal timeline, version nodes, two vertical reference lines (enrolment start, primary completion), drag/click a node → outcome panel shows that version's primary+secondary outcomes; changed text diff-highlighted (removals struck, additions marked, word-level LCS diff in JS); always-visible version date + "N days after enrolment"; deep links out to `https://clinicaltrials.gov/study/{nct}?tab=history#version-content` both versions.
- Case studies strip: ACTT-1 + 2–4 pipeline-found trials (pick highest-severity, well-documented ones), one-line story each.
- Design: follow global CLAUDE.md §8 — deliberate palette/typography, signature element = the scrubber itself; no AI-default looks.

- [ ] Build against hardcoded ACTT-1 JSON first, then switch to API; verify in browser (screenshot); commit

### v0.6 CHECKPOINT
Browser demo: open trial, drag slider, mortality-style switch visible with dates on screen, aggregate index filters work.

---

## v1.0 — Complete

### Task 12: Reproducibility + methodology

**Files:** Create/finish `Makefile` (`make ingest pipeline adjudicate benchmark serve all`), `README.md` (what it is → quickstart → headline number → methodology → **documented limitations** per PRD §7.4: detects that not why; registry quality varies; US-only; corpus bounds; benchmark caveats), `docs/methodology.md`.

- [ ] Clean-run check: fresh clone semantics — `make all` from cache reproduces headline number; README numbers match `headline.py` output
- [ ] Final commit; tag v1.0

### v1.0 CHECKPOINT
Full report: headline number, benchmark table, tier mix, UI demo, limitations — reproducible from the repo.

## Self-review notes

- Spec coverage: PRD §3.1 output fields → findings table + trial API; §6.3 taxonomy → Task 6; TECH §6.2 date trap → Task 5; §5.1 dangerous cases → Task 7 tests; §7 adjudication → Task 8; §8 benchmark → Task 9; §9 interface → Tasks 10–11; preflight P1 done (2026-08-08, ACTT-1), P2 folded into v0.2 headline, P3 folded into Task 2.
- Deliberate scope bounds vs TECH-PRD, reported honestly at checkpoints: bounded corpus (CORPUS_LIMIT, expandable); no GPU cross-encoder (rule T2 + LLM T3); no human κ study (needs humans); EU registries roadmap-only (per PRD).
