import gzip
import json
from datetime import date
from pathlib import Path

from ctcm import config, db
from ctcm.extract import _parse_date, extract_snapshot, load_corpus

FIXTURES = Path(__file__).parent / "fixtures"


def test_extract_actt1_v0_ordinal_primary_and_estimated_start():
    raw = json.loads((FIXTURES / "actt1_v0.json").read_text())
    snap = extract_snapshot(raw)

    primaries = [o for o in snap.outcomes if o.outcome_type == "PRIMARY"]
    assert len(primaries) == 1
    assert primaries[0].measure == (
        "Percentage of subjects reporting each severity rating on the 7-point ordinal scale"
    )
    assert primaries[0].time_frame == "Day 15"

    assert snap.timeline.start_date == date(2020, 3, 12)
    assert snap.timeline.start_date_type == "ESTIMATED"


def test_extract_handles_bare_protocol_section_shape():
    raw = json.loads((FIXTURES / "actt1_v0.json").read_text())
    bare = raw["study"]  # {protocolSection: ..., hasResults: ...} without the outer "study" key
    snap = extract_snapshot(bare)
    assert snap.timeline.start_date == date(2020, 3, 12)


def test_extract_missing_modules_never_raises():
    assert extract_snapshot({}) is not None
    assert extract_snapshot({"study": {}}) is not None
    assert extract_snapshot({"protocolSection": {}}) is not None
    snap = extract_snapshot({"protocolSection": {"outcomesModule": {}}})
    assert snap.outcomes == []
    assert snap.timeline.start_date is None


def test_parse_date_lenient_month_and_year_only():
    assert _parse_date("2020-03-12") == date(2020, 3, 12)
    assert _parse_date("2020-03") == date(2020, 3, 1)  # missing day -> day 1
    assert _parse_date("2020") == date(2020, 1, 1)
    assert _parse_date(None) is None
    assert _parse_date("") is None
    assert _parse_date("not-a-date") is None


def test_load_corpus_twice_keeps_measure_norm_populated(tmp_path, monkeypatch):
    # Regression: a bare re-run of load_corpus() must not null out measure_norm --
    # it's the expected workflow once the background ingest adds more trials.
    monkeypatch.setattr(config, "DATA_DIR", tmp_path)
    monkeypatch.setattr(config, "CACHE_DIR", tmp_path / "cache")
    monkeypatch.setattr(config, "DB_PATH", tmp_path / "test.db")

    raw = json.loads((FIXTURES / "actt1_v0.json").read_text())
    trial_dir = config.CACHE_DIR / "NCT04280705"
    trial_dir.mkdir(parents=True)
    history = {"changes": [{"version": 0, "date": "2020-02-20", "moduleLabels": []}]}
    (trial_dir / "history.json").write_text(json.dumps(history))
    (trial_dir / "v0.json.gz").write_bytes(gzip.compress(json.dumps(raw).encode()))

    load_corpus()
    load_corpus()

    conn = db.connect()
    rows = conn.execute("SELECT measure_norm FROM outcomes WHERE nct_id='NCT04280705'").fetchall()
    assert rows, "expected outcome rows from the fixture"
    assert all(r["measure_norm"] for r in rows)


def test_load_corpus_scoped_by_nct_ids_touches_only_the_named_trial(tmp_path, monkeypatch):
    # task14-review.md Important #1: ctcm.monitor's scoped rerun relies on nct_ids narrowing
    # the walk to exactly the named trial directories, not the whole cache dir.
    monkeypatch.setattr(config, "DATA_DIR", tmp_path)
    monkeypatch.setattr(config, "CACHE_DIR", tmp_path / "cache")
    monkeypatch.setattr(config, "DB_PATH", tmp_path / "test.db")

    raw = json.loads((FIXTURES / "actt1_v0.json").read_text())
    for nct in ("NCT00000001", "NCT00000002"):
        trial_dir = config.CACHE_DIR / nct
        trial_dir.mkdir(parents=True)
        history = {"changes": [{"version": 0, "date": "2020-02-20", "moduleLabels": []}]}
        (trial_dir / "history.json").write_text(json.dumps(history))
        (trial_dir / "v0.json.gz").write_bytes(gzip.compress(json.dumps(raw).encode()))

    load_corpus(nct_ids=["NCT00000001"])

    conn = db.connect()
    assert {r["nct_id"] for r in conn.execute("SELECT nct_id FROM trials")} == {"NCT00000001"}
    assert {r["nct_id"] for r in conn.execute("SELECT DISTINCT nct_id FROM outcomes")} == {"NCT00000001"}
    conn.close()
