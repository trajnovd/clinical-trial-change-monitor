# Holst et al. 2023 benchmark data — acquisition notes

Source paper: Holst M, et al. "Invisible outcome changes: an audit of outcome
switching in publications compared to registry entries and registered
history." *PLOS Medicine* (2023). DOI [10.1371/journal.pmed.1004306](https://doi.org/10.1371/journal.pmed.1004306).
(Published from the preprint "ASCERTAIN" / medRxiv 10.1101/2023.02.20.23286182.)

## 1. Where the data came from

**Primary source — GitHub repo, cloned via raw file download (no `git`
available in this run, used `curl` against `raw.githubusercontent.com`
instead of `git clone`):**

- Repo: https://github.com/Martin-R-H/InvisibleOutcomeChanges (default branch
  `main`, confirmed via `api.github.com/repos/Martin-R-H/InvisibleOutcomeChanges`)
- File tree enumerated via `api.github.com/repos/Martin-R-H/InvisibleOutcomeChanges/git/trees/main?recursive=1`
- All 12 R scripts + `README.md` + `renv.lock` fetched from
  `https://raw.githubusercontent.com/Martin-R-H/InvisibleOutcomeChanges/main/<path>`
- Two data files fetched the same way (see inventory below); the rest of
  `data/` (raw registry-history dumps, up to 48MB each) were **not**
  downloaded — see §5.

This repo turned out to be the exact data/analysis repo behind the paper (the
README self-identifies as "HiddenOutcomeChanges" / preprint link) and is also
the repo cited by name in this project's own `PRD-trial-registry-monitor.md`
(line 182), so no fallback to OSF or PLOS supplementary files was needed for
the primary label data.

**Secondary source — OSF, used only for the codebook (GitHub repo has no
codebook file):**

- OSF project: https://osf.io/e2uct/ ("Final dataset of this project" —
  confirmed via `api.osf.io/v2/nodes/e2uct/`, same content as the GitHub
  `data/` folder)
- Codebook: https://osf.io/werxf/ → resolved via `api.osf.io/v2/guids/werxf/`
  to file `ASCERTAIN_Codebook_v3_amendments.docx` (59KB), downloaded from
  `https://osf.io/download/werxf/`. This is the authoritative column
  dictionary used throughout this document (§3).

