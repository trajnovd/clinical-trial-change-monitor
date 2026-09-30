"""ctcm/api.py against a small fixture db (tmp_path, built via ctcm.db.connect() same
as test_pipeline.py) -- no network, no dependency on the real data/ctcm.db. Covers the
filtering/sort/q contract, pagination and the meaning of `total` (v0.7), /api/meta's
facet counts and case studies (v0.7), the trial-detail shape including headlineFinding
(v0.7), 404, the two adjudications-table states (absent -- the common case today --
and present with either key strategy), the publications array (Task 13, absent-table
and populated states), the CSV export (header, quoting, filters, headers) and the
/api/changes monitoring feed (empty corpus, ordering, paging)."""

import csv
import io

from fastapi.testclient import TestClient

from ctcm import config, db, monitor
from ctcm.adjudicate import content_hash, ensure_schema
from ctcm.api import EXPORT_COLUMNS, app
from ctcm.publink import upsert_publications
from ctcm.reslink import ensure_schema as reslink_ensure_schema

client = TestClient(app)


def _seed(conn):
    conn.execute(
        "INSERT INTO trials(nct_id, study_type, phase, overall_status, lead_sponsor, sponsor_class, "
        "conditions, enrolment_count, first_posted_date, version_count) VALUES "
        "('NCT1', 'INTERVENTIONAL', 'PHASE3', 'COMPLETED', 'Alpha Sponsor', 'NIH', '[\"COVID-19\"]', 100, '2020-01-01', 2)"
    )
    conn.execute(
        "INSERT INTO trials(nct_id, study_type, phase, overall_status, lead_sponsor, sponsor_class, "
        "conditions, enrolment_count, first_posted_date, version_count) VALUES "
        "('NCT2', 'INTERVENTIONAL', 'PHASE2', 'RECRUITING', 'Beta Sponsor', 'INDUSTRY', '[\"Diabetes\"]', 50, '2021-01-01', 1)"
    )

    conn.execute("INSERT INTO versions(nct_id, version_no, version_date, module_labels) VALUES ('NCT1', 0, '2020-01-01', '[]')")
    conn.execute(
        "INSERT INTO versions(nct_id, version_no, version_date, module_labels) "
        "VALUES ('NCT1', 1, '2020-02-01', '[\"Outcome Measures\"]')"
    )
    conn.execute("INSERT INTO versions(nct_id, version_no, version_date, module_labels) VALUES ('NCT2', 0, '2021-01-01', '[]')")

    conn.execute(
        "INSERT INTO outcomes(nct_id, version_no, outcome_type, ordinal, measure, time_frame, measure_norm) "
        "VALUES ('NCT1', 0, 'PRIMARY', 0, 'Original measure', 'Day 1', 'original measure')"
    )
    conn.execute(
        "INSERT INTO outcomes(nct_id, version_no, outcome_type, ordinal, measure, time_frame, measure_norm) "
        "VALUES ('NCT1', 0, 'SECONDARY', 0, 'Sec A', 'Day 1', 'sec a')"
    )
    conn.execute(
        "INSERT INTO outcomes(nct_id, version_no, outcome_type, ordinal, measure, time_frame, measure_norm) "
        "VALUES ('NCT1', 1, 'PRIMARY', 0, 'New measure', 'Day 1', 'new measure')"
    )
    conn.execute(
        "INSERT INTO outcomes(nct_id, version_no, outcome_type, ordinal, measure, time_frame, measure_norm) "
        "VALUES ('NCT2', 0, 'PRIMARY', 0, 'Untouched measure', 'Day 1', 'untouched measure')"
    )

    conn.execute(
        "INSERT INTO timeline_facts(nct_id, version_no, start_date, start_date_type, primary_completion_date, "
        "primary_completion_type) VALUES ('NCT1', 0, '2019-12-01', 'ACTUAL', NULL, NULL)"
    )
    conn.execute(
        "INSERT INTO timeline_facts(nct_id, version_no, start_date, start_date_type, primary_completion_date, "
        "primary_completion_type) VALUES ('NCT1', 1, '2019-12-01', 'ACTUAL', '2020-03-01', 'ACTUAL')"
    )
    conn.execute(
        "INSERT INTO timeline_facts(nct_id, version_no, start_date, start_date_type, primary_completion_date, "
        "primary_completion_type) VALUES ('NCT2', 0, '2021-01-01', 'ACTUAL', NULL, NULL)"
    )

    conn.execute(
        "INSERT INTO findings(finding_id, nct_id, from_version, to_version, change_type, severity, before_measure, "
        "after_measure, days_after_enrolment, days_after_primary_completion, confidence, resolved_by, rationale) "
        "VALUES (1, 'NCT1', 0, 1, 'PRIMARY_REPLACED', 'SIGNAL', 'Original measure', 'New measure', 32, -30, 1.0, 'T0', "
        "\"PRIMARY_REPLACED: 'Original measure' -> 'New measure' (32 days after enrolment, v0->v1).\")"
    )
    # NCT2 has zero findings -- exercises the LEFT JOIN's all-NULL headline branch.
    conn.commit()


def _fixture_db(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "DATA_DIR", tmp_path)
    monkeypatch.setattr(config, "CACHE_DIR", tmp_path / "cache")
    monkeypatch.setattr(config, "DB_PATH", tmp_path / "test.db")
    conn = db.connect()
    _seed(conn)
    conn.close()


