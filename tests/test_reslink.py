"""ctcm/reslink.py: the pure linkage rules (version window, ±90d date proximity,
the three-valued combine), the guarded results_linked ALTER, the cache-driven
enrichment pass (fixture db + fake data/cache, same _patch_config shape as
test_monitor.py), and the strict-genuine trial count."""

import gzip
import json

from ctcm import config, db, reslink


def _patch_config(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "DATA_DIR", tmp_path)
    monkeypatch.setattr(config, "CACHE_DIR", tmp_path / "cache")
    monkeypatch.setattr(config, "DB_PATH", tmp_path / "test.db")


def _write_cache(nct, changes, snapshots):
    """changes: history.json's list. snapshots: {version_no: statusModule dict}."""
    trial_dir = config.CACHE_DIR / nct
    trial_dir.mkdir(parents=True, exist_ok=True)
    (trial_dir / "history.json").write_text(json.dumps({"changes": changes}))
    for version_no, status_module in snapshots.items():
        raw = {"study": {"protocolSection": {"statusModule": status_module}}}
        (trial_dir / f"v{version_no}.json.gz").write_bytes(gzip.compress(json.dumps(raw).encode()))


def _seed_trial(conn, nct, versions, findings):
    """versions: [(version_no, version_date)]. findings: [(finding_id, from_v, to_v,
    change_type, severity, days_after_enrolment, days_after_primary_completion)]."""
    conn.execute(
        "INSERT INTO trials(nct_id, study_type, phase, overall_status, lead_sponsor, sponsor_class, "
        "conditions, enrolment_count, first_posted_date, version_count) "
        "VALUES (?, 'INTERVENTIONAL', 'PHASE3', 'COMPLETED', 'S', 'NIH', '[]', 10, '2020-01-01', ?)",
        (nct, len(versions)),
    )
    for v, d in versions:
        conn.execute(
            "INSERT INTO versions(nct_id, version_no, version_date, module_labels) VALUES (?, ?, ?, '[]')", (nct, v, d)
        )
    for fid, from_v, to_v, change_type, severity, dae, dapc in findings:
        conn.execute(
            "INSERT INTO findings(finding_id, nct_id, from_version, to_version, change_type, severity, "
            "before_measure, after_measure, days_after_enrolment, days_after_primary_completion, confidence, "
            "resolved_by, rationale) VALUES (?, ?, ?, ?, ?, ?, 'a', 'b', ?, ?, 1.0, 'T0', 'r')",
            (fid, nct, from_v, to_v, change_type, severity, dae, dapc),
        )
    conn.commit()


# ---- pure rules ----------------------------------------------------------------------


def test_first_results_version_picks_lowest_matching_version():
    changes = [
        {"version": 3, "moduleLabels": ["Study Results"]},  # contains "Results"
        {"version": 0, "moduleLabels": ["Outcome Measures"]},
        {"version": 2, "moduleLabels": ["Participant Flow", "Outcome Measures"]},
    ]
    assert reslink.first_results_version(changes) == 2


def test_first_results_version_none_when_no_results_label_ever():
    changes = [{"version": 0, "moduleLabels": []}, {"version": 1, "moduleLabels": ["Outcome Measures"]}]
    assert reslink.first_results_version(changes) is None
    assert reslink.first_results_version([]) is None


def test_first_results_version_matches_each_results_section_label():
    for label in ["Participant Flow", "Baseline Characteristics", "Adverse Events", "Results"]:
        assert reslink.first_results_version([{"version": 5, "moduleLabels": [label]}]) == 5


def test_in_window_boundaries():
    assert reslink.in_window(None, 0, 9) is False  # trial never posted results
    assert reslink.in_window(5, 0, 9) is True
    assert reslink.in_window(9, 0, 9) is True  # inclusive at to_version
    assert reslink.in_window(0, 0, 9) is False  # exclusive at from_version: pre-existing section
    assert reslink.in_window(10, 0, 9) is False  # results arrived after the window


def test_near_results_date_90_day_boundary():
    assert reslink.near_results_date("2020-06-01", ["2020-08-30"]) is True  # exactly 90 days
    assert reslink.near_results_date("2020-06-01", ["2020-08-31"]) is False  # 91 days
    assert reslink.near_results_date("2020-06-01", ["2020-05-01"]) is True  # proximity is two-sided
    assert reslink.near_results_date("2020-06-01", ["2019-01-01", "2020-06-15"]) is True  # any date qualifies


