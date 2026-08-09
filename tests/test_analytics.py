"""ctcm/analytics.py + the /api/analytics endpoint (Task 16), against a small fixture
db built the same way test_api.py builds one (tmp_path, ctcm.db.connect()). No network,
no dependency on the real data/ctcm.db."""

from fastapi.testclient import TestClient

from ctcm import analytics, config, db
from ctcm.adjudicate import content_hash, ensure_schema
from ctcm.api import app

client = TestClient(app)


def _trial(conn, nct_id, sponsor, sponsor_class, phase, conditions, first_posted):
    conn.execute(
        "INSERT INTO trials(nct_id, study_type, phase, overall_status, lead_sponsor, sponsor_class, "
        "conditions, enrolment_count, first_posted_date, version_count) VALUES "
        "(?, 'INTERVENTIONAL', ?, 'COMPLETED', ?, ?, ?, 100, ?, 1)",
        (nct_id, phase, sponsor, sponsor_class, conditions, first_posted),
    )


def _finding(conn, nct_id, severity, days_after_enrolment, finding_id, change_type="PRIMARY_REPLACED"):
    conn.execute(
        "INSERT INTO findings(finding_id, nct_id, from_version, to_version, change_type, severity, before_measure, "
        "after_measure, days_after_enrolment, days_after_primary_completion, confidence, resolved_by, rationale) "
        "VALUES (?, ?, 0, 1, ?, ?, 'a', 'b', ?, NULL, 1.0, 'T0', 'r')",
        (finding_id, nct_id, change_type, severity, days_after_enrolment),
    )


def _seed(conn):
    # Sponsor "Alpha" has exactly 5 trials (meets the >=5 floor), 2 of them SIGNAL.
    for i in range(5):
        _trial(conn, f"NCT-A{i}", "Alpha", "INDUSTRY", "PHASE3", '["Diabetes"]', "2019-01-01")
    _finding(conn, "NCT-A0", "SIGNAL", 10, 1)
    _finding(conn, "NCT-A1", "SIGNAL", 20, 2)

    # Sponsor "Beta" has only 4 trials -- below the floor, must not appear at all,
    # even though its rate (100%) would otherwise be the highest in the fixture.
    for i in range(4):
        _trial(conn, f"NCT-B{i}", "Beta", "NIH", "PHASE2", '["COVID-19"]', "2020-06-01")
    _finding(conn, "NCT-B0", "SIGNAL", 5, 3)
    _finding(conn, "NCT-B1", "SIGNAL", 5, 4)
    _finding(conn, "NCT-B2", "SIGNAL", 5, 5)
    _finding(conn, "NCT-B3", "SIGNAL", 5, 6)

    # One more trial, no findings at all -- exercises the zero-signal / zero-finding path,
    # and gives sponsor_class OTHER + phase NA + year 2021 each exactly one trial.
    _trial(conn, "NCT-C0", "Gamma", "OTHER", "NA", '["Diabetes", "Obesity"]', "2021-03-15")

    # Pre-enrolment CONTEXT finding on one of Alpha's trials -- must land in the "<0"
    # histogram bucket and must NOT count toward Alpha's signal_trials (severity != SIGNAL).
    _finding(conn, "NCT-A2", "CONTEXT", -15, 7, change_type="REWORDED")

    # One finding with a large positive days_after_enrolment -- exercises the ">1095" bucket.
    _finding(conn, "NCT-A3", "SIGNAL", 2000, 8)

    conn.commit()


def _fixture_db(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "DATA_DIR", tmp_path)
    monkeypatch.setattr(config, "CACHE_DIR", tmp_path / "cache")
    monkeypatch.setattr(config, "DB_PATH", tmp_path / "test.db")
    conn = db.connect()
    _seed(conn)
    conn.close()
    return conn


# ---------------------------------------------------------------------------
# signal_rate_by_sponsor: the >=5-trial floor and denominator honesty
# ---------------------------------------------------------------------------