_TRIAL_INSERT = (
    "INSERT INTO trials(nct_id, study_type, phase, overall_status, lead_sponsor, sponsor_class, "
    "conditions, enrolment_count, first_posted_date, version_count) VALUES "
    "(?, 'INTERVENTIONAL', 'PHASE1', 'COMPLETED', 'Pager Sponsor', 'OTHER', '[]', 1, '2020-01-01', 1)"
)
_FINDING_INSERT = (
    "INSERT INTO findings(finding_id, nct_id, from_version, to_version, change_type, severity, before_measure, "
    "after_measure, days_after_enrolment, days_after_primary_completion, confidence, resolved_by, rationale) "
    "VALUES (?, ?, 0, 1, 'PRIMARY_REPLACED', ?, 'a', 'b', 5, ?, 1.0, 'T0', 'r')"
)


def _seed_bulk_trials(conn, n):
    """n extra finding-less trials, ids sorting after the fixture's NCT1/NCT2 --
    pagination only cares how many rows match, not what is in them."""
    conn.executemany(_TRIAL_INSERT, [(f"NCTP{i:04d}",) for i in range(n)])
    conn.commit()


def _seed_trial_with_finding(conn, nct_id, finding_id, severity, days_after_primary_completion):
    conn.execute(_TRIAL_INSERT, (nct_id,))
    conn.execute(_FINDING_INSERT, (finding_id, nct_id, severity, days_after_primary_completion))
    conn.commit()


def test_healthz():
    r = client.get("/healthz")
    assert r.status_code == 200
    assert r.json() == {"status": "ok"}


def test_root_serves_ui_index_html():
    r = client.get("/")
    assert r.status_code == 200
    assert "Redline" in r.text  # ui/index.html's <title>


def test_list_trials_returns_both_with_headline_counts(tmp_path, monkeypatch):
    _fixture_db(tmp_path, monkeypatch)
    r = client.get("/api/trials")
    assert r.status_code == 200
    body = r.json()
    assert body["total"] == 2
    assert {row["nctId"] for row in body["rows"]} == {"NCT1", "NCT2"}
    assert body["counts"] == {
        "trials": 2,
        "completedTrials": body["counts"]["completedTrials"],
        "versionsRead": body["counts"]["versionsRead"],
        "outcomeRecords": body["counts"]["outcomeRecords"],
        "signalTrials": 1,
        "postCompletionTrials": 0,
        "caveat": body["counts"]["caveat"],
    }
    assert "completed trials that posted results" in body["counts"]["caveat"]

    nct2_row = next(row for row in body["rows"] if row["nctId"] == "NCT2")
    assert nct2_row["severity"] is None
    assert nct2_row["changeType"] is None
    assert nct2_row["adjudication"] is None

    nct1_row = next(row for row in body["rows"] if row["nctId"] == "NCT1")
    assert nct1_row["severity"] == "SIGNAL"
    assert nct1_row["changeType"] == "PRIMARY_REPLACED"
    assert nct1_row["daysAfterEnrolment"] == 32


def test_list_trials_filters_by_severity(tmp_path, monkeypatch):
    _fixture_db(tmp_path, monkeypatch)
    r = client.get("/api/trials", params={"severity": "SIGNAL"})
    body = r.json()
    assert body["total"] == 1
    assert body["rows"][0]["nctId"] == "NCT1"


def test_list_trials_filters_by_sponsor_class_and_phase(tmp_path, monkeypatch):
    _fixture_db(tmp_path, monkeypatch)
    r = client.get("/api/trials", params={"sponsor_class": "INDUSTRY", "phase": "PHASE2"})
    body = r.json()
    assert body["total"] == 1
    assert body["rows"][0]["nctId"] == "NCT2"

    r = client.get("/api/trials", params={"sponsor_class": "INDUSTRY", "phase": "PHASE3"})
    assert r.json()["total"] == 0  # AND across groups: no trial is both


def test_list_trials_q_matches_nct_id_sponsor_and_conditions(tmp_path, monkeypatch):
    _fixture_db(tmp_path, monkeypatch)
    assert client.get("/api/trials", params={"q": "NCT2"}).json()["total"] == 1
    assert client.get("/api/trials", params={"q": "beta"}).json()["total"] == 1  # sponsor, case-insensitive
    assert client.get("/api/trials", params={"q": "diabetes"}).json()["total"] == 1  # condition
    assert client.get("/api/trials", params={"q": "nonexistent"}).json()["total"] == 0


def test_list_trials_sponsor_is_exact_match_not_substring(tmp_path, monkeypatch):
    """Fix round 1 (task16-review.md Critical-1): the analytics sponsor drill-down
    needs an exact filter, distinct from q='s deliberate substring search -- a sponsor
    whose name contains another's as a substring ("Pfizer" inside "Pfizer Sub Corp")
    must not cross-match."""
    _fixture_db(tmp_path, monkeypatch)
    conn = db.connect()
    conn.execute(
        "INSERT INTO trials(nct_id, study_type, phase, overall_status, lead_sponsor, sponsor_class, "
        "conditions, enrolment_count, first_posted_date, version_count) VALUES "
        "('NCT3', 'INTERVENTIONAL', 'PHASE3', 'COMPLETED', 'Pfizer', 'INDUSTRY', '[]', 10, '2020-01-01', 1)"
    )
    conn.execute(
        "INSERT INTO trials(nct_id, study_type, phase, overall_status, lead_sponsor, sponsor_class, "
        "conditions, enrolment_count, first_posted_date, version_count) VALUES "
        "('NCT4', 'INTERVENTIONAL', 'PHASE3', 'COMPLETED', 'Pfizer Sub Corp', 'INDUSTRY', '[]', 10, '2020-01-01', 1)"
    )
    # Simulates the registry's raw HTML-entity-escaped storage (see api.py's _ue
    # docstring) -- exact match must decode this before comparing to the plain param.
    conn.execute(
        "INSERT INTO trials(nct_id, study_type, phase, overall_status, lead_sponsor, sponsor_class, "
        "conditions, enrolment_count, first_posted_date, version_count) VALUES "
        "('NCT5', 'INTERVENTIONAL', 'PHASE3', 'COMPLETED', 'AT&amp;T Health', 'INDUSTRY', '[]', 10, '2020-01-01', 1)"
    )
    conn.commit()
    conn.close()

    r = client.get("/api/trials", params={"sponsor": "Pfizer"})
    assert r.json()["total"] == 1
    assert r.json()["rows"][0]["nctId"] == "NCT3"

    r = client.get("/api/trials", params={"sponsor": "Pfizer Sub Corp"})
    assert r.json()["total"] == 1
    assert r.json()["rows"][0]["nctId"] == "NCT4"

    # q= is unchanged -- still a substring match across both (the contrast case).
    assert client.get("/api/trials", params={"q": "Pfizer"}).json()["total"] == 2

    # decoded param matches the raw HTML-entity-escaped stored value
    r = client.get("/api/trials", params={"sponsor": "AT&T Health"})
    assert r.json()["total"] == 1
    assert r.json()["rows"][0]["nctId"] == "NCT5"


