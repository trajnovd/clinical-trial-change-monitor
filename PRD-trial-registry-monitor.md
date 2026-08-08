# PRD — Clinical Trial Registry Change Monitor

**Working name:** TBD (candidates in §11)
**Version:** 0.1 draft
**Date:** 8 August 2026
**Owner:** Risto

---

## 1. The one-line version

Clinical trials publicly declare what they will measure before they start. Some of them quietly change that declaration afterwards. Every old version is public. Nobody reads them. We do.

---

## 2. Problem

### 2.1 How it works today

Before a clinical trial begins, researchers must register it publicly — on ClinicalTrials.gov, the EU register, or an equivalent. The registration states the **primary outcome**: the single thing the trial is designed to measure. For example, "all-cause mortality at 12 months."

This exists so that a trial can't quietly redefine success after seeing its results. Journals require registration (ICMJE), regulators require it (FDAAA 801), and funders require it.

### 2.2 What goes wrong

Registrations can be edited after the trial starts. Sometimes for legitimate reasons — recruitment shortfalls, safety board instructions, new external evidence. Sometimes not: the registered primary outcome fails, and it is swapped for a secondary outcome that happened to look good. The published paper then reports the replacement as though it were the plan all along.

This is called **outcome switching**, and it converts a failed trial into an apparent success.

### 2.3 Scale

- Across 27 separate cohort studies, the median rate of discrepancy between registered and published primary outcomes is **~31%**; among prospectively registered trials, ~41% (Jones et al. 2015, *BMC Medicine*).
- Secondary outcome discrepancies are higher still: 64% of trials in five top journals showed non-primary outcome differences (Fleming, Koletsi, Dwan & Pandis, *PLoS ONE* 2015).
- The COMPare project (Oxford CEBM, Goldacre) manually checked 67 RCTs in NEJM, JAMA, Lancet, BMJ and Annals. **Nine were correctly reported.** They found 301 unreported pre-specified outcomes and 357 silently added novel outcomes.
- Institutional response is poor: of 58 correction letters COMPare sent to journals, 23 (40%) were published, at a median delay of 99 days.

### 2.4 Why it matters beyond academia

Reboxetine is the canonical case: an antidepressant that appeared effective in published literature and turned out to be approximately placebo-equivalent once unreported outcomes were accounted for. Patients took it for years. Outcome switching is one of the mechanisms by which that happens.

### 2.5 Why it isn't already solved

Detecting it has required a human to open a registry entry, open the paper, and compare — for one trial at a time. COMPare did 67 trials in six weeks with a team. There are over 500,000 registered trials. Manual auditing does not scale, so nobody audits.

---

## 3. What we build

**A system that reads every version of every trial registration and reports where the declared outcome changed after the trial began.**

Not the published paper. Just the registry, against itself.

ClinicalTrials.gov retains full version history and exposes it through a public API. When someone edits a registration, the previous version remains permanently accessible. The evidence is already public — it has simply never been read at scale.

### 3.1 Output

For each flagged trial:
- The original registered primary outcome, with the date and version it was recorded
- The current primary outcome, with the date it changed
- A classification of the change type
- The relevant timeline markers (enrolment start, primary completion date) that determine whether the change happened before or after data existed
- A direct link to both registry versions so any claim can be verified independently

And in aggregate: a browsable, filterable index of every trial where this occurred, by sponsor, condition, phase, year, and journal where published.

### 3.2 Explicitly not in scope for v1

- We do not parse published papers. That removes the paywall constraint that limits every competing approach.
- We do not assert misconduct. We report that a documented change occurred, when, and what it was. Interpretation belongs to the reader.
- We do not do statistical error detection, image duplication, or plagiarism. Those are solved and owned by others.

---

## 4. Why this specific product

### 4.1 The competitive situation