def test_near_results_date_null_cases():
    assert reslink.near_results_date("2020-06-01", []) is False  # readable snapshot, no results posted
    assert reslink.near_results_date(None, ["2020-06-01"]) is None  # no version date to compare
    assert reslink.near_results_date("not-a-date", ["2020-06-01"]) is None


def test_classify_three_valued_or():
    assert reslink.classify(True, False) == 1
    assert reslink.classify(False, True) == 1
    assert reslink.classify(True, None) == 1
    assert reslink.classify(False, False) == 0
    assert reslink.classify(None, False) is None
    assert reslink.classify(False, None) is None
    assert reslink.classify(None, None) is None


# ---- schema guard --------------------------------------------------------------------


def test_ensure_schema_adds_results_linked_column_and_is_idempotent(tmp_path, monkeypatch):
    _patch_config(tmp_path, monkeypatch)
    conn = db.connect()
    reslink.ensure_schema(conn)
    reslink.ensure_schema(conn)  # second call is a no-op, not an error
    cols = {r["name"] for r in conn.execute("PRAGMA table_info(findings)")}
    conn.close()
    assert "results_linked" in cols


# ---- enrichment ----------------------------------------------------------------------


def test_enrich_all_window_date_and_undetermined_cases(tmp_path, monkeypatch):
    _patch_config(tmp_path, monkeypatch)
    conn = db.connect()

    # NCT_W: rule (a). Results section first appears at v2; finding 1 (v0->v1)
    # predates it, finding 2 (v1->v2) contains it. Newest snapshot carries no
    # results dates, so rule (b) is a determined False for both.
    _seed_trial(
        conn, "NCT_W",
        versions=[(0, "2020-01-01"), (1, "2020-02-01"), (2, "2021-01-01")],
        findings=[
            (1, 0, 1, "PRIMARY_REPLACED", "SIGNAL", 30, -300),
            (2, 1, 2, "POST_COMPLETION_CHANGE", "SIGNAL", 360, 100),
        ],
    )
    _write_cache(
        "NCT_W",
        changes=[
            {"version": 0, "moduleLabels": []},
            {"version": 1, "moduleLabels": ["Outcome Measures"]},
            {"version": 2, "moduleLabels": ["Participant Flow", "Outcome Measures"]},
        ],
        snapshots={2: {}},
    )

    # NCT_D: rule (b). No results-section label anywhere in history, but the newest
    # snapshot's statusModule dates the results posting within 90 days of the
    # finding's to_version date.
    _seed_trial(
        conn, "NCT_D",
        versions=[(0, "2020-01-01"), (1, "2021-06-01")],
        findings=[(3, 0, 1, "POST_COMPLETION_CHANGE", "SIGNAL", 400, 120)],
    )
    _write_cache(
        "NCT_D",
        changes=[{"version": 0, "moduleLabels": []}, {"version": 1, "moduleLabels": ["Outcome Measures"]}],
        snapshots={1: {"resultsFirstSubmitDate": "2021-05-01", "resultsFirstPostDateStruct": {"date": "2021-07-15"}}},
    )

    # NCT_U: no cache dir at all (a CTIS-shaped trial) -- both rules undetermined.
    _seed_trial(
        conn, "NCT_U",
        versions=[(0, "2020-01-01"), (1, "2020-06-01")],
        findings=[(4, 0, 1, "PRIMARY_REPLACED", "SIGNAL", 30, -10)],
    )

    totals = reslink.enrich_all(conn)
    linked = {r["finding_id"]: r["results_linked"] for r in conn.execute("SELECT finding_id, results_linked FROM findings")}
    conn.close()

    assert linked == {1: 0, 2: 1, 3: 1, 4: None}
    assert totals == {"linked": 2, "not_linked": 1, "undetermined": 1}


def test_enrich_all_reads_history_json_not_just_stored_versions(tmp_path, monkeypatch):
    """The reason labels come from history.json: the true first results-touching
    version (v5 here) is often NOT a stored row in `versions` (ingest fetches only
    v0 + outcome-touching + final), and must still land inside the window."""
    _patch_config(tmp_path, monkeypatch)
    conn = db.connect()
    _seed_trial(
        conn, "NCT_H",
        versions=[(0, "2020-01-01"), (9, "2021-01-01")],  # stored subset only
        findings=[(1, 0, 9, "POST_COMPLETION_CHANGE", "SIGNAL", 300, 200)],
    )
    _write_cache(
        "NCT_H",
        changes=[
            {"version": 0, "moduleLabels": []},
            {"version": 5, "moduleLabels": ["Participant Flow"]},
            {"version": 9, "moduleLabels": ["Outcome Measures"]},
        ],
        snapshots={9: {}},
    )

    reslink.enrich_all(conn)
    (linked,) = conn.execute("SELECT results_linked FROM findings WHERE finding_id=1").fetchone()
    conn.close()
    assert linked == 1