def test_list_trials_sort_direction(tmp_path, monkeypatch):
    _fixture_db(tmp_path, monkeypatch)
    r = client.get("/api/trials", params={"sort": "nctId"})
    assert [row["nctId"] for row in r.json()["rows"]] == ["NCT1", "NCT2"]
    r = client.get("/api/trials", params={"sort": "-nctId"})
    assert [row["nctId"] for row in r.json()["rows"]] == ["NCT2", "NCT1"]


def test_list_trials_invalid_sort_is_400(tmp_path, monkeypatch):
    _fixture_db(tmp_path, monkeypatch)
    r = client.get("/api/trials", params={"sort": "nonsense"})
    assert r.status_code == 400


def test_list_trials_pages_without_overlap_and_total_stays_the_filtered_count(tmp_path, monkeypatch):
    """v0.7 changed what `total` means: it was len(rows), it is now the COUNT(*) over
    the same WHERE. The pager renders "1-4 of 9" off it, so a `total` that tracked the
    page size would report "1-4 of 4" on every page and never offer a Next."""
    _fixture_db(tmp_path, monkeypatch)
    conn = db.connect()
    _seed_bulk_trials(conn, 7)  # 9 trials in the corpus
    conn.close()

    pages = [client.get("/api/trials", params={"sort": "nctId", "limit": 4, "offset": o}).json() for o in (0, 4, 8)]
    assert [len(p["rows"]) for p in pages] == [4, 4, 1]
    assert [p["total"] for p in pages] == [9, 9, 9]

    paged_ids = [row["nctId"] for p in pages for row in p["rows"]]
    assert len(set(paged_ids)) == 9  # no row served twice, none skipped
    assert paged_ids == sorted(paged_ids)  # sort survives paging

    # ...and the count honours the filter, rather than counting the whole corpus
    filtered = client.get("/api/trials", params={"sponsor_class": "OTHER", "limit": 2}).json()
    assert len(filtered["rows"]) == 2
    assert filtered["total"] == 7


def test_list_trials_limit_defaults_to_50_and_is_clamped_to_200(tmp_path, monkeypatch):
    """Over-max limits clamp rather than 400/422: a stale bookmark should render a
    (large) page, not an error. The cap still holds -- ?limit=1000 cannot make the
    server serialise the whole corpus."""
    _fixture_db(tmp_path, monkeypatch)
    conn = db.connect()
    _seed_bulk_trials(conn, 205)  # 207 trials in the corpus
    conn.close()

    assert len(client.get("/api/trials").json()["rows"]) == 50

    body = client.get("/api/trials", params={"limit": 1000}).json()
    assert len(body["rows"]) == 200
    assert body["total"] == 207


def test_list_trials_offset_past_the_end_is_empty_rows_not_404(tmp_path, monkeypatch):
    _fixture_db(tmp_path, monkeypatch)
    r = client.get("/api/trials", params={"offset": 500})
    assert r.status_code == 200
    assert r.json()["rows"] == []
    assert r.json()["total"] == 2  # still the full filtered count, so the UI can page back


def test_meta_facet_counts_equal_the_matching_trials_total(tmp_path, monkeypatch):
    """The filter dropdown labels options "Signal (721)" from these counts and then
    fetches the rows with the matching /api/trials filter -- if the two used different
    denominators the option would promise a row count it doesn't deliver."""
    _fixture_db(tmp_path, monkeypatch)
    conn = db.connect()
    _seed_bulk_trials(conn, 3)  # 3 more OTHER/PHASE1 trials, so facet counts differ
    conn.close()

    meta = client.get("/api/meta").json()
    assert meta["counts"] == client.get("/api/trials").json()["counts"]
    assert set(meta["filters"]) == {"severity", "changeType", "sponsorClass", "phase"}

    for facet, param in (
        ("severity", "severity"),
        ("changeType", "change_type"),
        ("sponsorClass", "sponsor_class"),
        ("phase", "phase"),
    ):
        options = meta["filters"][facet]
        assert [o["count"] for o in options] == sorted((o["count"] for o in options), reverse=True), facet
        for option in options:
            total = client.get("/api/trials", params={param: option["value"]}).json()["total"]
            assert total == option["count"], (facet, option)

    assert meta["filters"]["sponsorClass"][0] == {"value": "OTHER", "count": 3}  # count desc
    assert meta["filters"]["phase"][0]["value"] == "PHASE1"


