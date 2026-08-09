# Benchmark: Holst et al. 2023 ground truth

Scores the ctcm pipeline's findings against the manually-adjudicated
within-registry outcome-change labels from Holst M, et al., "Invisible
outcome changes: an audit of outcome switching in publications compared to
registry entries and registered history," *PLOS Medicine* (2023),
[10.1371/journal.pmed.1004306](https://doi.org/10.1371/journal.pmed.1004306).

Full data-acquisition notes (source URLs, file inventory, column dictionary):
`benchmark/DATA-NOTES.md`. This file covers what that one doesn't: the final
mapping decisions as implemented, the split protocol, and how to run it.

## Quickstart

```
.venv/bin/python benchmark/fetch_holst.py     # idempotent -- downloads CSV + codebook if absent
.venv/bin/python benchmark/evaluate.py --split dev
```

Writes `benchmark/results_dev.md`. `--split heldout` is reserved for the
single v1.0 gate run (TECH-PRD) -- it prints a loud warning banner, never
writes a results file, and refuses to run at all unless `CTCM_RELEASE_EVAL=1`
is set (`CTCM_RELEASE_EVAL=1 .venv/bin/python benchmark/evaluate.py --split
heldout`). See "Incident log" below for why that gate exists.

## What's being scored

The **559-trial ClinicalTrials.gov within-registry-history subset**
(`registry == 'ClinicalTrials.gov' AND referenceid` populated in
`processed_history_data_analyses.csv`) -- **not** the 292-trial
registry-vs-publication sample. That second sample compares the registry to
the published paper's stated outcome; this tool only ever compares a
registry to its own earlier versions (`PRD-trial-registry-monitor.md` line
51), so the 292-trial sample is out of scope by construction. See
DATA-NOTES.md SS4/SS6/SS7 for the full reasoning.

Each of the 559 trials was manually rated across three registry-history
phase transitions (Holst's own boundaries, defined purely by date
arithmetic):

| Phase prefix | Transition |
|---|---|
| `change_a_i_` | registration -> end of recruitment |
| `change_i_p_` | end of recruitment -> end of post-completion |
| `change_p_l_` | end of post-completion -> latest version |

## Mapping decisions (row -> our `change_type`)

Implemented in `benchmark/mapping.py`, unit-tested in `tests/test_benchmark.py`.

**Axis 1 -- what changed**, applied per phase prefix:

| Holst flag(s) | -> `change_type` | Confidence |
|---|---|---|
| `primary_from_secondary` | `SECONDARY_PROMOTED` | High |
| `primary_to_secondary` | `PRIMARY_DEMOTED` | High |
| `change_timing`, alone in its phase | `TIMEPOINT_CHANGED` | High |
| `new_primary` AND `primary_omitted` (same phase) | `PRIMARY_REPLACED` | Medium-high |
| `new_primary` only | `PRIMARY_ADDED` | High |
| `primary_omitted` only | `PRIMARY_REMOVED` | High |
| `added_measurement` / `added_aggregation` / `added_timing` | `PRIMARY_NARROWED` | Medium |
| `change_measurement` / `change_aggregation` | `PRIMARY_NARROWED` | **Low** -- see below |
| `omitted_measurement` / `omitted_aggregation` / `omitted_timing` | `PRIMARY_BROADENED` | Medium -- see below |
| `no_change` (all sub-flags '0'/blank) | negative example | High |
| `no_phase` | phase excluded from evaluation entirely | High |

**Low-confidence call, called out explicitly per the task brief:**
`change_measurement`/`change_aggregation` mean "an existing primary's
measurement type or aggregation method changed in a significant way" --
ambiguous between "this is really `PRIMARY_REPLACED`" (the codebook says
"significant parts changed") and "this is `PRIMARY_NARROWED`" (the measure
itself is still there, just altered). We default to `PRIMARY_NARROWED`
because Holst's own severity coding (`fun/recode_outcome_changes.R`,
`recode_outcomes_nonsevere_1`) rates these two flags as *non-severe* --
categorically milder than the swap/demote/promote group Holst rates
*severe*, which argues against lumping them in with `PRIMARY_REPLACED`. This
is a judgment call, not a fact derived from the codebook; a different
default would shift a handful of labels across two low-volume categories.
Not re-litigated mid-dev-iteration (task brief: iterate on mapping bugs, not
model tuning) -- if it turns out to matter, it's one `if` branch to flip.

