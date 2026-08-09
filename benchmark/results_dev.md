# Holst benchmark -- dev split results

Coverage: 151 evaluated / 279 labeled (54%), 128 awaiting ingest.

## Per change_type (trial-level)

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

## Low-precision flags (< 30%, n >= 3 predicted-positive trials)

- **PRIMARY_REPLACED**: precision 0.111 (1 TP / 8 FP).
- **TIMEPOINT_CHANGED**: precision 0.161 (5 TP / 26 FP).
- **PRIMARY_REMOVED**: precision 0.000 (0 TP / 6 FP).
Root cause not diagnosed here -- could be a real weakness at whichever tier is resolving these pairs (see tier attribution below), a mapping edge case, or small-sample noise at this coverage level. Not safe to treat as reliable without further investigation; flagged uniformly, not singled out.

## Overall false-positive rate

Trials where Holst confirmed no primary-outcome change at all, but we flagged >=1: 22 / 34 (0.647).

## Tier attribution (findings.resolved_by)

- T0: 286 (55%)
- T1: 152 (29%)
- T3: 59 (11%)
- T2_UNRESOLVED: 19 (4%)
- T2: 1 (0%)

## Known limitations

- Human inter-rater kappa (TECH-PRD SS8.3) requires human coders -- out of scope for this machine-only benchmark.
- TIMELINE_REVISED and REWORDED have no Holst ground truth and are excluded from scoring entirely (see benchmark/README.md).
- change_measurement/change_aggregation map to PRIMARY_NARROWED at low confidence (documented in benchmark/README.md) -- Holst's own severity coding treats them as categorically milder than the swap/demote/promote group.
- change_timing -> TIMEPOINT_CHANGED is suppressed when another axis-1-producing flag fires in the same phase, approximating DATA-NOTES' per-primary co-occurrence caveat at phase granularity (the CSV has no per-primary flags) -- see benchmark/README.md.
- Findings below come from the full T0-T3 semantic cascade (`ctcm/pipeline.py` -> `ctcm/match.py`), not just exact-string T0 matching -- see tier attribution above for the actual mix on this sample.