def test_meta_facets_exclude_trials_with_no_headline_finding(tmp_path, monkeypatch):
    """NCT2 has no findings, so its severity/changeType come back NULL from the LEFT
    JOIN. NULL is not a filter a user can pick, so it must not appear as an option --
    while the trial itself still counts under sponsorClass/phase, which it does have."""
    _fixture_db(tmp_path, monkeypatch)
    meta = client.get("/api/meta").json()
    assert meta["filters"]["severity"] == [{"value": "SIGNAL", "count": 1}]
    assert meta["filters"]["changeType"] == [{"value": "PRIMARY_REPLACED", "count": 1}]
    assert {o["value"] for o in meta["filters"]["sponsorClass"]} == {"NIH", "INDUSTRY"}


def test_meta_case_studies_are_the_five_widest_signal_rows(tmp_path, monkeypatch):
    """Top 5 SIGNAL rows by |days after primary completion| desc, NULLs last: the sign
    doesn't matter (a change 400 days before completion is as notable as one 400 days
    after), CONTEXT never qualifies however wide it is, and the rows are index rows --
    the notable-cases cards render rationale and adjudication straight from them."""
    _fixture_db(tmp_path, monkeypatch)
    conn = db.connect()
    for nct_id, fid, severity, dapc in [
        ("NCTC1", 101, "SIGNAL", 10),
        ("NCTC2", 102, "SIGNAL", -400),
        ("NCTC3", 103, "SIGNAL", 300),
        ("NCTC4", 104, "SIGNAL", None),
        ("NCTC5", 105, "SIGNAL", -200),
        ("NCTC6", 106, "SIGNAL", 100),
        ("NCTC7", 107, "CONTEXT", 9999),  # widest of all, and still not a case study
    ]:
        _seed_trial_with_finding(conn, nct_id, fid, severity, dapc)
    conn.close()

    cases = client.get("/api/meta").json()["caseStudies"]
    assert [c["nctId"] for c in cases] == ["NCTC2", "NCTC3", "NCTC5", "NCTC6", "NCT1"]
    assert all(c["severity"] == "SIGNAL" for c in cases)
    assert set(cases[0]) == set(client.get("/api/trials").json()["rows"][0])  # same row shape
    assert "rationale" in cases[0] and "adjudication" in cases[0]


def test_get_trial_detail_shape(tmp_path, monkeypatch):
    _fixture_db(tmp_path, monkeypatch)
    r = client.get("/api/trials/NCT1")
    assert r.status_code == 200
    d = r.json()
    assert d["nctId"] == "NCT1"
    assert d["sponsorClass"] == "NIH"
    assert d["conditions"] == ["COVID-19"]
    # anchors: earliest recorded start/pcd across fetched versions (ctcm.timeline.anchors)
    assert d["enrolmentStart"] == "2019-12-01"
    assert d["primaryCompletion"] == "2020-03-01"

    assert [v["version"] for v in d["versions"]] == [0, 1]
    v0, v1 = d["versions"]
    assert v0["primaryOutcomes"] == [{"measure": "Original measure", "description": None, "timeFrame": "Day 1"}]
    assert v0["secondaryOutcomes"][0]["measure"] == "Sec A"
    assert v1["primaryOutcomes"][0]["measure"] == "New measure"
    # no cached raw snapshot in this fixture (no data/cache/NCT1/*) -- degrades to None, not a 500
    assert v0["overallStatus"] is None
    assert d["briefTitle"] is None

    assert len(d["findings"]) == 1
    f = d["findings"][0]
    assert f["changeType"] == "PRIMARY_REPLACED"
    assert f["severity"] == "SIGNAL"
    assert f["adjudication"] is None
    # a/b are 1-indexed display versions: int-API v0/v1 -> a=1&b=2 (verified against
    # the live Record History "Compare" flow, see ctcm/api.py:_compare_url)
    assert f["compareUrl"] == "https://clinicaltrials.gov/study/NCT1?tab=history&a=1&b=2#version-content-panel"


def test_get_trial_headline_finding_matches_the_index_row(tmp_path, monkeypatch):
    """The trial view's finding banner reads headlineFinding and must never re-rank
    client-side: this is the same finding the index row shows for the trial, so the
    two views cannot disagree about which change is the headline."""
    _fixture_db(tmp_path, monkeypatch)
    detail = client.get("/api/trials/NCT1").json()
    hf = detail["headlineFinding"]
    assert set(hf) == {
        "severity",
        "changeType",
        "daysAfterEnrolment",
        "daysAfterPrimaryCompletion",
        "rationale",
        "adjudication",
        "fromVersion",
        "toVersion",
    }
    assert (hf["severity"], hf["changeType"]) == ("SIGNAL", "PRIMARY_REPLACED")
    assert (hf["fromVersion"], hf["toVersion"]) == (0, 1)
    assert hf["daysAfterEnrolment"] == 32
    assert hf["rationale"].startswith("PRIMARY_REPLACED:")
    assert hf["adjudication"] is None

    index_row = next(row for row in client.get("/api/trials").json()["rows"] if row["nctId"] == "NCT1")
    assert {k: hf[k] for k in ("severity", "changeType", "daysAfterEnrolment", "daysAfterPrimaryCompletion", "rationale")} == {
        k: index_row[k] for k in ("severity", "changeType", "daysAfterEnrolment", "daysAfterPrimaryCompletion", "rationale")
    }


