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
single v1.0 gate run (TECH-PRD) -- it prints a loud warning banner and never
writes a results file, so a stray invocation can't leak held-out numbers into
the repo early.

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
| `change_timing` | `TIMEPOINT_CHANGED` | High |
| `new_primary` AND `primary_omitted` (same phase) | `PRIMARY_REPLACED` | Medium-high |
| `new_primary` only | `PRIMARY_ADDED` | High |
| `primary_omitted` only | `PRIMARY_REMOVED` | High |
| `added_measurement` / `added_aggregation` / `added_timing` | `PRIMARY_NARROWED` | Medium |
| `change_measurement` / `change_aggregation` | `PRIMARY_NARROWED` | **Low** -- see below |
| `omitted_measurement` / `omitted_aggregation` / `omitted_timing` | *(unmapped)* | -- see below |
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

**Axis 2 -- when it changed, and why it collapses two phases into one code:**
`ctcm/classify.py`'s severity override (`classify()`, ~lines 209-211)
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

For `POST_COMPLETION_CHANGE` specifically, an unmappable `omitted_*`
broadening in `change_i_p_`/`change_p_l_` still counts as ground truth --
our pipeline's PCD override fires on *any* detected primary-outcome text
difference after completion, narrow or broad, so Holst confirming some
change occurred there (even one we can't name) is still a real positive for
that code.

**Unmapped (excluded from scoring):** `omitted_measurement` /
`omitted_aggregation` / `omitted_timing` -- the mirror image of
`PRIMARY_NARROWED` ("detail dropped, made less specific"). Our taxonomy has
no "primary broadened" code. Per DATA-NOTES SS7.1 option (a): dropped from
per-type scoring entirely rather than credited as a `PRIMARY_NARROWED` miss
(counting a broadening as evidence our narrowing detector failed would
misrepresent what the taxonomy covers). Trials whose *only* Holst signal in
a phase is one of these three flags are also excluded from the
false-positive-rate denominator: we can't honestly call a pipeline finding
there a false positive (Holst did observe something) or a true negative
(it's not "no change").

**Not scored at all:**
- `TIMELINE_REVISED` -- Holst never coded registry date-field edits, only
  outcome-text changes. No ground truth exists in this dataset for this
  code (DATA-NOTES SS7.2).
- `REWORDED` -- our own "no semantic change, wording only" bucket
  (severity `CONTEXT`). Holst's own `no_change` definition explicitly
  excludes wording-only edits from counting as a change too, so there's no
  separate Holst category to score `REWORDED` against; it's structurally
  equivalent to a Holst negative, not a distinct positive class.

## Structural detector limitation surfaced by this benchmark

T0 (`ctcm.classify.t0_matcher`) only ever returns `"SAME"` or `None` -- it
never returns `"NARROWED"`, so `PRIMARY_NARROWED` can **never** be predicted
by the current pipeline. Every `PRIMARY_NARROWED` false negative in
`results_dev.md` is expected at v0.2/v0.3-pending, not a mapping bug. Fixed
by v0.3's fuzzy-matching tiers (embedding + cross-encoder), out of scope for
this benchmark task.

## Split protocol

50/50 dev/held-out split over the 559 labeled trial IDs, seeded
(`SPLIT_SEED = 20260808` in `benchmark/evaluate.py`) with `random.Random`,
computed **once** and persisted to `benchmark/splits/dev_ids.txt` /
`benchmark/splits/heldout_ids.txt`. Every subsequent `evaluate.py` run reads
those files back verbatim -- the split cannot silently reshuffle even if the
CSV is re-fetched and gains a row (`get_or_create_split` in `evaluate.py`,
covered by `tests/test_benchmark.py`). Only `dev` may be iterated against;
`heldout` is a single final run at v1.0.

## Coverage

Holst's 1,402 CT.gov trial IDs are being ingested into `data/cache` by a
background process (`data/ingest_holst.log`) that started before this task
and was still running partway through it. `evaluate.py` scores whatever
subset of the labeled split is already in `data/cache` (via the shared
`data/ctcm.db`, `load_corpus()` + `run_pipeline()`, both idempotent) and
reports `evaluated / labeled` coverage explicitly in the results file. Below
50% coverage, the report says so and flags itself as a checkpoint, not the
final number -- re-run once ingestion finishes.

## Known limitation: no inter-rater kappa

TECH-PRD SS8.3 calls for a human inter-rater reliability (kappa) study
against a second set of human coders. That requires human coders -- it's out
of scope for a machine-only benchmark and is not attempted here. What this
benchmark measures instead is agreement between the pipeline's automated
findings and Holst's *already-adjudicated* labels (i.e. detector accuracy
against an existing gold standard), not a second independent human rating.
