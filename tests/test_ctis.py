"""Runnable check for ctcm/ctis.py: field mapping (outcomes + meta), the outcome-relevant
content hash gate, the trials.registry ALTER guard, CTISAdapter's local-only protocol
conformance, snapshot_pass's fetch+gate+store+upsert, search_watchlist's pagination/filter,
and that CTIS-sourced rows flow through ctcm.pipeline.run_pipeline() unmodified. No real
HTTP calls anywhere (httpx.MockTransport, mirrors tests/test_ingest.py); no real sleeping
(ctis.asyncio.sleep monkeypatched to a no-op, mirrors tests/test_ingest.py)."""

import asyncio
import copy
import json
from datetime import date
from pathlib import Path

import httpx

from ctcm import config, ctis, db
from ctcm.pipeline import run_pipeline

FIXTURES = Path(__file__).parent / "fixtures"
BASE = json.loads((FIXTURES / "ctis_retrieve_sample.json").read_text())
CT = "2025-523333-26-00"


def _patch_config(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "DATA_DIR", tmp_path)
    monkeypatch.setattr(config, "CACHE_DIR", tmp_path / "cache")
    monkeypatch.setattr(config, "DB_PATH", tmp_path / "test.db")


def _fast(monkeypatch):
    real_sleep = asyncio.sleep
    monkeypatch.setattr(ctis.asyncio, "sleep", lambda _: real_sleep(0))


def _retrieve_transport(responses: dict[str, dict]) -> httpx.MockTransport:
    def handler(request: httpx.Request) -> httpx.Response:
        ct = request.url.path.rsplit("/", 1)[-1]
        return httpx.Response(200, json=responses[ct])

    return httpx.MockTransport(handler)


def _changed_primary(text: str) -> dict:
    raw = copy.deepcopy(BASE)
    ep = raw["authorizedApplication"]["authorizedPartI"]["trialDetails"]["trialInformation"]["endPoint"]
    ep["primaryEndPoints"][0]["endPoint"] = text
    return raw


# ---- field mapping (pure, fixture-driven) ------------------------------------------------


def test_map_outcomes_primary_and_secondary_from_fixture():
    outcomes = ctis.map_outcomes(BASE)
    primaries = [o for o in outcomes if o["outcome_type"] == "PRIMARY"]
    secondaries = [o for o in outcomes if o["outcome_type"] == "SECONDARY"]

    assert len(primaries) == 1
    assert len(secondaries) == 3
    assert primaries[0]["ordinal"] == 0
    assert primaries[0]["measure"].startswith("1. Percentage of participants who are euthyroid")
    assert primaries[0]["time_frame"] is None  # not derivable from CTIS's endpoint shape
    assert primaries[0]["description"] is None
    assert [o["ordinal"] for o in secondaries] == [0, 1, 2]


def test_map_outcomes_missing_modules_never_raises():
    assert ctis.map_outcomes({}) == []
    assert ctis.map_outcomes({"authorizedApplication": {}}) == []


def test_map_meta_from_fixture():
    meta = ctis.map_meta(BASE)
    assert meta == {
        "study_type": "INTERVENTIONAL",
        "phase": "PHASE3",
        "sponsor": "Argenx",
        "sponsor_class": "Pharmaceutical company",
        "conditions": ["Graves' Disease (GD)"],
        "enrolment_count": 85,
        "overall_status": "Under evaluation",
        "first_posted_date": "2026-08-07",
    }


def test_map_meta_unmapped_phase_code_kept_honest_not_guessed():
    raw = copy.deepcopy(BASE)
    raw["authorizedApplication"]["authorizedPartI"]["trialDetails"]["trialInformation"]["trialCategory"]["trialPhase"] = "99"
    assert ctis.map_meta(raw)["phase"] == "CODE_99"


# ---- outcome-relevant content hash (the version gate) -------------------------------------


def test_outcomes_content_hash_stable_and_sensitive_to_measure_text():
    h1 = ctis.outcomes_content_hash(ctis.map_outcomes(BASE))
    h2 = ctis.outcomes_content_hash(ctis.map_outcomes(json.loads(json.dumps(BASE))))
    assert h1 == h2  # identical content -> identical hash, reproducibly

    changed = ctis.outcomes_content_hash(ctis.map_outcomes(_changed_primary("a materially different endpoint")))
    assert changed != h1