def test_get_trial_headline_finding_is_ranked_not_just_the_first_finding(tmp_path, monkeypatch):
    """findings[] is ordered by version then finding_id, which is not the headline
    order: here the low-id CONTEXT finding comes first in that list, but SIGNAL
    outranks it (_headline_key's first level). Serving findings[0] would banner the
    wrong change."""
    _fixture_db(tmp_path, monkeypatch)
    conn = db.connect()
    _seed_trial_with_finding(conn, "NCTH1", 200, "CONTEXT", 5)
    conn.execute(
        "INSERT INTO findings(finding_id, nct_id, from_version, to_version, change_type, severity, before_measure, "
        "after_measure, days_after_enrolment, days_after_primary_completion, confidence, resolved_by, rationale) "
        "VALUES (201, 'NCTH1', 1, 2, 'PRIMARY_REMOVED', 'SIGNAL', 'b', NULL, 9, 1, 1.0, 'T0', 'r')"
    )
    conn.commit()
    conn.close()

    detail = client.get("/api/trials/NCTH1").json()
    assert detail["findings"][0]["severity"] == "CONTEXT"  # the list order, unchanged
    assert detail["headlineFinding"]["changeType"] == "PRIMARY_REMOVED"
    assert detail["headlineFinding"]["toVersion"] == 2


def test_get_trial_headline_finding_is_null_without_findings(tmp_path, monkeypatch):
    _fixture_db(tmp_path, monkeypatch)
    detail = client.get("/api/trials/NCT2").json()
    assert detail["findings"] == []
    assert detail["headlineFinding"] is None


def test_get_trial_404_on_unknown_nct(tmp_path, monkeypatch):
    _fixture_db(tmp_path, monkeypatch)
    r = client.get("/api/trials/NCT_DOES_NOT_EXIST")
    assert r.status_code == 404


def test_works_without_adjudications_table(tmp_path, monkeypatch):
    """The common case today: no adjudications table exists at all yet."""
    _fixture_db(tmp_path, monkeypatch)
    conn = db.connect()
    tables = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    conn.close()
    assert "adjudications" not in tables

    detail = client.get("/api/trials/NCT1").json()
    assert detail["findings"][0]["adjudication"] is None
    index_row = next(row for row in client.get("/api/trials").json()["rows"] if row["nctId"] == "NCT1")
    assert index_row["adjudication"] is None


def _seed_adjudication(conn, chash, severity_confirmed, rationale, finding_id=999999, confidence=0.8):
    """finding_id defaults to a value that matches nothing in the fixture's findings
    table -- the whole point of the content-hash join is that finding_id is allowed
    to be stale/wrong, per the real ctcm/adjudicate.py schema (ADJUDICATIONS_SCHEMA)."""
    ensure_schema(conn)
    conn.execute(
        "INSERT INTO adjudications(content_hash, finding_id, nct_id, severity_confirmed, confidence, rationale, "
        "defence, prosecution, model, created_at) VALUES (?,?,?,?,?,?,?,?,?,?)",
        (chash, finding_id, "NCT1", severity_confirmed, confidence, rationale, "d", "p", "m", "2020-01-01"),
    )
    conn.commit()


def test_adjudication_joins_by_content_hash_despite_stale_finding_id(tmp_path, monkeypatch):
    """The real ctcm/adjudicate.py table is keyed by content_hash, not finding_id --
    pipeline re-runs delete+reinsert findings, renumbering finding_id on every run
    (by design), so a join against finding_id alone is a no-op on any corpus that's
    been re-run since the adjudication was written (confirmed: this is exactly
    what's wrong with the real data/ctcm.db right now). content_hash is computed
    from the finding's own content, so a stale finding_id doesn't break the join."""
    _fixture_db(tmp_path, monkeypatch)
    conn = db.connect()
    chash = content_hash("NCT1", 0, 1, "PRIMARY_REPLACED", "Original measure", "New measure")
    _seed_adjudication(conn, chash, "HIGH", "First sentence of the verdict. Second sentence with more detail.")
    conn.close()

    detail = client.get("/api/trials/NCT1").json()
    adj = detail["findings"][0]["adjudication"]
    assert adj is not None
    assert adj["concern"] == "HIGH"
    assert adj["confidence"] == 0.8
    assert adj["rationale"] == "First sentence of the verdict."  # excerpted to one sentence

    index_row = next(row for row in client.get("/api/trials").json()["rows"] if row["nctId"] == "NCT1")
    assert index_row["adjudication"] == adj


def test_adjudication_unreviewed_is_suppressed(tmp_path, monkeypatch):
    """UNREVIEWED means the defence/prosecution/judge chain itself failed (LLM
    timeout, malformed JSON, ...) -- adjudicate.py stores an internal error string
    as the "rationale" in that case, not a judged concern level. That's not fit to
    show as if it were a real verdict, so it's treated the same as no adjudication."""
    _fixture_db(tmp_path, monkeypatch)
    conn = db.connect()
    chash = content_hash("NCT1", 0, 1, "PRIMARY_REPLACED", "Original measure", "New measure")
    _seed_adjudication(conn, chash, "UNREVIEWED", "adjudication failed: judge call failed: timed out", confidence=0.0)
    conn.close()

    detail = client.get("/api/trials/NCT1").json()
    assert detail["findings"][0]["adjudication"] is None


def test_get_trial_publications_empty_when_no_publications_table(tmp_path, monkeypatch):
    """Common case for a fixture (or a not-yet-run-publink corpus): no publications
    table exists at all -- degrades to an empty list, not a 500."""
    _fixture_db(tmp_path, monkeypatch)
    detail = client.get("/api/trials/NCT1").json()
    assert detail["publications"] == []


