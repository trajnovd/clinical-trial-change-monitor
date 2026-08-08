# Holst benchmark -- dev split results

Coverage: 75 evaluated / 279 labeled (27%), 204 awaiting ingest.

**Ingest coverage is below 50%.** These numbers are a checkpoint on a partial sample, not the final v1.0 benchmark -- re-run `evaluate.py --split dev` once `data/ingest_holst.log` reports the full Holst corpus ingested.

## Per change_type (trial-level)

| change_type | TP | FP | FN | TN | precision | recall | F1 |
|---|---|---|---|---|---|---|---|
| PRIMARY_REPLACED | 0 | 20 | 0 | 55 | 0.000 | n/a | n/a |
| PRIMARY_DEMOTED | 1 | 0 | 1 | 73 | 1.000 | 0.500 | 0.667 |
| SECONDARY_PROMOTED | 1 | 1 | 0 | 73 | 0.500 | 1.000 | 0.667 |
| PRIMARY_NARROWED | 0 | 0 | 20 | 55 | n/a | 0.000 | n/a |
| TIMEPOINT_CHANGED | 1 | 18 | 3 | 53 | 0.053 | 0.250 | 0.087 |
| POST_COMPLETION_CHANGE | 30 | 9 | 9 | 27 | 0.769 | 0.769 | 0.769 |
| PRIMARY_ADDED | 12 | 6 | 1 | 56 | 0.667 | 0.923 | 0.774 |
| PRIMARY_REMOVED | 0 | 6 | 0 | 69 | 0.000 | n/a | n/a |

## Overall false-positive rate

Trials where Holst confirmed no primary-outcome change at all, but we flagged >=1: 11 / 15 (0.733).

## Tier attribution (findings.resolved_by)

- T0: 250 (100%)

## Known limitations

- Human inter-rater kappa (TECH-PRD SS8.3) requires human coders -- out of scope for this machine-only benchmark.
- TIMELINE_REVISED and REWORDED have no Holst ground truth and are excluded from scoring entirely (see benchmark/README.md).
- omitted_measurement/omitted_aggregation/omitted_timing (Holst's 'broadened' sub-flags) have no corresponding change_type in our taxonomy and are excluded from per-type scoring; trials whose only Holst signal is one of these are also excluded from the false-positive-rate denominator (can't call our finding there a clean FP or TN when Holst did observe *something*).
- change_measurement/change_aggregation map to PRIMARY_NARROWED at low confidence (documented in benchmark/README.md) -- Holst's own severity coding treats them as categorically milder than the swap/demote/promote group.
- PRIMARY_NARROWED recall is structurally 0 at T0: `t0_matcher` (ctcm/classify.py) only ever returns SAME or None, never NARROWED, so this code cannot be predicted at all until v0.3's fuzzy-matching tiers land -- every PRIMARY_NARROWED FN below is expected, not a bug.
