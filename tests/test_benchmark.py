"""Pure unit tests for benchmark/mapping.py (row->GroundTruth mapping, join
scoring) and benchmark/evaluate.py's split logic. No network, no real CSV, no
shared db -- small in-memory fixtures only."""

import importlib.util
import sys
from pathlib import Path

import pytest

BENCH_DIR = Path(__file__).resolve().parent.parent / "benchmark"


def _load(module_name: str, filename: str):
    spec = importlib.util.spec_from_file_location(module_name, BENCH_DIR / filename)
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    spec.loader.exec_module(module)
    return module


mapping = _load("benchmark_mapping", "mapping.py")
evaluate = _load("benchmark_evaluate", "evaluate.py")


def row(**overrides) -> dict:
    """A labeled-subset CSV row with every phase defaulted to 'not applicable /
    nothing flagged'; override specific columns per test."""
    r = {"id": "NCT00000000", "registry": "ClinicalTrials.gov", "referenceid": "1"}
    r.update(overrides)
    return r


# --- Axis-1 (recruitment phase, never PCD-overridden) ---------------------------------


def test_new_primary_only_is_primary_added():
    gt = mapping.trial_ground_truth(row(change_a_i_new_primary="1"))
    assert gt.types == {"PRIMARY_ADDED"}


def test_primary_omitted_only_is_primary_removed():
    gt = mapping.trial_ground_truth(row(change_a_i_primary_omitted="1"))
    assert gt.types == {"PRIMARY_REMOVED"}


def test_new_primary_and_primary_omitted_together_is_primary_replaced():
    gt = mapping.trial_ground_truth(row(change_a_i_new_primary="1", change_a_i_primary_omitted="1"))
    assert gt.types == {"PRIMARY_REPLACED"}
    assert "PRIMARY_ADDED" not in gt.types
    assert "PRIMARY_REMOVED" not in gt.types


def test_primary_from_secondary_is_secondary_promoted():
    gt = mapping.trial_ground_truth(row(change_a_i_primary_from_secondary="1"))
    assert gt.types == {"SECONDARY_PROMOTED"}


def test_primary_to_secondary_is_primary_demoted():
    gt = mapping.trial_ground_truth(row(change_a_i_primary_to_secondary="1"))
    assert gt.types == {"PRIMARY_DEMOTED"}


def test_change_timing_is_timepoint_changed():
    gt = mapping.trial_ground_truth(row(change_a_i_change_timing="1"))
    assert gt.types == {"TIMEPOINT_CHANGED"}


def test_change_timing_suppressed_by_cooccurring_change_same_phase():
    """DATA-NOTES SS6 caveat: change_timing -> TIMEPOINT_CHANGED only "and no
    add/omit/new/omitted on the same primary". The CSV has no per-primary
    granularity (phase-level flags only), so this is approximated at the
    phase level: any other axis-1-producing flag in the same phase suppresses
    the plain timing read (see benchmark/README.md)."""
    gt = mapping.trial_ground_truth(row(change_a_i_change_timing="1", change_a_i_new_primary="1"))
    assert gt.types == {"PRIMARY_ADDED"}
    assert "TIMEPOINT_CHANGED" not in gt.types


def test_change_timing_in_different_phase_is_not_suppressed():
    """The co-occurrence guard is per-phase, not trial-wide."""
    gt = mapping.trial_ground_truth(row(change_a_i_change_timing="1", change_i_p_new_primary="1"))
    assert gt.types == {"TIMEPOINT_CHANGED", "POST_COMPLETION_CHANGE"}


def test_added_measurement_is_primary_narrowed():
    gt = mapping.trial_ground_truth(row(change_a_i_added_measurement="1"))
    assert gt.types == {"PRIMARY_NARROWED"}