**`change_timing` co-occurrence caveat, approximated at phase granularity:**
DATA-NOTES SS6 qualifies the `change_timing` row: "`change_timing = 1` (and
no add/omit/new/omitted **on the same primary**)". The CSV only carries
phase-level flags, not per-primary ones -- there is no column that says
*which* primary a given sub-flag refers to, so the literal per-primary check
can't be implemented against this data. `mapping.py`'s `_axis1_codes`
approximates it at phase granularity instead: `change_timing` only produces
`TIMEPOINT_CHANGED` when no other axis-1-producing flag (`new_primary`,
`primary_omitted`, `primary_from_secondary`, `primary_to_secondary`,
`added_*`, `omitted_*`, `change_measurement`, `change_aggregation`) also
fired in that same phase. This is a proxy, not the literal caveat -- it will
also suppress a genuine, clean timing change that happens to share a phase
with an unrelated change to a *different* primary, trading a little recall
for not double-counting an ambiguous same-primary case as two independent
findings. Low observed impact so far (4 ground-truth `TIMEPOINT_CHANGED`
positives among 75 dev trials pre-fix); revisit if `TIMEPOINT_CHANGED`
volume grows enough for the false-suppression rate to matter.

**Axis 2 -- when it changed, and why it collapses two phases into one code:**
the POST_COMPLETION_CHANGE severity override in `ctcm/classify.py` (the
`if days_pcd is not None and days_pcd >= 0` branch in `classify()`)
unconditionally rewrites a finding's `change_type` to
`POST_COMPLETION_CHANGE` for *any* primary change dated on/after
`primary_completion_date`, regardless of what kind of change it is. That
means our pipeline can never emit e.g. `PRIMARY_ADDED` for a change that
happens after completion -- it always collapses to `POST_COMPLETION_CHANGE`.
So for Holst's `change_i_p_` (post-completion) and `change_p_l_`
(post-publication) phases, ground truth is tagged `POST_COMPLETION_CHANGE`
only, not the Axis-1 code -- that's the only code our pipeline could
possibly predict there, and scoring against the Axis-1 code instead would
manufacture false negatives out of a taxonomy-collapse design decision, not
a real detection miss. `change_a_i_` (recruitment phase) precedes
completion by construction and is never overridden, so its Axis-1 identity
is scored directly.

For `POST_COMPLETION_CHANGE` specifically, an `omitted_*` broadening in
`change_i_p_`/`change_p_l_` still counts as ground truth -- our pipeline's
PCD override fires on *any* detected primary-outcome text difference after
completion, narrow or broad, so Holst confirming some change occurred there
is still a real positive for that code, same as every other axis-1 category.

