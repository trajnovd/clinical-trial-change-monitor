"""ctcm/api.py against a small fixture db (tmp_path, built via ctcm.db.connect() same
as test_pipeline.py) -- no network, no dependency on the real data/ctcm.db. Covers the
filtering/sort/q contract, the trial-detail shape, 404, and the two adjudications-table
states (absent -- the common case today -- and present with either key strategy)."""

from fastapi.testclient import TestClient

from ctcm import config, db
from ctcm.api import app

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
    assert body["counts"] == {"trials": 2, "signalTrials": 1, "postCompletionTrials": 0, "caveat": body["counts"]["caveat"]}
    assert "results-posted" in body["counts"]["caveat"]

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


def test_adjudications_natural_key_join(tmp_path, monkeypatch):
    """If adjudicate.py lands with a (nct_id, from_version, to_version, change_type)
    natural key instead of a finding_id FK, the join still finds it."""
    _fixture_db(tmp_path, monkeypatch)
    conn = db.connect()
    conn.execute(
        "CREATE TABLE adjudications(nct_id TEXT, from_version INT, to_version INT, change_type TEXT, "
        "concern TEXT, confidence REAL, rationale TEXT)"
    )
    conn.execute(
        "INSERT INTO adjudications VALUES ('NCT1', 0, 1, 'PRIMARY_REPLACED', 'moderate', 0.8, 'looks like a genuine switch')"
    )
    conn.commit()
    conn.close()

    detail = client.get("/api/trials/NCT1").json()
    adj = detail["findings"][0]["adjudication"]
    assert adj is not None
    assert adj["concern"] == "moderate"

    index_row = next(row for row in client.get("/api/trials").json()["rows"] if row["nctId"] == "NCT1")
    assert index_row["adjudication"]["concern"] == "moderate"


def test_adjudications_finding_id_join(tmp_path, monkeypatch):
    """If adjudicate.py instead uses a finding_id FK, that strategy is tried first."""
    _fixture_db(tmp_path, monkeypatch)
    conn = db.connect()
    conn.execute("CREATE TABLE adjudications(finding_id INTEGER, concern TEXT)")
    conn.execute("INSERT INTO adjudications VALUES (1, 'low')")
    conn.commit()
    conn.close()

    detail = client.get("/api/trials/NCT1").json()
    assert detail["findings"][0]["adjudication"]["concern"] == "low"
