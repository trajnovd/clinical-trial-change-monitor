# Technical PRD — Clinical Trial Registry Change Monitor

**Companion to:** PRD-trial-registry-monitor.md

---

## 0. Preflight

Three facts determine whether this product exists. Establish them before building anything.

**P1. The version history API returns full outcome contents per version** — not just change metadata or a diff summary. Pull complete history for five known trials and inspect the payload.

**P2. The signal exists at volume.** Sample 1,000 random interventional trials, count how many show any post-enrolment primary outcome change. This number shapes the entire product narrative.

**P3. Outcome field structure is consistent across eras.** ClinicalTrials.gov changed its data model around the 2017 FDAAA Final Rule. Pre-2017 registrations may carry free-text outcome fields where post-2017 carry structured ones. If parsing diverges by era, that's a known cost rather than a surprise.

If P1 fails, the product does not exist in this form. If P2 comes back thin, the framing shifts from breadth to depth. If P3 diverges, restricting to post-2017 is an acceptable v1 scope reduction.

---

## 1. Scope

**The product:** ingest every version of every clinical trial registration, detect where the declared outcome measures changed, classify each change against the trial's own timeline, and publish the result as a queryable, verifiable index.

**In scope:**
- ClinicalTrials.gov (other registries in the roadmap)
- Interventional studies
- Primary and secondary outcome measures
- Version-to-version change detection
- Benchmarking against human-coded ground truth
- Public interface: per-trial timeline view and aggregate index

**Out of scope, deliberately:**
- Published paper parsing. Not needing the paper is the strategic advantage — it removes the paywall ceiling every competing approach runs into. Do not break this without a reason.
- Any assertion of intent or misconduct. The product reports documented changes. Interpretation belongs to the reader.
- Results data, adverse events, statistical analysis plans.

---

## 2. Architecture

```
┌──────────────────────────────────────────────────────────┐
│  INGESTION                                                │
│  CTG API v2 → version list → snapshots → extracted store  │
│  Deterministic · idempotent · resumable                   │
└──────────────────────────────────────────────────────────┘
                          ↓
┌──────────────────────────────────────────────────────────┐
│  EXTRACTION & NORMALISATION                               │
│  snapshot → outcome records + timeline facts              │
│  Deterministic                                            │
└──────────────────────────────────────────────────────────┘
                          ↓
┌──────────────────────────────────────────────────────────┐
│  MATCHING CASCADE                          ← the ML core  │
│  Which outcomes across two versions are the same measure? │
│  T0 exact → T1 embedding → T2 cross-encoder → T3 LLM      │
└──────────────────────────────────────────────────────────┘
                          ↓
┌──────────────────────────────────────────────────────────┐
│  TIMELINE CLASSIFICATION                                  │
│  Position each change against enrolment and completion    │
│  Deterministic                                            │
└──────────────────────────────────────────────────────────┘
                          ↓
┌──────────────────────────────────────────────────────────┐
│  ADJUDICATION (multi-agent)                               │
│  Change type, severity, confidence, written rationale     │
│  Adversarial: defence vs prosecution vs judge             │
└──────────────────────────────────────────────────────────┘
                          ↓
                findings → API → interface
```

**Governing principle:** everything that can be deterministic must be. Models run only where semantics genuinely require them. This is not merely cost control — deterministic components are what permit hard accuracy claims. "The timeline classifier cannot be wrong, it is date arithmetic" is a stronger statement than any benchmark figure.

---

## 3. Data

### 3.1 Source

ClinicalTrials.gov API v2, `https://clinicaltrials.gov/api/v2/`. Public domain, unauthenticated.

- `GET /studies` — search and filter, paginated via `pageToken`
- `GET /studies/{nctId}` — current record
- Version list and per-version snapshot endpoints (confirm exact paths in P1)

If the history API disappoints, `cthist` (Carlisle, PLOS ONE, doi 10.1371/journal.pone.0270909) and the ClinicalTrials History Scraper already solve historical retrieval. Read their source before writing your own.

### 3.2 Volume