# ---- trials.registry ALTER guard -----------------------------------------------------------


def test_ensure_schema_adds_registry_column_default_ctgov_and_is_idempotent(tmp_path, monkeypatch):
    _patch_config(tmp_path, monkeypatch)
    conn = db.connect()
    conn.execute("INSERT INTO trials(nct_id, version_count) VALUES ('NCT1', 1)")
    conn.commit()

    ctis.ensure_schema(conn)
    ctis.ensure_schema(conn)  # must not raise "duplicate column"

    cols = {r["name"] for r in conn.execute("PRAGMA table_info(trials)")}
    assert "registry" in cols
    row = conn.execute("SELECT registry FROM trials WHERE nct_id='NCT1'").fetchone()
    assert row["registry"] == "ctgov"  # pre-existing v1.0 rows default to ctgov, not NULL
    conn.close()


# ---- watchlist round trip -------------------------------------------------------------------


def test_watchlist_round_trip(tmp_path, monkeypatch):
    _patch_config(tmp_path, monkeypatch)
    ctis.write_watchlist(["2025-523333-26-00", "2025-520678-20-00"])
    assert ctis.read_watchlist() == ["2025-523333-26-00", "2025-520678-20-00"]


def test_read_watchlist_missing_file_returns_empty(tmp_path, monkeypatch):
    _patch_config(tmp_path, monkeypatch)
    assert ctis.read_watchlist() == []


# ---- CTISAdapter: local-only RegistryAdapter conformance --------------------------------------


def test_ctis_adapter_reads_local_snapshot_series_not_network(tmp_path, monkeypatch):
    _patch_config(tmp_path, monkeypatch)
    changed = _changed_primary("a later, different primary endpoint")
    ctis._write_snapshot(CT, date(2026, 8, 1), BASE)
    ctis._write_snapshot(CT, date(2026, 8, 5), changed)

    adapter = ctis.CTISAdapter()

    async def run():
        versions = await adapter.list_versions(CT)
        v0 = await adapter.fetch_version(CT, 0)
        v1 = await adapter.fetch_version(CT, 1)
        return versions, v0, v1

    versions, v0, v1 = asyncio.run(run())
    assert versions == [
        {"version": 0, "date": "2026-08-01", "labels": []},
        {"version": 1, "date": "2026-08-05", "labels": []},
    ]
    assert v0 == BASE
    assert v1 == changed


def test_ctis_adapter_unknown_trial_has_empty_version_series(tmp_path, monkeypatch):
    _patch_config(tmp_path, monkeypatch)
    result = asyncio.run(ctis.CTISAdapter().list_versions("2099-000000-00-00"))
    assert result == []


# ---- snapshot_pass: fetch + hash gate + store + upsert -----------------------------------------


def test_snapshot_pass_stores_baseline_then_second_identical_pass_stores_zero(tmp_path, monkeypatch):
    """The hash-gate proof the task brief asks a real run to demonstrate: first pass stores,
    an immediate second pass over the same (unchanged) live state stores nothing."""
    _patch_config(tmp_path, monkeypatch)
    _fast(monkeypatch)
    transport = _retrieve_transport({CT: BASE})

    async def run():
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            r1 = await ctis.snapshot_pass([CT], client, today=date(2026, 8, 9))
            r2 = await ctis.snapshot_pass([CT], client, today=date(2026, 8, 9))
        return r1, r2

    r1, r2 = asyncio.run(run())

    assert r1 == ctis.SnapshotResult(checked=1, stored=[CT], skipped=[], failed=[])
    assert r2 == ctis.SnapshotResult(checked=1, stored=[], skipped=[CT], failed=[])

    snaps = list((config.CACHE_DIR / "ctis" / CT).glob("snap-*.json.gz"))
    assert len(snaps) == 1  # second pass never wrote a duplicate/second file

    conn = db.connect()
    trial = conn.execute("SELECT * FROM trials WHERE nct_id=?", (CT,)).fetchone()
    versions = conn.execute("SELECT version_no FROM versions WHERE nct_id=?", (CT,)).fetchall()
    outcomes = conn.execute("SELECT * FROM outcomes WHERE nct_id=? AND version_no=0", (CT,)).fetchall()
    conn.close()

    assert trial["registry"] == "ctis"
    assert trial["phase"] == "PHASE3"
    assert trial["sponsor_class"] == "Pharmaceutical company"  # DB column is lead_sponsor/sponsor_class
    assert trial["version_count"] == 1
    assert [v["version_no"] for v in versions] == [0]  # not duplicated by the second pass
    assert len(outcomes) == 4  # 1 primary + 3 secondary from the fixture
    assert any(o["measure_norm"] for o in outcomes)  # ctcm.normalize.norm() actually ran