def test_signal_rate_by_sponsor_applies_five_trial_floor(tmp_path, monkeypatch):
    _fixture_db(tmp_path, monkeypatch)
    conn = db.connect()
    rows = analytics.signal_rate_by_sponsor(conn)
    conn.close()

    sponsors = {r["sponsor"] for r in rows}
    assert "Alpha" in sponsors
    assert "Beta" not in sponsors  # only 4 trials -- below the floor
    assert "Gamma" not in sponsors  # only 1 trial


def test_signal_rate_by_sponsor_numerator_and_denominator(tmp_path, monkeypatch):
    _fixture_db(tmp_path, monkeypatch)
    conn = db.connect()
    rows = analytics.signal_rate_by_sponsor(conn)
    conn.close()

    alpha = next(r for r in rows if r["sponsor"] == "Alpha")
    # 5 trials total; SIGNAL findings land on A0, A1, A3 (A2's is CONTEXT, doesn't count).
    assert alpha["totalTrials"] == 5
    assert alpha["signalTrials"] == 3
    assert alpha["rate"] == 0.6


# ---------------------------------------------------------------------------
# by_sponsor_class / by_phase / by_year: same rate-table shape, no floor
# ---------------------------------------------------------------------------


def test_by_sponsor_class_has_no_floor_and_every_row_has_a_denominator(tmp_path, monkeypatch):
    _fixture_db(tmp_path, monkeypatch)
    conn = db.connect()
    rows = analytics.by_sponsor_class(conn)
    conn.close()

    by_class = {r["sponsorClass"]: r for r in rows}
    assert by_class["OTHER"]["totalTrials"] == 1  # single-trial group still shows up (no floor here)
    for r in rows:
        assert r["totalTrials"] > 0
        assert 0.0 <= r["rate"] <= 1.0
        assert set(r) == {"sponsorClass", "signalTrials", "totalTrials", "rate", "lowN"}


def test_by_sponsor_class_flags_low_n_instead_of_dropping_the_row(tmp_path, monkeypatch):
    """Fix round 1 (task16-review.md Important-1): no hard floor here (dropping "INDIV"
    or a real early year would hide data), but a group below MIN_SPONSOR_TRIALS must be
    marked, not presented at the same confidence as a real-sample row."""
    _fixture_db(tmp_path, monkeypatch)
    conn = db.connect()
    rows = analytics.by_sponsor_class(conn)
    conn.close()

    by_class = {r["sponsorClass"]: r for r in rows}
    assert by_class["INDUSTRY"]["totalTrials"] == 5
    assert by_class["INDUSTRY"]["lowN"] is False
    assert by_class["NIH"]["totalTrials"] == 4
    assert by_class["NIH"]["lowN"] is True
    assert by_class["OTHER"]["totalTrials"] == 1
    assert by_class["OTHER"]["lowN"] is True


def test_by_phase_groups_correctly(tmp_path, monkeypatch):
    _fixture_db(tmp_path, monkeypatch)
    conn = db.connect()
    rows = analytics.by_phase(conn)
    conn.close()

    by_phase = {r["phase"]: r for r in rows}
    assert by_phase["PHASE3"]["totalTrials"] == 5
    assert by_phase["PHASE2"]["totalTrials"] == 4
    assert by_phase["NA"]["totalTrials"] == 1


def test_by_year_is_chronological_not_rate_sorted(tmp_path, monkeypatch):
    _fixture_db(tmp_path, monkeypatch)
    conn = db.connect()
    rows = analytics.by_year(conn)
    conn.close()

    assert [r["year"] for r in rows] == sorted(r["year"] for r in rows)
    assert {r["year"] for r in rows} == {"2019", "2020", "2021"}


# ---------------------------------------------------------------------------
# CTIS exclusion (fix round 1, task16-review.md Critical-2)
# ---------------------------------------------------------------------------


