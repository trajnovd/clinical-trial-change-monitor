"""T0 diff + change taxonomy + findings (TECH-PRD §5-6). Deterministic, no model calls.

diff_pair() pairs up outcome rows between two consecutive versions using a
pluggable matcher and reduces the leftovers into RawChanges. classify() turns
those RawChanges into severity-scored Findings using timeline anchors.

matcher shape: matcher(before: OutcomeRow, after: OutcomeRow) -> str | None.
"SAME" means the pair is the same underlying measure (identity across the
version pair); None means no match. v0.3's semantic cascade returns richer
relations (SAME/REWORDED/NARROWED/BROADENED/TIMEPOINT_CHANGED/DIFFERENT) from
the same call signature -- diff_pair doesn't need to change, only its callers
that read the return type (# ponytail: today only "SAME" is consumed, wiring
the extra relation values through _classify_matched_pair is v0.3's job).
"""

from collections import defaultdict
from dataclasses import dataclass
from datetime import date

from ctcm.timeline import Anchors, position

PRIMARY = "PRIMARY"


@dataclass(frozen=True)
class OutcomeRow:
    nct_id: str
    version_no: int
    outcome_type: str
    ordinal: int
    measure: str
    description: str | None
    time_frame: str | None
    measure_norm: str


@dataclass(frozen=True)
class RawChange:
    kind: str  # ADDED / REMOVED / REPLACED / DEMOTED / PROMOTED / TIMEPOINT / REWORDED
    before: OutcomeRow | None
    after: OutcomeRow | None


@dataclass(frozen=True)
class Finding:
    nct_id: str
    from_version: int
    to_version: int
    change_type: str
    severity: str
    before_measure: str | None
    after_measure: str | None
    days_after_enrolment: int | None
    days_after_primary_completion: int | None
    confidence: float
    resolved_by: str
    rationale: str


def t0_matcher(before: OutcomeRow, after: OutcomeRow) -> str | None:
    """T0: exact measure_norm equality, ~90% of pairs (TECH-PRD §5.3). Anything not
    byte-identical after normalisation falls through to the ADDED/REMOVED/REPLACED
    collapse in diff_pair -- there's no tier here to call it REWORDED/NARROWED/etc,
    that's what v0.3's embedding + cross-encoder tiers are for."""
    if before.measure_norm and before.measure_norm == after.measure_norm:
        return "SAME"
    return None


def _classify_matched_pair(before: OutcomeRow, after: OutcomeRow) -> RawChange | None:
    if before.outcome_type != after.outcome_type:
        if before.outcome_type == PRIMARY:
            return RawChange("DEMOTED", before, after)
        if after.outcome_type == PRIMARY:
            return RawChange("PROMOTED", before, after)
        return None  # e.g. SECONDARY -> OTHER: outside the taxonomy, not interesting
    if before.measure != after.measure:
        return RawChange("REWORDED", before, after)
    if (before.time_frame or "") != (after.time_frame or ""):
        return RawChange("TIMEPOINT", before, after)
    return None  # fully identical, nothing changed


def _collapse_unmatched(unmatched_before: list[OutcomeRow], unmatched_after: list[OutcomeRow]) -> list[RawChange]:
    """Same outcome_type + same ordinal, on both sides of a version pair with no exact
    match: that's one measure replaced by another, not an independent add + remove.
    # ponytail: ordinal-position pairing; a same-version reorder of unrelated outcomes
    would misfire as REPLACED -- upgrade to matcher-assisted pairing if that shows up."""
    by_type_before = defaultdict(list)
    for b in unmatched_before:
        by_type_before[b.outcome_type].append(b)
    by_type_after = defaultdict(list)
    for a in unmatched_after:
        by_type_after[a.outcome_type].append(a)

    changes = []
    for outcome_type in set(by_type_before) | set(by_type_after):
        bs = sorted(by_type_before.get(outcome_type, []), key=lambda r: r.ordinal)
        as_ = sorted(by_type_after.get(outcome_type, []), key=lambda r: r.ordinal)
        n = min(len(bs), len(as_))
        changes.extend(RawChange("REPLACED", bs[i], as_[i]) for i in range(n))
        changes.extend(RawChange("REMOVED", b, None) for b in bs[n:])
        changes.extend(RawChange("ADDED", None, a) for a in as_[n:])
    return changes