def test_snapshot_pass_stores_new_version_when_primary_endpoint_text_changes(tmp_path, monkeypatch):
    _patch_config(tmp_path, monkeypatch)
    _fast(monkeypatch)
    changed = _changed_primary("1. A materially different primary endpoint measure")

    async def run():
        async with httpx.AsyncClient(transport=_retrieve_transport({CT: BASE}), base_url="http://test") as client:
            r1 = await ctis.snapshot_pass([CT], client, today=date(2026, 8, 1))
        async with httpx.AsyncClient(transport=_retrieve_transport({CT: changed}), base_url="http://test") as client:
            r2 = await ctis.snapshot_pass([CT], client, today=date(2026, 8, 5))
        return r1, r2

    r1, r2 = asyncio.run(run())
    assert r1.stored == [CT]
    assert r2.stored == [CT]  # genuinely different outcome content -> a second version is kept

    conn = db.connect()
    versions = conn.execute("SELECT version_no, version_date FROM versions WHERE nct_id=? ORDER BY version_no", (CT,)).fetchall()
    trial = conn.execute("SELECT version_count FROM trials WHERE nct_id=?", (CT,)).fetchone()
    conn.close()

    assert [v["version_no"] for v in versions] == [0, 1]
    assert versions[1]["version_date"] == "2026-08-05"
    assert trial["version_count"] == 2


def test_snapshot_pass_same_day_collision_suffixes_the_filename_instead_of_overwriting(tmp_path, monkeypatch):
    """task15-review.md Important #2: a same-day second snapshot_pass call whose content
    genuinely changed must not overwrite the first call's raw payload -- two files on
    disk, two versions rows, nothing lost."""
    _patch_config(tmp_path, monkeypatch)
    _fast(monkeypatch)
    changed = _changed_primary("1. A same-day, later, materially different primary endpoint")
    same_day = date(2026, 8, 9)

    async def run():
        async with httpx.AsyncClient(transport=_retrieve_transport({CT: BASE}), base_url="http://test") as client:
            r1 = await ctis.snapshot_pass([CT], client, today=same_day)
        async with httpx.AsyncClient(transport=_retrieve_transport({CT: changed}), base_url="http://test") as client:
            r2 = await ctis.snapshot_pass([CT], client, today=same_day)
        return r1, r2

    r1, r2 = asyncio.run(run())
    assert r1.stored == [CT]
    assert r2.stored == [CT]  # genuinely different content -- kept, not skipped, despite the same date

    on_disk = {p.name for p in (config.CACHE_DIR / "ctis" / CT).glob("snap-*.json.gz")}
    assert on_disk == {"snap-2026-08-09.json.gz", "snap-2026-08-09-2.json.gz"}  # two distinct files, not one overwritten

    # _local_snapshots (not a raw lexicographic sort -- "-2.json.gz" sorts before ".json.gz"
    # as plain strings) orders them chronologically + by intraday collision order.
    files = ctis._local_snapshots(CT)
    assert [p.name for p in files] == ["snap-2026-08-09.json.gz", "snap-2026-08-09-2.json.gz"]
    # nothing overwritten: the first file still holds exactly what the first call fetched
    assert ctis._read_snapshot(files[0]) == BASE
    assert ctis._read_snapshot(files[1]) == changed

    conn = db.connect()
    versions = conn.execute("SELECT version_no, version_date FROM versions WHERE nct_id=? ORDER BY version_no", (CT,)).fetchall()
    conn.close()
    assert [v["version_no"] for v in versions] == [0, 1]  # both rows kept, not collapsed
    assert versions[0]["version_date"] == versions[1]["version_date"] == "2026-08-09"


