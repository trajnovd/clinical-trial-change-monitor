from datetime import date

from ctcm.classify import OutcomeRow, classify, diff_pair, t0_matcher, timeline_revised
from ctcm.timeline import Anchors, Revision


def _row(outcome_type, ordinal, measure, time_frame=None, measure_norm=None, nct="NCT1", version_no=0):
    return OutcomeRow(
        nct_id=nct,
        version_no=version_no,
        outcome_type=outcome_type,
        ordinal=ordinal,
        measure=measure,
        description=None,
        time_frame=time_frame,
        measure_norm=measure_norm if measure_norm is not None else measure.lower(),
    )


# ---- diff_pair --------------------------------------------------------------------


def test_diff_pair_identical_outcomes_produce_no_changes():
    before = [_row("PRIMARY", 0, "Overall survival")]
    after = [_row("PRIMARY", 0, "Overall survival")]
    assert diff_pair(before, after, t0_matcher) == []


def test_diff_pair_detects_demotion():
    before = [_row("PRIMARY", 0, "Overall survival")]
    after = [_row("SECONDARY", 0, "Overall survival")]
    changes = diff_pair(before, after, t0_matcher)
    assert len(changes) == 1
    assert changes[0].kind == "DEMOTED"


def test_diff_pair_detects_promotion():
    before = [_row("SECONDARY", 0, "Overall survival")]
    after = [_row("PRIMARY", 0, "Overall survival")]
    changes = diff_pair(before, after, t0_matcher)
    assert len(changes) == 1
    assert changes[0].kind == "PROMOTED"


def test_diff_pair_detects_timepoint_change_same_measure():
    before = [_row("PRIMARY", 0, "ALT", time_frame="Day 15")]
    after = [_row("PRIMARY", 0, "ALT", time_frame="Day 29")]
    changes = diff_pair(before, after, t0_matcher)
    assert len(changes) == 1
    assert changes[0].kind == "TIMEPOINT"


def test_diff_pair_detects_reworded_same_norm_different_raw_text():
    before = [_row("PRIMARY", 0, "OS", measure_norm="overall survival")]
    after = [_row("PRIMARY", 0, "Overall Survival", measure_norm="overall survival")]
    changes = diff_pair(before, after, t0_matcher)
    assert len(changes) == 1
    assert changes[0].kind == "REWORDED"


def test_diff_pair_collapses_added_and_removed_same_ordinal_into_replaced():
    before = [_row("PRIMARY", 0, "7-point ordinal scale")]
    after = [_row("PRIMARY", 0, "Time to recovery")]
    changes = diff_pair(before, after, t0_matcher)
    assert len(changes) == 1
    assert changes[0].kind == "REPLACED"
    assert changes[0].before.measure == "7-point ordinal scale"
    assert changes[0].after.measure == "Time to recovery"


def test_diff_pair_pure_addition_no_matching_removal():
    before = []
    after = [_row("PRIMARY", 0, "New endpoint")]
    changes = diff_pair(before, after, t0_matcher)
    assert len(changes) == 1
    assert changes[0].kind == "ADDED"
    assert changes[0].before is None


def test_diff_pair_pure_removal_no_matching_addition():
    before = [_row("PRIMARY", 0, "Dropped endpoint")]
    after = []
    changes = diff_pair(before, after, t0_matcher)
    assert len(changes) == 1
    assert changes[0].kind == "REMOVED"
    assert changes[0].after is None


# ---- classify -----------------------------------------------------------------------

ANCHORS_POST_ENROL = Anchors(start=date(2020, 2, 21), start_type="ACTUAL", pcd=date(2020, 12, 1), pcd_type="ESTIMATED", revisions=[])


def test_classify_demote_post_enrolment_is_signal():
    before = [_row("PRIMARY", 0, "Overall survival")]
    after = [_row("SECONDARY", 0, "Overall survival")]
    changes = diff_pair(before, after, t0_matcher)
    findings = classify("NCT1", 0, 1, date(2020, 4, 16), changes, ANCHORS_POST_ENROL)
    assert len(findings) == 1
    assert findings[0].change_type == "PRIMARY_DEMOTED"
    assert findings[0].severity == "SIGNAL"
    assert findings[0].days_after_enrolment == 55


def test_classify_same_change_pre_enrolment_is_context():
    before = [_row("PRIMARY", 0, "Overall survival")]
    after = [_row("SECONDARY", 0, "Overall survival")]
    changes = diff_pair(before, after, t0_matcher)
    # before the 2020-02-21 start date
    findings = classify("NCT1", 0, 1, date(2020, 1, 1), changes, ANCHORS_POST_ENROL)
    assert len(findings) == 1
    assert findings[0].change_type == "PRIMARY_DEMOTED"
    assert findings[0].severity == "CONTEXT"


def test_classify_added_removed_collapse_is_primary_replaced_signal():
    before = [_row("PRIMARY", 0, "7-point ordinal scale")]
    after = [_row("PRIMARY", 0, "Time to recovery")]
    changes = diff_pair(before, after, t0_matcher)
    findings = classify("NCT04280705", 0, 14, date(2020, 4, 16), changes, ANCHORS_POST_ENROL)
    assert len(findings) == 1
    assert findings[0].change_type == "PRIMARY_REPLACED"
    assert findings[0].severity == "SIGNAL"
    assert findings[0].before_measure == "7-point ordinal scale"
    assert findings[0].after_measure == "Time to recovery"


