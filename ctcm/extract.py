"""Cache -> normalised rows. Deterministic, no model calls (global-constraints.md).

extract_snapshot() parses one version's raw JSON into a Snapshot. load_corpus()
walks the whole cache and upserts trials/versions/outcomes/timeline_facts.
"""

import gzip
import hashlib
import json
from datetime import date

from pydantic import BaseModel

from ctcm import config, db
from ctcm.normalize import norm

OUTCOME_KEYS = (("PRIMARY", "primaryOutcomes"), ("SECONDARY", "secondaryOutcomes"), ("OTHER", "otherOutcomes"))


class OutcomeRec(BaseModel):
    outcome_type: str
    ordinal: int
    measure: str
    description: str | None = None
    time_frame: str | None = None


class TimelineRec(BaseModel):
    start_date: date | None = None
    start_date_type: str | None = None
    primary_completion_date: date | None = None
    primary_completion_type: str | None = None
    completion_date: date | None = None


class Meta(BaseModel):
    study_type: str | None = None
    phase: str | None = None
    sponsor: str | None = None
    sponsor_class: str | None = None
    conditions: list[str] = []
    enrolment_count: int | None = None
    overall_status: str | None = None
    first_posted_date: date | None = None


class Snapshot(BaseModel):
    outcomes: list[OutcomeRec] = []
    timeline: TimelineRec = TimelineRec()
    meta: Meta = Meta()


def _parse_date(s: str | None) -> date | None:
    """Leniently parse registry date strings: 'YYYY-MM-DD', 'YYYY-MM' (day->1), 'YYYY'."""
    if not s:
        return None
    parts = s.split("-")
    try:
        if len(parts) == 3:
            return date(int(parts[0]), int(parts[1]), int(parts[2]))
        if len(parts) == 2:
            return date(int(parts[0]), int(parts[1]), 1)
        if len(parts) == 1:
            return date(int(parts[0]), 1, 1)
    except ValueError:
        return None
    return None


def extract_snapshot(raw: dict) -> Snapshot:
    """Parse one version's raw JSON. Handles both {study:{protocolSection...}} and a
    bare {protocolSection:...} shape; any missing module -> empty/None, never KeyError."""
    study = raw.get("study", raw) or {}
    ps = study.get("protocolSection", {}) or {}

    om = ps.get("outcomesModule", {}) or {}
    outcomes = [
        OutcomeRec(
            outcome_type=outcome_type,
            ordinal=i,
            measure=o.get("measure") or "",
            description=o.get("description"),
            time_frame=o.get("timeFrame"),
        )
        for outcome_type, key in OUTCOME_KEYS
        for i, o in enumerate(om.get(key) or [])
    ]

    sm = ps.get("statusModule", {}) or {}
    start = sm.get("startDateStruct") or {}
    pcd = sm.get("primaryCompletionDateStruct") or {}
    comp = sm.get("completionDateStruct") or {}
    first_posted = sm.get("studyFirstPostDateStruct") or {}
    timeline = TimelineRec(
        start_date=_parse_date(start.get("date")),
        start_date_type=start.get("type"),
        primary_completion_date=_parse_date(pcd.get("date")),
        primary_completion_type=pcd.get("type"),
        completion_date=_parse_date(comp.get("date")),
    )

    scm = ps.get("sponsorCollaboratorsModule", {}) or {}
    lead_sponsor = scm.get("leadSponsor") or {}
    dm = ps.get("designModule", {}) or {}
    cm = ps.get("conditionsModule", {}) or {}
    phases = dm.get("phases") or []

    meta = Meta(
        study_type=dm.get("studyType"),
        phase=",".join(phases) if phases else None,
        sponsor=lead_sponsor.get("name"),
        sponsor_class=lead_sponsor.get("class"),
        conditions=cm.get("conditions") or [],
        enrolment_count=(dm.get("enrollmentInfo") or {}).get("count"),
        overall_status=sm.get("overallStatus"),
        first_posted_date=_parse_date(first_posted.get("date")),
    )

    return Snapshot(outcomes=outcomes, timeline=timeline, meta=meta)


def _version_of(snap_path) -> int:
    return int(snap_path.name[len("v"):-len(".json.gz")])


