"""Pure mapping + scoring logic for the Holst et al. 2023 benchmark (see
benchmark/DATA-NOTES.md SS6 for the full mapping rationale, benchmark/README.md
for the authoritative decision table). No I/O, no network -- everything here
is a pure function of a CSV row dict (`trial_ground_truth`) or of small
prediction/label dicts (`score_by_type`, `false_positive_rate`), so it's
testable against fixtures without touching the real 13.7MB CSV or the shared db.

Row values in the source CSV are '1' (flagged true), '0' (explicitly
unflagged) or 'NULL' (left blank by rater) -- DATA-NOTES SS3 documents that
Holst's own R code (`fun/recode_outcome_changes.R`) treats '0'/'NULL'/missing
identically as "not this category", so `_flag` below only ever tests `== '1'`.
"""

from dataclasses import dataclass

# Our ctcm/classify.py change_type taxonomy, minus the two codes with no Holst
# ground truth: REWORDED (severity=CONTEXT, "no semantic change" -- Holst's own
# no_change bucket explicitly excludes wording-only edits too, so it has no
# separate mapped category to score against) and TIMELINE_REVISED (Holst never
# coded registry date-field edits, only outcome-text changes -- DATA-NOTES SS7.2).
CHANGE_TYPES = (
    "PRIMARY_REPLACED",
    "PRIMARY_DEMOTED",
    "SECONDARY_PROMOTED",
    "PRIMARY_NARROWED",
    "TIMEPOINT_CHANGED",
    "POST_COMPLETION_CHANGE",
    "PRIMARY_ADDED",
    "PRIMARY_REMOVED",
)

PHASE_PREFIXES = ("change_a_i_", "change_i_p_", "change_p_l_")
# ctcm/classify.py's severity override (lines ~209-211) unconditionally rewrites
# change_type to POST_COMPLETION_CHANGE for any primary change dated on/after
# primary_completion_date, regardless of what kind of change it is -- so for
# Holst's post-completion and post-publication phases, the axis-1 identity
# (added/removed/replaced/...) is never separately joinable against our
# findings; only the axis-2 tag is. Recruitment-stage (change_a_i_) changes are
# pre-completion by construction, never overridden, so axis-1 identity survives.
POST_COMPLETION_PREFIXES = ("change_i_p_", "change_p_l_")

# The mirror image of PRIMARY_NARROWED ("detail dropped, made less specific") --
# our taxonomy has no "primary broadened" code, so these three sub-flags can't
# be mapped to any change_type. Dropped from scoring entirely per DATA-NOTES
# SS7.1 option (a); tracked only via GroundTruth.has_unmapped_change so trials
# where this is the *only* signal get excluded from the FPR denominator instead
# of being silently miscounted as a true negative.
UNMAPPABLE_SUFFIXES = ("omitted_measurement", "omitted_aggregation", "omitted_timing")


@dataclass(frozen=True)
class GroundTruth:
    types: frozenset[str]
    has_unmapped_change: bool
    no_change_confirmed: bool  # every applicable phase explicitly rated no_change


def _flag(row: dict, prefix: str, suffix: str) -> bool:
    return row.get(prefix + suffix) == "1"


def _axis1_codes(row: dict, prefix: str) -> frozenset[str]:
    """Axis-1 ("what changed") mapping for one phase prefix -- DATA-NOTES SS6 table."""
    codes: set[str] = set()
    if _flag(row, prefix, "primary_from_secondary"):
        codes.add("SECONDARY_PROMOTED")
    if _flag(row, prefix, "primary_to_secondary"):
        codes.add("PRIMARY_DEMOTED")
    if _flag(row, prefix, "change_timing"):
        codes.add("TIMEPOINT_CHANGED")

    new_primary = _flag(row, prefix, "new_primary")
    primary_omitted = _flag(row, prefix, "primary_omitted")
    if new_primary and primary_omitted:
        codes.add("PRIMARY_REPLACED")
    elif new_primary:
        codes.add("PRIMARY_ADDED")
    elif primary_omitted:
        codes.add("PRIMARY_REMOVED")

    if any(_flag(row, prefix, s) for s in ("added_measurement", "added_aggregation", "added_timing")):
        codes.add("PRIMARY_NARROWED")
    # Low-confidence mapping decision (documented in benchmark/README.md): Holst's
    # own severity coding rates change_measurement/change_aggregation as
    # "non-severe", milder than the swap/demote/promote group -- default to
    # PRIMARY_NARROWED (closer in spirit: existing primary kept, detail altered)
    # rather than PRIMARY_REPLACED (which implies the measure itself is gone).
    if any(_flag(row, prefix, s) for s in ("change_measurement", "change_aggregation")):
        codes.add("PRIMARY_NARROWED")
    return frozenset(codes)