| | Estimate |
|---|---|
| Registered studies | ~540,000 |
| Interventional | ~380,000 |
| Interventional, post-2008, ≥2 versions | ~200,000 |
| Mean versions per study | ~8–12 |
| Snapshots to fetch | ~2M |
| Snapshot size | 30–150 KB |
| Raw storage if stored whole | 100–300 GB |

Do not store whole snapshots. Extract the outcome module and timeline fields on ingest, keep a content hash for reproducibility, discard the rest. Storage drops to a few GB and everything downstream gets fast.

### 3.3 Ingestion

Filter before fetching:
1. Interventional only
2. Start date ≥ 2008 (post-FDAAA registration mandate; data quality is materially better)
3. ≥2 versions (a single-version study cannot have changed)

**Concentric corpora.** Start with completed interventional trials with posted results (~60,000). This is where signal concentrates and it is a tenth of the work. Expand outward once the pipeline is proven.

Assume throttling. Build for it from the first line: async with bounded concurrency, exponential backoff on 429/5xx, a persistent resumable job queue, and a local cache keyed by `(nct_id, version)` so re-runs are free. Full ingest is a long-running background job — start it early and build against partial data.

### 3.4 Schema

```sql
trials(
  nct_id TEXT PRIMARY KEY,
  study_type TEXT,
  phase TEXT,
  overall_status TEXT,
  lead_sponsor TEXT,
  sponsor_class TEXT,          -- INDUSTRY / NIH / OTHER
  conditions TEXT[],
  enrolment_count INT,
  first_posted_date DATE,
  version_count INT
)

versions(
  nct_id TEXT,
  version_no INT,
  version_date DATE,
  content_hash TEXT,
  PRIMARY KEY (nct_id, version_no)
)

outcomes(                      -- the core table
  nct_id TEXT,
  version_no INT,
  outcome_type TEXT,           -- PRIMARY / SECONDARY / OTHER
  ordinal INT,
  measure TEXT,
  description TEXT,
  time_frame TEXT,
  measure_norm TEXT,
  embedding VECTOR(768)
)

timeline_facts(                -- as recorded in each version; see §6.2
  nct_id TEXT,
  version_no INT,
  start_date DATE,
  start_date_type TEXT,        -- ACTUAL / ESTIMATED
  primary_completion_date DATE,
  primary_completion_type TEXT,
  completion_date DATE
)

findings(
  finding_id UUID PRIMARY KEY,
  nct_id TEXT,
  from_version INT,
  to_version INT,
  change_type TEXT,
  severity TEXT,               -- SIGNAL / CONTEXT / NOISE
  before_measure TEXT,
  after_measure TEXT,
  days_after_enrolment INT,
  days_after_primary_completion INT,
  confidence REAL,
  resolved_by TEXT,            -- which cascade tier decided
  rationale TEXT
)
```

---

## 4. Extraction and normalisation

Deterministic, two jobs.

**Parse the outcome module** from each snapshot into `outcomes` rows, handling era differences found in P3.

**Normalise measure strings** for cheap matching:
- Lowercase, strip punctuation, collapse whitespace
- Expand a curated abbreviation dictionary (HAM-D → Hamilton Depression Rating Scale; OS → overall survival; PFS → progression-free survival; ORR → objective response rate). Build it from the most frequent tokens in the corpus — modest effort, large downstream payoff.
- Split trailing timepoints into `time_frame` where the registry did not

Normalisation quality determines how much of the corpus resolves free at Tier 0. It is the highest-leverage unglamorous work in the system.

---

## 5. The matching cascade

### 5.1 The real problem

For each consecutive version pair, decide for each outcome whether two strings describe **the same measured thing**. Surface similarity and semantic equivalence diverge in both directions:

| A | B | Text similarity | Truth |
|---|---|---|---|
| "Change in HAM-D from baseline to week 6" | "Hamilton Depression Rating Scale improvement at 6 weeks" | low | **same** |
| "All-cause mortality at 12 months" | "Cardiovascular mortality at 12 months" | very high | **different** |
| "Overall survival" | "Overall survival at 24 months" | high | different — timepoint added |

Row two is the dangerous case: narrowing a mortality endpoint is a serious change that looks nearly identical as text. Cosine similarity gets it exactly backwards. This is why the system needs a trained discriminator rather than an embedding threshold — and it is the cleanest demonstration that this is real engineering rather than a wrapper.

