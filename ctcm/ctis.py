"""EU CTIS adapter (v2 Task 15) -- PROSPECTIVE ONLY.

CTIS (euclinicaltrials.eu) runs an undocumented but public, unauthenticated REST
API (endpoints verified live 2026-08-09, .superpowers/sdd/2026-08-08-trial-registry-
monitor/v2-research.md): POST /search and GET /retrieve/{ctNumber}. Both return
CURRENT STATE ONLY -- the pre-2024-06-17 relaunch system reportedly exposed a
version/history field that no longer exists (v2-research.md, decisive evidence from
the `ctrdata` R package's own source and doc comments). There is no CTIS-native
"give me this trial as of date X" call, and there never will be a backfill: this
adapter's own snapshot history starts the day it first snapshots a trial, and
EU coverage of this whole product begins at that adoption date, not retroactively.

Design consequence: OUR "versions" are self-manufactured, not registry-provided.
snapshot_pass() polls GET /retrieve/{ctNumber} for each watched trial and keeps a
new gzipped copy (data/cache/ctis/{ctNumber}/snap-{YYYY-MM-DD}.json.gz, or
snap-{YYYY-MM-DD}-{N}.json.gz for a same-day collision -- see _write_snapshot) ONLY
when its outcome-relevant content hash differs from the most recently kept snapshot
(or none exists yet) -- same sha256-of-canonical-JSON discipline as
ctcm.extract._outcomes_content_hash, reimplemented here (not imported) because it
operates on this module's plain-dict outcome shape, not extract.py's pydantic
OutcomeRec. Every kept snapshot IS a version by construction, one physical file per
version, so there is no separate "which of the fetched revisions do we bother
keeping" fetch rule the way ctcm.ingest/ctcm.monitor need for CT.gov's real history
list.

CTISAdapter (the ctcm.monitor.RegistryAdapter shape, commit 42d31f3) is a pure
LOCAL reader over that snapshot series -- list_versions()/fetch_version() never
touch the network, unlike ctcm.monitor.CTGovAdapter. For CTIS, "check whether a new
version exists" and "fetch the live state" are the same single network call, not
two, so snapshot_pass() does its own fetching directly rather than routing through
this class; CTISAdapter exists to satisfy the shared protocol structurally (so a
future generic consumer could read either registry's version series the same way).

Naming debt (documented per the brief, not fixed here): trials.nct_id is a TEXT
PRIMARY KEY sized and named for ClinicalTrials.gov's NCT format. CTIS rows store
their ctNumber (format "2025-523333-26-00") in that same nct_id column -- no schema
change, no collision (the formats never overlap), but the column name is now a
misnomer for a fifth of the table. trials.registry ('ctgov' default | 'ctis') is
what actually tells the two apart; added via a guarded ALTER (ensure_schema below),
same pattern as ctcm.monitor.ensure_schema's findings.first_seen_at.

Field-mapping honesty notes (v2-research.md's payload dump, cross-checked live
against a real CTIS retrieve() response while building this adapter -- see
task15-report.md for the probe transcript):
  - Outcomes live at authorizedApplication.authorizedPartI.trialDetails
    .trialInformation.endPoint.{primary,secondary}EndPoints -- each item is
    {id, number, endPoint (free text), isPrimary, ...per-language translations
    we don't use}. There is no dedicated per-endpoint timeframe field (unlike
    CT.gov's outcomesModule[].timeFrame) -- outcomes.time_frame is left NULL
    rather than regex-guessed out of the free-text endpoint description.
  - trials.overall_status stores CTIS's own `ctStatus` string (e.g. "Authorised",
    "Under evaluation") -- this is an AUTHORISATION status, not a recruitment
    status. CTIS exposes no single recruitment-status field at the trial level,
    only a per-member-state clinicalTrialStatusHistory array; collapsing that to
    one value was judged out of scope for this task and not attempted.
  - trials.first_posted_date stores `decisionDate` (truncated to a date) --
    CTIS's closest analog to CT.gov's studyFirstPostDate, not an exact match.
  - trials.phase maps the numeric `trialCategory.trialPhase` code via a small
    table built from codes actually observed live (1/2/3/5/6); an unseen code
    is stored as "CODE_{n}" rather than guessed.
  - CTIS's own /search has no verified recruitment-status filter key (several
    plausible key names were probed live and silently ignored -- see
    task15-report.md); search_watchlist() approximates "ongoing" client-side via
    resultsFirstReceived == "No" (no results posted yet -> still running or not
    yet reported).

Findings flow through the SAME pipeline unchanged: once snapshot_pass() upserts
rows into the existing trials/versions/outcomes tables, ctcm.pipeline.run_pipeline()
(unmodified, already iterates every nct_id in trials regardless of registry) picks
CTIS trials up exactly like CT.gov ones the next time it's run (`make pipeline`).
timeline_facts is intentionally left unpopulated for CTIS trials -- the brief scopes
field mapping to the outcomes schema only, and ctcm.timeline.anchors()/classify()
already handle a trial with no timeline_facts rows gracefully (Anchors of all-None,
severity defaults to SIGNAL rather than being suppressed -- see classify.py).
"""

