"""Unit coverage for evidence assembly, judge-JSON parsing/fence-stripping, the
fraud/misconduct word filter, and the adjudicate() cache -- all against a FAKE
llm callable (no subprocess, no network). The one real `claude -p` end-to-end
run is manual (scripts/run_adjudicate.py --nct NCT04280705), not part of the
suite.
"""

import json

import pytest

from ctcm import config, db
from ctcm.adjudicate import (
    Adjudication,
    _clean_response,
    _defuse,
    _strip_fences,
    adjudicate,
    assemble_evidence,
    content_hash,
    ensure_schema,
)


@pytest.fixture
def conn(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "DATA_DIR", tmp_path)
    monkeypatch.setattr(config, "CACHE_DIR", tmp_path / "cache")
    monkeypatch.setattr(config, "DB_PATH", tmp_path / "test.db")
    c = db.connect()
    ensure_schema(c)
    yield c
    c.close()


def _seed_actt1_like(conn):
    conn.execute(
        "INSERT INTO trials(nct_id, phase, overall_status, lead_sponsor, sponsor_class) "
        "VALUES ('NCT1', 'PHASE3', 'COMPLETED', 'NIAID', 'NIH')"
    )
    conn.execute(
        "INSERT INTO timeline_facts(nct_id, version_no, start_date, start_date_type, primary_completion_date, primary_completion_type) "
        "VALUES ('NCT1', 9, '2020-02-21', 'ACTUAL', '2020-06-18', 'ESTIMATED')"
    )
    conn.execute(
        "INSERT INTO timeline_facts(nct_id, version_no, start_date, start_date_type, primary_completion_date, primary_completion_type) "
        "VALUES ('NCT1', 14, '2020-02-21', 'ACTUAL', '2020-06-18', 'ESTIMATED')"
    )
    conn.execute(
        "INSERT INTO outcomes(nct_id, version_no, outcome_type, ordinal, measure, time_frame, measure_norm) "
        "VALUES ('NCT1', 9, 'PRIMARY', 0, '8-point ordinal scale', 'Day 15', '8 point ordinal scale')"
    )
    conn.execute(
        "INSERT INTO outcomes(nct_id, version_no, outcome_type, ordinal, measure, time_frame, measure_norm) "
        "VALUES ('NCT1', 14, 'SECONDARY', 0, '8-point ordinal scale', 'Day 15', '8 point ordinal scale')"
    )
    conn.execute(
        "INSERT INTO findings(finding_id, nct_id, from_version, to_version, change_type, severity, before_measure, "
        "after_measure, days_after_enrolment, days_after_primary_completion, confidence, resolved_by, rationale) "
        "VALUES (1, 'NCT1', 9, 14, 'PRIMARY_DEMOTED', 'SIGNAL', '8-point ordinal scale', '8-point ordinal scale', "
        "55, -35, 1.0, 'T0', \"PRIMARY_DEMOTED: '8-point ordinal scale' -> '8-point ordinal scale' (55 days after enrolment, v9->v14).\")"
    )
    conn.commit()
    return conn.execute("SELECT * FROM findings WHERE finding_id=1").fetchone()


class FakeLLM:
    """Returns queued responses in call order; records every prompt it saw."""

    def __init__(self, responses):
        self.responses = list(responses)
        self.prompts = []

    def __call__(self, prompt: str) -> str:
        self.prompts.append(prompt)
        if not self.responses:
            raise RuntimeError("FakeLLM exhausted")
        r = self.responses.pop(0)
        if isinstance(r, Exception):
            raise r
        return r


JUDGE_OK = json.dumps({"concern": "moderate", "confidence": 0.6, "rationale": "Demoted 55 days after enrolment, v9->v14."})


# ---- fence stripping / JSON parsing --------------------------------------------------


def test_strip_fences_removes_json_fence():
    assert _strip_fences('```json\n{"a": 1}\n```') == '{"a": 1}'


def test_strip_fences_removes_bare_fence():
    assert _strip_fences('```\n{"a": 1}\n```') == '{"a": 1}'


