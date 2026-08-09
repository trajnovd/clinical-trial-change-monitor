"""Runnable check for ctcm/monitor.py's non-network logic: version-count diff,
new-snapshot fetch into the cache layout, content-hash new-finding detection,
first_seen_at reconciliation, and monitor_log.jsonl line shape. Fake adapter
throughout -- no real HTTP calls (mirrors tests/test_ingest.py's MockTransport
approach, but the adapter protocol here doesn't need httpx at all)."""

import asyncio
import gzip
import json

from ctcm import config, db, monitor
from ctcm.extract import load_corpus
from ctcm.pipeline import run_pipeline


def _patch_config(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "DATA_DIR", tmp_path)
    monkeypatch.setattr(config, "CACHE_DIR", tmp_path / "cache")
    monkeypatch.setattr(config, "DB_PATH", tmp_path / "test.db")


def _raw(measure, start="2020-01-05", pcd="2020-06-01"):
    return {
        "study": {
            "protocolSection": {
                "outcomesModule": {"primaryOutcomes": [{"measure": measure, "timeFrame": "Day 1"}]},
                "statusModule": {
                    "startDateStruct": {"date": start, "type": "ACTUAL"},
                    "primaryCompletionDateStruct": {"date": pcd, "type": "ACTUAL"},
                    "overallStatus": "COMPLETED",
                },
                "designModule": {"studyType": "INTERVENTIONAL", "phases": ["PHASE3"], "enrollmentInfo": {"count": 100}},
                "sponsorCollaboratorsModule": {"leadSponsor": {"name": "Acme", "class": "INDUSTRY"}},
                "conditionsModule": {"conditions": ["Flu"]},
            }
        }
    }


def _seed_trial(nct: str, versions: list[tuple[int, str, list[str], dict]]) -> None:
    """versions: (version_no, date, labels, raw_dict) tuples. Writes the same
    cache layout ctcm.ingest.fetch_trial would have left on disk."""
    trial_dir = config.CACHE_DIR / nct
    trial_dir.mkdir(parents=True, exist_ok=True)
    changes = []
    for version_no, date, labels, raw in versions:
        changes.append({"version": version_no, "date": date, "moduleLabels": labels})
        (trial_dir / f"v{version_no}.json.gz").write_bytes(gzip.compress(json.dumps(raw).encode()))
    (trial_dir / "history.json").write_text(json.dumps({"changes": changes}))


class FakeAdapter:
    """Implements monitor.RegistryAdapter with in-memory data -- no network."""

    def __init__(self, versions: dict, snapshots: dict):
        self.versions = versions  # nct_id -> [{"version","date","labels"}, ...]
        self.snapshots = snapshots  # (nct_id, version) -> raw dict
        self.fetch_calls: list[tuple[str, int]] = []

    async def list_versions(self, nct_id):
        return self.versions[nct_id]

    async def fetch_version(self, nct_id, version):
        self.fetch_calls.append((nct_id, version))
        return self.snapshots[(nct_id, version)]


def _baseline(env):
    # NCT1: one cached version, nothing to diff yet -- the trial this pass will find a new version for.
    _seed_trial("NCT1", [(0, "2020-01-05", [], _raw("Overall survival"))])
    # NCT2: two cached versions already, one PRIMARY_REPLACED finding from the very first pipeline
    # run -- the pre-existing finding that must keep first_seen_at=NULL ("pre-monitoring") even
    # though run_pipeline() (called globally, see monitor.py's module docstring) reinserts it too.
    _seed_trial(
        "NCT2",
        [
            (0, "2020-01-05", [], _raw("Overall survival")),
            (1, "2020-03-01", ["Outcome Measures"], _raw("Progression free survival")),
        ],
    )
    load_corpus()
    run_pipeline(t3_enabled=False)


# ---- pure-function fetch rule ------------------------------------------------------------


def test_versions_to_fetch_matches_ingest_fetch_rule():
    versions = [
        {"version": 0, "labels": []},
        {"version": 1, "labels": ["Study Status"]},
        {"version": 2, "labels": ["Outcome Measures"]},
        {"version": 3, "labels": ["Contacts/Locations"]},
    ]
    assert monitor._versions_to_fetch(versions) == {0, 2, 3}  # v1: no outcome touch, not final -- skipped
    assert monitor._versions_to_fetch([]) == set()


def test_ensure_schema_adds_first_seen_at_column_and_is_idempotent(tmp_path, monkeypatch):
    _patch_config(tmp_path, monkeypatch)
    conn = db.connect()
    monitor.ensure_schema(conn)
    monitor.ensure_schema(conn)  # must not raise "duplicate column"
    cols = {r["name"] for r in conn.execute("PRAGMA table_info(findings)")}
    assert "first_seen_at" in cols
    conn.close()


# ---- check_updates: no-op when nothing changed -----------------------------------------