PLOS supplementary files were not needed (fallback #2 unused).

## 2. File inventory

All files live under `benchmark/data/InvisibleOutcomeChanges/` (mirrors the
GitHub repo layout) except `nct_ids.txt`, which is our own derived output at
`benchmark/data/nct_ids.txt`.

| File | Size | What it is |
|---|---|---|
| `data/processed_history_data_analyses.csv` | 13.7MB | **The label file.** 1,746 rows (1 per trial), 134 columns. Combines trial metadata, registry-history summary, and all manual Numbat rating columns. This is the file DATA-NOTES §3–4 describe. |
| `data/data_IntoValue_included.csv` | 1.9MB | Trial-level metadata only (title, sponsor, phase, dates, publication linkage), no outcome-change labels. Downloaded for cross-reference; not required for the mapping. |
| `data/2022-08-12_051025-form_3-refset_16-final.tsv` | 98KB | One of four raw Numbat rating exports (pre-merge). Downloaded as a sanity check on the merge logic in `6_merge_ratings_data.R`; not needed once `processed_history_data_analyses.csv` is in hand. |
| `ASCERTAIN_Codebook_v3_amendments.docx` | 58KB | Authoritative column dictionary (from OSF, see §1). |
| `README.md`, `fun/recode_outcome_changes.R`, `fun/assign_medical_fields.R`, `1_download_sample.R` … `8_interrater_reliability.R`, `tests/T1_assess_additional_rating_file.R` | ~250KB total | Full analysis pipeline scripts from the repo. `fun/recode_outcome_changes.R` is the one that defines how raw rating columns roll up into "severe"/"non-severe"/"any" change flags — read directly to confirm the mapping in §4. |
| `nct_ids.txt` (in `benchmark/data/`, not the subfolder) | 17KB | **Our derived output.** 1,402 unique ClinicalTrials.gov NCT IDs, one per line, sorted. Extracted from `processed_history_data_analyses.csv` where `registry == 'ClinicalTrials.gov'`. |

**Not downloaded** (README-documented but out of scope for label extraction —
see §5 for sizes and why they were skipped): `combined_history_data.csv`
(45MB), `historical_versions_ct.csv` (48MB), `historical_versions_ct_2022-01-24.csv`
(43MB), `historical_versions_DRKS.csv` (4.9MB), `processed_history_data_short.csv`
(13MB, superseded by `_analyses.csv`), `data_IntoValue_extended.csv` (2MB,
superseded by `_included.csv` for our purposes), and the other 3 Numbat TSV
exports (0.9–2.9MB each, pre-merge, superseded by `_analyses.csv`).

## 3. What's in `processed_history_data_analyses.csv` — column dictionary

Confirmed against `ASCERTAIN_Codebook_v3_amendments.docx`. 134 columns, four
logical blocks:

**A. Trial metadata** (`id`, `registry`, `title`, `main_sponsor`, `study_type`,
`intervention_type`, `phase`, `enrollment`→ not present here (in
`data_IntoValue_included.csv` instead), `recruitment_status`, `masking`,
`allocation`, `doi`, `pmid`, `url`, `pub_title`, `publication_type`,
`journal_pubmed`, `journal_unpaywall`, `is_publication_2y`,
`is_publication_5y`) — from the IntoValue dataset (a separate project
covering German university-hospital trials 2009–2017, registered on either
ClinicalTrials.gov or DRKS).

- `id`: trial identifier, `NCT########` for ClinicalTrials.gov or
  `DRKS########` for the German registry.
- `registry`: `'ClinicalTrials.gov'` or `'DRKS'`. **1,402 CT.gov / 344 DRKS
  rows, 1,746 total, no duplicate IDs.**

**B. Registry-history summary** (`total_versions`, `first_reg_date`,
`first_status`, `final_status`, `original_start_date(_precision/_type)`,
`original_completion_date(_precision/_type)`, `publication_date`,
`latest_version_date`, `results_posted(_date)`, `trial_phase_start`,
`trial_phase_final`, `has_*_phase` flags, `p_outcome_changed_*` flags,
`p_outcome_start/last_*/final`, `s_outcome_*`) — derived by the paper's own
web-scraper (`cthist`) from ClinicalTrials.gov/DRKS version-history dumps.
Trial phases are defined purely by date arithmetic (from
`4_process_history_data.R` lines 371–375):

```
pre_recruitment : version_date <  original_start_date
recruitment     : original_start_date <= version_date < original_completion_date
post_completion : original_completion_date <= version_date < publication_date
post_publication: version_date >= publication_date
```

`p_outcome_changed_recruitment/postcompletion/postpublication` are booleans:
did the **verbatim primary-outcome text string** differ between the first and
last version-history record within that phase. These are the automated
screen that decided which trials got sent for manual line-by-line rating
(§C) — NOT themselves outcome-*type* labels, just "something changed here."

**C. Manual within-registry change ratings** (the actual label columns) — two
human raters coded every trial that the automated screen (`p_outcome_changed_*`)
flagged, comparing the primary-outcome text at the *start* vs *end* of each
of three phase-transitions:

| Column prefix | Transition | Codebook term |
|---|---|---|
| `change_a_i_*` | start-of-registration → end of recruitment phase | "recruitment stage" |
| `change_i_p_*` | end of recruitment → end of post-completion phase | "post-completion stage" |
| `change_p_l_*` | end of post-completion → latest available version | "post-publication stage" |

For each prefix, 16 binary sub-columns (not mutually exclusive — a trial can
have several `== '1'` at once):

| Sub-column | Codebook definition (verbatim, condensed) |
|---|---|
| `new_primary` | A new primary outcome was introduced — **also fires when a composite outcome was split into several new primaries** |
| `primary_from_secondary` | The new primary was previously listed as a secondary outcome |
| `change_measurement` / `change_aggregation` / `change_timing` | The measurement type / metric-aggregation method / assessment timing changed in a **significant** way for an existing primary (e.g. timing 24h→48h) |
| `added_measurement` / `added_aggregation` / `added_timing` | Detail was specified for an existing primary **for the first time**, i.e. narrowed (e.g. "seizure rate" → "seizure rate as recorded by family members") |
| `omitted_measurement` / `omitted_aggregation` / `omitted_timing` | Detail was **dropped** from an existing primary, i.e. broadened (inverse of the above) |
| `primary_to_secondary` | A primary outcome was demoted to secondary |
| `primary_omitted` | A primary outcome was dropped entirely (not demoted, just gone) |
| `points_to_results` | Marker: this version already has results posted (phase-boundary bookkeeping, not a change type) |
| `no_change` | Explicitly rated "nothing outcome-relevant changed" (typo fixes, stats-method wording, redundant scale descriptions are *not* counted as changes) |
| `no_phase` | This phase didn't exist for this trial |

Value encoding actually observed in the CSV: `'1'` = flagged true, `'0'` =
explicitly unflagged, `'NULL'` = left blank by the rater (functionally
equivalent to `'0'` — the paper's own R code in `fun/recode_outcome_changes.R`
only ever tests `== '1'`, treating `'0'`/`'NULL'`/`'NA'` identically as "not
this category"), `'NA'` = trial was never sent for manual rating in this
phase (either the phase didn't exist per the automated screen, or the
automated screen found zero text difference — 843/1,402 CT.gov trials fall
in this bucket for every phase simultaneously, i.e. no phase-level manual
review was ever triggered).

**D. Manual registry-vs-publication ratings** (`has_publication_rating`,
`pub_outcome_phrasing`, `outcome_determined`, `pub_outcome_change_*` — same
16 sub-categories as §C, `pub_outcome_reference(_binary)`, `pub_outcome_time`,
`pub_sig_*`) — **a different comparison axis: latest registry entry vs. the
published paper's stated outcome, not registry-version vs. registry-version.**
`has_publication_rating` "indicates whether a trial was among the **292**
trials that were randomly selected for publication screening" (codebook,
verbatim) — **this is the 292 figure**, and it is the publication-comparison
sample, not the within-registry-history sample. See §6 for why this matters.

## 4. How many trials / NCT IDs

| Population | CT.gov | DRKS | Total |
|---|---|---|---|
| Full IntoValue cohort in this file | 1,402 | 344 | 1,746 |
| Sent for manual within-registry rating (`referenceid` present, §C columns populated) | 559 | 30 | 589 |
| Selected for the publication-comparison sample (`has_publication_rating == TRUE`, §D columns populated) | 240 | 52 | **292** |

`benchmark/data/nct_ids.txt` contains all **1,402** unique ClinicalTrials.gov
NCT IDs from the full cohort (not just the 559 or 240 subsets) — this
project's tool is CT.gov-only (`PRD-trial-registry-monitor.md` line 202: "We
do not yet cover non-US registries"), and a useful FPR/negative-class
evaluation needs the ~843 trials the automated screen found *no* text
difference in, not just the ones that got a detailed rating. IDs verified
unique (`1402 == len(set(ids))`).

Within-phase label density for the 559 CT.gov trials with manual ratings
(recruitment / post-completion / post-publication counts of `no_change=='1'`
vs. any specific category `=='1'`): recruitment 368 no-change / ~191 some
change; post-completion 334 no-change / ~225 some change; post-publication
208 no-change / ~76 some change (phase didn't always exist — `no_phase` flags
account for the remainder). Full per-column counts were computed during
acquisition and are reproducible by re-running the same `csv.DictReader`
pass used to write `nct_ids.txt` (encoding is `latin-1`, not UTF-8 — the file
has non-ASCII bytes, e.g. sponsor/journal names).

## 5. Size note

Nothing downloaded exceeds 50MB (largest is `processed_history_data_analyses.csv`
at 13.7MB). Total downloaded footprint: ~16MB. The three largest files in
the source repo — `historical_versions_ct.csv` (48MB), `combined_history_data.csv`
(45MB), `historical_versions_ct_2022-01-24.csv` (43MB) — are raw scraped
ClinicalTrials.gov/DRKS version-history dumps, i.e. the *inputs* to Holst's
own diffing pipeline, not label outputs. They were deliberately **not**
downloaded: they duplicate what our own ingestion pipeline will pull directly
from the ClinicalTrials.gov API v2 `/version` endpoint for the same 1,402 NCT
IDs, and re-deriving change labels from them would just be re-implementing
Holst's own (unlabeled-by-us) diffing logic rather than using Holst's
human-adjudicated labels as ground truth. If a future task needs a
second-opinion raw-data cross-check, they're one `curl` away at the URLs in
§1.

## 6. Proposed mapping to our `change_type` codes

Our codes, per `TECH-PRD-trial-registry-monitor (1).md` §6.1:
`PRIMARY_REPLACED`, `PRIMARY_DEMOTED`, `SECONDARY_PROMOTED`,
`PRIMARY_NARROWED`, `TIMEPOINT_CHANGED`, `POST_COMPLETION_CHANGE`,
`PRIMARY_ADDED`, `PRIMARY_REMOVED`, `TIMELINE_REVISED`.

**Two independent axes need to be composed, both derived from Holst's phase
labels — this is the core of the proposed mapping:**

**Axis 1 — WHAT changed** (from the 16 sub-category flags in §C, per phase
prefix):

| Holst flag(s) | → our `change_type` | Confidence | Rationale |
|---|---|---|---|
| `primary_from_secondary` = 1 | `SECONDARY_PROMOTED` | High | Direct 1:1 match to codebook definition |
| `primary_to_secondary` = 1 | `PRIMARY_DEMOTED` | High | Direct 1:1 match |
| `change_timing` = 1 (and no add/omit/new/omitted on the same primary) | `TIMEPOINT_CHANGED` | High | Codebook example ("timing 24h→48h") is exactly our definition ("same measure, different timepoint") |
| `new_primary` = 1 AND `primary_omitted` = 1 (same phase) | `PRIMARY_REPLACED` | Medium-High | Net effect is old measure gone, new measure in; codebook explicitly allows this joint pattern via the "composite split" caveat |
| `new_primary` = 1 AND `primary_omitted` = 0/NULL | `PRIMARY_ADDED` | High | Pure addition, outcome count increases |
| `primary_omitted` = 1 AND `new_primary` = 0/NULL | `PRIMARY_REMOVED` | High | Pure removal, outcome count decreases |
| `added_measurement` / `added_aggregation` / `added_timing` = 1 | `PRIMARY_NARROWED` | Medium | Codebook: detail specified "for the first time" = narrowing scope; matches our definition's spirit ("narrowed scope") even though our worked example (all-cause→cardiovascular mortality) is a coarser kind of narrowing |
| `change_measurement` / `change_aggregation` = 1 (without new/omitted) | `PRIMARY_NARROWED` (proposed default) or `PRIMARY_REPLACED` (alternative) | **Low — flagged, needs adjudication** | Ambiguous by construction: codebook says "significant parts changed" which sounds like `PRIMARY_REPLACED`, but Holst's own severity coding (`fun/recode_outcome_changes.R`, `recode_outcomes_nonsevere_1`) rates these as *non-severe*, i.e. milder than `new_primary`/`primary_omitted`/`primary_from_secondary`/`primary_to_secondary` (which Holst rates *severe*). Recommend treating this as a genuinely hard case for the pipeline's own adjudication step rather than a fixed ground-truth label — or splitting dev-set iteration to test both mappings and see which correlates better with Holst's severity tier |
| `omitted_measurement` / `omitted_aggregation` / `omitted_timing` = 1 | **No corresponding code** | — | This is a broadening (detail removed, less specific), the mirror image of `added_*`. Our taxonomy only has `PRIMARY_NARROWED`, no "primary broadened." **Flagged as unmappable** — see §7 |
| `no_change` = 1 | negative example (no change_type) | High | True negative, useful for precision/FPR |
| `no_phase` = 1 | exclude from evaluation | High | Phase doesn't exist for this trial, not a labeling gap |

**Axis 2 — WHEN it changed** (from which phase-prefix fired, cross-cutting
with Axis 1, not exclusive):

| Holst phase prefix | Timing | → our `change_type` |
|---|---|---|
| `change_a_i_*` (recruitment stage: registration → end of recruitment) | Post-enrolment, pre-completion | Tag with the Axis-1 code only |
| `change_i_p_*` (post-completion stage: completion → publication) | **After primary completion date** | Tag with Axis-1 code **AND** additionally flag `POST_COMPLETION_CHANGE` (matches our definition exactly: "Any primary change after the primary completion date") |
| `change_p_l_*` (post-publication stage: after publication → latest version) | After primary completion date, and results already public | Tag with Axis-1 code **AND** additionally flag `POST_COMPLETION_CHANGE` (even higher-confidence case: results existed) |

Confidence: **High** for the phase→`POST_COMPLETION_CHANGE` cross-cut — it
follows directly and deterministically from Holst's own phase-boundary
definitions (§3.B date arithmetic), no interpretation required.

## 7. Label types we cannot map (flagged)

1. **`omitted_measurement` / `omitted_aggregation` / `omitted_timing`** — no
   corresponding `change_type` code exists for "primary outcome broadened /
   made less specific." Our taxonomy is asymmetric (has `PRIMARY_NARROWED`,
   no opposite). Options for the eval harness: (a) drop these rows from
   scoring entirely (treat as untestable), or (b) count them as a
   `PRIMARY_NARROWED` miss if our pipeline doesn't also detect *some* primary
   change there (partial credit for "detected a change" even if the
   direction/type is wrong). Recommend (a) for the dev-set F1 numbers and
   noting the gap explicitly in `results.md`, since (b) would silently
   misrepresent what our taxonomy covers.