### 5.2 Tiers

**T0 — exact match after normalisation.** Expect the large majority of outcome pairs across consecutive versions to be byte-identical; most edits touch contact details, not outcomes. Free, instant, zero error.

**T1 — embedding retrieval.** Embed non-identical sets, compute the similarity matrix. Use a biomedical encoder (PubMedBERT, BioLinkBERT, `S-PubMedBert-MS-MARCO`) rather than a general one; clinical vocabulary matters. Accept above ~0.95 as same, below ~0.45 as different, escalate the band between.

**T2 — fine-tuned cross-encoder.** The model. Takes both strings jointly, outputs same / different / narrowed / broadened. Cross-encoders outperform bi-encoders substantially on exactly this kind of fine distinction because they attend across both texts.

Training data is bootstrapped rather than hand-labelled:
- *Positives* — identical outcome strings across versions of the same trial with surrounding context unchanged; LLM-generated paraphrases of real corpus strings
- *Hard negatives* — outcomes from different trials in the same condition area (similar vocabulary, different measure); different outcomes within one trial
- *Hard cases* — ambiguous-band pairs labelled by an LLM against a written rubric, spot-checked manually

This is knowledge distillation: an expensive model labels, a cheap model runs at scale. It is a legitimate ML contribution and the honest answer to "did you train a model."

**T3 — LLM adjudication.** Residual hard cases only. Structured output: `{same, relationship, confidence, reasoning}`.

### 5.3 Economics

| Tier | Share | Cost |
|---|---|---|
| T0 | ~90% | zero |
| T1 | ~7% | local GPU |
| T2 | ~2.5% | local GPU |
| T3 | ~0.5% | a few hundred dollars across the full corpus |

LLM-only across ~2M pairs would run into five figures and take weeks. The cascade is the difference between a system that can run on the whole registry and one that cannot — worth stating explicitly, because it is the kind of engineering judgement that separates a product from a notebook.

---

## 6. Timeline classification

Deterministic date arithmetic. Highest leverage per unit of effort in the system.

For a change at version *v* dated *d*: `days_after_enrolment = d − start_date`, `days_after_primary_completion = d − primary_completion_date`.

### 6.1 Change taxonomy

**SIGNAL**

| Code | Definition |
|---|---|
| `PRIMARY_REPLACED` | Primary swapped for a different measure, post-enrolment |
| `PRIMARY_DEMOTED` | Primary moved to secondary, post-enrolment |
| `SECONDARY_PROMOTED` | Secondary moved to primary, post-enrolment |
| `PRIMARY_NARROWED` | Same construct, narrowed scope (all-cause → cardiovascular mortality) |
| `TIMEPOINT_CHANGED` | Same measure, different timepoint, post-enrolment |
| `POST_COMPLETION_CHANGE` | Any primary change after the primary completion date — highest severity, results existed |
| `PRIMARY_ADDED` / `PRIMARY_REMOVED` | Count of primary outcomes changed post-enrolment |

**CONTEXT** — any of the above pre-enrolment; rewording with no semantic change; changes carrying a documented rationale.

**NOISE** — formatting, typos, administrative fields, non-outcome modules.

### 6.2 The date-manipulation trap

**Start dates and completion dates are themselves editable, and they move.**

A sponsor can retroactively push `start_date` forward, which makes a post-enrolment outcome change appear pre-enrolment. Using current date fields therefore systematically undercounts precisely the cases that matter most.

- Always use the **earliest recorded** value of `start_date` and `primary_completion_date` across all versions
- Emit `TIMELINE_REVISED` as an independent finding whenever these dates move — retrospective date edits are interesting in their own right
- Track `ESTIMATED` → `ACTUAL` transitions; only ACTUAL is trustworthy

Most naive implementations miss this. Handling it correctly is a credibility marker under questioning.

---

## 7. Adjudication

Runs only on SIGNAL candidates. Genuine multi-agent architecture, warranted because the question is contestable rather than factual.

