# Clinical Trial Registry Change Monitor

Ingests every fetched version of a ClinicalTrials.gov trial registration and
detects post-enrolment changes to its primary outcome measures, anchored to
the earliest-recorded enrolment/completion dates so a later edit to those
dates can't hide a real change.

## Quickstart

```
make venv                    # uv venv + editable install into .venv/
make ingest                  # network: fetches registry history into data/cache/ (resumable, skip if already cached)
make load                    # data/cache/ -> data/ctcm.db (trials/versions/outcomes/timeline_facts)
make pipeline                # runs the T0-T3 cascade, writes findings (network-free by default, see below)
make headline                # prints the headline number + breakdowns
```

`make all` runs `load pipeline headline` in sequence — the reproducibility
check below runs exactly that, from the cache already committed in this
tree, without touching the network.

**UI:** `make serve` starts the API + UI at `http://127.0.0.1:8742/` — a
timeline scrubber over any trial's outcome-measure history, plus a
filterable index of every finding with deep links to the registry's own
`?tab=history` compare pages.

**Tests:** `make test` (147 tests, `pytest -q`).

`make adjudicate` (multi-agent LLM review of SIGNAL findings) and `make
benchmark` (scores findings against the Holst ground truth) both call out to
`claude -p` and are **not** part of `make all` — see "Reproducing the
headline number" below.

## The headline number

Verbatim `.venv/bin/python scripts/headline.py` output on the corpus checked
into this tree:

```
corpus: 1220 trials
HEADLINE -- trials with >=1 post-enrolment primary change: 721
trials with >=1 post-completion change: 616
  caveat: this corpus is results-posted trials only (ingest discovery query requires ResultsFirstPostDate) -- sponsors routinely add/adjust outcome rows around results entry as registry housekeeping, not editorial endpoint-switching, so POST_COMPLETION_CHANGE is an upper bound, not a purity signal. Lead with the post-enrolment-primary-change number above instead.
  caveat: 110 SIGNAL findings rest on unresolved semantic matches pending T3 coverage (resolved_by='T2_UNRESOLVED' -- the cascade escalated these but never got an LLM adjudication; confidence=0.5, not the usual 1.0, but severity is not downgraded).
```

by change_type: `POST_COMPLETION_CHANGE` 2171, `TIMELINE_REVISED` 1637,
`REWORDED` 483, `TIMEPOINT_CHANGED` 152, `PRIMARY_ADDED` 117,
`PRIMARY_REMOVED` 93, `PRIMARY_REPLACED` 57, `PRIMARY_DEMOTED` 14,
`PRIMARY_NARROWED` 9, `PRIMARY_BROADENED` 8, `SECONDARY_PROMOTED` 6.

by severity: `SIGNAL` 2598, `CONTEXT` 2149.

**Reproducing the headline number.** `make all` (`load` → `pipeline` →
`headline`) reproduces this exact output from the `data/cache/` and
`data/ctcm.db` already in this tree, with **no network access** —
`pipeline`'s T3 LLM-escalation budget defaults to `T3_LIMIT=0`, and
`ctcm.match.T3Client` checks its sqlite cache before checking that budget, so
every already-resolved T3 verdict still applies for free and only a
genuinely new `claude -p` call is refused. Live escalation of the residual
`T2_UNRESOLVED` pairs is `make pipeline T3_LIMIT=200` (or higher) — that does
call the LLM and can change the numbers above; `adjudicate` and `benchmark`
are the other two targets that touch the LLM/CLI and are excluded from `all`
for the same reason.

## Benchmark

