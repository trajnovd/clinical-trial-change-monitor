"""Timeline anchors: deterministic date arithmetic (TECH-PRD §6). No model calls.

The date-manipulation trap (global-constraints.md): start_date and
primary_completion_date are themselves editable fields that sponsors can push
forward, which would make a real post-enrolment change look pre-enrolment.
anchors() defends against that by taking the EARLIEST recorded value of each
date across every fetched version, never the latest/current one.
"""

from dataclasses import dataclass, field
from datetime import date


@dataclass(frozen=True)
class Revision:
    field: str  # "start" or "pcd"
    from_version: int
    to_version: int
    old: date
    new: date


@dataclass(frozen=True)
class Anchors:
    start: date | None
    start_type: str | None
    pcd: date | None
    pcd_type: str | None
    revisions: list[Revision] = field(default_factory=list)


def _resolve(rows: list[tuple[int, date | None, str | None]], field_name: str) -> tuple[date | None, str | None, list[Revision]]:
    """rows: (version_no, date, type) in ascending version_no order, nulls already filtered out.
    Returns (earliest date value, type of the record matching that value -- ACTUAL preferred
    on ties, revisions list of every value change between consecutive observed versions)."""
    if not rows:
        return None, None, []

    earliest = min(d for _, d, _ in rows)
    tied = [r for r in rows if r[1] == earliest]
    chosen = next((r for r in tied if r[2] == "ACTUAL"), tied[0])

    revisions = []
    prev_v, prev_d = rows[0][0], rows[0][1]
    for v, d, _ in rows[1:]:
        if d != prev_d:
            revisions.append(Revision(field=field_name, from_version=prev_v, to_version=v, old=prev_d, new=d))
        prev_v, prev_d = v, d

    return chosen[1], chosen[2], revisions


def anchors(nct_id: str, conn) -> Anchors:
    """Earliest recorded start_date and primary_completion_date across all fetched
    versions of one trial, plus every version where either date moved."""
    rows = conn.execute(
        "SELECT version_no, start_date, start_date_type, primary_completion_date, primary_completion_type "
        "FROM timeline_facts WHERE nct_id=? ORDER BY version_no",
        (nct_id,),
    ).fetchall()

    start_rows = [(r["version_no"], date.fromisoformat(r["start_date"]), r["start_date_type"]) for r in rows if r["start_date"]]
    pcd_rows = [
        (r["version_no"], date.fromisoformat(r["primary_completion_date"]), r["primary_completion_type"])
        for r in rows
        if r["primary_completion_date"]
    ]

    start, start_type, start_revisions = _resolve(start_rows, "start")
    pcd, pcd_type, pcd_revisions = _resolve(pcd_rows, "pcd")

    return Anchors(start=start, start_type=start_type, pcd=pcd, pcd_type=pcd_type, revisions=start_revisions + pcd_revisions)


def position(change_date: date, a: Anchors) -> tuple[int | None, int | None]:
    """(days after enrolment, days after primary completion) for a change dated change_date."""
    days_after_enrolment = (change_date - a.start).days if a.start else None
    days_after_pcd = (change_date - a.pcd).days if a.pcd else None
    return days_after_enrolment, days_after_pcd