- **Extractor** — assembles the evidence package: both versions, dates, sponsor, phase, stated rationale if any
- **Defence** — argues the change is legitimate (recruitment shortfall, regulatory instruction, pre-data clarification)
- **Prosecution** — argues it is concerning (post-hoc, post-results, favourable direction)
- **Judge** — assigns severity and confidence given both arguments

Output is a confidence score and a written rationale, never a verdict of misconduct. The rationale is what makes the interface trustworthy: a user must be able to read *why* something was flagged and disagree with it.

Structured outputs throughout. Log every prompt and response — reproducibility will be asked about.

---

## 8. Benchmark

### 8.1 Ground truth

| Source | N | Labels | Role |
|---|---|---|---|
| **Holst et al. 2023** — osf.io/e2uct; GitHub Martin-R-H/InvisibleOutcomeChanges | 292 | Registry version-history outcome changes | **Primary — our exact task** |
| COMPare — Figshare 10.6084/m9.figshare.7717361 | 67 trials / 658 outcome records | Registry vs publication | Secondary |
| IntoValue — Zenodo 10.5281/zenodo.5141342 | ~1,900+ | Structured trial corpus | Sampling frame |

### 8.2 Protocol

Split Holst into development and held-out test portions. **Do not touch the test split until the final evaluation.** At this N, iterating against test silently overfits and any reported number becomes a lie.

Report per change type: precision, recall, F1. Report false-positive rate separately and prominently. Report cascade tier attribution — how much was decided deterministically versus by model.

### 8.3 Independent validation set