def _outcomes_content_hash(outcomes: list[OutcomeRec]) -> str:
    """sha256 of a canonical JSON encoding of the outcome list, for reproducible diffing."""
    canonical = [o.model_dump(mode="json") for o in outcomes]
    blob = json.dumps(canonical, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(blob.encode()).hexdigest()


def load_corpus(nct_ids: list[str] | None = None) -> None:
    """Walk data/cache/{nct}/, upsert trials/versions/outcomes/timeline_facts for every
    fetched (nct, version) snapshot found. Idempotent: INSERT OR REPLACE throughout, so
    re-running after more of the ingest finishes just adds/refreshes rows.

    nct_ids, if given, scopes the walk to just those trial directories (used by
    ctcm.monitor's scoped rerun so one changed trial doesn't force a full-corpus pass);
    default None preserves the original whole-cache-dir behaviour for every other
    caller, unchanged."""
    conn = db.connect()
    if nct_ids is not None:
        trial_dirs = [config.CACHE_DIR / nct for nct in sorted(nct_ids) if (config.CACHE_DIR / nct).is_dir()]
    else:
        trial_dirs = sorted(p for p in config.CACHE_DIR.iterdir() if p.is_dir()) if config.CACHE_DIR.exists() else []

    for trial_dir in trial_dirs:
        nct = trial_dir.name
        history_path = trial_dir / "history.json"
        if not history_path.exists():
            continue
        changes = json.loads(history_path.read_text()).get("changes", [])
        by_version = {c["version"]: c for c in changes}

        snap_paths = sorted(trial_dir.glob("v*.json.gz"), key=_version_of)
        if not snap_paths:
            continue

        last_meta: Meta | None = None
        for snap_path in snap_paths:
            version_no = _version_of(snap_path)
            raw = json.loads(gzip.decompress(snap_path.read_bytes()))
            snap = extract_snapshot(raw)
            last_meta = snap.meta

            change = by_version.get(version_no, {})
            module_labels = json.dumps(change.get("moduleLabels") or [])
            content_hash = _outcomes_content_hash(snap.outcomes)

            conn.execute(
                "INSERT OR REPLACE INTO versions(nct_id, version_no, version_date, module_labels, content_hash) "
                "VALUES (?, ?, ?, ?, ?)",
                (nct, version_no, change.get("date"), module_labels, content_hash),
            )
            conn.execute(
                "INSERT OR REPLACE INTO timeline_facts(nct_id, version_no, start_date, start_date_type, "
                "primary_completion_date, primary_completion_type, completion_date) VALUES (?, ?, ?, ?, ?, ?, ?)",
                (
                    nct,
                    version_no,
                    _iso(snap.timeline.start_date),
                    snap.timeline.start_date_type,
                    _iso(snap.timeline.primary_completion_date),
                    snap.timeline.primary_completion_type,
                    _iso(snap.timeline.completion_date),
                ),
            )
            for o in snap.outcomes:
                conn.execute(
                    "INSERT OR REPLACE INTO outcomes(nct_id, version_no, outcome_type, ordinal, measure, "
                    "description, time_frame, measure_norm) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                    (nct, version_no, o.outcome_type, o.ordinal, o.measure, o.description, o.time_frame, norm(o.measure)),
                )

        if last_meta is not None:
            conn.execute(
                "INSERT OR REPLACE INTO trials(nct_id, study_type, phase, overall_status, lead_sponsor, "
                "sponsor_class, conditions, enrolment_count, first_posted_date, version_count) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    nct,
                    last_meta.study_type,
                    last_meta.phase,
                    last_meta.overall_status,
                    last_meta.sponsor,
                    last_meta.sponsor_class,
                    json.dumps(last_meta.conditions),
                    last_meta.enrolment_count,
                    _iso(last_meta.first_posted_date),
                    len(changes),
                ),
            )
        conn.commit()
    conn.close()


def _iso(d: date | None) -> str | None:
    return d.isoformat() if d else None


if __name__ == "__main__":
    load_corpus()
    c = db.connect()
    for table in ("trials", "versions", "outcomes", "timeline_facts"):
        (n,) = c.execute(f"SELECT COUNT(*) FROM {table}").fetchone()
        print(f"{table}: {n} rows")
    c.close()
