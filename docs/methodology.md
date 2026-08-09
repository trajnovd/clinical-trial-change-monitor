# Methodology

How the Clinical Trial Registry Change Monitor decides that a trial's primary
outcome changed, when it changed relative to enrolment/completion, and how
severe that is — end to end, with the thresholds and the failure modes. This
is the document to argue with if you think a number is wrong: every rule
below is implemented at the file:line cited, not paraphrased from a spec.

**Scope, stated precisely:** this tool diffs a registry entry against its own
earlier versions on the same registry (ClinicalTrials.gov). It does not
compare a registry entry to the published paper describing the trial — that
is a different, harder problem (registry-vs-publication), out of scope by
design (`PRD-trial-registry-monitor.md` line 51). "Change" means "the
registry record now says something different than it used to say," nothing
about why.

## 1. Ingestion (`ctcm/ingest.py`)

**Discovery.** `GET /api/v2/studies` (paginated via `pageToken`), filtered to:

```
AREA[StudyType]INTERVENTIONAL AND
AREA[ResultsFirstPostDate]RANGE[2008-01-01,MAX] AND
AREA[StartDate]RANGE[2008-01-01,MAX]
```

Interventional, results posted, started in or after 2008. Capped at
`CORPUS_LIMIT` (`ctcm/config.py`, default 800, `CTCM_CORPUS_LIMIT` env or
`--limit`) — bounded on purpose (see §10, "Bounded corpus").

**Fetch rule.** For each discovered NCT ID: `GET
/api/int/studies/{nct}/history` for the version list, then `GET
.../history/{v}` only for v0, every version whose `moduleLabels` includes
"Outcome Measures," and the final version (`ctcm/ingest.py:fetch_trial`) —
not every version in between. Both endpoints are the internal (`int`) API,
not the public v2 one — v2 has no version-history endpoint. Every fetched
`(nct_id, version)` snapshot is gzipped to `data/cache/{nct}/v{n}.json.gz`;
the version list to `data/cache/{nct}/history.json`. A snapshot already on
disk is never re-fetched — ingestion is resumable and re-runs are free
(`global-constraints.md`). Consequence: anchors, revisions, and
`TIMELINE_REVISED` detection (§5) are computed only over this fetched
subset, not the trial's full version history — v0 is always included, so the
earliest-recorded-date defence (§5) still holds, but a start/completion-date
edit made in a version between two fetched snapshots (neither touching
Outcome Measures) is invisible to this tool, and `TIMELINE_REVISED` recall is
bounded accordingly. Fetch every version instead of this subset if that
recall matters more than the smaller/faster ingest.

**Failure isolation.** One trial's fetch failure (429, DNS, timeout) is
caught, logged, and does not abort the batch or block any other trial
(`ctcm/ingest.py:_one`) — a trial simply doesn't make it into the cache and
is picked up by a re-run. `MAX_CONCURRENCY = 8`, `MAX_RETRIES = 5`.

**Current corpus:** 1,220 trials — the 800-trial default discovery sweep plus
Holst benchmark trials (§9) that independently satisfy the same filter,
deduplicated by `nct_id`.

## 2. Extraction (`ctcm/extract.py`)

`load_corpus()` walks `data/cache/`, parses every cached snapshot
(`extract_snapshot()`), and upserts `trials` / `versions` / `outcomes` /
`timeline_facts` in `data/ctcm.db`. Deterministic, no model calls
(`global-constraints.md`). `INSERT OR REPLACE` throughout — idempotent, so a
partial ingest can be re-loaded any time without duplicating rows.

**Era handling.** Registry snapshots span trials registered from 1999 to the
present, and the schema was not stable across that span. Verified directly
(`NCT00000620`, ACCORD, started 1999-09, fetched via the same `int` history
API): the same modern JSON shape
(`protocolSection.{identificationModule,statusModule,outcomesModule,...}`)
is served for both eras, but early versions can differ in ways that would
raise if not handled:
- `startDateStruct` may have no `type` (ACTUAL/ESTIMATED) key at all — that
  flag didn't exist at registration time for the earliest trials.
- `outcomesModule` can be `{}` (no primary/secondary outcomes) on `v0` for
  trials registered before outcomes were a required field.
- `primaryCompletionDateStruct` may be absent on early versions.
- Outcome `description` text can carry raw HTML (`<p>...</p>`) on some
  versions — not stripped, since only `measure`/`timeFrame` feed matching.

