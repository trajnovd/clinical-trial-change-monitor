"""Publication linking (v2 Task 13): tier assignment, upsert idempotency, date
arithmetic, trial-selection priority. Fixtures/fakes only -- no real network (async
fetch tests use httpx.MockTransport, same non-network pattern as test_ingest.py)."""

import asyncio

import httpx

from ctcm import config, db, publink


# ---- tier assignment ----------------------------------------------------------------


def test_assign_tiers_prioritizes_high_over_medium_over_low():
    si_pmids = {"111"}
    abstract_records = [{"pmid": "111", "title": "Abstract hit"}, {"pmid": "222", "title": "Abstract only"}]
    fulltext_records = [
        {"pmid": "111", "title": "Fulltext hit"},
        {"pmid": "222", "title": "Fulltext too"},
        {"pmid": "333", "title": "Fulltext only"},
    ]
    rows = {r["pmid"]: r for r in publink.assign_tiers(si_pmids, abstract_records, fulltext_records)}

    assert rows["111"]["tier"] == "HIGH"  # in si + abstract + fulltext -> highest wins
    assert rows["111"]["source"] == "epmc_abstract+pubmed_si"
    assert rows["222"]["tier"] == "MEDIUM"  # abstract + fulltext, no si
    assert rows["333"]["tier"] == "LOW"  # fulltext only


def test_assign_tiers_si_only_pmid_gets_stub_row():
    """An [si]-linked pmid Europe PMC never surfaced still gets a HIGH row -- the
    curated linkage itself is the evidence, metadata is just unavailable."""
    rows = {r["pmid"]: r for r in publink.assign_tiers({"999"}, [], [])}
    assert rows["999"]["tier"] == "HIGH"
    assert rows["999"]["source"] == "pubmed_si"
    assert rows["999"]["title"] is None
    assert rows["999"]["pub_date"] is None


def test_assign_tiers_drops_records_with_no_pmid():
    rows = publink.assign_tiers(set(), [], [{"pmcid": "PMC1", "title": "preprint, no pmid"}])
    assert rows == []


def test_normalize_record_maps_epmc_fields_and_open_access_flag():
    rec = {
        "pmid": "38618926", "doi": "10.1177/17407745241238443", "title": "Some trial report",
        "journalTitle": "Clin Trials", "firstPublicationDate": "2024-04-15", "isOpenAccess": "Y",
    }
    norm = publink._normalize_record(rec)
    assert norm == {
        "pmid": "38618926", "doi": "10.1177/17407745241238443", "title": "Some trial report",
        "journal": "Clin Trials", "pub_date": "2024-04-15", "oa": 1,
    }

    assert publink._normalize_record({"pmid": "1", "isOpenAccess": "N"})["oa"] == 0
    assert publink._normalize_record({"pmid": "1"})["oa"] is None  # field absent -- unknown, not False
    # no fallback from the coarser pubYear -- a fabricated day-precision date would
    # corrupt days_after_change below
    assert publink._normalize_record({"pmid": "1", "pubYear": "2024"})["pub_date"] is None


def test_records_for_trial_parses_raw_eutils_and_epmc_shapes():
    raw = {
        "si": {"esearchresult": {"idlist": ["111"]}},
        "abstract": {"resultList": {"result": [{"pmid": "111", "title": "A"}]}},
        "fulltext": {"resultList": {"result": [{"pmid": "222", "title": "B"}]}},
    }
    rows = {r["pmid"]: r for r in publink.records_for_trial(raw)}
    assert rows["111"]["tier"] == "HIGH"
    assert rows["222"]["tier"] == "LOW"


def test_records_for_trial_handles_empty_result_lists():
    raw = {"si": {"esearchresult": {"idlist": []}}, "abstract": {}, "fulltext": {"resultList": {}}}
    assert publink.records_for_trial(raw) == []


# ---- date arithmetic ------------------------------------------------------------------


def test_days_after_change_positive_when_pub_postdates_change():
    assert publink.days_after_change("2020-06-01", "2020-05-01") == 31


def test_days_after_change_none_when_pub_predates_or_equals_change():
    assert publink.days_after_change("2020-04-01", "2020-05-01") is None
    assert publink.days_after_change("2020-05-01", "2020-05-01") is None


def test_days_after_change_none_on_missing_dates():
    assert publink.days_after_change(None, "2020-05-01") is None
    assert publink.days_after_change("2020-05-01", None) is None


# ---- upsert idempotency ---------------------------------------------------------------


def _fixture_conn(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "DATA_DIR", tmp_path)
    monkeypatch.setattr(config, "CACHE_DIR", tmp_path / "cache")
    monkeypatch.setattr(config, "DB_PATH", tmp_path / "test.db")
    return db.connect()