def test_change_measurement_low_confidence_defaults_to_primary_narrowed():
    """The documented low-confidence call (README): change_measurement /
    change_aggregation -> PRIMARY_NARROWED, not PRIMARY_REPLACED."""
    gt = mapping.trial_ground_truth(row(change_a_i_change_measurement="1"))
    assert gt.types == {"PRIMARY_NARROWED"}
    gt2 = mapping.trial_ground_truth(row(change_a_i_change_aggregation="1"))
    assert gt2.types == {"PRIMARY_NARROWED"}


def test_omitted_measurement_is_primary_broadened():
    """PRIMARY_BROADENED is a v0.3 ctcm addition (T2's mirror of NARROWED) --
    omitted_measurement/aggregation/timing used to be unmappable, now they map
    directly, same shape as added_* -> PRIMARY_NARROWED."""
    gt = mapping.trial_ground_truth(row(change_a_i_omitted_measurement="1"))
    assert gt.types == {"PRIMARY_BROADENED"}
    assert gt.no_change_confirmed is False


def test_omitted_aggregation_and_timing_are_also_primary_broadened():
    gt = mapping.trial_ground_truth(row(change_a_i_omitted_aggregation="1"))
    assert gt.types == {"PRIMARY_BROADENED"}
    gt2 = mapping.trial_ground_truth(row(change_a_i_omitted_timing="1"))
    assert gt2.types == {"PRIMARY_BROADENED"}


# --- Axis-2 (post-completion phases collapse to POST_COMPLETION_CHANGE) ---------------


def test_i_p_new_primary_maps_to_post_completion_change_not_primary_added():
    gt = mapping.trial_ground_truth(row(change_i_p_new_primary="1"))
    assert gt.types == {"POST_COMPLETION_CHANGE"}


def test_p_l_change_timing_maps_to_post_completion_change_not_timepoint_changed():
    gt = mapping.trial_ground_truth(row(change_p_l_change_timing="1"))
    assert gt.types == {"POST_COMPLETION_CHANGE"}


def test_i_p_omitted_measurement_maps_to_post_completion_change_not_primary_broadened():
    """Same axis-2 collapse rule applies to PRIMARY_BROADENED as every other
    axis-1 code: post-completion phases only ever surface POST_COMPLETION_CHANGE."""
    gt = mapping.trial_ground_truth(row(change_i_p_omitted_measurement="1"))
    assert gt.types == {"POST_COMPLETION_CHANGE"}


def test_recruitment_and_post_completion_changes_both_present():
    gt = mapping.trial_ground_truth(row(change_a_i_new_primary="1", change_p_l_primary_to_secondary="1"))
    assert gt.types == {"PRIMARY_ADDED", "POST_COMPLETION_CHANGE"}


# --- no_phase / no_change / confirmation semantics -------------------------------------


def test_no_phase_excludes_that_phase_entirely():
    gt = mapping.trial_ground_truth(row(change_a_i_no_phase="1", change_a_i_new_primary="1"))
    # new_primary shouldn't have been rated if the phase doesn't exist, but even
    # if present in the data, no_phase='1' means "don't look at this phase".
    assert gt.types == frozenset()


def test_all_phases_no_change_confirms_no_change():
    gt = mapping.trial_ground_truth(
        row(change_a_i_no_change="1", change_i_p_no_change="1", change_p_l_no_change="1")
    )
    assert gt.types == frozenset()
    assert gt.no_change_confirmed is True


def test_no_applicable_phases_does_not_confirm_no_change():
    """All three phases marked no_phase -- nothing was ever rated, so we can't
    honestly say Holst confirmed 'no change' (there's simply no evidence)."""
    gt = mapping.trial_ground_truth(row(change_a_i_no_phase="1", change_i_p_no_phase="1", change_p_l_no_phase="1"))
    assert gt.no_change_confirmed is False