import asyncio
import gzip
import hashlib
import json
import logging
import sqlite3
import time
from dataclasses import dataclass
from datetime import date, datetime, timezone
from pathlib import Path

import httpx

from ctcm import config, db
from ctcm.normalize import norm

logger = logging.getLogger(__name__)

CTIS_BASE = "https://euclinicaltrials.eu/ctis-public-api"
SEARCH_URL = f"{CTIS_BASE}/search"
RETRIEVE_URL = f"{CTIS_BASE}/retrieve"

PHASE_3_SEARCH_CODE = "5"  # verified live 2026-08-09 against /search (task15-report.md probe)
MAX_RETRIES = 5
MIN_INTERVAL = 0.5  # ponytail: measured-guess pacing floor, same politeness reasoning as
# ctcm.monitor.CTGovAdapter (that class's docstring) -- CTIS's rate-limit behavior is
# equally undocumented (v2-research.md's Risks section); upgrade path: retune or swap in a
# real token bucket if a real run still 429s at this floor.

# Numeric trialCategory.trialPhase codes actually observed live 2026-08-09 (task15-report.md);
# an unseen code is never guessed (see module docstring).
_PHASE_CODE_LABELS = {
    "1": "PHASE1",  # Human Pharmacology (Phase I) - First administration to humans
    "2": "PHASE1",  # Human Pharmacology (Phase I) - Bioequivalence Study
    "3": "PHASE1",  # Human Pharmacology (Phase I) - Other
    "5": "PHASE3",  # Therapeutic confirmatory (Phase III)
    "6": "PHASE4",  # Therapeutic use (Phase IV)
}


# ---- schema guard: trials.registry column -----------------------------------------------


def ensure_schema(conn: sqlite3.Connection) -> None:
    """Idempotent, same PRAGMA-guarded-ALTER pattern as ctcm.monitor.ensure_schema (sqlite
    has no 'ADD COLUMN IF NOT EXISTS'; belt-and-braces except for a concurrent agent's own
    ctis pass winning the race between the check and the ALTER -- data/ctcm.db is shared,
    per the task brief)."""
    cols = {r["name"] for r in conn.execute("PRAGMA table_info(trials)")}
    if "registry" in cols:
        return
    try:
        conn.execute("ALTER TABLE trials ADD COLUMN registry TEXT NOT NULL DEFAULT 'ctgov'")
        conn.commit()
    except sqlite3.OperationalError as e:
        if "duplicate column" not in str(e).lower():
            raise


# ---- field mapping (pure, no network) ------------------------------------------------


def _trial_information(raw: dict) -> tuple[dict, dict]:
    part_i = ((raw.get("authorizedApplication") or {}).get("authorizedPartI")) or {}
    trial_info = ((part_i.get("trialDetails") or {}).get("trialInformation")) or {}
    return part_i, trial_info


def _phase_label(code: str | None) -> str | None:
    if code is None:
        return None
    return _PHASE_CODE_LABELS.get(code, f"CODE_{code}")


def map_outcomes(raw: dict) -> list[dict]:
    """CTIS retrieve() payload -> outcomes-schema shape: [{outcome_type, ordinal, measure,
    description, time_frame}, ...], PRIMARY then SECONDARY, ordinal reset per type (matches
    ctcm.extract's OUTCOME_KEYS convention)."""
    _, ti = _trial_information(raw)
    endpoints = ti.get("endPoint") or {}
    out = []
    for outcome_type, key in (("PRIMARY", "primaryEndPoints"), ("SECONDARY", "secondaryEndPoints")):
        for i, e in enumerate(endpoints.get(key) or []):
            out.append(
                {
                    "outcome_type": outcome_type,
                    "ordinal": i,
                    "measure": (e.get("endPoint") or "").strip(),
                    "description": None,
                    "time_frame": None,  # not derivable -- see module docstring
                }
            )
    return out