**RegCheck** (regcheck.app; Cummins et al., arXiv 2601.13330, January 2026; University of Bern with Oxford's Bennett Institute) is the closest existing tool. It is open source and free. A user pastes in a trial ID and uploads a paper; an LLM compares the two and flags deviations, including outcome switching.

We must assume judges may know about it. Our differentiation is concrete and defensible:

| | RegCheck | Us |
|---|---|---|
| Input | Human pastes one trial ID + one paper | Automated, whole registry |
| Scale | One pair at a time, on request | Continuous, all trials |
| Needs the paper | Yes — paywalls cap coverage | No |
| Reads version history | No | **Yes — this is the core** |
| Catches silent post-hoc edits | No | Yes |
| Published accuracy | None yet | Benchmarked (§7) |

The version history is the wedge. RegCheck compares a registration to a paper. We compare a registration to its own past — which is where the most damaging changes hide, because the original wording has been overwritten and no reader of either the registry or the paper would ever see it.

### 4.2 Why it fits our constraints

- All data is free, public, and bulk-accessible from Skopje. No permissions, partnerships, NDAs, or customer access required.
- The output is objectively verifiable — a change either occurred between two versions or it did not.
- Labelled ground truth exists for benchmarking (§7).
- The demo is visually self-evident in under ten seconds to someone with no medical background.

---

## 5. Team and roles

Three people. Two technical (both with Claude), one on narrative, video and design.

**Risto — technical lead, architecture and benchmark.** Owns the pipeline design, the change-classification logic, and the benchmark study. This is the piece that carries the pitch's credibility, and it maps directly to prior AI-versus-human-expert benchmarking work.

**Second technical — data engineering and interface.** Owns registry ingestion, the version-diffing engine, storage, and the demo UI. The UI matters as much as the backend: the demo *is* the product for pitch purposes.

**Third — narrative, video, design.** Owns the pitch story, the video production, slides, naming, and the one-page explainer. Also owns the domain research: assembling the case studies, the harm examples, and the sourcing behind every claim we make on stage. This person does not need to code, but does need to understand the problem well enough to defend it under questioning.

**Shared:** all three rehearse the pitch. The video should feature more than one person.

---

## 6. Technical design

### 6.1 Pipeline

```
ClinicalTrials.gov API v2 (/studies, /version endpoints)
        ↓
[1] Snapshot ingestion — pull all historical versions per NCT ID
        ↓
[2] Structural diff — extract primary/secondary outcome fields per version
        ↓
[3] Semantic matcher — decide whether two differently-worded outcomes
    are the same measure ("HAM-D at 6 weeks" vs "Hamilton Depression
    Rating Scale score, 6-week timepoint")
        ↓
[4] Timeline classifier — locate each change relative to enrolment
    start and primary completion date
        ↓
[5] Adjudicator — assign change type and confidence; suppress noise
        ↓
Index + API + browsable UI
```

Steps 3 and 5 are where LLM/agent work is genuinely warranted. Steps 1, 2 and 4 are deterministic and should stay that way — anything that can be done with exact logic must be, because deterministic components are what let us make hard accuracy claims.

### 6.2 Data sources

- **ClinicalTrials.gov API v2** — REST, JSON, unauthenticated, public domain. Includes a `/version` endpoint exposing historical records. Primary source for v1.
- **`cthist`** (R package, Carlisle) and the ClinicalTrials History Scraper — existing tooling for mass historical download. Use these rather than rebuilding; they are proven.
- **EU CTIS / EUCTR / ISRCTN** — accessible programmatically (see the `ctrdata` R package). Out of scope for v1, in scope for the roadmap.
- **WHO ICTRP** — XML dump available. Roadmap.

Verify the `/version` endpoint hands back what we need *before* building anything else. This is the single dependency the entire product rests on.

### 6.3 Change taxonomy

The product's quality lives in this classification. Getting it wrong makes us an accusation machine.

**Signal — report prominently:**
- Primary outcome replaced entirely, after enrolment began
- Primary outcome demoted to secondary, after enrolment began
- Secondary outcome promoted to primary, after enrolment began
- Any primary outcome change after the primary completion date (i.e. after results existed)
- Primary outcome timepoint or measurement scale changed post-enrolment

**Context — report, flagged as lower concern:**
- Changes made before enrolment began
- Changes with an accompanying documented rationale
- Purely clarifying rewording with no change in what is measured

**Noise — suppress:**
- Formatting, typos, administrative field updates
- Contact details, recruitment sites, eligibility text
- Anything not touching outcome definitions

**Design principle:** a change is a fact, not an accusation. We surface facts and label them accurately. We never use the words "fraud" or "misconduct" in product output.

---

## 7. Benchmark protocol

This is the part that makes the pitch credible, and it is what nobody else has published.

### 7.1 Ground truth

- **COMPare** — 67 trials, 658 outcome-level records, manually coded. Available at compare-trials.org and Figshare (DOI 10.6084/m9.figshare.7717361).
- **Holst et al. 2023** (*PLOS Medicine*, DOI 10.1371/journal.pmed.1004306) — 292 trials manually audited *with registry version history*, which is precisely our task. Data at osf.io/e2uct and GitHub (Martin-R-H/InvisibleOutcomeChanges).
- **IntoValue** — the surrounding structured corpus, Zenodo 10.5281/zenodo.5141342.

Holst is the more important of the two, because it audits version history rather than registry-versus-paper.

### 7.2 What we report

Precision, recall and F1 per change type, against the human-coded labels. Plus false-positive rate stated explicitly and separately.

### 7.3 Targets

- Recall ≥ 0.85 on primary-outcome changes in the Holst set
- False-positive rate < 5%
- Every flagged trial independently verifiable via the two linked registry versions

### 7.4 Honesty requirement

State the limitations in the pitch before a judge raises them:
- We detect *that* a change occurred, not *why*. Some changes are legitimate.
- Registry data quality varies; some trials are poorly registered.
- We do not yet cover non-US registries.

Volunteering limitations reads as rigour. Getting caught omitting them reads as overclaiming.

---

## 8. Timeline

### Phase 0 — application (by 9 August)

| Task | Owner |
|---|---|
| Verify the `/version` endpoint returns usable history for one real trial | Technical |
| Write application answers | Risto + narrative |
| Film pitch video | All three |
| Submit | Risto |

Do not build in Phase 0. The application is a screening step. What it needs is a clear problem, a clear product, and a team that looks like it can execute.

### Phase 1 — weeks 1–2 (mid-August)

- Ingestion working against the full registry
- Structural outcome-field diffing across versions
- First raw count: how many trials show any post-enrolment primary outcome change
- **Gate:** if that number is trivially small, or if the version data is unusable, stop and reassess. Everything depends on this.

### Phase 2 — weeks 3–5 (late August–mid September)

- Semantic matcher (same measure, different wording)
- Timeline classifier
- Change taxonomy implemented with noise suppression
- Benchmark harness against Holst; first accuracy numbers

### Phase 3 — weeks 6–7 (mid–late September)

- Demo UI: the timeline scrubber (§9)
- Benchmark tightened to target numbers
- Aggregate index and filtering
- Case studies assembled — three to five specific, well-documented trials

### Phase 4 — final week (to 1 October)

- Pitch rehearsal, repeatedly
- Freeze the demo. Nothing new after this point.
- Prepare answers to the hard questions (§12)

---

## 9. The demo

Ten seconds, no medical knowledge needed.

**Shot 1 — one trial, timeline scrubber.** Version 1, dated before enrolment: primary outcome is *all-cause mortality*. Drag the slider forward. Version 4, dated after the trial completed: primary outcome is now *improvement in symptom score*. Mortality is gone. The two dates are visible on screen the whole time.

**Shot 2 — zoom out.** The same pattern across a wall of trials, with a running counter. "This has happened N times."

**Shot 3 — the scoreboard.** Precision, recall, false-positive rate, against the human-audited benchmark.

The killer line, delivered over shot 1: *"Every version of this was public the entire time. Nobody looked."*

---

## 10. Who pays

Ranked by plausibility. To be validated during the build, not assumed.

1. **Systematic review and HTA bodies** — Cochrane, NICE, IQWiG, ICER. They do this manually today and have workflow budgets. Strongest fit.
2. **Publisher integrity workflows** — Frontiers (AIRA), Digital Science (Ripeta), the STM Integrity Hub. A 2026 Frontiers review explicitly names automated registry cross-referencing as an unexploited opportunity, which is unusually direct evidence of demand.
3. **Pharma competitive intelligence, litigation support, healthcare investors** — high willingness to pay, but relationship-driven sales.

**Avoid first:** selling to journals as pre-publication screening. COMPare's correspondence record shows entrenched editorial resistance.

Honest position for the pitch: the buyer is not yet validated. Say so. Frame the eight weeks as building the evidence base — the index and the benchmark — that makes the buyer conversation possible, rather than claiming a pipeline that doesn't exist.

---

## 11. Naming

Needs to convey monitoring and transparency, not accusation.

Candidates: **Redline**, **Priorwork**, **Trialwatch**, **Versionary**, **Baseline**, **First Version**.

Check availability before the video is filmed.

---

## 12. Risks and answers

**"RegCheck already does this."**
RegCheck compares a registration to a paper, one at a time, when a human asks. We compare a registration to its own history, across the whole registry, automatically. Different input, different failure mode, different coverage — and we don't need the paper, so paywalls don't cap us.

**"Aren't most of these changes legitimate?"**
Some are, and we label them as such. Nobody knows the proportion, because nobody has measured at scale. That is the gap. Making changes visible helps honest researchers, whose reasons are documented — it only removes cover from those without one.

**"Who pays?"**
Answer honestly per §10. Do not invent a pipeline.

**"Why hasn't anyone done this?"**
Because it requires diffing hundreds of thousands of registry snapshots and resolving semantically equivalent medical language across them. The data has always been public; reading it at scale has not been feasible until recently.

**Technical risk: the `/version` endpoint may be incomplete or rate-limited.** Verify before submitting the application. If ClinicalTrials.gov history proves unusable, the product does not exist.

**Scope risk: the temptation to add paper parsing.** Resist it in v1. Not needing the paper is the advantage, not a limitation.

---

## 13. Open questions

1. Name — decide before filming.
2. How many trials actually show post-enrolment primary outcome changes? Unknown until Phase 1. The entire pitch scales with this number.
3. Should v1 cover EU CTIS as well, or US only? Default: US only, EU as roadmap.
4. Is the aggregate index public and free (transparency-project positioning) or gated (commercial positioning)? Affects both narrative and business model.