def test_get_trial_publications_shape_tier_order_and_timing_note(tmp_path, monkeypatch):
    """NCT1's fixture SIGNAL finding (v0->v1) has to_version=1, version_date
    2020-02-01 -- the trial's only SIGNAL finding, so that's the reference change
    date. A LOW-tier pub before that date gets no timing note; a HIGH-tier pub after
    it does, and rows are ordered HIGH before LOW regardless of insertion order."""
    _fixture_db(tmp_path, monkeypatch)
    conn = db.connect()
    upsert_publications(
        conn, "NCT1",
        [
            {
                "pmid": "222", "doi": None, "title": "Early mention", "journal": "J Noise", "pub_date": "2020-01-15",
                "oa": 0, "tier": "LOW", "source": "epmc_fulltext",
            },
            {
                "pmid": "111", "doi": "10.1/x", "title": "The trial report", "journal": "NEJM", "pub_date": "2020-06-01",
                "oa": 1, "tier": "HIGH", "source": "epmc_abstract+pubmed_si",
            },
        ],
    )
    conn.close()

    pubs = client.get("/api/trials/NCT1").json()["publications"]
    assert [p["pmid"] for p in pubs] == ["111", "222"]  # HIGH before LOW

    high = pubs[0]
    assert high["tier"] == "HIGH"
    assert high["title"] == "The trial report"
    assert high["oa"] is True
    assert high["daysAfterSignalChange"] == 121  # 2020-06-01 minus 2020-02-01
    assert high["timingNote"] == "published 121 days after the primary outcome changed"

    low = pubs[1]
    assert low["tier"] == "LOW"
    assert low["oa"] is False
    assert low["daysAfterSignalChange"] is None  # predates the change -- no "after" claim
    assert low["timingNote"] is None


def test_get_trial_publications_no_timing_note_without_a_signal_finding(tmp_path, monkeypatch):
    _fixture_db(tmp_path, monkeypatch)
    conn = db.connect()
    upsert_publications(
        conn, "NCT2",
        [{"pmid": "1", "doi": None, "title": "T", "journal": None, "pub_date": "2099-01-01", "oa": None, "tier": "MEDIUM", "source": "epmc_abstract"}],
    )
    conn.close()

    pubs = client.get("/api/trials/NCT2").json()["publications"]
    assert pubs[0]["daysAfterSignalChange"] is None
    assert pubs[0]["timingNote"] is None
    assert pubs[0]["oa"] is None


def test_signal_change_date_matches_headline_finding_sql_on_secondary_tiebreak(tmp_path, monkeypatch):
    """Regression for task13-review.md Important #2: _signal_change_date must use
    the same 4-level tie-break as HEADLINE_FINDING_SQL (severity, |days after
    primary completion| desc, |days after enrolment| desc, finding_id asc) --  not
    stop at the second level. Two SIGNAL findings tie on |daysAfterPrimaryCompletion|
    but differ on |daysAfterEnrolment|: finding A (to_version=1, lower finding_id,
    sorts first in the findings list) has the SMALLER enrolment magnitude; finding B
    (to_version=2, higher finding_id) has the larger one. Only a tie-break that
    actually consults days_after_enrolment picks B -- a naive "first max on a tie"
    implementation (the pre-fix code) would silently pick A instead."""
    monkeypatch.setattr(config, "DATA_DIR", tmp_path)
    monkeypatch.setattr(config, "CACHE_DIR", tmp_path / "cache")
    monkeypatch.setattr(config, "DB_PATH", tmp_path / "test.db")
    conn = db.connect()
    conn.execute(
        "INSERT INTO trials(nct_id, study_type, phase, overall_status, lead_sponsor, sponsor_class, "
        "conditions, enrolment_count, first_posted_date, version_count) VALUES "
        "('NCT9', 'INTERVENTIONAL', 'PHASE3', 'COMPLETED', 'S', 'NIH', '[]', 10, '2020-01-01', 3)"
    )
    for v, d in [(0, "2020-01-01"), (1, "2020-03-01"), (2, "2020-05-01")]:
        conn.execute(
            "INSERT INTO versions(nct_id, version_no, version_date, module_labels) VALUES ('NCT9', ?, ?, '[]')", (v, d)
        )
    conn.execute(
        "INSERT INTO findings(finding_id, nct_id, from_version, to_version, change_type, severity, before_measure, "
        "after_measure, days_after_enrolment, days_after_primary_completion, confidence, resolved_by, rationale) "
        "VALUES (10, 'NCT9', 0, 1, 'PRIMARY_REPLACED', 'SIGNAL', 'a', 'b', 10, 100, 1.0, 'T0', 'r')"
    )
    conn.execute(
        "INSERT INTO findings(finding_id, nct_id, from_version, to_version, change_type, severity, before_measure, "
        "after_measure, days_after_enrolment, days_after_primary_completion, confidence, resolved_by, rationale) "
        "VALUES (11, 'NCT9', 0, 2, 'PRIMARY_REPLACED', 'SIGNAL', 'a', 'c', 50, 100, 1.0, 'T0', 'r')"
    )
    conn.commit()
    upsert_publications(
        conn, "NCT9",
        [{"pmid": "1", "doi": None, "title": "T", "journal": None, "pub_date": "2020-06-01", "oa": None, "tier": "HIGH", "source": "pubmed_si"}],
    )
    conn.close()

    pub = client.get("/api/trials/NCT9").json()["publications"][0]
    # correct headline finding is B (v0->v2): 2020-06-01 minus v2's 2020-05-01 = 31 days.
    # the pre-fix bug would resolve to v1's 2020-03-01 instead (92 days) -- a decisively
    # different, wrong answer, not just an off-by-one.
    assert pub["daysAfterSignalChange"] == 31
    assert pub["timingNote"] == "published 31 days after the primary outcome changed"