def test_unconfirmed_gap_phase_blocks_no_change_confirmation():
    """Phase applicable, nothing flagged, but no_change also not explicitly '1'
    (blank on everything) -- treated as an unconfirmed gap, not a negative."""
    gt = mapping.trial_ground_truth(row())  # every column absent -> '.get' returns None everywhere
    assert gt.no_change_confirmed is False


# --- score_by_type / false_positive_rate (pure join logic) -----------------------------


def test_score_by_type_counts_tp_fp_fn():
    predicted = {
        "NCT1": frozenset({"PRIMARY_ADDED"}),  # TP
        "NCT2": frozenset({"PRIMARY_ADDED"}),  # FP (not in truth)
        "NCT3": frozenset(),  # FN (truth has it, we missed it)
        "NCT4": frozenset(),  # TN
    }
    truth = {
        "NCT1": mapping.GroundTruth(types=frozenset({"PRIMARY_ADDED"}), no_change_confirmed=False),
        "NCT2": mapping.GroundTruth(types=frozenset(), no_change_confirmed=True),
        "NCT3": mapping.GroundTruth(types=frozenset({"PRIMARY_ADDED"}), no_change_confirmed=False),
        "NCT4": mapping.GroundTruth(types=frozenset(), no_change_confirmed=True),
    }
    scores = mapping.score_by_type(predicted, truth, change_types=("PRIMARY_ADDED",))
    s = scores["PRIMARY_ADDED"]
    assert (s.tp, s.fp, s.fn, s.tn) == (1, 1, 1, 1)
    assert s.precision == 0.5
    assert s.recall == 0.5
    assert s.f1 == 0.5


def test_score_by_type_only_counts_trials_present_in_both_dicts():
    predicted = {"NCT1": frozenset({"PRIMARY_ADDED"}), "NCT_UNCACHED": frozenset({"PRIMARY_ADDED"})}
    truth = {"NCT1": mapping.GroundTruth(frozenset({"PRIMARY_ADDED"}), False), "NCT2": mapping.GroundTruth(frozenset(), True)}
    scores = mapping.score_by_type(predicted, truth, change_types=("PRIMARY_ADDED",))
    # NCT_UNCACHED has no label, NCT2 has no prediction entry (not evaluated) -> only NCT1 counted
    assert (scores["PRIMARY_ADDED"].tp, scores["PRIMARY_ADDED"].fp, scores["PRIMARY_ADDED"].fn) == (1, 0, 0)


def test_precision_and_recall_undefined_when_denominator_zero():
    predicted = {"NCT1": frozenset()}
    truth = {"NCT1": mapping.GroundTruth(frozenset(), True)}
    s = mapping.score_by_type(predicted, truth, change_types=("PRIMARY_ADDED",))["PRIMARY_ADDED"]
    assert s.precision is None
    assert s.recall is None
    assert s.f1 is None


def test_false_positive_rate_denominator_is_confirmed_no_change_trials_only():
    predicted = {
        "NCT1": frozenset({"PRIMARY_ADDED"}),  # FP: confirmed no-change, but we flagged something
        "NCT2": frozenset({"PRIMARY_ADDED"}),  # excluded: not a confirmed negative (some Holst signal)
        "NCT3": frozenset(),  # true negative
    }
    truth = {
        "NCT1": mapping.GroundTruth(frozenset(), no_change_confirmed=True),
        "NCT2": mapping.GroundTruth(frozenset({"PRIMARY_BROADENED"}), no_change_confirmed=False),
        "NCT3": mapping.GroundTruth(frozenset(), no_change_confirmed=True),
    }
    fp, denom = mapping.false_positive_rate(predicted, truth)
    assert (fp, denom) == (1, 2)  # NCT1 + NCT3 in denom, only NCT1 flagged


# --- evaluate.py split protocol ---------------------------------------------------------