def _seed_ctis_trial(conn):
    """Same shape as ctcm.ctis's own inserts (version_count=1, so it can never have a
    finding) -- added via raw SQL rather than importing ctcm.ctis, which is a
    concurrently-edited file this task doesn't own."""
    conn.execute("ALTER TABLE trials ADD COLUMN registry TEXT NOT NULL DEFAULT 'ctgov'")
    conn.execute(
        "INSERT INTO trials(nct_id, study_type, phase, overall_status, lead_sponsor, sponsor_class, "
        "conditions, enrolment_count, first_posted_date, version_count, registry) VALUES "
        "('2025-500000-11-00', 'INTERVENTIONAL', 'PHASE3', 'Authorised', 'Alpha', 'Pharmaceutical company', "
        "'[\"Diabetes\"]', 10, '2026-01-01', 1, 'ctis')"
    )
    conn.commit()


def test_ctis_trial_excluded_from_every_trials_table_aggregate(tmp_path, monkeypatch):
    """A registry='ctis' trial is prospective-only and structurally can't have a
    finding -- it must not dilute a denominator, appear as a spurious 0%-rate row of
    its own vocabulary, or fabricate a chronologically-misleading byYear row."""
    _fixture_db(tmp_path, monkeypatch)
    conn = db.connect()
    _seed_ctis_trial(conn)
    conn.close()

    conn = db.connect()
    by_sponsor = analytics.signal_rate_by_sponsor(conn)
    by_class = analytics.by_sponsor_class(conn)
    by_phase_rows = analytics.by_phase(conn)
    by_year_rows = analytics.by_year(conn)
    conditions = analytics.top_conditions(conn)
    excluded = analytics.excluded_ctis_count(conn)
    conn.close()

    alpha = next(r for r in by_sponsor if r["sponsor"] == "Alpha")
    assert alpha["totalTrials"] == 5  # unmoved by the CTIS trial sharing its sponsor name
    assert "Pharmaceutical company" not in {r["sponsorClass"] for r in by_class}
    assert next(r for r in by_phase_rows if r["phase"] == "PHASE3")["totalTrials"] == 5
    assert "2026" not in {r["year"] for r in by_year_rows}
    diabetes = next(r for r in conditions if r["condition"] == "Diabetes")
    assert diabetes["totalTrials"] == 6  # 5 Alpha + NCT-C0, not 7
    assert excluded == 1


def test_excluded_ctis_count_zero_when_registry_column_absent(tmp_path, monkeypatch):
    """The common case today: a fixture (or any corpus) that predates ctcm.ctis's
    ALTER TABLE has no `registry` column at all -- every trial is ctgov by
    construction, nothing to exclude, no crash on the missing column."""
    _fixture_db(tmp_path, monkeypatch)
    conn = db.connect()
    assert "registry" not in {r["name"] for r in conn.execute("PRAGMA table_info(trials)")}
    assert analytics.excluded_ctis_count(conn) == 0
    conn.close()


# ---------------------------------------------------------------------------
# Sponsor drill-down parity (fix round 1, task16-review.md Critical-1)
# ---------------------------------------------------------------------------


def test_sponsor_drilldown_parity_with_analytics_row(tmp_path, monkeypatch):
    """The analytics UI's drill-down promises "click this row to see the trials
    behind it" -- /api/trials?sponsor=<name> must return exactly the row's own
    totalTrials/signalTrials, not a substring-matched superset. "Alpha Pharma"
    contains "Alpha" as a substring -- proves sponsor= isn't reusing q='s LIKE."""
    _fixture_db(tmp_path, monkeypatch)
    conn = db.connect()
    for i in range(5):
        _trial(conn, f"NCT-AP{i}", "Alpha Pharma", "INDUSTRY", "PHASE2", "[]", "2022-01-01")
    _finding(conn, "NCT-AP0", "SIGNAL", 5, 50)
    conn.commit()
    conn.close()

    by_sponsor = {r["sponsor"]: r for r in client.get("/api/analytics").json()["signalRateBySponsor"]}
    assert {"Alpha", "Alpha Pharma"} <= set(by_sponsor)

    for name in ("Alpha", "Alpha Pharma"):
        row = by_sponsor[name]
        drilldown = client.get("/api/trials", params={"sponsor": name}).json()
        assert drilldown["total"] == row["totalTrials"], name
        signal_count = sum(1 for t in drilldown["rows"] if t["severity"] == "SIGNAL")
        assert signal_count == row["signalTrials"], name