def test_enrich_all_is_the_reconciliation_after_a_pipeline_rerun(tmp_path, monkeypatch):
    """The pipeline delete+reinserts findings with fresh finding_ids and NULL
    results_linked; re-running the enrichment reclassifies the new rows to the
    same values -- pure recomputation, nothing keyed to the old ids."""
    _patch_config(tmp_path, monkeypatch)
    conn = db.connect()
    _seed_trial(
        conn, "NCT_R",
        versions=[(0, "2020-01-01"), (1, "2021-01-01")],
        findings=[(1, 0, 1, "POST_COMPLETION_CHANGE", "SIGNAL", 300, 100)],
    )
    _write_cache(
        "NCT_R",
        changes=[{"version": 0, "moduleLabels": []}, {"version": 1, "moduleLabels": ["Participant Flow"]}],
        snapshots={1: {}},
    )
    reslink.enrich_all(conn)

    # Simulate ctcm.pipeline's per-trial delete+reinsert (renumbered id, NULL column).
    conn.execute("DELETE FROM findings WHERE nct_id='NCT_R'")
    conn.execute(
        "INSERT INTO findings(finding_id, nct_id, from_version, to_version, change_type, severity, "
        "before_measure, after_measure, days_after_enrolment, days_after_primary_completion, confidence, "
        "resolved_by, rationale) VALUES (77, 'NCT_R', 0, 1, 'POST_COMPLETION_CHANGE', 'SIGNAL', 'a', 'b', "
        "300, 100, 1.0, 'T0', 'r')"
    )
    conn.commit()
    assert conn.execute("SELECT results_linked FROM findings WHERE finding_id=77").fetchone()[0] is None

    reslink.enrich_all(conn)
    (linked,) = conn.execute("SELECT results_linked FROM findings WHERE finding_id=77").fetchone()
    conn.close()
    assert linked == 1


# ---- strict-genuine trial count ------------------------------------------------------


def test_strict_genuine_trial_count_none_without_the_column(tmp_path, monkeypatch):
    _patch_config(tmp_path, monkeypatch)
    conn = db.connect()
    assert reslink.strict_genuine_trial_count(conn) is None
    conn.close()


def test_strict_genuine_trial_count_branches(tmp_path, monkeypatch):
    _patch_config(tmp_path, monkeypatch)
    conn = db.connect()
    reslink.ensure_schema(conn)
    versions = [(0, "2020-01-01"), (1, "2020-06-01")]
    # counts: during the trial (post-enrolment, pre-completion) -- linkage irrelevant
    _seed_trial(conn, "NCT_DURING", versions, [(1, 0, 1, "PRIMARY_REPLACED", "SIGNAL", 30, -10)])
    conn.execute("UPDATE findings SET results_linked=1 WHERE finding_id=1")
    # counts: post-completion within 365d, determined not results-linked
    _seed_trial(conn, "NCT_POST_OK", versions, [(2, 0, 1, "POST_COMPLETION_CHANGE", "SIGNAL", 300, 200)])
    conn.execute("UPDATE findings SET results_linked=0 WHERE finding_id=2")
    # not: post-completion but results-linked
    _seed_trial(conn, "NCT_LINKED", versions, [(3, 0, 1, "POST_COMPLETION_CHANGE", "SIGNAL", 300, 200)])
    conn.execute("UPDATE findings SET results_linked=1 WHERE finding_id=3")
    # not: past the 365-day window
    _seed_trial(conn, "NCT_LATE", versions, [(4, 0, 1, "POST_COMPLETION_CHANGE", "SIGNAL", 500, 366)])
    conn.execute("UPDATE findings SET results_linked=0 WHERE finding_id=4")
    # not: undetermined linkage (NULL) deliberately does not count as genuine
    _seed_trial(conn, "NCT_NULL", versions, [(5, 0, 1, "POST_COMPLETION_CHANGE", "SIGNAL", 300, 200)])
    # not: never a SIGNAL finding
    _seed_trial(conn, "NCT_MOD", versions, [(6, 0, 1, "PRIMARY_REPLACED", "MODERATE", 30, -10)])
    conn.execute("UPDATE findings SET results_linked=0 WHERE finding_id=6")
    conn.commit()

    assert reslink.strict_genuine_trial_count(conn) == 2
    conn.close()