def map_meta(raw: dict) -> dict:
    """CTIS retrieve() payload -> trials-row shape. See module docstring for the honest
    caveats on overall_status/first_posted_date/phase."""
    part_i, ti = _trial_information(raw)
    sponsors = part_i.get("sponsors") or []
    primary_sponsor = next((s for s in sponsors if s.get("primary")), sponsors[0] if sponsors else {})
    org = primary_sponsor.get("organisation") or {}
    conditions = [c.get("medicalCondition") for c in (part_i.get("medicalConditions") or []) if c.get("medicalCondition")]
    trial_category = ti.get("trialCategory") or {}
    decision_date = (raw.get("decisionDate") or "")[:10] or None

    return {
        "study_type": "INTERVENTIONAL",  # CTIS/Regulation (EU) 536/2014 scope is interventional CTs only
        "phase": _phase_label(trial_category.get("trialPhase")),
        "sponsor": org.get("name"),
        "sponsor_class": org.get("type"),
        "conditions": conditions,
        "enrolment_count": part_i.get("rowSubjectCount"),
        "overall_status": raw.get("ctStatus"),
        "first_posted_date": decision_date,
    }


def outcomes_content_hash(outcomes: list[dict]) -> str:
    """sha256 of canonical JSON -- same discipline as ctcm.extract._outcomes_content_hash
    (sort_keys, compact separators), reimplemented (not imported) for this module's plain-
    dict outcome shape rather than extract.py's pydantic OutcomeRec. This IS the "OUR
    versions differ" gate: two snapshots with identical outcome_type/ordinal/measure/
    description/time_frame across every outcome hash identically regardless of any other
    field CTIS returns (sponsor edits, contact changes, etc. never mint a version)."""
    canonical = [
        {
            "outcome_type": o["outcome_type"],
            "ordinal": o["ordinal"],
            "measure": o["measure"],
            "description": o["description"],
            "time_frame": o["time_frame"],
        }
        for o in outcomes
    ]
    blob = json.dumps(canonical, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(blob.encode()).hexdigest()


# ---- local snapshot cache: data/cache/ctis/{ctNumber}/snap-{date}.json.gz ---------------


def _trial_dir(ct_number: str) -> Path:
    # computed from config.CACHE_DIR on every call (not cached at import time) so tests can
    # monkeypatch config.CACHE_DIR, matching ctcm.ingest._trial_dir's own pattern.
    d = config.CACHE_DIR / "ctis" / ct_number
    d.mkdir(parents=True, exist_ok=True)
    return d


def _parse_snap_stem(stem: str) -> tuple[date, int]:
    """stem is a snap file's name minus the '.json.gz' suffix: 'YYYY-MM-DD' (a day's
    first kept snapshot, intraday order 1) or 'YYYY-MM-DD-N' (N=2,3,... -- a same-day
    collision suffix, task15-review.md Important #2). Raises ValueError on anything
    else, same as a bare date.fromisoformat would."""
    if len(stem) == 10:
        return date.fromisoformat(stem), 1
    date_part, _, n = stem.rpartition("-")
    return date.fromisoformat(date_part), int(n)


def _snap_date(path: Path) -> date:
    return _parse_snap_stem(path.name[len("snap-") : -len(".json.gz")])[0]


def _local_snapshots(ct_number: str) -> list[Path]:
    """Every kept snapshot FILE for this ct_number, oldest first (chronological, then
    same-day collision order) -- the local "version series" both CTISAdapter and
    snapshot_pass's hash gate read from. One entry per physical file, not per calendar
    date: a same-day collision (I2 fix, _write_snapshot below) keeps its own file and
    therefore its own entry here, so `len(...)` -- used for version_no assignment --
    never undercounts a genuinely-kept intraday snapshot. Directory may not exist yet
    (first-ever snapshot_pass for this trial) -- that's an empty series, not an error."""
    d = config.CACHE_DIR / "ctis" / ct_number
    if not d.exists():
        return []
    paths = []
    for p in d.glob("snap-*.json.gz"):
        try:
            _parse_snap_stem(p.name[len("snap-") : -len(".json.gz")])
        except ValueError:
            continue
        paths.append(p)
    return sorted(paths, key=lambda p: _parse_snap_stem(p.name[len("snap-") : -len(".json.gz")]))


def _read_snapshot(path: Path) -> dict:
    return json.loads(gzip.decompress(path.read_bytes()))


def _write_snapshot(ct_number: str, snap_date: date, raw: dict) -> Path:
    """Writes a new kept snapshot. Never overwrites an existing file: a same-day second
    (or third, ...) call whose content genuinely differs gets a -2/-3/... suffix instead
    of clobbering snap-{date}.json.gz -- the designed cadence is one pass/day, so this is
    a rare edge rather than the common case, but it must never destroy a prior call's raw
    payload (task15-review.md Important #2: the old behavior silently overwrote it,
    orphaning the earlier versions row from any backing file). Returns the path written.
    # ponytail: unbounded linear probe for the next free suffix -- fine at same-day
    # collision counts in the single digits; revisit if same-day multi-snapshot ever
    # becomes routine rather than a rare edge."""
    path = _trial_dir(ct_number) / f"snap-{snap_date.isoformat()}.json.gz"
    n = 2
    while path.exists():
        path = _trial_dir(ct_number) / f"snap-{snap_date.isoformat()}-{n}.json.gz"
        n += 1
    path.write_bytes(gzip.compress(json.dumps(raw).encode()))
    return path


# ---- RegistryAdapter (ctcm.monitor protocol), local-only ------------------------------


class CTISAdapter:
    """ctcm.monitor.RegistryAdapter shape over CTIS's local snapshot series -- see module
    docstring for why this never makes a network call itself."""

    async def list_versions(self, ct_number: str) -> list[dict]:
        return [{"version": i, "date": _snap_date(p).isoformat(), "labels": []} for i, p in enumerate(_local_snapshots(ct_number))]

    async def fetch_version(self, ct_number: str, version: int) -> dict:
        return _read_snapshot(_local_snapshots(ct_number)[version])


# ---- HTTP: serialized dispatch with a pacing floor, backoff on 429/5xx ------------------


class _Pacer:
    """Bare monotonic-clock floor between dispatches -- no lock needed (unlike
    ctcm.monitor.CTGovAdapter's _lock): snapshot_pass()/search_watchlist() are already plain
    sequential loops (brief: "Serialize requests"), never called concurrently against the
    same instance."""

    def __init__(self, min_interval: float = MIN_INTERVAL):
        self.min_interval = min_interval
        self._next_ok = 0.0

    async def wait(self) -> None:
        now = time.monotonic()
        if self._next_ok > now:
            await asyncio.sleep(self._next_ok - now)
        self._next_ok = time.monotonic() + self.min_interval


async def _request(client: httpx.AsyncClient, method: str, url: str, **kwargs) -> dict:
    """Same backoff shape as ctcm.ingest._get_json/ctcm.publink._get_json -- duplicated
    rather than imported (both of those are bound to a different client/endpoint shape;
    this one needs both GET and POST against one base)."""
    delay = 1.0
    for attempt in range(MAX_RETRIES):
        resp = await client.request(method, url, **kwargs)
        if resp.status_code == 429 or resp.status_code >= 500:
            if attempt == MAX_RETRIES - 1:
                resp.raise_for_status()
            await asyncio.sleep(delay)
            delay *= 2
            continue
        resp.raise_for_status()
        return resp.json()
    raise RuntimeError(f"unreachable: retries exhausted for {url}")  # pragma: no cover


# ---- bootstrap: search CTIS for the starter watchlist -----------------------------------


async def search_watchlist(
    client: httpx.AsyncClient, target: int = 200, phase_code: str = PHASE_3_SEARCH_CODE, page_size: int = 100
) -> list[str]:
    """POST /search, paginated, trialPhaseCode=[phase_code] (phase 3 by default -- verified
    live), sorted decisionDate DESC (most recently authorised trials first -- CTIS is young
    enough that recency is a reasonable proxy for "still active"). "recruiting/ongoing" is
    approximated client-side via resultsFirstReceived == 'No' (see module docstring) since
    no verified recruitment-status search-filter key exists."""
    pacer = _Pacer()
    ct_numbers: list[str] = []
    page = 1
    while len(ct_numbers) < target:
        body = {
            "pagination": {"page": page, "size": page_size},
            "sort": {"property": "decisionDate", "direction": "DESC"},
            "searchCriteria": {"trialPhaseCode": [phase_code]},
        }
        await pacer.wait()
        data = await _request(client, "POST", SEARCH_URL, json=body)
        rows = data.get("data") or []
        if not rows:
            break
        for r in rows:
            ct = r.get("ctNumber")
            if ct and r.get("resultsFirstReceived") == "No":
                ct_numbers.append(ct)
        if not (data.get("pagination") or {}).get("nextPage"):
            break
        page += 1
    return ct_numbers[:target]


def write_watchlist(ct_numbers: list[str]) -> None:
    path = config.DATA_DIR / "ctis_watchlist.txt"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(ct_numbers) + ("\n" if ct_numbers else ""))