# ---------------------------------------------------------------------------
# top_conditions
# ---------------------------------------------------------------------------


def test_top_conditions_counts_trials_and_signal_trials(tmp_path, monkeypatch):
    _fixture_db(tmp_path, monkeypatch)
    conn = db.connect()
    rows = analytics.top_conditions(conn)
    conn.close()

    by_condition = {r["condition"]: r for r in rows}
    # Diabetes: 5 Alpha trials + NCT-C0 = 6 total; 3 of Alpha's are SIGNAL.
    assert by_condition["Diabetes"]["totalTrials"] == 6
    assert by_condition["Diabetes"]["signalTrials"] == 3
    assert by_condition["COVID-19"]["totalTrials"] == 4
    assert by_condition["Obesity"]["totalTrials"] == 1


def test_top_conditions_respects_limit(tmp_path, monkeypatch):
    _fixture_db(tmp_path, monkeypatch)
    conn = db.connect()
    rows = analytics.top_conditions(conn, limit=2)
    conn.close()
    assert len(rows) == 2
    assert rows[0]["totalTrials"] >= rows[1]["totalTrials"]  # ranked by trial count desc


# ---------------------------------------------------------------------------
# timing_histogram: bucket edges
# ---------------------------------------------------------------------------


def test_timing_histogram_bucket_edges(tmp_path, monkeypatch):
    _fixture_db(tmp_path, monkeypatch)
    conn = db.connect()
    rows = analytics.timing_histogram(conn)
    conn.close()

    by_bucket = {r["bucket"]: r["count"] for r in rows}
    total = rows[0]["total"]
    # Findings with non-null days_after_enrolment: A0=10, A1=20, B0..B3=5 (x4), A2=-15, A3=2000 = 8
    assert total == 8
    assert by_bucket["<0"] == 1  # A2's -15
    assert by_bucket["0-90"] == 6  # A0=10, A1=20, B0..B3=5
    assert by_bucket["91-365"] == 0
    assert by_bucket["366-1095"] == 0
    assert by_bucket[">1095"] == 1  # A3's 2000
    assert sum(by_bucket.values()) == total  # every finding lands in exactly one bucket
    for r in rows:
        assert r["total"] == total  # denominator is the same across every row
        assert r["rate"] == round(r["count"] / total, 4)  # every row carries a rate, not just count/total


def test_timing_histogram_boundary_values_land_in_the_inclusive_bucket(tmp_path, monkeypatch):
    """0, 90, 91, 365, 366, 1095 are all bucket edges -- confirm the BETWEEN clauses
    are inclusive on both ends and don't double-count or drop a boundary day."""
    monkeypatch.setattr(config, "DATA_DIR", tmp_path)
    monkeypatch.setattr(config, "CACHE_DIR", tmp_path / "cache")
    monkeypatch.setattr(config, "DB_PATH", tmp_path / "test.db")
    conn = db.connect()
    _trial(conn, "NCT-X", "X", "INDUSTRY", "PHASE1", "[]", "2020-01-01")
    for i, days in enumerate([0, 90, 91, 365, 366, 1095]):
        _finding(conn, "NCT-X", "SIGNAL", days, 100 + i)
    conn.commit()
    conn.close()

    conn = db.connect()
    rows = analytics.timing_histogram(conn)
    conn.close()
    by_bucket = {r["bucket"]: r["count"] for r in rows}
    assert by_bucket["0-90"] == 2  # 0, 90
    assert by_bucket["91-365"] == 2  # 91, 365
    assert by_bucket["366-1095"] == 2  # 366, 1095