def test_strip_fences_passthrough_when_unfenced():
    assert _strip_fences('{"a": 1}') == '{"a": 1}'


def test_clean_response_parses_as_json_after_stripping():
    cleaned, filtered = _clean_response(f"```json\n{JUDGE_OK}\n```")
    verdict = json.loads(cleaned)
    assert verdict["concern"] == "moderate"
    assert filtered is False


# ---- word filter ----------------------------------------------------------------------


def test_defuse_replaces_fraud_case_insensitive():
    cleaned, filtered = _defuse("This looks like Fraud to me.")
    assert "fraud" not in cleaned.lower()
    assert "concerning" in cleaned
    assert filtered is True


def test_defuse_replaces_misconduct():
    cleaned, filtered = _defuse("Possible scientific misconduct here.")
    assert "misconduct" not in cleaned.lower()
    assert filtered is True


def test_defuse_leaves_clean_text_untouched():
    cleaned, filtered = _defuse("This change looks concerning given the timing.")
    assert cleaned == "This change looks concerning given the timing."
    assert filtered is False


# ---- content hash -----------------------------------------------------------------------


def test_content_hash_stable_for_same_inputs():
    a = content_hash("NCT1", 9, 14, "PRIMARY_DEMOTED", "x", "y")
    b = content_hash("NCT1", 9, 14, "PRIMARY_DEMOTED", "x", "y")
    assert a == b


def test_content_hash_differs_on_change_type():
    a = content_hash("NCT1", 9, 14, "PRIMARY_DEMOTED", "x", "y")
    b = content_hash("NCT1", 9, 14, "PRIMARY_ADDED", "x", "y")
    assert a != b


# ---- evidence assembly -----------------------------------------------------------------


def test_assemble_evidence_includes_sponsor_dates_and_outcomes(conn):
    finding = _seed_actt1_like(conn)
    evidence = assemble_evidence(finding, conn)
    assert "NIAID" in evidence
    assert "PHASE3" in evidence
    assert "8-point ordinal scale" in evidence
    assert "2020-02-21" in evidence
    assert "55" in evidence  # days_after_enrolment
    assert "PRIMARY_DEMOTED" in evidence


def test_assemble_evidence_handles_missing_trial_row(conn):
    conn.execute(
        "INSERT INTO findings(finding_id, nct_id, from_version, to_version, change_type, severity, "
        "before_measure, after_measure, rationale) VALUES (2, 'NCTGHOST', 0, 1, 'PRIMARY_ADDED', 'SIGNAL', "
        "NULL, 'New endpoint', 'x')"
    )
    conn.commit()
    finding = conn.execute("SELECT * FROM findings WHERE finding_id=2").fetchone()
    evidence = assemble_evidence(finding, conn)
    assert "unknown" in evidence.lower()


# ---- adjudicate() end to end against the fake llm --------------------------------------


def test_adjudicate_happy_path_writes_row_and_returns_verdict(conn):
    finding = _seed_actt1_like(conn)
    fake = FakeLLM(["defence text, v9->v14, 55 days after enrolment.", "prosecution text, rebuts the defence.", JUDGE_OK])

    adj = adjudicate(finding, conn, llm=fake)

    assert isinstance(adj, Adjudication)
    assert adj.severity_confirmed == "MODERATE"
    assert adj.confidence == 0.6
    assert "v9->v14" in adj.defence
    assert "rebuts" in adj.prosecution
    assert len(fake.prompts) == 3  # defence, prosecution, judge -- in that order

    row = conn.execute("SELECT * FROM adjudications").fetchone()
    assert row["nct_id"] == "NCT1"
    assert row["severity_confirmed"] == "MODERATE"


def test_adjudicate_prosecution_sees_defence_argument(conn):
    finding = _seed_actt1_like(conn)
    fake = FakeLLM(["UNIQUE_DEFENCE_MARKER", "prosecution reply", JUDGE_OK])
    adjudicate(finding, conn, llm=fake)
    assert "UNIQUE_DEFENCE_MARKER" in fake.prompts[1]