# ---------------------------------------------------------------------------
# /api/export.csv
# ---------------------------------------------------------------------------


def _export(**params):
    """The export parsed the way a reviewer's tool would parse it -- through
    csv.reader, not str.split(','), which is the whole point of the quoting tests
    below."""
    r = client.get("/api/export.csv", params=params)
    assert r.status_code == 200
    return r, list(csv.reader(io.StringIO(r.text)))


def test_export_csv_header_row_and_index_row_contents(tmp_path, monkeypatch):
    """The header is a contract someone's ingest script columns off, so it's asserted
    exactly, in order. Body rows are the index rows: a finding-less trial exports with
    empty finding cells rather than dropping out of the export."""
    _fixture_db(tmp_path, monkeypatch)
    _, rows = _export()

    assert tuple(rows[0]) == EXPORT_COLUMNS
    assert len(rows) == 1 + client.get("/api/trials").json()["total"]

    nct1 = next(r for r in rows[1:] if r[0] == "NCT1")
    assert nct1 == [
        "NCT1", "Alpha Sponsor", "NIH", "PHASE3", "COVID-19", "COMPLETED",
        "SIGNAL", "PRIMARY_REPLACED", "32", "-30", "0", "1",
        "PRIMARY_REPLACED: 'Original measure' -> 'New measure' (32 days after enrolment, v0->v1).",
        "", "",  # no adjudications table in this fixture
        "https://clinicaltrials.gov/study/NCT1?tab=history",
    ]

    nct2 = next(r for r in rows[1:] if r[0] == "NCT2")
    assert nct2[6:12] == ["", "", "", "", "", ""]  # severity..to_version all empty, row still exported


def test_export_csv_quotes_commas_and_quotes_in_sponsor_names(tmp_path, monkeypatch):
    """Real lead_sponsor values contain commas ("National Heart, Lung, and Blood
    Institute") -- a naive join would shift every later column of that row by one.
    Embedded double quotes are the second half of the same trap."""
    _fixture_db(tmp_path, monkeypatch)
    nasty = 'Smith, "Jones" & Co, Ltd.'
    conn = db.connect()
    conn.execute(
        "INSERT INTO trials(nct_id, study_type, phase, overall_status, lead_sponsor, sponsor_class, "
        "conditions, enrolment_count, first_posted_date, version_count) VALUES "
        "('NCTQ1', 'INTERVENTIONAL', 'PHASE3', 'COMPLETED', ?, 'INDUSTRY', "
        "'[\"Heart Failure\", \"Diabetes\"]', 10, '2020-01-01', 1)",
        (nasty,),
    )
    conn.commit()
    conn.close()

    _, rows = _export()
    row = next(r for r in rows[1:] if r[0] == "NCTQ1")
    assert row[1] == nasty  # survives the round-trip through csv.reader intact
    assert len(row) == len(EXPORT_COLUMNS)  # ...without shifting any later column
    assert row[4] == "Heart Failure; Diabetes"  # the conditions array as one cell


def test_export_csv_applies_the_same_filters_as_the_index(tmp_path, monkeypatch):
    """An export taken from a filtered screen must contain that screen's rows and no
    others -- same WHERE builder, so severity=SIGNAL cannot leak a CONTEXT row."""
    _fixture_db(tmp_path, monkeypatch)
    conn = db.connect()
    _seed_trial_with_finding(conn, "NCTX1", 401, "CONTEXT", 5)
    _seed_trial_with_finding(conn, "NCTX2", 402, "SIGNAL", 5)
    conn.close()

    _, rows = _export(severity="SIGNAL")
    severities = {r[6] for r in rows[1:]}
    assert severities == {"SIGNAL"}
    assert {r[0] for r in rows[1:]} == {"NCT1", "NCTX2"}
    assert len(rows) - 1 == client.get("/api/trials", params={"severity": "SIGNAL"}).json()["total"]

    # ...and the export is the whole filtered set, not one page of it
    _, all_rows = _export()
    assert len(all_rows) - 1 == 4

    assert [r[0] for r in _export(sort="nctId")[1][1:]] == ["NCT1", "NCT2", "NCTX1", "NCTX2"]
    assert client.get("/api/export.csv", params={"sort": "nonsense"}).status_code == 400


def test_export_csv_download_headers(tmp_path, monkeypatch):
    _fixture_db(tmp_path, monkeypatch)
    r, _ = _export()
    assert r.headers["content-type"].startswith("text/csv")
    assert r.headers["content-disposition"] == 'attachment; filename="redline-trials.csv"'


# ---------------------------------------------------------------------------
# /api/changes
# ---------------------------------------------------------------------------


def _stamp(conn, stamps: dict[int, str]):
    """findings.first_seen_at only exists after ctcm.monitor.ensure_schema's guarded
    ALTER, so the stamping helper does what a monitor pass does: add the column, then
    write the discovery timestamps. Everything it doesn't touch stays NULL -- the
    historical-finding state the real corpus is entirely in."""
    monitor.ensure_schema(conn)
    for finding_id, ts in stamps.items():
        conn.execute("UPDATE findings SET first_seen_at=? WHERE finding_id=?", (ts, finding_id))
    conn.commit()


