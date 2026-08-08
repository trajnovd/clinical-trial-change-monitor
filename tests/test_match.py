"""Task 7: the semantic matching cascade (T0-T3). See ctcm/match.py module docstring
for the design; TECH-PRD §5.1's three canonical pairs (verbatim strings) are the
non-negotiable acceptance cases. Network/CLI calls are never exercised here -- T3 is
always a fake callable or absent."""

import json

import pytest

from ctcm.classify import OutcomeRow
from ctcm.match import MATCH_RELATIONS, T3BudgetExceeded, T3Client, match_outcomes


def _row(measure, measure_norm=None, time_frame=None, outcome_type="PRIMARY", ordinal=0, nct="NCT1", version_no=0):
    from ctcm.normalize import norm

    return OutcomeRow(
        nct_id=nct,
        version_no=version_no,
        outcome_type=outcome_type,
        ordinal=ordinal,
        measure=measure,
        description=None,
        time_frame=time_frame,
        measure_norm=measure_norm if measure_norm is not None else norm(measure),
    )


def _only_pair(result):
    assert len(result.pairs) == 1
    return result.pairs[0]


# ---- TECH-PRD §5.1 canonical cases (verbatim strings), no T3 available -------------


def test_ham_d_pair_resolves_same_ish_not_at_t3():
    before = [_row("Change in HAM-D from baseline to week 6")]
    after = [_row("Hamilton Depression Rating Scale improvement at 6 weeks")]
    result = match_outcomes(before, after, t3=None)
    pair = _only_pair(result)
    assert pair.relation in ("SAME", "REWORDED")
    assert pair.tier not in ("T3", "T3_FALLBACK")


def test_all_cause_vs_cardiovascular_mortality_never_same():
    before = [_row("All-cause mortality at 12 months")]
    after = [_row("Cardiovascular mortality at 12 months")]
    result = match_outcomes(before, after, t3=None)
    pair = _only_pair(result)
    assert pair.relation != "SAME"
    assert pair.relation in ("NARROWED", "DIFFERENT")


def test_overall_survival_with_added_timepoint_is_timepoint_changed():
    before = [_row("Overall survival")]
    after = [_row("Overall survival at 24 months")]
    result = match_outcomes(before, after, t3=None)
    pair = _only_pair(result)
    assert pair.relation == "TIMEPOINT_CHANGED"


# ---- tier mechanics -----------------------------------------------------------------


def test_t0_exact_norm_match_is_same_at_t0():
    before = [_row("Overall survival")]
    after = [_row("Overall Survival", measure_norm="overall survival")]
    result = match_outcomes(before, after)
    pair = _only_pair(result)
    assert pair.relation == "SAME"
    assert pair.tier == "T0"
    assert pair.score == 1.0


def test_low_similarity_pair_resolves_different_at_t1_without_t3():
    before = [_row("Overall survival")]
    after = [_row("Number of participants with serious adverse events")]

    def fail_if_called(b, a):
        raise AssertionError("T1 should have rejected this before T3 was ever consulted")

    result = match_outcomes(before, after, t3=fail_if_called)
    pair = _only_pair(result)
    assert pair.relation == "DIFFERENT"
    assert pair.tier == "T1"


def test_reverse_qualifier_is_broadened():
    before = [_row("Cardiovascular mortality at 12 months")]
    after = [_row("All-cause mortality at 12 months")]
    result = match_outcomes(before, after, t3=None)
    pair = _only_pair(result)
    assert pair.relation == "BROADENED"
    assert pair.tier == "T2"


# ---- T3 injection: fakes only, never the real CLI ------------------------------------


def test_t3_disabled_escalation_counted_as_t2_unresolved():
    # Ambiguous band, no T2 rule fires, no T3 -> conservative DIFFERENT, tier T2_UNRESOLVED,
    # and the escalation is counted in tier_counts (brief: "counted").
    before = [_row("Six minute walk distance improvement")]
    after = [_row("Six minute walk distance change from screening")]
    result = match_outcomes(before, after, t3=None)
    pair = _only_pair(result)
    assert pair.relation == "DIFFERENT"
    assert pair.tier == "T2_UNRESOLVED"
    assert result.tier_counts.get("T2_UNRESOLVED") == 1


def test_t3_fake_resolves_residual_ambiguous_pair():
    before = [_row("Six minute walk distance improvement")]
    after = [_row("Six minute walk distance change from screening")]

    def fake_t3(b, a):
        return {"relation": "SAME", "confidence": 0.8, "reasoning": "same construct, reworded"}

    result = match_outcomes(before, after, t3=fake_t3)
    pair = _only_pair(result)
    assert pair.relation == "SAME"
    assert pair.tier == "T3"
    assert result.tier_counts.get("T3") == 1


def test_t3_exception_falls_back_conservative():
    before = [_row("Six minute walk distance improvement")]
    after = [_row("Six minute walk distance change from screening")]

    def broken_t3(b, a):
        raise RuntimeError("claude -p timed out")

    result = match_outcomes(before, after, t3=broken_t3)
    pair = _only_pair(result)
    assert pair.relation == "DIFFERENT"
    assert pair.tier == "T3_FALLBACK"
    assert result.tier_counts.get("T3_FALLBACK") == 1


def test_t3_malformed_response_falls_back_conservative():
    before = [_row("Six minute walk distance improvement")]
    after = [_row("Six minute walk distance change from screening")]

    def malformed_t3(b, a):
        return {"relation": "MAYBE", "confidence": 0.5}

    result = match_outcomes(before, after, t3=malformed_t3)
    pair = _only_pair(result)
    assert pair.relation == "DIFFERENT"
    assert pair.tier == "T3_FALLBACK"