One extraction path handles both eras: every field access defaults to
`None`/`[]` instead of raising. No separate legacy parser.

## 3. Normalisation (`ctcm/normalize.py`)

`norm()`: lowercase, expand curated abbreviations (40+ entries — `HAM-D`,
`PFS`, `HbA1c`, `NYHA`, etc., picked to their least-ambiguous clinical
reading; e.g. `HR`→"heart rate", not "hazard ratio"), strip punctuation,
collapse whitespace. Two measure strings describing the same construct
should come out byte-identical after `norm()`. `split_timepoint()` peels a
trailing timepoint phrase ("... at 24 months") off a measure string when the
registry left it in the measure text instead of the dedicated `time_frame`
field.

The abbreviation dictionary is a static, hand-curated list — an upgrade path
if T0's ~90% exact-match rate (§4) ever falls short: mine it from corpus
token frequency instead of maintaining it by hand.

## 4. Semantic matching cascade (`ctcm/match.py`)

For a candidate (before, after) outcome pair, four tiers decide "same
measure, and how," cheapest first — each tier only sees what the previous
one couldn't resolve:

| Tier | Rule | Resolves |
|---|---|---|
| **T0** | `measure_norm` byte equality | SAME, ~90% of pairs (TECH-PRD §5.3) |
| **T1** | Token-set Jaccard on `measure_norm`, stopword-filtered (generic English + clinical-outcome boilerplate: "change", "baseline", "improvement", "assessment" — otherwise these dominate the token set over the words that actually identify the construct) | ≥0.9 → REWORDED, ≤0.35 → DIFFERENT, else escalate |
| **T2** | Deterministic rules: (a) same stem, different trailing timepoint → TIMEPOINT_CHANGED; (b) qualifier-narrowing lexicon (`cardiovascular`, `cardiac`, `cancer`, `respiratory`, `renal`, ... — a fixed 11-entry set) on an identical head noun → NARROWED/BROADENED, either direction | Runs on the escalated band **and** on T1's high-similarity band before accepting SAME/REWORDED — high lexical similarity is exactly where "all-cause mortality" vs "cardiovascular mortality" hides (TECH-PRD §5.1) |
| **T3** | LLM adjudication (`claude -p`, `claude-haiku-4-5`), injectable so tests never touch the network | Residual ambiguous pairs T0-T2 could not decide |