def _phase_applicable(row: dict, prefix: str) -> bool:
    return row.get(prefix + "no_phase") != "1"


def _phase_no_change(row: dict, prefix: str) -> bool:
    return row.get(prefix + "no_change") == "1"


def _phase_has_unmapped(row: dict, prefix: str) -> bool:
    return any(_flag(row, prefix, s) for s in UNMAPPABLE_SUFFIXES)


def trial_ground_truth(row: dict) -> GroundTruth:
    """Map one labeled-subset CSV row to a GroundTruth. Pure function of the row dict."""
    types: set[str] = set()
    has_unmapped = False
    any_applicable = False
    all_confirmed_no_change = True

    for prefix in PHASE_PREFIXES:
        if not _phase_applicable(row, prefix):
            continue
        any_applicable = True

        axis1 = _axis1_codes(row, prefix)
        unmapped = _phase_has_unmapped(row, prefix)
        has_unmapped = has_unmapped or unmapped

        if prefix in POST_COMPLETION_PREFIXES:
            if axis1 or unmapped:
                types.add("POST_COMPLETION_CHANGE")
        else:
            types |= axis1

        if axis1 or unmapped:
            all_confirmed_no_change = False
        elif not _phase_no_change(row, prefix):
            # Applicable phase, nothing flagged, but no explicit no_change='1'
            # either (blank/NULL on every sub-column) -- a rating gap. Doesn't
            # occur in the 559 CT.gov subset as shipped (verified empirically
            # during acquisition), but don't let a future re-export silently
            # inflate the FPR denominator on an unconfirmed negative.
            all_confirmed_no_change = False

    return GroundTruth(
        types=frozenset(types),
        has_unmapped_change=has_unmapped,
        no_change_confirmed=any_applicable and all_confirmed_no_change,
    )


@dataclass(frozen=True)
class TypeScore:
    tp: int
    fp: int
    fn: int
    tn: int

    @property
    def precision(self) -> float | None:
        return self.tp / (self.tp + self.fp) if (self.tp + self.fp) else None

    @property
    def recall(self) -> float | None:
        return self.tp / (self.tp + self.fn) if (self.tp + self.fn) else None

    @property
    def f1(self) -> float | None:
        p, r = self.precision, self.recall
        if p is None or r is None:
            return None
        return 2 * p * r / (p + r) if (p + r) else 0.0


def score_by_type(
    predicted: dict[str, frozenset[str]], truth: dict[str, GroundTruth], change_types=CHANGE_TYPES
) -> dict[str, TypeScore]:
    """Trial-level per-change_type precision/recall/F1: does the trial have >=1
    finding of type X vs >=1 Holst label of mapped type X. Only nct_ids present
    in both `predicted` and `truth` count (the evaluated set) -- pass an
    (possibly empty-set-valued) entry for every trial you want counted as
    evaluated, even ones with zero findings."""
    evaluated = sorted(set(predicted) & set(truth))
    out = {}
    for ct in change_types:
        tp = fp = fn = tn = 0
        for nct in evaluated:
            pred_has = ct in predicted[nct]
            true_has = ct in truth[nct].types
            if pred_has and true_has:
                tp += 1
            elif pred_has and not true_has:
                fp += 1
            elif not pred_has and true_has:
                fn += 1
            else:
                tn += 1
        out[ct] = TypeScore(tp, fp, fn, tn)
    return out


def false_positive_rate(predicted: dict[str, frozenset[str]], truth: dict[str, GroundTruth]) -> tuple[int, int]:
    """(false positives, denominator) among evaluated trials where Holst confirmed
    no change at all. Trials whose only Holst signal is an unmappable omitted_*
    broadening are excluded from the denominator (DATA-NOTES SS7.1) -- we can't
    honestly call our pipeline's finding there a false positive OR a true
    negative when Holst did observe *something*, just not something our
    taxonomy names."""
    evaluated = sorted(set(predicted) & set(truth))
    denom = [nct for nct in evaluated if truth[nct].no_change_confirmed]
    fp = sum(1 for nct in denom if predicted[nct])
    return fp, len(denom)