def read_watchlist() -> list[str]:
    path = config.DATA_DIR / "ctis_watchlist.txt"
    if not path.exists():
        return []
    return [line.strip() for line in path.read_text().splitlines() if line.strip()]


# ---- persistence: trials/versions/outcomes upserts (registry='ctis') --------------------


def _upsert_trial(conn: sqlite3.Connection, ct_number: str, meta: dict, version_count: int) -> None:
    conn.execute(
        "INSERT INTO trials(nct_id, study_type, phase, overall_status, lead_sponsor, sponsor_class, "
        "conditions, enrolment_count, first_posted_date, version_count, registry) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,'ctis') "
        "ON CONFLICT(nct_id) DO UPDATE SET study_type=excluded.study_type, phase=excluded.phase, "
        "overall_status=excluded.overall_status, lead_sponsor=excluded.lead_sponsor, "
        "sponsor_class=excluded.sponsor_class, conditions=excluded.conditions, "
        "enrolment_count=excluded.enrolment_count, first_posted_date=excluded.first_posted_date, "
        "version_count=excluded.version_count, registry='ctis'",
        (
            ct_number,
            meta["study_type"],
            meta["phase"],
            meta["overall_status"],
            meta["sponsor"],
            meta["sponsor_class"],
            json.dumps(meta["conditions"]),
            meta["enrolment_count"],
            meta["first_posted_date"],
            version_count,
        ),
    )