**T2's abbreviation guard.** Qualifier-lexicon matching alone misreads an
abbreviation spelled out in full ("MACE" → "major adverse cardiac event
(MACE)") as narrowing, because "cardiac" is both a genuine restrictive
qualifier and part of MACE's own expansion. `_shares_acronym()` checks the
raw (pre-normalisation) text for a shared all-caps acronym between before
and after and, if found, treats it as an expansion artifact rather than a
qualifier change (caught live against the corpus; see `v03-review.md`
Critical #1 for the false positive that motivated it).

**T3 failure handling.** Any T3 failure — CLI error, timeout, malformed
JSON, an unrecognised `relation` value — falls back to the conservative
`DIFFERENT`/`T3_FALLBACK`, never a guess. When no T3 callable is supplied, or
its call budget (`--t3-limit`) is exhausted, the same pairs resolve to
`DIFFERENT`/`T2_UNRESOLVED` instead — an honest "we didn't look," distinct
from T3_FALLBACK's "we looked and it failed." Findings resolved at either
unresolved tier carry `confidence=0.5` instead of `1.0`, but severity is
**not** downgraded — an unresolved semantic match is still surfaced, not
suppressed (see §7, `T2_UNRESOLVED` caveat).

**T3 caching.** `T3Client` caches every response in `llm_cache` keyed by
`sha256(prompt)`. The cache check happens *before* the budget check, so a
`--t3-limit 0` run still returns every previously-resolved T3 verdict for
free and only refuses genuinely new calls — this is what makes `make
pipeline`/`make all` reproducible from cache with zero network access (see
README "Reproducing the headline number").

**Assignment.** `match_outcomes()` scores every candidate pair once, then
resolves highest-score-first (greedy best-first) so a strong match elsewhere
can't be stolen by a weaker one considered earlier in list order. Each
item gets at most one T3 attempt (its single best-scoring remaining
candidate), not one per candidate it appears in — otherwise an item sitting
in a crowded outcome list could burn the LLM budget against every
moderately-similar candidate before giving up on all of them.

## 5. Timeline anchoring and the date-manipulation trap (`ctcm/timeline.py`)

**The trap:** `start_date` and `primary_completion_date` are themselves
editable registry fields. A sponsor can push either forward in a later
version — which would make a real post-enrolment outcome change look
pre-enrolment if the detector trusted whatever the *latest* version says.

**The defence:** `anchors()` takes the **earliest recorded** value of each
date across every fetched version of a trial, never the latest/current one
(`global-constraints.md`). On a tie, the version typed `ACTUAL` is preferred
over `ESTIMATED`. Every version where either date moved is recorded as a
`Revision` and becomes its own `TIMELINE_REVISED` finding (§7) — the edit
itself is evidence, independent of whether any outcome also changed.

`position(change_date, anchors)` returns `(days_after_enrolment,
days_after_primary_completion)` for a change dated `change_date`, computed
against those earliest-recorded anchors — this is what §7's severity rules
key off, and what adjudication (§8) cites instead of a per-version snapshot
that might show a superseded estimate.

## 6. Taxonomy and severity (`ctcm/classify.py`)

Only changes touching a `PRIMARY` outcome (on either side) are in scope —
everything else (secondary/other-only changes) is outside the taxonomy and
not emitted; noise is suppressed by construction, not filtered after the
fact.

| `change_type` | When |
|---|---|
| `PRIMARY_REPLACED` | Unambiguous 1:1 swap (exactly one primary removed, one added, same version pair) |
| `PRIMARY_ADDED` / `PRIMARY_REMOVED` | Ambiguous N:M leftover — never guess a specific pairing |
| `PRIMARY_DEMOTED` / `SECONDARY_PROMOTED` | Same matched measure, `outcome_type` changed |
| `PRIMARY_NARROWED` / `PRIMARY_BROADENED` | T2/T3 qualifier-scope relation |
| `TIMEPOINT_CHANGED` | Same stem, timepoint differs |
| `REWORDED` | Same construct, different text — always `CONTEXT` severity, timing irrelevant |
| `TIMELINE_REVISED` | A `start_date`/`primary_completion_date` value moved between versions (§5) |
| `POST_COMPLETION_CHANGE` | **Severity override**: any primary-outcome change dated on/after the earliest-recorded primary completion date, regardless of its underlying kind — collapses what would otherwise be ADDED/REPLACED/etc. |

**Severity:** `SIGNAL` if the change lands post-enrolment (or the start date
is unrecorded — missing timeline data must never suppress a real change, the
undercounting failure mode `global-constraints.md` warns against) or
post-completion; `CONTEXT` if it's pre-enrolment, or if it's a `REWORDED`
with no semantic content. `POST_COMPLETION_CHANGE` is always `SIGNAL` and
always wins over every other classification once its date condition holds —
by design, the pipeline can never emit e.g. `PRIMARY_ADDED` for a change
dated after completion; it always collapses to `POST_COMPLETION_CHANGE`
(relevant when reading benchmark scores, §9).

Every `Finding` records `resolved_by` (which cascade tier decided it) and a
plain-text `rationale` citing the actual version numbers and day counts —
never "fraud" or "misconduct" (`global-constraints.md`; enforced again at
the adjudication layer, §8).

## 7. Adjudication (`ctcm/adjudicate.py`)

For a `SIGNAL` finding, three sequential `claude -p` calls — **defence**,
**prosecution**, **judge** (judge sees both arguments) — turn a raw finding
into a concern verdict (`LOW`/`MODERATE`/`HIGH`, or `UNREVIEWED` on any
failure) with a plain-prose rationale. This is advisory triage on top of the
deterministic pipeline, not a replacement for it — the underlying
`change_type`/`severity` never change based on an adjudication.

**Evidence package** (`assemble_evidence()`): both versions' outcomes, both
versions' per-version timeline snapshots, sponsor/phase/status, and —
critically — the resolved `TIMELINE ANCHORS` block from §5, computed the
same way the day-counts on the finding were. Without it, a finding whose
version window predates a later `ESTIMATED→ACTUAL` correction (e.g.
`NCT04280705`, corrected only at `v20`) would show the model only the
superseded per-version estimate, with nothing to reconcile the day-count
against. Prompts explicitly instruct the judge to cite the anchors block,
not a per-version snapshot.

**Caching.** Verdicts are keyed by `sha256(nct_id, from_version, to_version,
change_type, before_measure, after_measure)` — a content hash, not
`finding_id`. `finding_id` is a delete+reinsert primary key
(`pipeline.py` rewrites a trial's findings on every run), so keying on it
would orphan every adjudication on the next pipeline run; the content hash
survives.

**Word filter.** Neither prompt ever invites "fraud" or "misconduct" —
inviting the word is the easiest way to get it back in the response. As a
second layer, every response is regex-filtered (`_defuse()`) before it's
persisted or returned, in case a model emits either word unprompted. The raw
model text is still logged verbatim to `data/adjudication_log.jsonl` for
audit — that log is an internal debug trail, not the product-facing output
the constraint targets.

## 8. Benchmark protocol (`benchmark/`)

Scored against Holst M, et al., *"Invisible outcome changes: an audit of
outcome switching in publications compared to registry entries and
registered history,"* PLOS Medicine (2023),
[10.1371/journal.pmed.1004306](https://doi.org/10.1371/journal.pmed.1004306)
— specifically the 559-trial **within-registry-history** subset
(`registry == 'ClinicalTrials.gov' AND referenceid` populated), not Holst's
292-trial registry-vs-publication sample, which compares to the published
paper and is out of scope by construction (§0). Full mapping-decision
reasoning (each Holst CSV flag → our `change_type`, including three
explicitly low/medium-confidence judgment calls) is in
`benchmark/README.md`; not repeated here.

**Split.** 50/50 dev/held-out over the 559 labeled trial IDs, seeded
(`SPLIT_SEED = 20260808`), computed once and persisted to
`benchmark/splits/{dev,heldout}_ids.txt`. Every later run reads those files
back verbatim — the split cannot reshuffle even if the source CSV is
re-fetched and gains a row.

**The held-out gate.** `evaluate.py --split heldout` is reserved for a
single run, at v1.0 release, ever. It refuses to run at all unless
`CTCM_RELEASE_EVAL=1` is set in the environment — not a default any
invocation can fall into.

**Why the gate exists (documented incident, 2026-08-08).** While verifying
`evaluate.py`'s CLI behaviour — specifically, does `--split heldout`
correctly skip writing `results_dev.md` — a real `--split heldout`
invocation ran against the actual 559-trial labels, not a fixture. This
violated "nothing may iterate against held-out" in spirit, even though the
letter of the file-write constraint held: no results file was written
(verified by hashing `results_dev.md` before/after), and stdout was piped
through `head -15` and never read by the person or process that ran it — no
held-out number was observed, recorded, or used to inform any mapping/code
decision. The stray process outlived the check that spawned it (blocked
several minutes on `data/ctcm.db` lock contention with a concurrent agent's
pipeline run) and was killed by the task coordinator before it produced any
output that was seen. Net effect: no leakage occurred, but the invocation
itself should never have happened. Remedy shipped in the same fix: the
`CTCM_RELEASE_EVAL=1` gate above, so a curiosity-driven or smoke-test
invocation can no longer execute the held-out join without a deliberate,
unmistakable opt-in. Full account: `benchmark/README.md` "Incident log".

**Coverage caveat.** `evaluate.py --split dev` scores whatever subset of the
dev split is currently in `data/cache`, and reports `evaluated/labeled`
coverage explicitly rather than silently scoring a shrunken sample as if it
were the full split. The committed `benchmark/results_dev.md` reports 151/279
(54%) — see README "Benchmark" for the numbers and their caveats. Re-run any
time to refresh against however much of the corpus is currently cached.

**Held-out discipline for v1.0.** No `--split heldout` run has been
performed or observed for this release (the one documented incident above
produced no visible output and is not a heldout number). `results_dev.md`'s
numbers are dev-split only; a proper held-out gate run is future work at
actual release time (see README "Documented limitations").

## 9. Deliberate deviations from the TECH-PRD stack, and upgrade paths

The TECH-PRD (§9.3) specifies DuckDB, a fine-tuned biomedical cross-encoder
on top of `sentence-transformers` embedding retrieval, direct Claude API
calls, and a React+Vite+Tailwind interface. Four deliberate substitutions
were made, each documented in code as a `# ponytail:` comment at the point
of use:

| TECH-PRD | Built instead | Why | Upgrade path |
|---|---|---|---|
| DuckDB | **sqlite3** (`ctcm/db.py` (the `sqlite3` import and `connect()`)) | Single-writer, single-file store at this corpus size doesn't need DuckDB's analytical/columnar strengths; sqlite is zero-setup and ships with Python | Swap the `db.py` connection layer for DuckDB if concurrent-writer or genuinely analytical (columnar aggregation over millions of rows) needs appear |
| `sentence-transformers` embedding retrieval + fine-tuned cross-encoder (T1/T2) | **Token-set Jaccard (T1) + a deterministic rule discriminator (T2)** (`ctcm/match.py` (`_jaccard` and `_qualifier_relation`)) | No labeled training data existed yet to fine-tune a cross-encoder against; lexical + rule matching is auditable (every T2 decision traces to a specific lexicon entry) and got the cascade shipped without a training pipeline | Swap in `sentence-transformers` (e.g. a biomedical encoder) for T1 if Jaccard's accuracy plateaus below target; train a cross-encoder on adjudicated/benchmark-confirmed pairs once enough exist |
| Direct Claude API calls | **`claude -p` CLI subprocess** (`ctcm/match.py` (the `T3Client` subprocess call), `ctcm/adjudicate.py`) | No API key plumbing/billing setup needed during development; the CLI was already authenticated in the dev environment | Swap `subprocess.run(["claude", "-p", ...])` for a direct API client call if CLI startup latency or subprocess overhead becomes the bottleneck at higher T3/adjudication volume |
| React + Vite + Tailwind | **Vanilla HTML/CSS/JS, one file, no build step** (`ui/index.html`) | A single timeline-scrubber view doesn't need componentization or a build pipeline; zero-build keeps `make serve` instant and the whole UI auditable in one file | Migrate to React/Vite once the UI grows past one view (e.g. the aggregate index + filtering gets its own page) or state management gets unwieldy in vanilla JS |

Every deviation trades a documented, real capability (columnar analytics,
learned semantic matching, direct API control, componentized UI) for
shipping v1.0 without first building that capability's prerequisites (a
multi-writer workload, labeled training data, API billing setup, a
multi-view UI). None of the four is a hidden shortcut — each is flagged in
the code it affects, not just here.

## 10. Where this can still be wrong

- **T2_UNRESOLVED pairs are real gaps, not resolved absences of change.**
  Every pair the cascade escalated to T3 but never got an LLM verdict for
  (no budget, or the run used `--t3-limit 0`) is counted as `SIGNAL` at
  reduced confidence (0.5), not silently dropped — but it is also not
  confirmed. On the full corpus (`make all` run), 1,628 of 13,153 tier
  decisions (12.4%) land here; see README "Documented limitations."
- **The qualifier-narrowing lexicon (§4) is an 11-entry hand-curated list.**
  A qualifier change using a clinical term outside that list (e.g. a rare
  organ-system qualifier) will not be caught by T2 and either escalates to
  T3 or, if T3 is unavailable, resolves as `T2_UNRESOLVED`/`DIFFERENT`.
- **The abbreviation dictionary (§3) is similarly a static, finite list.**
  A measure using an abbreviation not in it won't collapse to a T0 exact
  match and instead has to survive T1's Jaccard threshold or escalate.
- **`POST_COMPLETION_CHANGE`'s severity override is deliberately blunt**
  (§6): it fires on *any* detected primary-outcome text difference dated at
  or after completion, narrow or broad, editorial or routine
  results-entry housekeeping. This is why the README leads with the
  post-enrolment-primary-change number rather than this one — see
  `scripts/headline.py`'s own caveat text.

## 11. EU CTIS adapter: prospective-only coverage (`ctcm/ctis.py`)

**Plainly: EU coverage is prospective from the date this product first
snapshots a trial. No backfill exists anywhere — not in this product, not
in CTIS itself.** CTIS (euclinicaltrials.eu) runs a public, unauthenticated
REST API (`search` + `retrieve/{ctNumber}`, verified live 2026-08-09,
`.superpowers/sdd/2026-08-08-trial-registry-monitor/v2-research.md`), but it
serves **current state only**. The pre-2024-06-17 relaunch system reportedly
exposed a version-history field (`applications`) that the live system today
does not — confirmed by reading the `ctrdata` R package's own loader source
and doc comments (v2-research.md's "decisive evidence"). There is no
"give me this trial as of date X" call to fall back on, and no bulk history
export. Findings for a CTIS trial can therefore only ever describe changes
this product itself observed happening — never anything that happened before
the trial was first added to the watchlist and snapshotted.

**What this product builds instead.** `ctcm.ctis.snapshot_pass(ct_numbers)`
polls `GET /retrieve/{ctNumber}` for each watched trial and keeps a new
gzipped copy (`data/cache/ctis/{ctNumber}/snap-{YYYY-MM-DD}.json.gz`) *only*
when its outcome-relevant content hash differs from the most recently kept
snapshot (or none exists yet) — the same sha256-of-canonical-JSON discipline
`ctcm/extract.py` uses for CT.gov's `versions.content_hash`. Every kept
snapshot IS a version, by construction; there is no separate "which fetched
revision is worth keeping" fetch rule the way CT.gov's real history needs,
because CTIS never hands over more than one revision (the current one) per
call. `ctcm.ctis.CTISAdapter` implements the same `RegistryAdapter` protocol
`ctcm/monitor.py` defined for CT.gov (commit `42d31f3`), but purely as a
local reader over that snapshot series — unlike `CTGovAdapter`, it never
touches the network, because for CTIS "check for a new version" and "fetch
the live state" collapse into the same single call.

**Field mapping.** `ctcm.ctis.map_outcomes()` reads primary/secondary
endpoints from `authorizedApplication.authorizedPartI.trialDetails
.trialInformation.endPoint.{primary,secondary}EndPoints` into the same
outcomes shape (`outcome_type` PRIMARY/SECONDARY, `measure`) CT.gov rows
use — `time_frame` is left `NULL`: CTIS has no dedicated per-endpoint
timeframe field the way CT.gov's `outcomesModule[].timeFrame` does, and
regex-guessing one out of the free-text endpoint description was judged
worse than an honest `NULL`. `ctcm.ctis.map_meta()` maps sponsor, condition,
phase (a small code table built from codes observed live — an unseen code
is stored as `"CODE_{n}"`, never guessed), and enrolment count similarly.
`timeline_facts` is intentionally left unpopulated for CTIS trials — field
mapping was scoped to the outcomes schema only; `ctcm.timeline.anchors()`
and `ctcm.classify.classify()` already handle a trial with no timeline facts
by defaulting severity to `SIGNAL` rather than suppressing the finding (§5),
so this is a silent-but-correct degradation, not a crash risk.

**Naming debt.** `trials.nct_id` is a `TEXT PRIMARY KEY` sized and named for
ClinicalTrials.gov's NCT format. CTIS rows store their ctNumber (format
`2025-523333-26-00`) in that same column — no schema change, no collision
(the formats never overlap), but the column name is a misnomer for a fifth
of the table now. `trials.registry` (`'ctgov'` default | `'ctis'`, added via
a guarded `ALTER TABLE`, same pattern as `ctcm.monitor.ensure_schema`'s
`findings.first_seen_at`) is what actually distinguishes the two.

**Starter watchlist.** `scripts/run_ctis.py --bootstrap` searches CTIS for
phase-3 trials (`searchCriteria.trialPhaseCode: ["5"]`, verified live)
sorted by `decisionDate` descending, approximating "ongoing/recruiting"
client-side via `resultsFirstReceived == "No"` — CTIS's `/search` exposes no
verified recruitment-status filter key (several plausible key names were
probed live and silently ignored; unknown `searchCriteria` keys return the
full unfiltered result set rather than an error, which is itself a footgun
worth knowing about this API). This is a documented approximation, not a
precise recruiting filter: it correctly excludes trials whose results are
already posted, and correctly includes everything else, including trials
that are authorised but not yet actively recruiting.

**Findings flow through the same pipeline, unchanged.** Once
`snapshot_pass()` upserts rows into `trials`/`versions`/`outcomes`,
`ctcm.pipeline.run_pipeline()` — completely unmodified, already iterating
every `nct_id` in `trials` regardless of registry — picks up CTIS trials
exactly like CT.gov ones the next time `make pipeline` runs. No CTIS-specific
branch exists anywhere in the diff/classify/severity code.

## 12. Publication linking (`ctcm/publink.py`)

**What it links, and how.** For each selected trial, three queries run
against two APIs and merge into one row per PMID, highest tier wins
(`assign_tiers`, line 99):

- **HIGH** — PubMed eutils `esearch` with `term={nct}[si]`: the NCT ID as a
  publisher-asserted secondary-source ID (ICMJE trial-registration
  metadata) — curated linkage, not text-mined (`_pubmed_si`, line 199).
- **MEDIUM** — Europe PMC `ABSTRACT:"{nct}"`: the NCT ID appears in the
  paper's own abstract. A paper rarely cites another trial's ID there, so
  this is a reasonable "this paper is substantially about the trial" signal.
- **LOW** — Europe PMC bare `"{nct}"` free-text: catches every indexed
  mention (comparator citations, reviews, reference lists) —
  **10-40x noisier than the two tiers above**, per the v2 research verifying
  these three query forms live (2026-08-09, module docstring). Capped at
  `LOW_TIER_PAGE_SIZE=100` results (line 43) — a deliberate cap on the noisy
  query, not full recall (one trial alone hit 371 matches). LOW is never
  presented as "the trial's paper," only as "mentions this trial."

A PMID that's `[si]`-linked but never surfaced in either Europe PMC query
still gets a HIGH-tier stub row (pmid only, no title/journal/date) — the
linkage itself is the evidence; metadata is best-effort.

**The truncation trap eutils doesn't warn about.** `esearch` defaults to
`retmax=20` with no warning in the response when the true count is larger —
silently truncating the *curated* HIGH tier would be worse than truncating
the deliberately-capped LOW tier. `SI_RETMAX=500` (line 54) is set
explicitly, and if the real `count` still exceeds what came back,
`_pubmed_si` refetches once with `retmax=count` (lines 209-210) rather than
quietly dropping the rest — verified live: `NCT00000620` has 109 `[si]`-linked
papers, only 20 came back with no `retmax` set at all.

**Never parses paper content.** Only link + tier + evidence source are
stored (`publications(nct_id, pmid, doi, title, journal, pub_date, oa, tier,
source)`) — the PRD explicitly defers full-text parsing. `days_after_change`
(line 142) computes whole days between a publication and a SIGNAL finding's
change date only when the publication postdates the change (never a
"published before" framing), and leaves the result `None` rather than
fabricate a day-precision date from Europe PMC's coarser pub-year fields.

**Trial selection.** `select_nct_ids` (line 239) prioritizes trials with an
adjudicated SIGNAL finding first, then any SIGNAL trial, then everything
else alphabetically — the case-study population gets linked first under
`--limit`.

## 13. Continuous monitoring (`ctcm/monitor.py`)

**What a pass does, per trial.** Refetch the version-history *list* only
(~1 request via `RegistryAdapter.list_versions` — cheap), compare its length
to `trials.version_count`. A trial whose history grew gets its new versions
fetched under the same fetch rule as ingest (v0, outcome-touching, final)
into the existing `data/cache/{nct}/` layout; `load_corpus()` and
`run_pipeline()` then re-run scoped to just the changed trials
(`check_updates`, line 314).

**Atomic write ordering closes a real data-loss path.**
`_fetch_new_snapshots` (line 217) fetches every wanted-but-uncached snapshot
first, and only refreshes `history.json` — atomically, via temp-file +
`Path.replace` — once *all* of them are confirmed on disk. If a fetch fails
partway, the exception propagates before `history.json` is touched: the old
(smaller) version count is left in place, so the next pass sees the same
known count and retries the missing version, rather than a
partially-fetched trial silently losing that version forever.

**New-finding detection is a multiset diff, not a set diff.**
`run_pipeline()` does delete+reinsert per scoped trial, so a changed trial's
findings get fresh `finding_id`s and `NULL` `first_seen_at` on every call.
`check_updates` snapshots each changed trial's pre-existing findings by
content hash (`_findings_multiset`, line 249) as a **list** of prior
`first_seen_at` values per hash, not a set — two findings can legitimately
share an identical content hash within one trial (duplicate/near-duplicate
outcome text is real registry data), and a set would collapse them, letting
one silently inherit the other's `first_seen_at`. `_reconcile_trial` (line
279) consumes each hash's queue in order as matching post-rerun rows are
seen; once a queue is empty, further rows with that hash are the surplus —
genuinely new — stamped with `now()` and appended to
`data/monitor_log.jsonl`.

**`t3_limit=0`, not `t3_enabled=False`.** The rerun uses
`run_pipeline(t3_enabled=True, t3_limit=0, ...)` (line 390) so that
`ctcm.match.T3Client`'s cache lookup still runs before its budget check — an
unchanged candidate pair replays to the same cached tier decision every
time. `t3_enabled=False` would skip `T3Client` (and its cache) entirely,
flipping every previously LLM-resolved pair on a changed trial to
`T2_UNRESOLVED` and minting spurious "new" content hashes on every single
pass, even with nothing genuinely new in the registry.

**Registry-routed, not CT.gov-only.** `check_updates` reads each trial's
`trials.registry` column and splits ids into `ctgov_ids`/`ctis_ids` *before*
any network call (line 338): `ctgov_ids` go through `CTGovAdapter`, which
serializes requests behind a `MIN_INTERVAL=0.4`s floor between dispatches
(line 126) — CT.gov's history endpoint was verified live to start 429ing
under any overlapping concurrency (a measured guess, not a published limit;
`# ponytail:` at the class docstring flags the global lock as the ceiling,
tune `MIN_INTERVAL` or swap in a real token bucket if a production run still
429s). `ctis_ids` go once, batched, to `ctcm.ctis.snapshot_pass()` rather
than through `CTISAdapter` — `CTISAdapter` is a pure local reader (§11), so
routing through it here would just re-read what's already on disk and never
detect a real external change; `snapshot_pass()` batched once also preserves
CTIS's own internal serialized-request pacing, which per-trial concurrent
calls would defeat.

**Limitation: prospective, like CTIS.** A trial's history before the pass
that first checks it is untouched — monitoring finds new versions going
forward, it does not backfill anything `make ingest` missed.

## 14. Sponsor/class/phase/year/condition analytics (`ctcm/analytics.py`, `/api/analytics`)

**"Signal" is exactly the headline definition, reused.** A trial counts
once it has ≥1 finding with `severity='SIGNAL'` — already "post-enrolment
primary change" by construction (`classify.py` only assigns `SIGNAL` to a
primary-outcome change on or after enrolment; §6), so no extra day-count
filter is needed here (module docstring, lines 9-14).

**Denominator honesty rules.**
- Every rate row carries its numerator (`signalTrials`) and denominator
  (`totalTrials`) alongside the rate — never a bare percentage (`_rate_row`,
  line 79).
- `signal_rate_by_sponsor` enforces a hard floor, `MIN_SPONSOR_TRIALS=5`
  (line 38, used at line 123): below that, one or two trials would swing a
  sponsor between 0% and 100%, reading as signal it isn't — those sponsors
  are dropped from the table entirely, not just flagged.
- `bySponsorClass`/`byPhase`/`byYear`/`topConditions` have **no hard
  floor** (dropping a real category like a rare sponsor class or an early
  year would hide data, not protect it), but every row still carries
  `lowN: total < 5` so a reader can see when a rate rests on a handful of
  trials, without the table silently omitting it.

**`ctgov`-only, with the exclusion disclosed, not silent.** EU CTIS trials
(`trials.registry='ctis'`) all have `version_count=1` — structurally
incapable of a finding, since a finding needs a diffed version pair. Folding
them into these tables wouldn't add real 0%-rate signal; it would dilute
every denominator with trials that were never eligible to contribute a
numerator, and would falsify the shared `RESULTS_POSTED_CAVEAT`
(`ctcm/api.py` (`RESULTS_POSTED_CAVEAT`)), which describes ctgov's completed/results-posted corpus
specifically. `_ctgov_only()` (line 64) excludes CTIS rows from every
`trials`-table aggregate; `excluded_ctis_count()` (line 70) reports how
many, surfaced by `/api/analytics` as `ctisExcludedCount` so the exclusion
is disclosed rather than invisible. `timing_histogram`/
`adjudication_concern_mix` need no such filter — both are keyed off
`findings`/`adjudications`, which (same `version_count=1` constraint) can
never contain a CTIS row in the first place.

**Limitation: sponsor names are grouped as the registry wrote them.**
`lead_sponsor` (`ctcm/extract.py` (`extract_snapshot`'s sponsor field)) is stored verbatim from
`leadSponsor.name`, with no canonicalization pass — the same sponsor
registered under two spellings (e.g. a name change, a punctuation variant)
splits into two rows here rather than one, each with its own (smaller, and
therefore more likely `lowN`) denominator.
