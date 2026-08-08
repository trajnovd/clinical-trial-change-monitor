from ctcm.normalize import ABBREV, norm, split_timepoint


def test_ham_d_and_spelled_out_scale_share_expanded_name():
    # TECH §5.1: low text similarity, same underlying measure -- abbreviation
    # expansion must make the shared scale name visible in both norm()s.
    a = norm("Change in HAM-D from baseline to week 6")
    b = norm("Hamilton Depression Rating Scale improvement at 6 weeks")
    assert "hamilton depression rating scale" in a
    assert "hamilton depression rating scale" in b


def test_all_cause_vs_cardiovascular_mortality_stay_different():
    # TECH §5.1: high text similarity, genuinely different measure -- normalisation
    # must NOT erase the qualifier that makes them different.
    assert norm("All-cause mortality at 12 months") != norm("Cardiovascular mortality at 12 months")


def test_norm_lowercases_strips_punctuation_collapses_whitespace():
    assert norm("  Overall  Survival, (ITT)!  ") == "overall survival itt"


def test_norm_expands_pfs_to_progression_free_survival():
    assert norm("PFS at week 12") == norm("progression free survival at week 12")


def test_abbrev_has_at_least_40_entries():
    assert len(ABBREV) >= 40


def test_hamd_no_hyphen_spelling_also_expands():
    assert "hamilton depression rating scale" in norm("HAMD total score")


def test_split_timepoint_tail_at_months():
    stem, tp = split_timepoint("Overall survival at 24 months")
    assert stem == "Overall survival"
    assert tp == "at 24 months"


def test_split_timepoint_tail_through_day():
    stem, tp = split_timepoint("Change from baseline in ALT through Day 29")
    assert stem == "Change from baseline in ALT"
    assert tp == "through Day 29"


def test_split_timepoint_no_trailing_timepoint_returns_none():
    stem, tp = split_timepoint("Overall survival")
    assert stem == "Overall survival"
    assert tp is None
