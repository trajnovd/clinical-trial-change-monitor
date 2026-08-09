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
#
# PRIMARY_BROADENED (ctcm/classify.py _CODE_BY_KIND["BROADENED"]) is a v0.3
# addition -- the T2 qualifier rule's mirror of NARROWED (e.g. "cardiovascular
# mortality" -> "all-cause mortality"). It didn't exist when this benchmark's
# mapping was first written, which is why omitted_measurement/aggregation/timing
# used to be treated as unmappable; now they map directly, same as added_* ->
# PRIMARY_NARROWED (see _axis1_codes below).
CHANGE_TYPES = (
    "PRIMARY_REPLACED",
    "PRIMARY_DEMOTED",
    "SECONDARY_PROMOTED",
    "PRIMARY_NARROWED",
    "PRIMARY_BROADENED",
    "TIMEPOINT_CHANGED",
    "POST_COMPLETION_CHANGE",
    "PRIMARY_ADDED",
    "PRIMARY_REMOVED",
)

PHASE_PREFIXES = ("change_a_i_", "change_i_p_", "change_p_l_")
# ctcm/classify.py's severity override (classify(), ~lines 225-227) unconditionally
# rewrites change_type to POST_COMPLETION_CHANGE for any primary change dated
# on/after primary_completion_date, regardless of what kind of change it is -- so
# for Holst's post-completion and post-publication phases, the axis-1 identity
# (added/removed/replaced/...) is never separately joinable against our
# findings; only the axis-2 tag is. Recruitment-stage (change_a_i_) changes are
# pre-completion by construction, never overridden, so axis-1 identity survives.
POST_COMPLETION_PREFIXES = ("change_i_p_", "change_p_l_")

# DATA-NOTES SS6's change_timing -> TIMEPOINT_CHANGED row is qualified "(and no
# add/omit/new/omitted on the same primary)" -- the CSV only carries phase-level
# flags, not per-primary ones, so "same primary" can't be checked directly. This
# is the phase-level approximation: any of these firing in the SAME phase as
# change_timing suppresses the plain timing read (see benchmark/README.md for
# why this is a defensible proxy, not the literal caveat).
_TIMING_COOCCURRENCE_SUFFIXES = (
    "new_primary",
    "primary_omitted",
    "primary_from_secondary",
    "primary_to_secondary",
    "added_measurement",
    "added_aggregation",
    "added_timing",
    "omitted_measurement",
    "omitted_aggregation",
    "omitted_timing",
    "change_measurement",
    "change_aggregation",
)


@dataclass(frozen=True)
class GroundTruth:
    types: frozenset[str]
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
    if _flag(row, prefix, "change_timing") and not any(
        _flag(row, prefix, s) for s in _TIMING_COOCCURRENCE_SUFFIXES
    ):
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
    # Mirror of the added_* block above: detail *dropped* from an existing
    # primary (made less specific) -> PRIMARY_BROADENED. Was unmappable before
    # ctcm's v0.3 taxonomy gained this code (see CHANGE_TYPES comment).
    if any(_flag(row, prefix, s) for s in ("omitted_measurement", "omitted_aggregation", "omitted_timing")):
        codes.add("PRIMARY_BROADENED")
    return frozenset(codes)


def _phase_applicable(row: dict, prefix: str) -> bool:
    return row.get(prefix + "no_phase") != "1"


def _phase_no_change(row: dict, prefix: str) -> bool:
    return row.get(prefix + "no_change") == "1"


def trial_ground_truth(row: dict) -> GroundTruth:
    """Map one labeled-subset CSV row to a GroundTruth. Pure function of the row dict."""
    types: set[str] = set()
    any_applicable = False
    all_confirmed_no_change = True

    for prefix in PHASE_PREFIXES:
        if not _phase_applicable(row, prefix):
            continue
        any_applicable = True

        axis1 = _axis1_codes(row, prefix)

        if prefix in POST_COMPLETION_PREFIXES:
            if axis1:
                types.add("POST_COMPLETION_CHANGE")
        else:
            types |= axis1

        if axis1:
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
    no change at all (GroundTruth.no_change_confirmed) -- every applicable phase
    was explicitly rated no_change, with nothing mapped flagged in any of them."""
    evaluated = sorted(set(predicted) & set(truth))
    denom = [nct for nct in evaluated if truth[nct].no_change_confirmed]
    fp = sum(1 for nct in denom if predicted[nct])
    return fp, len(denom)