def diff_pair(before: list[OutcomeRow], after: list[OutcomeRow], matcher) -> list[RawChange]:
    """Consumes match.match_outcomes (Task 7); v0.2 passes t0_matcher."""
    used_after: set[int] = set()
    changes: list[RawChange] = []
    unmatched_before: list[OutcomeRow] = []

    for b in before:
        matched_i = next((i for i, a in enumerate(after) if i not in used_after and matcher(b, a) == "SAME"), None)
        if matched_i is None:
            unmatched_before.append(b)
            continue
        used_after.add(matched_i)
        change = _classify_matched_pair(b, after[matched_i])
        if change is not None:
            changes.append(change)

    unmatched_after = [a for i, a in enumerate(after) if i not in used_after]
    changes.extend(_collapse_unmatched(unmatched_before, unmatched_after))
    return changes


_CODE_BY_KIND = {
    "REPLACED": "PRIMARY_REPLACED",
    "DEMOTED": "PRIMARY_DEMOTED",
    "PROMOTED": "SECONDARY_PROMOTED",
    "TIMEPOINT": "TIMEPOINT_CHANGED",
    "ADDED": "PRIMARY_ADDED",
    "REMOVED": "PRIMARY_REMOVED",
}


def _rationale(change_type: str, vfrom: int, vto: int, before_measure: str | None, after_measure: str | None, days: int | None) -> str:
    when = f"{days} days after enrolment" if days is not None else "at an unknown point relative to enrolment"
    if before_measure and after_measure:
        return f"{change_type}: '{before_measure}' -> '{after_measure}' ({when}, v{vfrom}->v{vto})."
    if after_measure:
        return f"{change_type}: '{after_measure}' added ({when}, v{vfrom}->v{vto})."
    return f"{change_type}: '{before_measure}' removed ({when}, v{vfrom}->v{vto})."


def classify(nct: str, vfrom: int, vto: int, vdate: date, changes: list[RawChange], anchors_obj: Anchors) -> list[Finding]:
    """Maps RawChanges to severity-scored Findings. Only changes touching a PRIMARY
    outcome (on either side) are in scope -- everything else is outside the taxonomy
    and, per global-constraints.md, not emitted (NOISE suppressed by construction)."""
    findings = []
    for c in changes:
        touches_primary = (c.before is not None and c.before.outcome_type == PRIMARY) or (
            c.after is not None and c.after.outcome_type == PRIMARY
        )
        if not touches_primary:
            continue

        before_measure = c.before.measure if c.before else None
        after_measure = c.after.measure if c.after else None

        if c.kind == "REWORDED":
            # CONTEXT unconditionally: no semantic change, timing doesn't matter (TECH-PRD §6.1).
            findings.append(
                Finding(
                    nct, vfrom, vto, "REWORDED", "CONTEXT", before_measure, after_measure, None, None, 1.0, "T0",
                    _rationale("REWORDED", vfrom, vto, before_measure, after_measure, None),
                )
            )
            continue

        days_enrol, days_pcd = position(vdate, anchors_obj)
        change_type = _CODE_BY_KIND[c.kind]

        if days_pcd is not None and days_pcd >= 0:
            change_type = "POST_COMPLETION_CHANGE"  # highest-severity override (TECH-PRD §6.1)
            severity = "SIGNAL"
        elif days_enrol is not None and days_enrol < 0:
            severity = "CONTEXT"  # pre-enrolment
        else:
            # post-enrolment, or start_date unrecorded -- don't let missing timeline
            # data suppress a real change (that's the undercounting failure mode
            # global-constraints.md warns about).
            severity = "SIGNAL"

        findings.append(
            Finding(
                nct, vfrom, vto, change_type, severity, before_measure, after_measure, days_enrol, days_pcd, 1.0, "T0",
                _rationale(change_type, vfrom, vto, before_measure, after_measure, days_enrol),
            )
        )
    return findings


def timeline_revised(nct: str, anchors_obj: Anchors) -> list[Finding]:
    """One Finding per anchors.revisions entry -- retrospective date edits are
    interesting independent of any outcome change (TECH-PRD §6.2). Call once per
    trial (not per version pair like classify()), since revisions are trial-scoped."""
    return [
        Finding(
            nct, rev.from_version, rev.to_version, "TIMELINE_REVISED", "CONTEXT", None, None, None, None, 1.0, "T0",
            f"{rev.field} date moved from {rev.old} to {rev.new} (v{rev.from_version}->v{rev.to_version}).",
        )
        for rev in anchors_obj.revisions
    ]