# ---------------------------------------------------------------------------
# adjudication_concern_mix: absent-table path + real distribution
# ---------------------------------------------------------------------------


def test_adjudication_concern_mix_empty_when_table_absent(tmp_path, monkeypatch):
    _fixture_db(tmp_path, monkeypatch)
    conn = db.connect()
    tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert "adjudications" not in tables
    assert analytics.adjudication_concern_mix(conn) == []
    conn.close()


def test_adjudication_concern_mix_excludes_unreviewed(tmp_path, monkeypatch):
    _fixture_db(tmp_path, monkeypatch)
    conn = db.connect()
    ensure_schema(conn)
    rows = [
        (content_hash("NCT-A0", 0, 1, "PRIMARY_REPLACED", "a", "b"), "HIGH"),
        (content_hash("NCT-A1", 0, 1, "PRIMARY_REPLACED", "a", "b"), "LOW"),
        (content_hash("NCT-A3", 0, 1, "PRIMARY_REPLACED", "a", "b"), "LOW"),
        (content_hash("NCT-B0", 0, 1, "PRIMARY_REPLACED", "a", "b"), "UNREVIEWED"),
    ]
    for chash, concern in rows:
        conn.execute(
            "INSERT INTO adjudications(content_hash, finding_id, nct_id, severity_confirmed, confidence, rationale, "
            "defence, prosecution, model, created_at) VALUES (?,?,?,?,?,?,?,?,?,?)",
            (chash, 1, "NCT-A0", concern, 0.8, "r", "d", "p", "m", "2020-01-01"),
        )
    conn.commit()
    conn.close()

    conn = db.connect()
    mix = analytics.adjudication_concern_mix(conn)
    conn.close()

    by_concern = {r["concern"]: r for r in mix}
    assert set(by_concern) == {"HIGH", "LOW"}  # UNREVIEWED excluded entirely
    assert by_concern["LOW"]["count"] == 2
    assert by_concern["HIGH"]["count"] == 1
    assert by_concern["LOW"]["total"] == 3  # denominator excludes UNREVIEWED too
    assert by_concern["HIGH"]["total"] == 3
    assert by_concern["LOW"]["rate"] == round(2 / 3, 4)
    # ordinal order (LOW, MODERATE, HIGH), not insertion/alphabetical
    assert [r["concern"] for r in mix] == ["LOW", "HIGH"]


# ---------------------------------------------------------------------------
# /api/analytics: payload shape, caveat, and the absent-adjudications path end to end
# ---------------------------------------------------------------------------


def test_api_analytics_payload_shape(tmp_path, monkeypatch):
    _fixture_db(tmp_path, monkeypatch)
    r = client.get("/api/analytics")
    assert r.status_code == 200
    body = r.json()

    assert set(body) == {
        "signalRateBySponsor", "bySponsorClass", "byPhase", "byYear", "topConditions",
        "timingHistogram", "adjudicationConcernMix", "ctisExcludedCount", "caveat",
    }
    assert "completed trials that posted results" in body["caveat"]
    assert body["adjudicationConcernMix"] == []  # no adjudications table in this fixture
    assert body["ctisExcludedCount"] == 0  # no registry column in this fixture -- nothing to exclude

    assert {r["sponsor"] for r in body["signalRateBySponsor"]} == {"Alpha"}
    alpha = body["signalRateBySponsor"][0]
    assert alpha["signalTrials"] == 3
    assert alpha["totalTrials"] == 5

    bucket_labels = [r["bucket"] for r in body["timingHistogram"]]
    assert bucket_labels == ["<0", "0-90", "91-365", "366-1095", ">1095"]