def test_upsert_publications_is_idempotent(tmp_path, monkeypatch):
    conn = _fixture_conn(tmp_path, monkeypatch)
    records = [
        {"pmid": "1", "doi": "d1", "title": "T1", "journal": "J1", "pub_date": "2020-01-01", "oa": 1, "tier": "LOW", "source": "epmc_fulltext"},
        {"pmid": "2", "doi": "d2", "title": "T2", "journal": "J2", "pub_date": "2020-02-01", "oa": 0, "tier": "MEDIUM", "source": "epmc_abstract"},
    ]
    publink.upsert_publications(conn, "NCT1", records)
    publink.upsert_publications(conn, "NCT1", records)  # re-run: no duplicate rows

    rows = conn.execute("SELECT pmid, tier FROM publications WHERE nct_id='NCT1' ORDER BY pmid").fetchall()
    assert [(r["pmid"], r["tier"]) for r in rows] == [("1", "LOW"), ("2", "MEDIUM")]


def test_upsert_publications_updates_tier_on_conflict(tmp_path, monkeypatch):
    """A pmid first seen only via the noisy full-text query, later confirmed via
    PubMed [si] on a re-run, should upgrade in place -- not sit stuck at LOW."""
    conn = _fixture_conn(tmp_path, monkeypatch)
    low = [{"pmid": "1", "doi": None, "title": "T1", "journal": None, "pub_date": None, "oa": None, "tier": "LOW", "source": "epmc_fulltext"}]
    publink.upsert_publications(conn, "NCT1", low)

    high = [{"pmid": "1", "doi": "d1", "title": "T1", "journal": "J1", "pub_date": "2020-01-01", "oa": 1, "tier": "HIGH", "source": "epmc_fulltext+pubmed_si"}]
    publink.upsert_publications(conn, "NCT1", high)

    row = conn.execute("SELECT * FROM publications WHERE nct_id='NCT1' AND pmid='1'").fetchone()
    assert row["tier"] == "HIGH"
    assert row["doi"] == "d1"
    assert conn.execute("SELECT COUNT(*) AS n FROM publications").fetchone()["n"] == 1


# ---- trial selection priority ----------------------------------------------------------


def _seed_trials_findings(conn):
    for nct in ("NCT_ADJ", "NCT_SIGNAL", "NCT_PLAIN"):
        conn.execute(
            "INSERT INTO trials(nct_id, study_type, phase, overall_status, lead_sponsor, sponsor_class, "
            "conditions, enrolment_count, first_posted_date, version_count) VALUES "
            f"('{nct}', 'INTERVENTIONAL', 'PHASE3', 'COMPLETED', 'S', 'NIH', '[]', 10, '2020-01-01', 1)"
        )
    conn.execute(
        "INSERT INTO findings(finding_id, nct_id, from_version, to_version, change_type, severity, "
        "before_measure, after_measure, days_after_enrolment, days_after_primary_completion, confidence, "
        "resolved_by, rationale) VALUES (1, 'NCT_ADJ', 0, 1, 'PRIMARY_REPLACED', 'SIGNAL', 'a', 'b', 10, -5, 1.0, 'T0', 'r')"
    )
    conn.execute(
        "INSERT INTO findings(finding_id, nct_id, from_version, to_version, change_type, severity, "
        "before_measure, after_measure, days_after_enrolment, days_after_primary_completion, confidence, "
        "resolved_by, rationale) VALUES (2, 'NCT_SIGNAL', 0, 1, 'PRIMARY_REPLACED', 'SIGNAL', 'a', 'b', 10, -5, 1.0, 'T0', 'r')"
    )
    conn.commit()


def test_select_nct_ids_puts_adjudicated_signal_trials_first(tmp_path, monkeypatch):
    from ctcm.adjudicate import content_hash, ensure_schema

    conn = _fixture_conn(tmp_path, monkeypatch)
    _seed_trials_findings(conn)
    ensure_schema(conn)
    chash = content_hash("NCT_ADJ", 0, 1, "PRIMARY_REPLACED", "a", "b")
    conn.execute(
        "INSERT INTO adjudications(content_hash, finding_id, nct_id, severity_confirmed, confidence, rationale, "
        "defence, prosecution, model, created_at) VALUES (?, 1, 'NCT_ADJ', 'HIGH', 0.9, 'r', 'd', 'p', 'm', 'now')",
        (chash,),
    )
    conn.commit()

    assert publink.select_nct_ids(conn, limit=10) == ["NCT_ADJ", "NCT_SIGNAL", "NCT_PLAIN"]
    assert publink.select_nct_ids(conn, limit=1) == ["NCT_ADJ"]


