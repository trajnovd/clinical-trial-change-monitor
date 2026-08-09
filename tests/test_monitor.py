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

    def __init__(self, versions: dict, snapshots: dict, fail: set[tuple[str, int]] = frozenset()):
        self.versions = versions  # nct_id -> [{"version","date","labels"}, ...]
        self.snapshots = snapshots  # (nct_id, version) -> raw dict
        self.fail = fail  # (nct_id, version) pairs whose fetch_version raises, simulating a transient error
        self.fetch_calls: list[tuple[str, int]] = []

    async def list_versions(self, nct_id):
        return self.versions[nct_id]

    async def fetch_version(self, nct_id, version):
        self.fetch_calls.append((nct_id, version))
        if (nct_id, version) in self.fail:
            raise RuntimeError(f"simulated transient fetch failure for {nct_id} v{version}")
        return self.snapshots[(nct_id, version)]


def _baseline(env):
    # NCT1: one cached version, nothing to diff yet -- the trial this pass will find a new version for.
    _seed_trial("NCT1", [(0, "2020-01-05", [], _raw("Overall survival"))])
    # NCT2: two cached versions already, one PRIMARY_REPLACED finding from the very first pipeline
    # run -- a pre-existing, pre-monitoring finding (first_seen_at=NULL) that a monitor pass over
    # NCT1+NCT2 must leave completely untouched: NCT2's own history won't grow, and the scoped
    # rerun (see monitor.py's module docstring) only ever reprocesses trials in `changed`.
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

    # NCT2's finding predates monitoring and NCT2 itself had no new version this pass, so the
    # scoped rerun (load_corpus/run_pipeline(nct_ids=["NCT1"])) never touches it at all --
    # first_seen_at must still read NULL, completely untouched by this monitor pass.
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


# ---- regression: task14-review.md Critical #1 (mid-trial fetch failure) -----------------


def test_check_updates_retries_after_a_mid_trial_fetch_failure(tmp_path, monkeypatch):
    # A fetch_version failure partway through a trial must leave history.json untouched, so
    # the next pass sees the same known version_count and retries the missing version --
    # not silently adopt a larger count for a snapshot that was never actually cached.
    _patch_config(tmp_path, monkeypatch)
    _seed_trial("NCT1", [(0, "2020-01-05", [], _raw("Overall survival"))])
    load_corpus()
    run_pipeline(t3_enabled=False)

    fresh_versions = [
        {"version": 0, "date": "2020-01-05", "labels": []},
        {"version": 1, "date": "2020-03-01", "labels": ["Outcome Measures"]},  # fetch fails here
        {"version": 2, "date": "2020-04-01", "labels": []},  # never reached this pass
    ]
    flaky = FakeAdapter(
        versions={"NCT1": fresh_versions},
        snapshots={("NCT1", 2): _raw("Time to recovery, final")},
        fail={("NCT1", 1)},
    )

    result = asyncio.run(monitor.check_updates(["NCT1"], flaky))

    assert result.changed == []  # the failure must keep this pass a no-op for NCT1
    assert flaky.fetch_calls == [("NCT1", 1)]  # v0 already cached (skipped); v2 never attempted -- v1 raised first
    history = json.loads((config.CACHE_DIR / "NCT1" / "history.json").read_text())
    assert [c["version"] for c in history["changes"]] == [0]  # untouched -- still the pre-pass count
    assert not (config.CACHE_DIR / "NCT1" / "v1.json.gz").exists()
    assert not (config.CACHE_DIR / "NCT1" / "v2.json.gz").exists()

    conn = db.connect()
    row = conn.execute("SELECT version_count FROM trials WHERE nct_id='NCT1'").fetchone()
    assert row["version_count"] == 1  # load_corpus() never ran -- nothing was in `changed`
    conn.close()

    # Retry: same fresh version list, nothing fails this time -- the missing versions are
    # picked up exactly as if the first pass had never touched anything.
    healthy = FakeAdapter(
        versions={"NCT1": fresh_versions},
        snapshots={("NCT1", 1): _raw("Time to recovery"), ("NCT1", 2): _raw("Time to recovery, final")},
    )
    result2 = asyncio.run(monitor.check_updates(["NCT1"], healthy))

    assert result2.changed == ["NCT1"]
    assert healthy.fetch_calls == [("NCT1", 1), ("NCT1", 2)]
    assert (config.CACHE_DIR / "NCT1" / "v1.json.gz").exists()
    assert (config.CACHE_DIR / "NCT1" / "v2.json.gz").exists()
    history2 = json.loads((config.CACHE_DIR / "NCT1" / "history.json").read_text())
    assert [c["version"] for c in history2["changes"]] == [0, 1, 2]


# ---- regression: task14-review.md Important #3 (content-hash multiset, not a set) -------


def test_reconcile_trial_multiset_distinguishes_duplicate_and_genuinely_new_findings(tmp_path, monkeypatch):
    # Two findings can share an identical content hash within one trial (duplicate outcome
    # text is real registry data). A set-keyed diff would collapse them; the fix's multiset
    # (list-per-hash) diff must count them separately: 2 pre-existing + 1 new-duplicate ==
    # exactly 1 surplus row, not 0 (falsely swallowed) and not 3 (falsely all-new).
    _patch_config(tmp_path, monkeypatch)
    conn = db.connect()
    monitor.ensure_schema(conn)
    conn.execute("INSERT INTO trials(nct_id, version_count) VALUES ('NCT9', 1)")

    def _insert(n):
        for _ in range(n):
            conn.execute(
                "INSERT INTO findings(nct_id, from_version, to_version, change_type, severity, "
                "before_measure, after_measure) VALUES "
                "('NCT9', 0, 1, 'PRIMARY_REPLACED', 'SIGNAL', 'Overall survival', 'Time to recovery')"
            )
        conn.commit()

    _insert(2)  # two pre-existing, identical-content findings -- both pre-monitoring (first_seen_at NULL)
    before = monitor._findings_multiset(conn, "NCT9")
    assert sum(len(v) for v in before.values()) == 2

    conn.execute("DELETE FROM findings WHERE nct_id='NCT9'")
    _insert(3)  # simulates run_pipeline() reinserting the same 2 plus one genuinely new duplicate

    now = "2026-08-09T00:00:00+00:00"
    new_rows = monitor._reconcile_trial(conn, "NCT9", before, now)
    conn.commit()

    assert len(new_rows) == 1  # exactly the surplus row

    seen_at = [
        r["first_seen_at"]
        for r in conn.execute("SELECT first_seen_at FROM findings WHERE nct_id='NCT9' ORDER BY finding_id")
    ]
    assert seen_at.count(None) == 2  # the two pre-existing rows restored to NULL
    assert seen_at.count(now) == 1  # exactly the surplus row stamped
    conn.close()