def test_adjudicate_judge_sees_both_arguments(conn):
    finding = _seed_actt1_like(conn)
    fake = FakeLLM(["DEFENCE_MARKER", "PROSECUTION_MARKER", JUDGE_OK])
    adjudicate(finding, conn, llm=fake)
    assert "DEFENCE_MARKER" in fake.prompts[2]
    assert "PROSECUTION_MARKER" in fake.prompts[2]


def test_adjudicate_caches_by_content_hash_second_call_skips_llm(conn):
    finding = _seed_actt1_like(conn)
    fake = FakeLLM(["defence", "prosecution", JUDGE_OK])
    first = adjudicate(finding, conn, llm=fake)

    fake2 = FakeLLM([])  # would raise if called at all
    second = adjudicate(finding, conn, llm=fake2)

    assert second == first
    assert fake2.prompts == []
    assert conn.execute("SELECT count(*) FROM adjudications").fetchone()[0] == 1


def test_adjudicate_llm_failure_yields_unreviewed_not_a_crash(conn):
    finding = _seed_actt1_like(conn)
    fake = FakeLLM([RuntimeError("subprocess timed out")])

    adj = adjudicate(finding, conn, llm=fake)

    assert adj.severity_confirmed == "UNREVIEWED"
    row = conn.execute("SELECT * FROM adjudications").fetchone()
    assert row["severity_confirmed"] == "UNREVIEWED"


def test_adjudicate_malformed_judge_json_yields_unreviewed(conn):
    finding = _seed_actt1_like(conn)
    fake = FakeLLM(["defence", "prosecution", "not json at all"])

    adj = adjudicate(finding, conn, llm=fake)

    assert adj.severity_confirmed == "UNREVIEWED"


def test_adjudicate_unrecognised_concern_value_yields_unreviewed(conn):
    finding = _seed_actt1_like(conn)
    bad_judge = json.dumps({"concern": "severe", "confidence": 0.9, "rationale": "x"})
    fake = FakeLLM(["defence", "prosecution", bad_judge])

    adj = adjudicate(finding, conn, llm=fake)

    assert adj.severity_confirmed == "UNREVIEWED"


def test_adjudicate_filters_banned_word_from_persisted_rationale(conn):
    finding = _seed_actt1_like(conn)
    judge_with_banned_word = json.dumps(
        {"concern": "high", "confidence": 0.8, "rationale": "This pattern suggests fraud given the post-completion timing."}
    )
    fake = FakeLLM(["defence", "prosecution", judge_with_banned_word])

    adj = adjudicate(finding, conn, llm=fake)

    assert "fraud" not in adj.rationale.lower()
    assert "concerning" in adj.rationale.lower()
    row = conn.execute("SELECT rationale FROM adjudications").fetchone()
    assert "fraud" not in row["rationale"].lower()


def test_adjudicate_logs_every_call_to_jsonl(conn, tmp_path):
    finding = _seed_actt1_like(conn)
    fake = FakeLLM(["defence", "prosecution", JUDGE_OK])
    adjudicate(finding, conn, llm=fake)

    log_path = tmp_path / "adjudication_log.jsonl"
    lines = log_path.read_text().strip().splitlines()
    assert len(lines) == 3
    roles = [json.loads(line)["role"] for line in lines]
    assert roles == ["defence", "prosecution", "judge"]
    for line in lines:
        entry = json.loads(line)
        assert entry["content_hash"] == content_hash("NCT1", 9, 14, "PRIMARY_DEMOTED", "8-point ordinal scale", "8-point ordinal scale")


def test_adjudicate_logs_failed_call_with_error_field(conn, tmp_path):
    finding = _seed_actt1_like(conn)
    fake = FakeLLM([RuntimeError("boom")])
    adjudicate(finding, conn, llm=fake)

    log_path = tmp_path / "adjudication_log.jsonl"
    lines = log_path.read_text().strip().splitlines()
    assert len(lines) == 1
    entry = json.loads(lines[0])
    assert entry["role"] == "defence"
    assert "boom" in entry["error"]