Scored against the 559-trial ClinicalTrials.gov within-registry-history
subset of Holst et al. 2023 (*PLOS Medicine*,
[10.1371/journal.pmed.1004306](https://doi.org/10.1371/journal.pmed.1004306)).
Numbers below are quoted verbatim from the committed `benchmark/results_dev.md`
(dev split only — see "Documented limitations"). Full mapping/scoring
protocol: `benchmark/README.md`; how the cascade producing these findings
works: `docs/methodology.md`.

**Coverage: 151 of 279 labeled dev-split trials evaluated (54%), 128 awaiting ingest.**
Treat every number below as measured at that coverage, not the full split.

| change_type | TP | FP | FN | TN | precision | recall | F1 |
|---|---|---|---|---|---|---|---|
| PRIMARY_REPLACED | 1 | 8 | 0 | 142 | 0.111 | 1.000 | 0.200 |
| PRIMARY_DEMOTED | 2 | 0 | 3 | 146 | 1.000 | 0.400 | 0.571 |
| SECONDARY_PROMOTED | 1 | 1 | 1 | 148 | 0.500 | 0.500 | 0.500 |
| PRIMARY_NARROWED | 2 | 0 | 20 | 129 | 1.000 | 0.091 | 0.167 |
| PRIMARY_BROADENED | 0 | 0 | 1 | 150 | n/a | 0.000 | n/a |
| TIMEPOINT_CHANGED | 5 | 26 | 4 | 116 | 0.161 | 0.556 | 0.250 |
| POST_COMPLETION_CHANGE | 65 | 19 | 20 | 47 | 0.774 | 0.765 | 0.769 |
| PRIMARY_ADDED | 14 | 6 | 2 | 129 | 0.700 | 0.875 | 0.778 |
| PRIMARY_REMOVED | 0 | 6 | 0 | 145 | 0.000 | n/a | n/a |

**Low-precision flags (<30%, n≥3 predicted-positive trials) — do not treat as
reliable without further investigation:**
- `PRIMARY_REPLACED`: precision 0.111 (1 TP / 8 FP)
- `TIMEPOINT_CHANGED`: precision 0.161 (5 TP / 26 FP)
- `PRIMARY_REMOVED`: precision 0.000 (0 TP / 6 FP)

Root cause not diagnosed at this coverage level — could be a real weakness at
whichever cascade tier is resolving these pairs, a mapping edge case, or
small-sample noise. Flagged uniformly by the benchmark's own thresholding
logic, not hand-picked.

**Overall false-positive rate:** of trials where Holst confirmed no
primary-outcome change at all, 22 of 34 (0.647) were flagged with ≥1 finding
anyway.

**Tier attribution on this sample** (`findings.resolved_by`): T0 55%, T1 29%,
T3 11%, T2_UNRESOLVED 4%, T2 0% (286/152/59/19/1 of 517 decisions) — a
different population and mix than the full-corpus numbers in "Architecture"
below, since the benchmark sample is only the 151 currently-cached dev-split
trials.

## Architecture

```
data/cache/{nct}/*.json.gz  --extract-->  data/ctcm.db (trials/versions/outcomes/timeline_facts)
                                                  |
                                    T0 -> T1 -> T2 -> T3 semantic matching cascade
                                                  |
                                    timeline anchoring (earliest-recorded dates)
                                                  |
                                    taxonomy + severity classification --> findings
                                                  |
                                    (optional) multi-agent adjudication --> adjudications
                                                  |
                                    FastAPI (read-only) + vanilla-JS timeline scrubber UI
```

Everything through classification is **deterministic — no model calls**
(`ingest.py`, `extract.py`, `normalize.py`, `classify.py`, `timeline.py`).
The only LLM-touching components are T3 (residual semantic-match
adjudication, cascade tier 4 of 4) and the standalone adjudication pass;
both are cached, budgeted, and fail closed to a conservative/honest-unknown
result rather than a guess.

**Cascade tiers** (full detail, thresholds, and code references:
`docs/methodology.md` §4): T0 exact `measure_norm` match; T1 token-set
Jaccard (≥0.9 REWORDED, ≤0.35 DIFFERENT, else escalate); T2 deterministic
timepoint/qualifier-narrowing rules; T3 LLM adjudication for what's left.

**Full-corpus tier mix** (from the `make all` pipeline run behind the
headline number above, 13,153 tier decisions): T0 10,724 (81.5%), T1 312
(2.4%), T2 30 (0.2%), T3 459 (3.5%), T2_UNRESOLVED 1,628 (12.4%).

**Timeline anchoring and the date-manipulation trap:** `start_date` and
`primary_completion_date` are themselves editable registry fields — a
sponsor pushing either forward in a later version could make a real
post-enrolment change look pre-enrolment. The anchor for both is always the
**earliest recorded** value across every fetched version, never the
latest/current one; every version where either date moved is itself
recorded as a `TIMELINE_REVISED` finding. Full mechanism:
`docs/methodology.md` §5.

**Deviations from the TECH-PRD stack** (sqlite3 in place of DuckDB; lexical
Jaccard + deterministic rules in place of a fine-tuned biomedical
cross-encoder; `claude -p` CLI in place of direct API calls; vanilla
HTML/CSS/JS in place of React+Vite+Tailwind) are each flagged in the code
they affect (`# ponytail:` comments) and documented with an upgrade path in
`docs/methodology.md` §9.

### Ingestion era note

`NCT00000620` (ACCORD, started 1999-09) was fetched via the `int` history
API and confirmed to serve the same modern JSON schema
(`protocolSection.{identificationModule,statusModule,outcomesModule,...}`)
as a 2020-registered trial — no separate legacy parser needed, one
extraction path handles both eras as long as every field access defaults to
`None`/`[]` instead of raising. Era differences observed and handled
defensively (`ctcm/extract.py`): early `startDateStruct` can lack the
ACTUAL/ESTIMATED `type` key entirely; `outcomesModule` can be `{}` on `v0`
for trials registered before outcomes were required; `primaryCompletionDateStruct`
may be absent on early versions; outcome `description` text can carry raw
HTML. Full detail: `docs/methodology.md` §2.

## Documented limitations

Volunteered here rather than left for someone else to find first.

- **We detect *that* a primary outcome changed, not *why*.** Some changes
  are legitimate — a recruitment shortfall forcing a redesign, a documented
  regulatory instruction, a pre-data clarification of ambiguous wording. The
  detector has no access to sponsors' reasons; `make adjudicate` produces an
  argued-both-sides review, not a verdict on intent.
- **Registry data quality varies.** Some trials are poorly registered —
  missing dates, empty outcome modules, inconsistent free-text measures —
  and the extraction layer degrades gracefully (defaults to `None`/missing)
  rather than failing, which means a poorly-registered trial can also
  under-report findings simply because there's less to diff.
- **US registry only.** ClinicalTrials.gov exclusively; no coverage of EU
  CTIS, ISRCTN, or other national/regional registries.
- **Bounded corpus.** The working corpus is capped at `CORPUS_LIMIT`
  (default 800 trials via discovery, 1,220 currently loaded once the Holst
  benchmark trials are folded in) — a deliberate scope bound for this
  release, not a claim about the full registry. Full-scale coverage needs
  raising `CORPUS_LIMIT`/`--limit`, a full ingest run (network-bound, hours
  at current concurrency), and re-running `make load pipeline`.
- **Benchmark is at partial coverage.** The dev-split numbers above are
  measured on 151 of 279 labeled trials (54%) — not the full dev split, and
  not the full 559-trial labeled set.
- **Dev-split numbers only; held-out untouched pending a release eval.** No
  `--split heldout` run has produced or recorded a number for this release
  (`benchmark/README.md`'s "Incident log" documents one stray invocation
  that produced no visible output and informed no decision — see
  `docs/methodology.md` §8 for the full account and the gate it led to).
  Held-out is reserved for a single, deliberate run at actual release time.
- **`T2_UNRESOLVED` findings are real gaps, not confirmed absences.** 110 of
  the 2,598 SIGNAL findings behind the headline number rest on pairs the
  cascade escalated but never got an LLM verdict for — carried at reduced
  confidence (0.5) rather than dropped, but also not confirmed. At the
  cascade's tier-decision level (not the finding level — most tier decisions
  never touch a primary outcome or become a finding at all), T2_UNRESOLVED
  accounts for 1,628 of 13,153 decisions (12.4%) across the full corpus.
- **No human inter-rater reliability (κ) study.** What the benchmark
  measures is agreement between this pipeline's automated findings and
  Holst's already-adjudicated labels — detector accuracy against an existing
  gold standard, not a second independent human rating. A κ study needs
  human coders and is out of scope here.

## Repository map

- `ctcm/` — the library: `ingest.py`, `extract.py`, `normalize.py`,
  `match.py` (cascade), `timeline.py` (anchoring), `classify.py` (taxonomy),
  `pipeline.py` (orchestration), `adjudicate.py`, `api.py`, `db.py`.
- `scripts/` — CLI entry points (`run_ingest.py`, `run_pipeline.py`,
  `run_adjudicate.py`, `headline.py`, `show_history.py`).
- `benchmark/` — Holst ground-truth mapping, split protocol, and results.
- `ui/index.html` — the timeline scrubber (single file, no build step).
- `docs/methodology.md` — full detection methodology, deviations, upgrade paths.