Holst is small. Sample ~200 trials from the working corpus, have each team member code them independently against a written rubric, measure inter-rater agreement (Cohen's κ), resolve disagreements into a consensus set.

This yields two things: a second test set, and the ability to state "human coders agree with each other at κ=0.8; the system agrees with consensus at 0.85." That reframes the claim from perfection to **parity with human experts** — more defensible and more impressive.

### 8.4 Targets

- Recall ≥ 0.85 on `PRIMARY_*` changes, held-out
- False-positive rate < 5%
- ≥ 85% of decisions resolved without an LLM call
- Every finding traceable to two linkable registry versions

---

## 9. Interface

### 9.1 Timeline scrubber

The single most important artefact in the product.

- Horizontal timeline for one trial, version markers as nodes
- Two vertical reference lines: enrolment start, primary completion
- Drag → the outcome panel updates to that version's state
- Changed text diff-highlighted; removals struck through, additions marked
- Version date and "N days after enrolment" always visible
- Deep-linkable per trial, with outbound links to both registry versions

Build it early against hardcoded data. The interface should drive the pipeline, not wait on it.

### 9.2 Aggregate index

Filterable by sponsor, sponsor class, condition, phase, year, change type, severity. Sortable. Headline counter: *N trials changed their primary outcome after enrolment began.*

### 9.3 Stack

- **Pipeline** — Python 3.11+, `httpx` async, `pydantic`, `polars`
- **Store** — DuckDB. Embedded, single file, excellent at this analytical workload, zero ops. Postgres only if concurrency demands it.
- **Vectors** — numpy + FAISS flat index. The corpus does not justify a vector database.
- **Models** — HuggingFace `transformers`, `sentence-transformers`. Fine-tune on rented GPU; a base-size cross-encoder trains in hours on one A100.
- **API** — FastAPI
- **Interface** — React + Vite + Tailwind; D3 or Recharts for the timeline
- **Orchestration** — a Makefile and idempotent scripts. Do not introduce a scheduler at this scale.

---

## 10. Versions

Each version is independently coherent, demonstrable, and useful. Nothing here is time-boxed; ship a version when its exit criteria hold.

---

### v0.1 — Corpus

*A queryable local corpus of outcome records across every version of every trial in the working set.*

- Preflight P1–P3 resolved
- Ingestion against the results-posted subset, with backoff, resumability, caching
- Outcome module parsing across eras
- Normalisation and abbreviation dictionary
- Timeline fact extraction with earliest-recorded-date logic

**Exit:** you can query "show every recorded version of the primary outcome for NCT#####" and get a correct, complete answer.

---

### v0.2 — First signal

*The raw count. The number the entire product narrative rests on.*

- T0 exact matching across consecutive versions
- Timeline classification and the full change taxonomy
- `findings` table populated
- `TIMELINE_REVISED` detection

**Exit:** a defensible answer to "how many trials changed their primary outcome after enrolment began, and how many after results existed."

This is the decision point. A large number means breadth is the story. A small one means depth and severity are the story. Either is workable; knowing which is essential.

---

### v0.3 — Semantic matching

*Detection that survives rewording.*

- T1 embedding retrieval with biomedical encoder
- Training data generation for T2
- Fine-tuned cross-encoder, evaluated against the development split
- T3 LLM adjudication for the residual
- Tier attribution instrumented

**Exit:** the system correctly identifies "HAM-D at 6 weeks" and "Hamilton Depression Rating Scale, week 6" as the same measure, and "all-cause mortality" and "cardiovascular mortality" as different — with the tier mix measured.

---

### v0.4 — Judgement

*Findings a reader can evaluate rather than merely receive.*

- Multi-agent adjudication over SIGNAL candidates
- Severity and confidence scoring
- Written rationale per finding
- Full prompt/response logging

**Exit:** every flagged trial carries a readable explanation of why it was flagged and what the counter-argument is.

---

### v0.5 — Validated

*Numbers that hold up under questioning.*

- Benchmark harness against Holst development split
- Independent validation set coded, κ measured, consensus resolved
- Iteration confined to development splits
- Single held-out evaluation, reported as-is
- Failure analysis by change type

**Exit:** precision, recall, F1 and false-positive rate published per change type, with the held-out discipline intact and human inter-rater agreement as the comparison baseline.

---

### v0.6 — Public

*Someone who has never seen it understands it in ten seconds.*

- Timeline scrubber, complete
- Aggregate index with filtering
- Deep links to source registry versions
- Case studies: a handful of specific, well-documented, independently checkable trials

**Exit:** hand it to someone with no medical background and no context; they understand what happened in that trial without explanation.

---

### v1.0 — Complete

*Everything above, coherent, frozen.*

- Full working corpus processed end to end
- All numbers reproducible from a clean run
- Documented limitations stated in the product itself
- Methodology write-up sufficient for external scrutiny

**Exit:** a stranger can reproduce the headline number from the repository.

---

### Beyond v1.0

Roadmap, in rough order of value:

- **EU CTIS / EUCTR / ISRCTN / ICTRP** — the `ctrdata` package covers all four. Multiplies coverage and makes the product genuinely international.
- **Publication linking** — connect flagged trials to their published papers via NCT numbers in full text (Europe PMC, PMC OA). This is where the product goes from "the registration changed" to "the registration changed and the paper reports the new version without disclosure." Deliberately deferred until the registry-side product stands alone, because it reintroduces the paywall dependency.
- **Continuous monitoring and alerts** — watch for new changes as they happen rather than auditing historically. Turns an index into a service.
- **Public API** — let systematic reviewers and HTA bodies query programmatically.
- **Sponsor-level analytics** — aggregate patterns by sponsor, therapeutic area, and over time. The most commercially interesting layer, and the most likely to be contested.

---

## 11. Risks

| Risk | Impact | Response |
|---|---|---|
| Version history incomplete | Fatal | P1 before anything; `cthist` fallback |
| Signal volume low | Narrative shifts | v0.2 gate; pivot to depth framing |
| Pre/post-2017 schema divergence | Parsing cost | P3; restrict to post-2017 if needed |
| Rate limiting | Slow ingest | Async, backoff, cache, start early |
| Cross-encoder underperforms | Cost, not correctness | Fall back to T3 for the whole band |
| Overfitting to Holst (N=292) | Reported numbers become false | Strict held-out discipline; independent set |
| Scope creep into paper parsing | Loses the strategic advantage | Explicitly deferred to post-v1.0 |

---

## 12. What the product is for

Three things carry it. Everything else is optional.

**A number nobody has published.** How many trials changed their primary outcome after enrolment began, and how many after results existed. Nobody has counted at scale.

**A benchmark that survives scrutiny.** Precision, recall, false-positive rate against human-coded ground truth, with held-out discipline intact. Stating your own error rate plainly is the strongest credibility signal available.

**A demonstration that needs no explanation.** The scrubber. Mortality disappears. The dates are on screen.