def _upsert_version(conn: sqlite3.Connection, ct_number: str, version_no: int, snap_date: date, content_hash: str) -> None:
    conn.execute(
        "INSERT OR REPLACE INTO versions(nct_id, version_no, version_date, module_labels, content_hash) "
        "VALUES (?, ?, ?, ?, ?)",
        (ct_number, version_no, snap_date.isoformat(), json.dumps([]), content_hash),
    )


def _upsert_outcomes(conn: sqlite3.Connection, ct_number: str, version_no: int, outcomes: list[dict]) -> None:
    conn.executemany(
        "INSERT OR REPLACE INTO outcomes(nct_id, version_no, outcome_type, ordinal, measure, description, "
        "time_frame, measure_norm) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        [
            (ct_number, version_no, o["outcome_type"], o["ordinal"], o["measure"], o["description"], o["time_frame"], norm(o["measure"]))
            for o in outcomes
        ],
    )


# ---- snapshot_pass: the live fetch + hash gate + store + upsert -------------------------


@dataclass(frozen=True)
class SnapshotResult:
    checked: int
    stored: list[str]
    skipped: list[str]
    failed: list[str]


async def snapshot_pass(ct_numbers: list[str], client: httpx.AsyncClient, today: date | None = None) -> SnapshotResult:
    """Fetch today's live state for each ct_number (GET /retrieve/{ctNumber}), and keep it
    as a new local snapshot + upsert trials/versions/outcomes ONLY if its outcome-relevant
    hash differs from the most recently kept snapshot (or none exists yet) -- the hash gate
    that makes a same-day rerun with unchanged content a no-op. A same-day rerun whose
    content genuinely changed intraday is NOT a no-op: it's a real second version, kept
    under a collision-suffixed filename by _write_snapshot rather than overwriting the
    first call's raw payload (task15-review.md Important #2).
    One trial's failure never aborts the batch (same shape as ctcm.ingest.ingest)."""
    today = today or datetime.now(timezone.utc).date()
    pacer = _Pacer()
    conn = db.connect()
    ensure_schema(conn)

    stored: list[str] = []
    skipped: list[str] = []
    failed: list[str] = []

    for ct in ct_numbers:
        try:
            await pacer.wait()
            raw = await _request(client, "GET", f"{RETRIEVE_URL}/{ct}")
        except Exception:
            logger.exception("failed to retrieve %s", ct)
            failed.append(ct)
            continue

        outcomes = map_outcomes(raw)
        new_hash = outcomes_content_hash(outcomes)
        existing = _local_snapshots(ct)
        prior_hash = outcomes_content_hash(map_outcomes(_read_snapshot(existing[-1]))) if existing else None

        if prior_hash == new_hash:
            skipped.append(ct)
            continue

        _write_snapshot(ct, today, raw)
        version_no = len(existing)
        meta = map_meta(raw)
        _upsert_trial(conn, ct, meta, version_no + 1)
        _upsert_version(conn, ct, version_no, today, new_hash)
        _upsert_outcomes(conn, ct, version_no, outcomes)
        conn.commit()
        stored.append(ct)

    conn.close()
    return SnapshotResult(checked=len(ct_numbers), stored=stored, skipped=skipped, failed=failed)
