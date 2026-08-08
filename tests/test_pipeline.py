"""Integration check for the run_pipeline glue: version-pairing loop, anchors
wiring, and delete+reinsert idempotency. classify.py/timeline.py already cover
the pure logic in isolation -- this exercises them wired together against sqlite."""

from ctcm import config, db
from ctcm.pipeline import run_pipeline


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
