"""Integration check for the run_pipeline glue: version-pairing loop, anchors
wiring, and delete+reinsert idempotency. classify.py/timeline.py already cover
the pure logic in isolation -- this exercises them wired together against sqlite."""

from datetime import date

from ctcm import config, db
from ctcm.classify import OutcomeRow, classify, diff_pair
from ctcm.match import MatchResult, Pair
from ctcm.pipeline import _cascade_matcher, run_pipeline
from ctcm.timeline import Anchors


def _seed(conn):
    conn.execute("INSERT INTO trials(nct_id, version_count) VALUES ('NCT1', 3)")
    for v, d, start, start_type in [(0, "2020-03-12", "2020-02-21", "ACTUAL"), (9, "2020-04-16", "2020-02-21", "ACTUAL")]:
        conn.execute("INSERT INTO versions(nct_id, version_no, version_date, module_labels) VALUES ('NCT1', ?, ?, '[]')", (v, d))
        conn.execute(
            "INSERT INTO timeline_facts(nct_id, version_no, start_date, start_date_type) VALUES ('NCT1', ?, ?, ?)",
            (v, start, start_type),
        )
    conn.execute(
        "INSERT INTO outcomes(nct_id, version_no, outcome_type, ordinal, measure, time_frame, measure_norm) "
        "VALUES ('NCT1', 0, 'PRIMARY', 0, '7-point ordinal scale', 'Day 15', '7 point ordinal scale')"
    )
    conn.execute(
        "INSERT INTO outcomes(nct_id, version_no, outcome_type, ordinal, measure, time_frame, measure_norm) "
        "VALUES ('NCT1', 9, 'PRIMARY', 0, 'Time to recovery', 'Day 29', 'time to recovery')"
    )
    conn.commit()


def test_run_pipeline_writes_expected_finding_and_is_idempotent(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "DATA_DIR", tmp_path)
    monkeypatch.setattr(config, "CACHE_DIR", tmp_path / "cache")
    monkeypatch.setattr(config, "DB_PATH", tmp_path / "test.db")
    conn = db.connect()
    _seed(conn)
    conn.close()

    n_first = run_pipeline()
    assert n_first >= 1

    conn = db.connect()
    rows = conn.execute("SELECT change_type, severity FROM findings WHERE nct_id='NCT1'").fetchall()
    conn.close()
    assert any(r["change_type"] == "PRIMARY_REPLACED" and r["severity"] == "SIGNAL" for r in rows)

    n_second = run_pipeline()
    assert n_second == n_first  # delete+reinsert: re-running doesn't duplicate rows


# ---- v10-report F1: tier_for's "T1" fallback on collapse-derived findings ---------


def _outcome_row(outcome_type, ordinal, measure, nct="NCT1", version_no=0):
    return OutcomeRow(
        nct_id=nct, version_no=version_no, outcome_type=outcome_type, ordinal=ordinal,
        measure=measure, description=None, time_frame=None, measure_norm=measure.lower(),
    )


_ANCHORS_POST_ENROL = Anchors(start=date(2020, 2, 21), start_type="ACTUAL", pcd=None, pcd_type=None, revisions=[])


def test_replaced_collapse_of_unresolved_leftover_gets_collapse_unmatched_and_half_confidence():
    # diff_pair's own per-outcome-type "exactly one leftover on each side" collapse
    # can pair two items match_outcomes never scored as a candidate pair together --
    # tier_for used to stamp any such collapse "T1" regardless, mis-crediting it as a
    # confident cascade decision. If either leftover's own best-attempted comparison
    # was T2_UNRESOLVED (escalated, never adjudicated), the collapse must say so.
    before = [_outcome_row("PRIMARY", 0, "Overall survival")]
    after = [_outcome_row("PRIMARY", 0, "Progression free survival")]
    # Hand-built MatchResult: b0's best-attempted comparison escalated and was never
    # adjudicated; a0's was a plain T1 reject. match_outcomes never paired b0 with a0
    # directly (no (0, 0) Pair), only these per-side singletons -- exactly the shape
    # Pass C's ambiguous-N:M-leftover branch produces.
    result = MatchResult(
        pairs=[
            Pair(0, None, "DIFFERENT", "T2_UNRESOLVED", 0.5),
            Pair(None, 0, "DIFFERENT", "T1", 0.2),
        ],
        tier_counts={},
    )
    matcher, tier_for = _cascade_matcher(before, after, result)

    changes = diff_pair(before, after, matcher)
    assert len(changes) == 1
    assert changes[0].kind == "REPLACED"

    tiers = {id(c): tier_for(c.before, c.after) for c in changes}
    assert tiers[id(changes[0])] == "COLLAPSE_UNMATCHED"

    findings = classify("NCT1", 0, 1, date(2020, 4, 16), changes, _ANCHORS_POST_ENROL, tiers)
    assert len(findings) == 1
    assert findings[0].resolved_by == "COLLAPSE_UNMATCHED"
    assert findings[0].confidence == 0.5