def test_classify_post_completion_change_overrides_to_highest_severity():
    before = [_row("PRIMARY", 0, "Overall survival")]
    after = [_row("PRIMARY", 0, "Progression free survival")]
    changes = diff_pair(before, after, t0_matcher)
    findings = classify("NCT1", 0, 1, date(2021, 1, 1), changes, ANCHORS_POST_ENROL)  # after pcd=2020-12-01
    assert len(findings) == 1
    assert findings[0].change_type == "POST_COMPLETION_CHANGE"
    assert findings[0].severity == "SIGNAL"


def test_classify_reworded_is_always_context_even_post_completion():
    before = [_row("PRIMARY", 0, "OS", measure_norm="overall survival")]
    after = [_row("PRIMARY", 0, "Overall Survival", measure_norm="overall survival")]
    changes = diff_pair(before, after, t0_matcher)
    findings = classify("NCT1", 0, 1, date(2021, 1, 1), changes, ANCHORS_POST_ENROL)
    assert len(findings) == 1
    assert findings[0].change_type == "REWORDED"
    assert findings[0].severity == "CONTEXT"


def test_classify_secondary_only_change_is_not_emitted():
    before = [_row("SECONDARY", 0, "Adverse events")]
    after = [_row("SECONDARY", 0, "Serious adverse events")]
    changes = diff_pair(before, after, t0_matcher)
    findings = classify("NCT1", 0, 1, date(2020, 4, 16), changes, ANCHORS_POST_ENROL)
    assert findings == []


def test_classify_promote_maps_to_secondary_promoted():
    before = [_row("SECONDARY", 0, "Overall survival")]
    after = [_row("PRIMARY", 0, "Overall survival")]
    changes = diff_pair(before, after, t0_matcher)
    findings = classify("NCT1", 0, 1, date(2020, 4, 16), changes, ANCHORS_POST_ENROL)
    assert len(findings) == 1
    assert findings[0].change_type == "SECONDARY_PROMOTED"
    assert findings[0].severity == "SIGNAL"


def test_classify_timepoint_change_maps_to_timepoint_changed():
    before = [_row("PRIMARY", 0, "ALT", time_frame="Day 15")]
    after = [_row("PRIMARY", 0, "ALT", time_frame="Day 29")]
    changes = diff_pair(before, after, t0_matcher)
    findings = classify("NCT1", 0, 1, date(2020, 4, 16), changes, ANCHORS_POST_ENROL)
    assert len(findings) == 1
    assert findings[0].change_type == "TIMEPOINT_CHANGED"


def test_classify_primary_removed_with_no_replacement():
    before = [_row("PRIMARY", 0, "Dropped endpoint")]
    after = []
    changes = diff_pair(before, after, t0_matcher)
    findings = classify("NCT1", 0, 1, date(2020, 4, 16), changes, ANCHORS_POST_ENROL)
    assert len(findings) == 1
    assert findings[0].change_type == "PRIMARY_REMOVED"
    assert findings[0].severity == "SIGNAL"


def test_classify_primary_added_with_no_prior_primary():
    before = []
    after = [_row("PRIMARY", 0, "New endpoint")]
    changes = diff_pair(before, after, t0_matcher)
    findings = classify("NCT1", 0, 1, date(2020, 4, 16), changes, ANCHORS_POST_ENROL)
    assert len(findings) == 1
    assert findings[0].change_type == "PRIMARY_ADDED"
    assert findings[0].severity == "SIGNAL"


def test_classify_extra_added_beyond_collapse_count_stays_primary_added():
    # 1 removed + 2 added, same type: one pair collapses to REPLACED, the
    # leftover addition stays PRIMARY_ADDED.
    before = [_row("PRIMARY", 0, "Dropped endpoint")]
    after = [_row("PRIMARY", 0, "Replacement endpoint"), _row("PRIMARY", 1, "Bonus new endpoint")]
    changes = diff_pair(before, after, t0_matcher)
    findings = classify("NCT1", 0, 1, date(2020, 4, 16), changes, ANCHORS_POST_ENROL)
    codes = sorted(f.change_type for f in findings)
    assert codes == ["PRIMARY_ADDED", "PRIMARY_REPLACED"]


# ---- timeline_revised -----------------------------------------------------------------


def test_timeline_revised_emits_one_finding_per_revision():
    anc = Anchors(
        start=date(2020, 1, 1),
        start_type="ESTIMATED",
        pcd=None,
        pcd_type=None,
        revisions=[Revision(field="start", from_version=0, to_version=3, old=date(2020, 1, 1), new=date(2020, 6, 1))],
    )
    findings = timeline_revised("NCT1", anc)
    assert len(findings) == 1
    assert findings[0].change_type == "TIMELINE_REVISED"
    assert findings[0].severity == "CONTEXT"
    assert findings[0].from_version == 0
    assert findings[0].to_version == 3