def test_select_nct_ids_ignores_unreviewed_adjudications(tmp_path, monkeypatch):
    """UNREVIEWED means the adjudication chain itself failed -- not a real verdict,
    so it must not count as "adjudicated" for prioritization purposes either."""
    from ctcm.adjudicate import content_hash, ensure_schema

    conn = _fixture_conn(tmp_path, monkeypatch)
    _seed_trials_findings(conn)
    ensure_schema(conn)
    chash = content_hash("NCT_ADJ", 0, 1, "PRIMARY_REPLACED", "a", "b")
    conn.execute(
        "INSERT INTO adjudications(content_hash, finding_id, nct_id, severity_confirmed, confidence, rationale, "
        "defence, prosecution, model, created_at) VALUES (?, 1, 'NCT_ADJ', 'UNREVIEWED', 0.0, 'r', '', '', 'm', 'now')",
        (chash,),
    )
    conn.commit()

    ordered = publink.select_nct_ids(conn, limit=10)
    assert ordered[:2] == ["NCT_ADJ", "NCT_SIGNAL"] or ordered[:2] == ["NCT_SIGNAL", "NCT_ADJ"]
    # NCT_ADJ no longer beats NCT_SIGNAL on priority 0 (both are priority 1, tiebreak alphabetical)
    assert ordered == sorted(["NCT_ADJ", "NCT_SIGNAL", "NCT_PLAIN"][:2]) + ["NCT_PLAIN"]


def test_select_nct_ids_works_without_adjudications_table(tmp_path, monkeypatch):
    conn = _fixture_conn(tmp_path, monkeypatch)
    _seed_trials_findings(conn)
    assert publink.select_nct_ids(conn, limit=10) == ["NCT_ADJ", "NCT_SIGNAL", "NCT_PLAIN"]  # both priority 1, alpha order


# ---- fetch + cache (MockTransport, no real network) --------------------------------------


def test_get_json_retries_on_429_then_succeeds(monkeypatch):
    real_sleep = asyncio.sleep
    monkeypatch.setattr(publink.asyncio, "sleep", lambda _: real_sleep(0))
    calls = {"n": 0}

    def handler(request):
        calls["n"] += 1
        if calls["n"] < 3:
            return httpx.Response(429)
        return httpx.Response(200, json={"ok": True})

    transport = httpx.MockTransport(handler)

    async def run():
        async with httpx.AsyncClient(transport=transport) as client:
            return await publink._get_json(client, "http://test/x", {})

    assert asyncio.run(run()) == {"ok": True}
    assert calls["n"] == 3


def test_fetch_raw_cached_skips_network_when_cache_present(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "CACHE_DIR", tmp_path)
    cached = {"si": {"esearchresult": {"idlist": ["1"]}}, "abstract": {}, "fulltext": {}}
    path = tmp_path / "publink" / "NCTX.json"
    path.parent.mkdir(parents=True)
    path.write_text('{"si": {"esearchresult": {"idlist": ["1"]}}, "abstract": {}, "fulltext": {}}')

    def handler(request):
        raise AssertionError("should not hit the network when a cache file exists")

    transport = httpx.MockTransport(handler)

    async def run():
        async with httpx.AsyncClient(transport=transport) as client:
            return await publink.fetch_raw_cached(client, "NCTX")

    assert asyncio.run(run()) == cached


def test_fetch_raw_cached_writes_cache_on_miss(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "CACHE_DIR", tmp_path)

    def handler(request):
        if "eutils" in str(request.url):
            return httpx.Response(200, json={"esearchresult": {"idlist": []}})
        return httpx.Response(200, json={"resultList": {"result": []}})

    transport = httpx.MockTransport(handler)

    async def run():
        async with httpx.AsyncClient(transport=transport) as client:
            return await publink.fetch_raw_cached(client, "NCTY")

    result = asyncio.run(run())
    assert result["si"] == {"esearchresult": {"idlist": []}}
    assert (tmp_path / "publink" / "NCTY.json").exists()


def test_link_all_isolates_per_trial_failures(tmp_path, monkeypatch):
    """One trial's fetch error must not abort the batch -- same resilience contract
    as ctcm.ingest.ingest."""
    monkeypatch.setattr(config, "CACHE_DIR", tmp_path / "cache")
    conn = _fixture_conn(tmp_path, monkeypatch)
    _seed_trials_findings(conn)

    def handler(request):
        if "NCT_PLAIN" in str(request.url):
            return httpx.Response(500)
        if "eutils" in str(request.url):
            return httpx.Response(200, json={"esearchresult": {"idlist": ["1"]}})
        return httpx.Response(200, json={"resultList": {"result": [{"pmid": "1", "title": "T"}]}})

    real_sleep = asyncio.sleep
    monkeypatch.setattr(publink.asyncio, "sleep", lambda _: real_sleep(0))

    async def run():
        transport = httpx.MockTransport(handler)
        orig_client = httpx.AsyncClient

        def client_factory(*args, **kwargs):
            kwargs["transport"] = transport
            return orig_client(*args, **kwargs)

        monkeypatch.setattr(publink.httpx, "AsyncClient", client_factory)
        return await publink.link_all(["NCT_ADJ", "NCT_SIGNAL", "NCT_PLAIN"], conn)

    tier_totals = asyncio.run(run())
    assert isinstance(tier_totals, publink.Counter)
    # NCT_PLAIN failed every retry (persistent 500) -- NCT_ADJ/NCT_SIGNAL still ran.
    n_linked = conn.execute("SELECT COUNT(DISTINCT nct_id) AS n FROM publications").fetchone()["n"]
    assert n_linked == 2
