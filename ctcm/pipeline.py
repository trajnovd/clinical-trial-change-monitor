"""v0.3 pipeline: for each trial, resolve timeline anchors, diff every consecutive
version pair through the T0-T3 semantic cascade (ctcm/match.py), classify the changes,
and write findings. Delete+reinsert per trial -- idempotent, so re-running after
load_corpus() picks up more of the corpus is free, and llm_cache makes T3 re-runs free
too (global-constraints.md).
"""

from collections import Counter
from datetime import date

from ctcm import db
from ctcm.classify import _UNRESOLVED_TIERS, OutcomeRow, classify, diff_pair, timeline_revised
from ctcm.match import MatchResult, T3Client, match_outcomes
from ctcm.timeline import anchors as compute_anchors


def _outcome_rows(conn, nct: str, version_no: int) -> list[OutcomeRow]:
    rows = conn.execute(
        "SELECT nct_id, version_no, outcome_type, ordinal, measure, description, time_frame, measure_norm "
        "FROM outcomes WHERE nct_id=? AND version_no=?",
        (nct, version_no),
    ).fetchall()
    return [OutcomeRow(**dict(r)) for r in rows]


def _write_findings(conn, nct: str, findings) -> None:
    conn.execute("DELETE FROM findings WHERE nct_id=?", (nct,))
    conn.executemany(
        "INSERT INTO findings(nct_id, from_version, to_version, change_type, severity, before_measure, "
        "after_measure, days_after_enrolment, days_after_primary_completion, confidence, resolved_by, rationale) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        [
            (
                f.nct_id, f.from_version, f.to_version, f.change_type, f.severity, f.before_measure,
                f.after_measure, f.days_after_enrolment, f.days_after_primary_completion, f.confidence,
                f.resolved_by, f.rationale,
            )
            for f in findings
        ],
    )


def _cascade_matcher(before: list[OutcomeRow], after: list[OutcomeRow], result: MatchResult):
    """Adapts match_outcomes' whole-list MatchResult into the pairwise
    matcher(before_row, after_row) -> str | None diff_pair requires (see classify.py's
    module docstring for that contract) -- diff_pair still drives the search/collapse,
    but every match it finds was decided by match_outcomes' greedy-best-first
    assignment, not by diff_pair's own first-available scan.

    Also returns a tier-by-index lookup (T0/T1/T2/T3/T2_UNRESOLVED/T3_FALLBACK) so the
    caller can stamp findings.resolved_by with the tier that actually decided each
    RawChange, matched or collapsed."""
    before_idx = {id(row): i for i, row in enumerate(before)}
    after_idx = {id(row): i for i, row in enumerate(after)}
    by_indices = {(p.b_idx, p.a_idx): p for p in result.pairs}

    def matcher(b: OutcomeRow, a: OutcomeRow) -> str | None:
        pair = by_indices.get((before_idx[id(b)], after_idx[id(a)]))
        return pair.relation if pair else None

    def tier_for(before_row: OutcomeRow | None, after_row: OutcomeRow | None) -> str:
        bi = before_idx[id(before_row)] if before_row is not None else None
        ai = after_idx[id(after_row)] if after_row is not None else None
        pair = by_indices.get((bi, ai))
        if pair:
            return pair.tier
        # No exact (bi, ai) decision: this is a collapse-derived finding (diff_pair's
        # own per-outcome-type REPLACED collapse of leftovers, decided by "exactly one
        # left on each side," not by the cascade ever scoring *this* pairing). Consult
        # each side's own best-attempted comparison -- match_outcomes' singleton
        # leftover Pairs, keyed (bi, None)/(None, ai) -- and if either was never
        # actually adjudicated (T2_UNRESOLVED/T3_FALLBACK), say so honestly instead of
        # borrowing T1's full confidence for a pairing the cascade never decided.
        side_tiers = [p.tier for p in (by_indices.get((bi, None)), by_indices.get((None, ai))) if p]
        if any(t in _UNRESOLVED_TIERS for t in side_tiers):
            return "COLLAPSE_UNMATCHED"
        return "T1"  # ambiguous N:M leftover, best-attempted tier was T1 on both sides

    return matcher, tier_for


def run_pipeline(t3_enabled: bool = True, t3_limit: int | None = 200) -> int:
    conn = db.connect()
    t3 = T3Client(conn, limit=t3_limit) if t3_enabled else None
    nct_ids = [r[0] for r in conn.execute("SELECT nct_id FROM trials")]
    total = 0
    tier_totals: Counter[str] = Counter()

    for nct in nct_ids:
        versions = conn.execute(
            "SELECT version_no, version_date FROM versions WHERE nct_id=? ORDER BY version_no", (nct,)
        ).fetchall()

        anc = compute_anchors(nct, conn)
        findings = list(timeline_revised(nct, anc))

        for vfrom, vto in zip(versions, versions[1:]):
            if not vto["version_date"]:
                continue  # can't compute a position without a date for the "to" version
            vdate = date.fromisoformat(vto["version_date"])
            before = _outcome_rows(conn, nct, vfrom["version_no"])
            after = _outcome_rows(conn, nct, vto["version_no"])

            result = match_outcomes(before, after, t3=t3)
            tier_totals.update(result.tier_counts)
            matcher, tier_for = _cascade_matcher(before, after, result)

            changes = diff_pair(before, after, matcher)
            tiers = {id(c): tier_for(c.before, c.after) for c in changes}
            findings.extend(classify(nct, vfrom["version_no"], vto["version_no"], vdate, changes, anc, tiers))

        _write_findings(conn, nct, findings)
        total += len(findings)
        conn.commit()

    conn.close()

    decided = sum(tier_totals.values())
    print(f"tier mix ({decided} decisions):")
    for tier, n in sorted(tier_totals.items(), key=lambda kv: -kv[1]):
        pct = 100 * n / decided if decided else 0.0
        print(f"  {tier}: {n} ({pct:.1f}%)")

    return total