def test_changes_is_empty_not_an_error_before_any_monitor_pass(tmp_path, monkeypatch):
    """The state of the real corpus today: findings exist, none was discovered by a
    monitor pass (and the column itself may not exist yet). The feed is honestly
    empty -- it must not 500 on the missing column, nor invent dates for the
    historical findings."""
    _fixture_db(tmp_path, monkeypatch)
    r = client.get("/api/changes")
    assert r.status_code == 200
    assert r.json() == {"rows": [], "total": 0, "asOf": None}

    # column present, still nothing stamped -- same answer
    conn = db.connect()
    _stamp(conn, {})
    conn.close()
    assert client.get("/api/changes").json() == {"rows": [], "total": 0, "asOf": None}


def test_changes_orders_newest_first_and_excludes_historical_findings(tmp_path, monkeypatch):
    """first_seen_at DESC, then finding_id DESC for same-timestamp ties (a monitor pass
    stamps everything it discovers with one `now`, so ties are the normal case, not the
    edge case). NCT1's fixture finding is historical (NULL) and never appears."""
    _fixture_db(tmp_path, monkeypatch)
    conn = db.connect()
    for nct_id, fid, severity in [("NCTM1", 301, "SIGNAL"), ("NCTM2", 302, "CONTEXT"), ("NCTM3", 303, "SIGNAL")]:
        _seed_trial_with_finding(conn, nct_id, fid, severity, 5)
    _stamp(conn, {301: "2026-01-02T00:00:00Z", 302: "2026-01-03T00:00:00Z", 303: "2026-01-03T00:00:00Z"})
    conn.close()

    body = client.get("/api/changes").json()
    assert [r["nctId"] for r in body["rows"]] == ["NCTM3", "NCTM2", "NCTM1"]
    assert body["total"] == 3
    assert body["asOf"] == "2026-01-03T00:00:00Z"  # newest stamp, not the newest page row

    row = body["rows"][0]
    assert set(row) == {
        "nctId", "sponsor", "conditions", "changeType", "severity",
        "fromVersion", "toVersion", "firstSeenAt", "compareUrl",
    }
    assert (row["severity"], row["changeType"]) == ("SIGNAL", "PRIMARY_REPLACED")
    assert row["sponsor"] == "Pager Sponsor"
    assert row["conditions"] == []
    assert row["firstSeenAt"] == "2026-01-03T00:00:00Z"
    assert row["compareUrl"] == "https://clinicaltrials.gov/study/NCTM3?tab=history&a=1&b=2#version-content-panel"


def test_changes_pages_with_a_clamped_limit(tmp_path, monkeypatch):
    """Same clamp contract as /api/trials: default 50, hard cap 200, offset never
    negative -- and `total` stays the full count of timestamped findings on every page
    so a client knows how far back the feed goes."""
    _fixture_db(tmp_path, monkeypatch)
    conn = db.connect()
    conn.executemany(_TRIAL_INSERT, [(f"NCTM{i:04d}",) for i in range(205)])
    conn.executemany(_FINDING_INSERT, [(1000 + i, f"NCTM{i:04d}", "SIGNAL", 5) for i in range(205)])
    conn.commit()
    _stamp(conn, {1000 + i: "2026-02-01T00:00:00Z" for i in range(205)})
    conn.close()

    assert len(client.get("/api/changes").json()["rows"]) == 50

    body = client.get("/api/changes", params={"limit": 1000}).json()
    assert len(body["rows"]) == 200
    assert body["total"] == 205

    tail = client.get("/api/changes", params={"limit": 1000, "offset": 200}).json()
    assert len(tail["rows"]) == 5
    assert tail["total"] == 205
    assert not {r["nctId"] for r in tail["rows"]} & {r["nctId"] for r in body["rows"]}  # no overlap

    assert len(client.get("/api/changes", params={"limit": 0}).json()["rows"]) == 1  # clamps up, not a 422
    assert client.get("/api/changes", params={"offset": -5}).json()["rows"][0] == body["rows"][0]

    past_end = client.get("/api/changes", params={"offset": 5000}).json()
    assert past_end["rows"] == []
    assert (past_end["total"], past_end["asOf"]) == (205, "2026-02-01T00:00:00Z")


def test_results_linkage_absent_column_omits_count_and_nulls_flag(tmp_path, monkeypatch):
    """A db the reslink enrichment never touched has no results_linked column at all:
    counts omit strictGenuineTrials (rather than 500ing or reporting a fabricated 0),
    and every finding serializes resultsLinked as null."""
    _fixture_db(tmp_path, monkeypatch)
    counts = client.get("/api/trials").json()["counts"]
    assert "strictGenuineTrials" not in counts
    detail = client.get("/api/trials/NCT1").json()
    assert detail["findings"][0]["resultsLinked"] is None


def test_results_linkage_after_enrichment(tmp_path, monkeypatch):
    """After ctcm.reslink runs: findings carry resultsLinked and counts carry
    strictGenuineTrials. NCT1's fixture SIGNAL finding (32 days after enrolment, 30
    before primary completion) is the during-the-trial strict branch, so the trial
    counts as strict genuine regardless of the finding's own linkage value."""
    _fixture_db(tmp_path, monkeypatch)
    conn = db.connect()
    reslink_ensure_schema(conn)
    conn.execute("UPDATE findings SET results_linked=1 WHERE finding_id=1")
    conn.commit()
    conn.close()

    body = client.get("/api/trials").json()
    assert body["counts"]["strictGenuineTrials"] == 1
    assert client.get("/api/trials/NCT1").json()["findings"][0]["resultsLinked"] is True

    conn = db.connect()
    conn.execute("UPDATE findings SET results_linked=0 WHERE finding_id=1")
    conn.commit()
    conn.close()
    assert client.get("/api/trials/NCT1").json()["findings"][0]["resultsLinked"] is False
    assert client.get("/api/trials").json()["counts"]["strictGenuineTrials"] == 1  # during-trial branch: linkage irrelevant
