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


# ---- v03-review.md Critical #1: T2's qualifier rule must not guess on generic -------
# token-subset/superset -- only a curated restrictive-qualifier lexicon hit may fire
# NARROWED/BROADENED. Regression fixtures below are the real corpus false positives
# the review found by hand-inspecting live findings, verbatim.


def test_abbreviation_expansion_pair_is_not_narrowed():
    # NCT01189890 v0->v12, real corpus false positive: spelling out an abbreviation
    # ("A1C" -> "Hemoglobin A1c (A1C)") added a word ("hemoglobin") that isn't a
    # restrictive qualifier -- generic token-subset used to misread this as narrowing.
    before = [_row("A1C change from baseline at Week 30")]
    after = [_row("Hemoglobin A1c (A1C) change from baseline at Week 30")]
    result = match_outcomes(before, after, t3=None)
    pair = _only_pair(result)
    assert pair.relation not in ("NARROWED", "BROADENED")


def test_added_unrelated_monitoring_parameters_is_not_narrowed():
    # NCT04604795 v0->v4, real corpus false positive, and a genuine *inversion*: three
    # more parameter categories were added to what's monitored (vital signs, labs,
    # ECG) -- broadening the scope of measurement, not narrowing it -- and the old
    # generic rule filed it as PRIMARY_NARROWED/SIGNAL, backwards. None of the added
    # words are in the curated restrictive-qualifier lexicon, so the fixed rule must
    # not fire NARROWED *or* BROADENED here -- it has no basis to claim either
    # direction and must escalate instead of guessing.
    before = [_row(
        "Part A: Number of participants with clinically significant changes in "
        "physical examination following oral dosing"
    )]
    after = [_row(
        "Part A: Number of participants with clinically significant changes in physical "
        "examination, vital signs, laboratory parameters and 12-lead electrocardiogram "
        "(ECG) following oral dosing"
    )]
    result = match_outcomes(before, after, t3=None)
    pair = _only_pair(result)
    assert pair.relation not in ("NARROWED", "BROADENED")


def test_acronym_expansion_colliding_with_lexicon_word_is_not_narrowed():
    # NCT00598637, found live after the lexicon fix above: "MACE" -> "major adverse
    # cardiac event (MACE)" is a pure acronym spell-out, but "cardiac" happens to be a
    # genuine restrictive-qualifier-lexicon word too, so the lexicon check alone still
    # misfired here. The shared, verbatim "MACE" acronym on both sides is the tell.
    before = [_row(
        "incidence of MACE defined as a composite of death, MI and Target Lesion "
        "revascularization (TLR)."
    )]
    after = [_row(
        "Incidence of major adverse cardiac event (MACE) defined as a composite of "
        "death, MI and target lesion revascularization (TLR)."
    )]
    result = match_outcomes(before, after, t3=None)
    pair = _only_pair(result)
    assert pair.relation not in ("NARROWED", "BROADENED")


def test_qualifier_lexicon_direction_correctness_on_the_nct04604795_shape():
    # Same enumerated-parameter-list shape as NCT04604795, but with an actual
    # restrictive-qualifier-lexicon word ("cardiovascular") inserted alongside the
    # unrelated ones -- this is the case the lexicon rule *should* catch, and must
    # get the direction right: added lexicon word -> NARROWED, removed -> BROADENED
    # (the mirror image of the same pair).
    narrower = [_row(
        "Part A: Number of participants with clinically significant changes in physical "
        "examination, vital signs and cardiovascular assessment following oral dosing"
    )]
    broader = [_row(
        "Part A: Number of participants with clinically significant changes in "
        "physical examination following oral dosing"
    )]
    added = match_outcomes(broader, narrower, t3=None)
    assert _only_pair(added).relation == "NARROWED"
    removed = match_outcomes(narrower, broader, t3=None)
    assert _only_pair(removed).relation == "BROADENED"


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


def test_t3_escalation_capped_to_one_attempt_per_item():
    # Two before items both land in the escalation band against the SAME after item,
    # at different scores. Without the one-shot-per-item cap (2f479d0), once the
    # best-scoring candidate's T3 call comes back DIFFERENT, the loser would still get
    # its own separate T3 call against the same after-item -- redundant re-litigation
    # of "does this item have a match at all" that the cap exists to avoid.
    before = [
        _row("Six minute walk distance improvement"),        # Jaccard vs after ~0.8 -- tried first
        _row("Six minute walk distance change from visit"),  # Jaccard vs after ~0.67 -- must be capped
    ]
    after = [_row("Six minute walk distance change from screening")]

    calls = []

    def counting_t3(b, a):
        calls.append(b.measure)
        return {"relation": "DIFFERENT", "confidence": 0.5, "reasoning": "not a match"}

    result = match_outcomes(before, after, t3=counting_t3)

    assert calls == ["Six minute walk distance improvement"]  # only one T3 call, ever
    assert result.tier_counts.get("T3") == 1
    # the second before-item was never even attempted -- capped, not "also tried and failed"
    second_item_pairs = [p for p in result.pairs if p.b_idx == 1]
    assert len(second_item_pairs) == 1
    assert second_item_pairs[0].a_idx is None


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
