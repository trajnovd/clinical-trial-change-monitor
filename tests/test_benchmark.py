"""Pure unit tests for benchmark/mapping.py (row->GroundTruth mapping, join
scoring) and benchmark/evaluate.py's split logic. No network, no real CSV, no
shared db -- small in-memory fixtures only."""

import importlib.util
import sys
from pathlib import Path

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


def test_omitted_measurement_is_unmapped_not_a_type():
    gt = mapping.trial_ground_truth(row(change_a_i_omitted_measurement="1"))
    assert gt.types == frozenset()
    assert gt.has_unmapped_change is True
    assert gt.no_change_confirmed is False  # something *did* change, just unmappable


# --- Axis-2 (post-completion phases collapse to POST_COMPLETION_CHANGE) ---------------


def test_i_p_new_primary_maps_to_post_completion_change_not_primary_added():
    gt = mapping.trial_ground_truth(row(change_i_p_new_primary="1"))
    assert gt.types == {"POST_COMPLETION_CHANGE"}


def test_p_l_change_timing_maps_to_post_completion_change_not_timepoint_changed():
    gt = mapping.trial_ground_truth(row(change_p_l_change_timing="1"))
    assert gt.types == {"POST_COMPLETION_CHANGE"}


def test_i_p_unmapped_only_still_flags_post_completion_change():
    """Our pipeline's PCD override doesn't care about narrow-vs-broaden -- any
    detected primary change after completion becomes POST_COMPLETION_CHANGE, so
    even an unmappable omitted_* broadening in this phase should count."""
    gt = mapping.trial_ground_truth(row(change_i_p_omitted_measurement="1"))
    assert gt.types == {"POST_COMPLETION_CHANGE"}
    assert gt.has_unmapped_change is True


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
        "NCT1": mapping.GroundTruth(types=frozenset({"PRIMARY_ADDED"}), has_unmapped_change=False, no_change_confirmed=False),
        "NCT2": mapping.GroundTruth(types=frozenset(), has_unmapped_change=False, no_change_confirmed=True),
        "NCT3": mapping.GroundTruth(types=frozenset({"PRIMARY_ADDED"}), has_unmapped_change=False, no_change_confirmed=False),
        "NCT4": mapping.GroundTruth(types=frozenset(), has_unmapped_change=False, no_change_confirmed=True),
    }
    scores = mapping.score_by_type(predicted, truth, change_types=("PRIMARY_ADDED",))
    s = scores["PRIMARY_ADDED"]
    assert (s.tp, s.fp, s.fn, s.tn) == (1, 1, 1, 1)
    assert s.precision == 0.5
    assert s.recall == 0.5
    assert s.f1 == 0.5


def test_score_by_type_only_counts_trials_present_in_both_dicts():
    predicted = {"NCT1": frozenset({"PRIMARY_ADDED"}), "NCT_UNCACHED": frozenset({"PRIMARY_ADDED"})}
    truth = {"NCT1": mapping.GroundTruth(frozenset({"PRIMARY_ADDED"}), False, False), "NCT2": mapping.GroundTruth(frozenset(), False, True)}
    scores = mapping.score_by_type(predicted, truth, change_types=("PRIMARY_ADDED",))
    # NCT_UNCACHED has no label, NCT2 has no prediction entry (not evaluated) -> only NCT1 counted
    assert (scores["PRIMARY_ADDED"].tp, scores["PRIMARY_ADDED"].fp, scores["PRIMARY_ADDED"].fn) == (1, 0, 0)


def test_precision_and_recall_undefined_when_denominator_zero():
    predicted = {"NCT1": frozenset()}
    truth = {"NCT1": mapping.GroundTruth(frozenset(), False, True)}
    s = mapping.score_by_type(predicted, truth, change_types=("PRIMARY_ADDED",))["PRIMARY_ADDED"]
    assert s.precision is None
    assert s.recall is None
    assert s.f1 is None


def test_false_positive_rate_excludes_unmapped_only_trials():
    predicted = {
        "NCT1": frozenset({"PRIMARY_ADDED"}),  # FP: confirmed no-change, but we flagged something
        "NCT2": frozenset({"PRIMARY_ADDED"}),  # excluded: only unmapped signal, not a confirmed negative
        "NCT3": frozenset(),  # true negative
    }
    truth = {
        "NCT1": mapping.GroundTruth(frozenset(), False, no_change_confirmed=True),
        "NCT2": mapping.GroundTruth(frozenset(), True, no_change_confirmed=False),
        "NCT3": mapping.GroundTruth(frozenset(), False, no_change_confirmed=True),
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


def test_split_written_to_disk_is_frozen_even_if_label_set_changes(tmp_path, monkeypatch):
    monkeypatch.setattr(evaluate, "SPLIT_DIR", tmp_path / "splits")
    ids = [f"NCT{i:08d}" for i in range(10)]
    dev1, heldout1 = evaluate.get_or_create_split(ids)

    # simulate a re-fetched CSV with one extra trial -- split must NOT silently change
    dev2, heldout2 = evaluate.get_or_create_split(ids + ["NCT99999999"])
    assert dev1 == dev2
    assert heldout1 == heldout2
