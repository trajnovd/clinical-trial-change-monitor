import json
from datetime import date
from pathlib import Path

from ctcm.extract import extract_snapshot

FIXTURES = Path(__file__).parent / "fixtures"


def test_extract_actt1_v0_ordinal_primary_and_estimated_start():
    raw = json.loads((FIXTURES / "actt1_v0.json").read_text())
    snap = extract_snapshot(raw)

    primaries = [o for o in snap.outcomes if o.outcome_type == "PRIMARY"]
    assert len(primaries) == 1
    assert primaries[0].measure == (
        "Percentage of subjects reporting each severity rating on the 7-point ordinal scale"
    )
    assert primaries[0].time_frame == "Day 15"

    assert snap.timeline.start_date == date(2020, 3, 12)
    assert snap.timeline.start_date_type == "ESTIMATED"


def test_extract_handles_bare_protocol_section_shape():
    raw = json.loads((FIXTURES / "actt1_v0.json").read_text())
    bare = raw["study"]  # {protocolSection: ..., hasResults: ...} without the outer "study" key
    snap = extract_snapshot(bare)
    assert snap.timeline.start_date == date(2020, 3, 12)


def test_extract_missing_modules_never_raises():
    assert extract_snapshot({}) is not None
    assert extract_snapshot({"study": {}}) is not None
    assert extract_snapshot({"protocolSection": {}}) is not None
    snap = extract_snapshot({"protocolSection": {"outcomesModule": {}}})
    assert snap.outcomes == []
    assert snap.timeline.start_date is None