def test_t3_budget_exceeded_treated_as_unresolved_not_failure():
    before = [_row("Six minute walk distance improvement")]
    after = [_row("Six minute walk distance change from screening")]

    def over_budget_t3(b, a):
        raise T3BudgetExceeded()

    result = match_outcomes(before, after, t3=over_budget_t3)
    pair = _only_pair(result)
    assert pair.relation == "DIFFERENT"
    assert pair.tier == "T2_UNRESOLVED"


# ---- greedy best-first assignment ----------------------------------------------------


def test_greedy_best_first_assignment_prefers_globally_best_pairing():
    # Two before items, two after items. "Overall survival" is a near-exact match for
    # the first after item; if a naive first-available scan (rather than best-first by
    # score) ran b0 against a0 and a1 in order, a worse candidate could steal a match
    # that belongs to a better-scoring pair elsewhere. Best-first must not let that happen.
    before = [
        _row("Overall survival", measure_norm="overall survival"),
        _row("Progression free survival", measure_norm="progression free survival"),
    ]
    after = [
        _row("Progression free survival", measure_norm="progression free survival"),
        _row("Overall survival", measure_norm="overall survival"),
    ]
    result = match_outcomes(before, after, t3=None)
    matched = {(p.b_idx, p.a_idx): p.relation for p in result.pairs if p.relation != "DIFFERENT" or (p.b_idx is not None and p.a_idx is not None)}
    assert result.pairs
    same_pairs = [p for p in result.pairs if p.relation == "SAME"]
    assert (0, 1) in [(p.b_idx, p.a_idx) for p in same_pairs]
    assert (1, 0) in [(p.b_idx, p.a_idx) for p in same_pairs]


def test_tier_counts_sum_matches_number_of_matched_pairs_when_no_escalation():
    before = [_row("Overall survival"), _row("Adverse events")]
    after = [_row("Overall survival"), _row("Adverse events")]
    result = match_outcomes(before, after, t3=None)
    assert sum(result.tier_counts.values()) == 2
    assert all(p.relation == "SAME" for p in result.pairs)


# ---- MATCH_RELATIONS export used by classify.py's contract --------------------------


def test_match_relations_matches_documented_contract():
    assert MATCH_RELATIONS == {"SAME", "REWORDED", "NARROWED", "BROADENED", "TIMEPOINT_CHANGED"}


# ---- T3Client: cache + fence-stripping + budget, subprocess mocked ------------------


class _FakeCompletedProcess:
    def __init__(self, returncode=0, stdout="", stderr=""):
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


def test_t3client_strips_json_fences_and_caches(tmp_path, monkeypatch):
    from ctcm import db, config

    monkeypatch.setattr(config, "DATA_DIR", tmp_path)
    monkeypatch.setattr(config, "CACHE_DIR", tmp_path / "cache")
    monkeypatch.setattr(config, "DB_PATH", tmp_path / "test.db")
    conn = db.connect()

    calls = []

    def fake_run(*args, **kwargs):
        calls.append(1)
        payload = json.dumps({"relation": "SAME", "confidence": 0.9, "reasoning": "same thing"})
        return _FakeCompletedProcess(returncode=0, stdout=f"```json\n{payload}\n```")

    import ctcm.match as match_mod

    monkeypatch.setattr(match_mod.subprocess, "run", fake_run)

    client = T3Client(conn)
    before = _row("Overall survival")
    after = _row("OS")
    result1 = client(before, after)
    assert result1["relation"] == "SAME"
    assert len(calls) == 1

    result2 = client(before, after)  # same prompt -> cache hit, no second subprocess call
    assert result2["relation"] == "SAME"
    assert len(calls) == 1
    conn.close()


def test_t3client_raises_on_nonzero_exit(tmp_path, monkeypatch):
    from ctcm import db, config

    monkeypatch.setattr(config, "DATA_DIR", tmp_path)
    monkeypatch.setattr(config, "CACHE_DIR", tmp_path / "cache")
    monkeypatch.setattr(config, "DB_PATH", tmp_path / "test.db")
    conn = db.connect()

    import ctcm.match as match_mod

    monkeypatch.setattr(
        match_mod.subprocess, "run", lambda *a, **k: _FakeCompletedProcess(returncode=1, stdout="", stderr="boom")
    )

    client = T3Client(conn)
    with pytest.raises(Exception):
        client(_row("Overall survival"), _row("OS"))
    conn.close()


def test_t3client_budget_exceeded_after_limit(tmp_path, monkeypatch):
    from ctcm import db, config

    monkeypatch.setattr(config, "DATA_DIR", tmp_path)
    monkeypatch.setattr(config, "CACHE_DIR", tmp_path / "cache")
    monkeypatch.setattr(config, "DB_PATH", tmp_path / "test.db")
    conn = db.connect()

    import ctcm.match as match_mod

    def fake_run(*args, **kwargs):
        payload = json.dumps({"relation": "DIFFERENT", "confidence": 0.5, "reasoning": "x"})
        return _FakeCompletedProcess(returncode=0, stdout=payload)

    monkeypatch.setattr(match_mod.subprocess, "run", fake_run)

    client = T3Client(conn, limit=1)
    client(_row("A"), _row("B"))  # consumes the one call
    with pytest.raises(T3BudgetExceeded):
        client(_row("C"), _row("D"))  # different prompt -> not a cache hit -> budget check
    conn.close()
