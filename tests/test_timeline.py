from datetime import date

from ctcm import config, db
from ctcm.timeline import anchors, position


def _seed_timeline_facts(conn, nct, rows):
    """rows: list of (version_no, start_date, start_date_type, pcd, pcd_type)."""
    for version_no, start, start_type, pcd, pcd_type in rows:
        conn.execute(
            "INSERT INTO timeline_facts(nct_id, version_no, start_date, start_date_type, "
            "primary_completion_date, primary_completion_type, completion_date) VALUES (?, ?, ?, ?, ?, ?, NULL)",
            (nct, version_no, start, start_type, pcd, pcd_type),
        )
    conn.commit()


def _isolated_conn(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "DATA_DIR", tmp_path)
    monkeypatch.setattr(config, "CACHE_DIR", tmp_path / "cache")
    monkeypatch.setattr(config, "DB_PATH", tmp_path / "test.db")
    return db.connect()


def test_anchors_earliest_recorded_start_prefers_actual(tmp_path, monkeypatch):
    conn = _isolated_conn(tmp_path, monkeypatch)
    _seed_timeline_facts(
        conn,
        "NCT04280705",
        [
            (0, "2020-03-12", "ESTIMATED", None, None),
            (9, "2020-02-21", "ACTUAL", None, None),
        ],
    )
    a = anchors("NCT04280705", conn)
    assert a.start == date(2020, 2, 21)
    assert a.start_type == "ACTUAL"


def test_anchors_forward_shift_earliest_wins_and_records_one_revision(tmp_path, monkeypatch):
    conn = _isolated_conn(tmp_path, monkeypatch)
    _seed_timeline_facts(
        conn,
        "NCT99999999",
        [
            (0, "2020-01-01", "ESTIMATED", None, None),
            (3, "2020-06-01", "ESTIMATED", None, None),
        ],
    )
    a = anchors("NCT99999999", conn)
    assert a.start == date(2020, 1, 1)
    assert len(a.revisions) == 1
    rev = a.revisions[0]
    assert rev.old == date(2020, 1, 1)
    assert rev.new == date(2020, 6, 1)
    assert rev.from_version == 0
    assert rev.to_version == 3
    assert rev.field == "start"


def test_anchors_no_timeline_facts_returns_none_and_no_revisions(tmp_path, monkeypatch):
    conn = _isolated_conn(tmp_path, monkeypatch)
    a = anchors("NCT00000000", conn)
    assert a.start is None
    assert a.start_type is None
    assert a.pcd is None
    assert a.pcd_type is None
    assert a.revisions == []


def test_anchors_pcd_tracked_independently_of_start(tmp_path, monkeypatch):
    conn = _isolated_conn(tmp_path, monkeypatch)
    _seed_timeline_facts(
        conn,
        "NCT22222222",
        [
            (0, "2020-01-01", "ACTUAL", "2021-01-01", "ESTIMATED"),
            (2, "2020-01-01", "ACTUAL", "2020-09-01", "ACTUAL"),
        ],
    )
    a = anchors("NCT22222222", conn)
    assert a.pcd == date(2020, 9, 1)
    assert a.pcd_type == "ACTUAL"
    assert len(a.revisions) == 1
    assert a.revisions[0].field == "pcd"


def test_position_days_after_enrolment_and_completion(tmp_path, monkeypatch):
    conn = _isolated_conn(tmp_path, monkeypatch)
    _seed_timeline_facts(
        conn,
        "NCT04280705",
        [
            (0, "2020-03-12", "ESTIMATED", "2020-05-01", "ESTIMATED"),
            (9, "2020-02-21", "ACTUAL", "2020-05-01", "ESTIMATED"),
        ],
    )
    a = anchors("NCT04280705", conn)
    days_enrol, days_pcd = position(date(2020, 4, 16), a)
    assert days_enrol == 55  # matches the known ACTT-1 validation case
    assert days_pcd == -15


def test_position_with_no_anchors_returns_none_none():
    from ctcm.timeline import Anchors

    empty = Anchors(start=None, start_type=None, pcd=None, pcd_type=None, revisions=[])
    assert position(date(2020, 1, 1), empty) == (None, None)


def test_no_revision_when_date_never_changes(tmp_path, monkeypatch):
    conn = _isolated_conn(tmp_path, monkeypatch)
    _seed_timeline_facts(
        conn,
        "NCT11111111",
        [
            (0, "2020-01-01", "ESTIMATED", None, None),
            (5, "2020-01-01", "ACTUAL", None, None),
        ],
    )
    a = anchors("NCT11111111", conn)
    assert a.revisions == []
    assert a.start == date(2020, 1, 1)
    assert a.start_type == "ACTUAL"  # ties prefer ACTUAL even across versions