def test_snapshot_pass_one_trial_failure_does_not_abort_the_batch(tmp_path, monkeypatch):
    _patch_config(tmp_path, monkeypatch)
    _fast(monkeypatch)

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("BAD-CT"):
            return httpx.Response(500)
        return httpx.Response(200, json=BASE)

    transport = httpx.MockTransport(handler)

    async def run():
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            return await ctis.snapshot_pass([CT, "BAD-CT"], client, today=date(2026, 8, 9))

    result = asyncio.run(run())
    assert result.stored == [CT]
    assert result.failed == ["BAD-CT"]
    assert result.checked == 2


# ---- search_watchlist: pagination, phase filter, resultsFirstReceived approximation -----------


def test_search_watchlist_filters_by_phase_and_no_results_yet_stops_at_target(monkeypatch):
    _fast(monkeypatch)
    page1 = {
        "pagination": {"totalRecords": 300, "currentPage": 1, "totalPages": 3, "nextPage": True},
        "data": [{"ctNumber": f"CT-{i}", "resultsFirstReceived": "No"} for i in range(3)]
        + [{"ctNumber": "CT-HAS-RESULTS", "resultsFirstReceived": "Yes"}],
    }
    page2 = {
        "pagination": {"totalRecords": 300, "currentPage": 2, "totalPages": 3, "nextPage": True},
        "data": [{"ctNumber": f"CT2-{i}", "resultsFirstReceived": "No"} for i in range(2)],
    }
    bodies = []

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        bodies.append(body)
        page = body["pagination"]["page"]
        return httpx.Response(200, json=page1 if page == 1 else page2)

    transport = httpx.MockTransport(handler)

    async def run():
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            return await ctis.search_watchlist(client, target=5, page_size=4)

    result = asyncio.run(run())

    assert result == ["CT-0", "CT-1", "CT-2", "CT2-0", "CT2-1"]  # CT-HAS-RESULTS filtered out
    assert len(bodies) == 2  # target reached after page 2 -- page 3 never requested
    assert bodies[0]["searchCriteria"] == {"trialPhaseCode": ["5"]}
    assert bodies[0]["sort"] == {"property": "decisionDate", "direction": "DESC"}


def test_search_watchlist_stops_on_empty_page(monkeypatch):
    _fast(monkeypatch)
    empty = {"pagination": {"totalRecords": 0, "currentPage": 1, "totalPages": 0, "nextPage": False}, "data": []}
    transport = httpx.MockTransport(lambda request: httpx.Response(200, json=empty))

    async def run():
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            return await ctis.search_watchlist(client, target=200)

    assert asyncio.run(run()) == []


# ---- integration: findings flow through the SAME pipeline, unchanged --------------------------


def test_ctis_snapshots_flow_through_existing_pipeline_unchanged(tmp_path, monkeypatch):
    """Proves the design claim in ctis.py's module docstring: once snapshot_pass() has
    upserted trials/versions/outcomes rows, plain ctcm.pipeline.run_pipeline() (imported
    unmodified) produces findings for the CTIS trial exactly like it would for a CT.gov one."""
    _patch_config(tmp_path, monkeypatch)
    _fast(monkeypatch)
    changed = _changed_primary("1. An entirely different, unrelated primary endpoint measure")

    async def run():
        async with httpx.AsyncClient(transport=_retrieve_transport({CT: BASE}), base_url="http://test") as client:
            await ctis.snapshot_pass([CT], client, today=date(2026, 8, 1))
        async with httpx.AsyncClient(transport=_retrieve_transport({CT: changed}), base_url="http://test") as client:
            await ctis.snapshot_pass([CT], client, today=date(2026, 8, 5))

    asyncio.run(run())

    n = run_pipeline(t3_enabled=False)
    assert n > 0

    conn = db.connect()
    findings = conn.execute("SELECT change_type, severity FROM findings WHERE nct_id=?", (CT,)).fetchall()
    conn.close()

    assert len(findings) > 0
    assert any(f["change_type"].startswith("PRIMARY") for f in findings)