def test_check_updates_no_op_when_history_length_matches_known_count(tmp_path, monkeypatch):
    _patch_config(tmp_path, monkeypatch)
    _baseline(tmp_path)

    adapter = FakeAdapter(
        versions={
            "NCT1": [{"version": 0, "date": "2020-01-05", "labels": []}],
            "NCT2": [
                {"version": 0, "date": "2020-01-05", "labels": []},
                {"version": 1, "date": "2020-03-01", "labels": ["Outcome Measures"]},
            ],
        },
        snapshots={},
    )

    result = asyncio.run(monitor.check_updates(["NCT1", "NCT2"], adapter))

    assert result == monitor.MonitorResult(checked=2, changed=[], new_findings=0)
    assert adapter.fetch_calls == []  # no history length grew -- never fetch a snapshot
    assert not (config.CACHE_DIR / "NCT1" / "v1.json.gz").exists()
    assert not (tmp_path / "monitor_log.jsonl").exists()


# ---- check_updates: a trial with a genuinely new version --------------------------------


def test_check_updates_detects_new_version_and_fetches_only_the_missing_snapshot(tmp_path, monkeypatch):
    _patch_config(tmp_path, monkeypatch)
    _baseline(tmp_path)

    adapter = FakeAdapter(
        versions={
            "NCT1": [
                {"version": 0, "date": "2020-01-05", "labels": []},
                {"version": 1, "date": "2020-03-01", "labels": ["Outcome Measures"]},
            ],
            "NCT2": [
                {"version": 0, "date": "2020-01-05", "labels": []},
                {"version": 1, "date": "2020-03-01", "labels": ["Outcome Measures"]},
            ],
        },
        snapshots={("NCT1", 1): _raw("Time to recovery")},
    )

    result = asyncio.run(monitor.check_updates(["NCT1", "NCT2"], adapter))

    assert result.checked == 2
    assert result.changed == ["NCT1"]  # NCT2's history length matches what's already known -- untouched
    assert adapter.fetch_calls == [("NCT1", 1)]  # v0 already cached, only the new v1 gets fetched
    assert (config.CACHE_DIR / "NCT1" / "v1.json.gz").exists()

    # history.json refreshed with the fresh version count, so a later plain ingest/load_corpus
    # sees it too instead of a stale cached count.
    history = json.loads((config.CACHE_DIR / "NCT1" / "history.json").read_text())
    assert [c["version"] for c in history["changes"]] == [0, 1]


def test_check_updates_new_finding_gets_first_seen_at_preexisting_stays_null(tmp_path, monkeypatch):
    _patch_config(tmp_path, monkeypatch)
    _baseline(tmp_path)

    adapter = FakeAdapter(
        versions={
            "NCT1": [
                {"version": 0, "date": "2020-01-05", "labels": []},
                {"version": 1, "date": "2020-03-01", "labels": ["Outcome Measures"]},
            ],
            "NCT2": [
                {"version": 0, "date": "2020-01-05", "labels": []},
                {"version": 1, "date": "2020-03-01", "labels": ["Outcome Measures"]},
            ],
        },
        snapshots={("NCT1", 1): _raw("Time to recovery")},
    )

    result = asyncio.run(monitor.check_updates(["NCT1", "NCT2"], adapter))
    assert result.new_findings == 1

    conn = db.connect()
    new_finding = conn.execute(
        "SELECT first_seen_at FROM findings WHERE nct_id='NCT1' AND change_type='PRIMARY_REPLACED'"
    ).fetchone()
    assert new_finding is not None
    assert new_finding["first_seen_at"] is not None  # stamped: genuinely new this pass

    # NCT2's finding predates monitoring and NCT2 itself had no new version this pass --
    # run_pipeline() still reinserted it (global delete+reinsert), so this is the check that
    # reconciliation actually restores the old value instead of leaving every re-inserted row
    # freshly NULL-then-relabelled as "new".
    preexisting = conn.execute(
        "SELECT first_seen_at FROM findings WHERE nct_id='NCT2' AND change_type='PRIMARY_REPLACED'"
    ).fetchone()
    assert preexisting is not None
    assert preexisting["first_seen_at"] is None
    conn.close()


def test_check_updates_logs_one_new_finding_line_with_expected_shape(tmp_path, monkeypatch):
    _patch_config(tmp_path, monkeypatch)
    _baseline(tmp_path)

    adapter = FakeAdapter(
        versions={
            "NCT1": [
                {"version": 0, "date": "2020-01-05", "labels": []},
                {"version": 1, "date": "2020-03-01", "labels": ["Outcome Measures"]},
            ],
            "NCT2": [
                {"version": 0, "date": "2020-01-05", "labels": []},
                {"version": 1, "date": "2020-03-01", "labels": ["Outcome Measures"]},
            ],
        },
        snapshots={("NCT1", 1): _raw("Time to recovery")},
    )

    asyncio.run(monitor.check_updates(["NCT1", "NCT2"], adapter))

    lines = (tmp_path / "monitor_log.jsonl").read_text().strip().splitlines()
    assert len(lines) == 1  # NCT2 produced no new finding -- only NCT1's logged
    entry = json.loads(lines[0])
    assert set(entry) == {"ts", "nct_id", "change_type", "severity", "from_version", "to_version"}
    assert entry["nct_id"] == "NCT1"
    assert entry["change_type"] == "PRIMARY_REPLACED"
    assert entry["from_version"] == 0
    assert entry["to_version"] == 1
    assert entry["severity"] in ("SIGNAL", "CONTEXT")