def test_split_is_deterministic_and_covers_all_ids_without_overlap(tmp_path, monkeypatch):
    monkeypatch.setattr(evaluate, "SPLIT_DIR", tmp_path / "splits")
    ids = [f"NCT{i:08d}" for i in range(20)]

    dev1, heldout1 = evaluate.get_or_create_split(ids)
    assert set(dev1) | set(heldout1) == set(ids)
    assert set(dev1) & set(heldout1) == set()
    assert abs(len(dev1) - len(heldout1)) <= 1

    # second call with the SAME ids (but shuffled input order) must return the
    # identical persisted split, not reshuffle
    dev2, heldout2 = evaluate.get_or_create_split(list(reversed(ids)))
    assert dev1 == dev2
    assert heldout1 == heldout2


# --- evaluate.py --split heldout release-eval gate (v05-review.md C2) -----------------


def test_heldout_refused_without_release_eval_env(monkeypatch):
    monkeypatch.delenv("CTCM_RELEASE_EVAL", raising=False)
    with pytest.raises(SystemExit):
        evaluate._require_release_eval_gate("heldout")


def test_heldout_allowed_with_release_eval_env(monkeypatch):
    monkeypatch.setenv("CTCM_RELEASE_EVAL", "1")
    evaluate._require_release_eval_gate("heldout")  # must not raise


def test_dev_split_never_gated(monkeypatch):
    monkeypatch.delenv("CTCM_RELEASE_EVAL", raising=False)
    evaluate._require_release_eval_gate("dev")  # must not raise regardless of env


# --- evaluate.py read_pipeline_snapshot: one connection, one consistent read ------------
# (v05-review.md C1 -- previously three separate connections could each observe a
# different commit of the shared, concurrently-written data/ctcm.db.)


def test_read_pipeline_snapshot_is_internally_consistent(tmp_path, monkeypatch):
    from ctcm import config, db as ctcm_db

    monkeypatch.setattr(config, "DATA_DIR", tmp_path)
    monkeypatch.setattr(config, "CACHE_DIR", tmp_path / "cache")
    monkeypatch.setattr(config, "DB_PATH", tmp_path / "test.db")

    conn = ctcm_db.connect()
    conn.execute("INSERT INTO trials(nct_id) VALUES ('NCT1'), ('NCT2')")
    conn.execute(
        "INSERT INTO findings(nct_id, from_version, to_version, change_type, severity, confidence, "
        "resolved_by, rationale) VALUES ('NCT1', 0, 1, 'PRIMARY_ADDED', 'SIGNAL', 1.0, 'T2', 'r')"
    )
    conn.execute(
        "INSERT INTO findings(nct_id, from_version, to_version, change_type, severity, confidence, "
        "resolved_by, rationale) VALUES ('NCT1', 0, 1, 'REWORDED', 'CONTEXT', 1.0, 'T0', 'r')"
    )
    conn.commit()
    conn.close()

    have_cache, predicted, tiers = evaluate.read_pipeline_snapshot({"NCT1", "NCT2", "NCT_NOT_CACHED"})

    assert have_cache == {"NCT1", "NCT2"}  # NCT_NOT_CACHED has no row in trials -> not evaluated
    assert predicted["NCT1"] == frozenset({"PRIMARY_ADDED"})  # REWORDED excluded (not in CHANGE_TYPES)
    assert predicted["NCT2"] == frozenset()  # cached, zero findings -> still counted as evaluated
    assert tiers == {"T2": 1, "T0": 1}  # tier count covers ALL findings, not just scored types


def test_split_written_to_disk_is_frozen_even_if_label_set_changes(tmp_path, monkeypatch):
    monkeypatch.setattr(evaluate, "SPLIT_DIR", tmp_path / "splits")
    ids = [f"NCT{i:08d}" for i in range(10)]
    dev1, heldout1 = evaluate.get_or_create_split(ids)

    # simulate a re-fetched CSV with one extra trial -- split must NOT silently change
    dev2, heldout2 = evaluate.get_or_create_split(ids + ["NCT99999999"])
    assert dev1 == dev2
    assert heldout1 == heldout2