2. **`TIMELINE_REVISED`** — no ground truth available in this dataset at
   all. Holst's labels are about *outcome* text changes only; they never
   coded whether `start_date`/`primary_completion_date` fields themselves
   were retroactively edited between versions. The raw version-history dumps
   (`historical_versions_ct.csv` etc., §5) would in principle let us derive
   this ourselves by diffing date fields directly, but that produces *our*
   labels, not Holst's independently-adjudicated ones — cannot be used as
   external ground truth for this code. `results.md` should state this as a
   flat limitation: no benchmark coverage for `TIMELINE_REVISED`.

3. **`change_measurement` / `change_aggregation`** — technically mappable
   (see Axis 1 table) but flagged as low-confidence because Holst's own
   severity scheme treats them as categorically different from the
   swap/demote/promote group that maps cleanly to our SIGNAL-tier codes.
   Not a hard "cannot map," but should not be treated as a clean label
   during dev-set iteration without a sanity check.

4. **`pub_outcome_change_*` (§D, the whole registry-vs-publication axis)** —
   not a mapping gap so much as a scope mismatch: our tool "compares the
   registry to its own history... not the published paper"
   (`PRD-trial-registry-monitor.md` line 51). These 240 CT.gov trials'
   publication-comparison labels are simply **out of scope** for this
   benchmark and should not be joined against our pipeline's predictions at
   all. This is also why the "292 trials" figure from the task brief should
   not be used as the benchmark trial count — it's the publication-screening
   sample size, not the within-registry-history sample size (589 CT.gov+DRKS
   trials, 559 of them CT.gov) that this tool's predictions are actually
   comparable to.

## 8. Encoding / parsing gotcha

`processed_history_data_analyses.csv` is **not valid UTF-8** (contains raw
Latin-1 bytes, e.g. in `main_sponsor`/`journal_pubmed` fields with accented
characters). Any future script reading it directly should open with
`encoding='latin-1'` (or `errors='replace'`) rather than the default UTF-8,
which raises `UnicodeDecodeError` partway through the file.