**`PRIMARY_BROADENED` -- `omitted_measurement` / `omitted_aggregation` /
`omitted_timing`:** the mirror image of `PRIMARY_NARROWED` ("detail dropped
from an existing primary, made less specific"). DATA-NOTES SS7.1 (written
before `PRIMARY_BROADENED` existed in ctcm's taxonomy) recommended dropping
these three sub-flags from scoring entirely, since at the time our taxonomy
had no "broadened" code. That's no longer true: `ctcm/classify.py`'s
`_CODE_BY_KIND["BROADENED"] = "PRIMARY_BROADENED"` was added in the Task 7
fix round (T2's qualifier rule mirrors NARROWED both directions -- e.g.
"cardiovascular mortality" -> "all-cause mortality"), so these flags now map
directly, the same shape as `added_* -> PRIMARY_NARROWED` above. Confidence
Medium, not High, for the same reason `added_*` is Medium rather than High:
the codebook's definition ("detail specified/dropped for the first time") is
a good match for our "narrowed/broadened scope" definition in spirit, but
the worked examples in each aren't identical in kind.

**Not scored at all:**
- `TIMELINE_REVISED` -- Holst never coded registry date-field edits, only
  outcome-text changes. No ground truth exists in this dataset for this
  code (DATA-NOTES SS7.2).
- `REWORDED` -- our own "no semantic change, wording only" bucket
  (severity `CONTEXT`). Holst's own `no_change` definition explicitly
  excludes wording-only edits from counting as a change too, so there's no
  separate Holst category to score `REWORDED` against; it's structurally
  equivalent to a Holst negative, not a distinct positive class.

## What the pipeline being scored actually is

`evaluate.py` calls `ctcm.pipeline.run_pipeline()`, which runs the full
**T0-T3 semantic cascade** (`ctcm/match.py`), not bare T0 exact-string
matching -- `t0_matcher` (`ctcm/classify.py`) is legacy unit-test scaffolding
only, unused by the pipeline since Task 7 (`diff_pair()`'s own docstring in
`ctcm/classify.py`: "v0.2 passes t0_matcher"). An earlier version of this document
claimed `PRIMARY_NARROWED` could never be predicted because `t0_matcher`
never returns `"NARROWED"` -- that was true of the matcher named, but not of
the pipeline actually invoked, and was flagged wrong in review
(`v05-review.md` C1). T2 and T3 can and do produce `PRIMARY_NARROWED`.

`results_dev.md`'s "Tier attribution" section reports the real, measured
T0/T1/T2/T3 mix for whatever ran; its "Low-precision flags" section
mechanically flags every change_type below a precision threshold on that
run's own numbers (not a hand-picked one), so a stale claim like this one
can't silently persist the same way again.

## Split protocol

50/50 dev/held-out split over the 559 labeled trial IDs, seeded
(`SPLIT_SEED = 20260808` in `benchmark/evaluate.py`) with `random.Random`,
computed **once** and persisted to `benchmark/splits/dev_ids.txt` /
`benchmark/splits/heldout_ids.txt`. Every subsequent `evaluate.py` run reads
those files back verbatim -- the split cannot silently reshuffle even if the
CSV is re-fetched and gains a row (`get_or_create_split` in `evaluate.py`,
covered by `tests/test_benchmark.py`). Only `dev` may be iterated against;
`heldout` is a single final run at v1.0, and `evaluate.py` refuses to run
`--split heldout` at all unless `CTCM_RELEASE_EVAL=1` is set in the
environment (`_require_release_eval_gate`, unit-tested) -- an explicit,
deliberate opt-in required every time, not a default any invocation can fall
into.

## Coverage

Holst's 1,402 CT.gov trial IDs were ingested into `data/cache` by a
background process (`data/ingest_holst.log`) that ran concurrently with most
of this task and reported `1402/1402` done (with some per-trial 429/DNS
failures along the way -- individual-trial fetch failures are caught and
logged, they don't abort the batch or block a trial from simply not making
it into `data/cache`). `evaluate.py` scores whatever subset of the labeled
split is actually in `data/cache` (via the shared `data/ctcm.db`,
`load_corpus()` + `run_pipeline()`, both idempotent) and reports `evaluated /
labeled` coverage explicitly in the results file -- below 50% coverage, the
report flags itself as a checkpoint rather than a final number. Re-run
`evaluate.py --split dev` any time to refresh against however much of the
corpus is currently cached.

## Incident log

**2026-08-08 -- a `--split heldout` run executed during dev-split
development.** While verifying `evaluate.py`'s CLI behavior (specifically:
does `--split heldout` correctly skip writing `results_dev.md`?), a real
`--split heldout` invocation was run against the actual 559-trial labels --
not a fixture, not a fake split. This is a violation of "nothing may iterate
against held-out" in spirit even though the letter of the file-write
constraint held: no results file was written (verified by hashing
`results_dev.md` before/after and confirming no change), and its stdout was
piped through `head -15` and never read by the person or process that ran
it -- no held-out number was observed, recorded, transcribed, or used to
inform any mapping/code decision in this benchmark. The stray process
outlived the check that spawned it (several minutes, blocked on lock
contention with another agent's concurrent pipeline run against the shared
db) and was killed by the task coordinator during review before it produced
any output that was seen. Net effect: no leakage occurred, but the
invocation itself should never have happened. Remedy shipped in the same
fix: `--split heldout` now hard-refuses to run without `CTCM_RELEASE_EVAL=1`
explicitly set (see Split protocol above), so a curiosity-driven or
smoke-test invocation can't execute the held-out join again without a
deliberate, unmistakable opt-in.

## Known limitation: no inter-rater kappa

TECH-PRD SS8.3 calls for a human inter-rater reliability (kappa) study
against a second set of human coders. That requires human coders -- it's out
of scope for a machine-only benchmark and is not attempted here. What this
benchmark measures instead is agreement between the pipeline's automated
findings and Holst's *already-adjudicated* labels (i.e. detector accuracy
against an existing gold standard), not a second independent human rating.
