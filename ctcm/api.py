"""Read-only FastAPI surface over ctcm.db (Task 10, v0.6). Serves ui/index.html plus
the two JSON endpoints its fetch layer consumes (see ui/index.html's getTrial() and
fetchIndex()). Connects with sqlite's mode=ro URI -- a UI user can never write to the
pipeline's data through this process, no matter what the route handlers do.

Doesn't import ctcm.db: that module's connect() opens read-write and re-runs the
schema DDL on every call, which is the wrong shape for a read-only request handler
and would couple this file to db.py's schema-owning code while it's being edited
elsewhere in parallel. ctcm.timeline is safe to import (pure dataclasses + date math,
not on the do-not-touch list) and is exactly what the brief asks for: timeline
anchors computed via ctcm.timeline.anchors.
"""

import gzip
import html
import json
import sqlite3
from collections import defaultdict

from fastapi import FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse

from ctcm import config
from ctcm.timeline import anchors as compute_anchors

UI_PATH = config.REPO_ROOT / "ui" / "index.html"

# Verbatim from scripts/headline.py's CLI caveat (v0.2 checkpoint) -- same corpus,
# same selection-bias fact, kept consistent across both surfaces rather than each
# inventing its own wording.
RESULTS_POSTED_CAVEAT = (
    "this corpus is results-posted trials only (ingest discovery query requires "
    "ResultsFirstPostDate) -- sponsors routinely add/adjust outcome rows around "
    "results entry as registry housekeeping, not editorial endpoint-switching, so "
    "POST_COMPLETION_CHANGE is an upper bound, not a purity signal. Lead with the "
    "post-enrolment-primary-change number above instead."
)

# One finding per trial, picked the same way case-study candidates are picked
# (severity first, then |days after primary completion|): the row's headline.
HEADLINE_FINDING_SQL = """
    SELECT nct_id, finding_id, from_version, to_version, change_type, severity,
           days_after_enrolment, days_after_primary_completion, rationale
    FROM (
        SELECT f.*, ROW_NUMBER() OVER (
            PARTITION BY f.nct_id
            ORDER BY
                CASE WHEN f.severity = 'SIGNAL' THEN 0 ELSE 1 END,
                ABS(f.days_after_primary_completion) DESC NULLS LAST,
                ABS(f.days_after_enrolment) DESC NULLS LAST,
                f.finding_id ASC
        ) AS rn
        FROM findings f
    )
    WHERE rn = 1
"""

# sort=<key> or sort=-<key> query values -> real, whitelisted SQL columns. Whitelist
# is required (not just convenient): these values are interpolated into the ORDER BY
# clause, where placeholders can't be used.
SORT_FIELDS = {
    "nctId": "t.nct_id",
    "sponsor": "t.lead_sponsor",
    "sponsorClass": "t.sponsor_class",
    "phase": "t.phase",
    "severity": "hf.severity",
    "changeType": "hf.change_type",
    "daysAfterEnrolment": "hf.days_after_enrolment",
    "daysAfterPrimaryCompletion": "hf.days_after_primary_completion",
    "enrolmentCount": "t.enrolment_count",
    "versionCount": "t.version_count",
}
DEFAULT_SORT = "-daysAfterEnrolment"

app = FastAPI(title="Clinical Trial Registry Change Monitor API")
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])


def _connect() -> sqlite3.Connection:
    conn = sqlite3.connect(f"file:{config.DB_PATH}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    return conn


def _table_exists(conn: sqlite3.Connection, name: str) -> bool:
    return conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)).fetchone() is not None


def _ue(s: str | None) -> str | None:
    """The int API's historical snapshots embed HTML-entity-escaped text inside
    JSON string values (e.g. measure text containing the literal characters
    '&#x2F;' instead of '/') -- extract.py stores it verbatim (out of scope here,
    owned by another file). Decode once at the API boundary so the UI's own
    escapeHtml() (which must stay, registry text isn't a trusted-safe sink) sees
    real characters instead of double-escaping stray entities into gibberish."""
    return html.unescape(s) if s else s


# ---------------------------------------------------------------------------
# Adjudications: table doesn't exist yet (ctcm/adjudicate.py, v0.4, lands
# separately). Query defensively -- check existence, introspect columns, degrade to
# "no adjudication data" rather than guess a schema and crash when it's missing.
# ---------------------------------------------------------------------------


def _adjudications_index(conn: sqlite3.Connection) -> tuple[str, dict] | None:
    """(strategy, {key: row_dict}) if an adjudications table with a usable key
    exists, else None. Tries a finding_id FK first, then the natural key
    (nct_id, from_version, to_version, change_type) that content_hash-keyed rows
    would still need to carry to be joinable back to a specific finding at all."""
    if not _table_exists(conn, "adjudications"):
        return None
    cols = {row[1] for row in conn.execute("PRAGMA table_info(adjudications)")}
    if "finding_id" in cols:
        rows = conn.execute("SELECT * FROM adjudications").fetchall()
        return "finding_id", {r["finding_id"]: dict(r) for r in rows}
    natural_key = {"nct_id", "from_version", "to_version", "change_type"}
    if natural_key <= cols:
        rows = conn.execute("SELECT * FROM adjudications").fetchall()
        return "natural", {(r["nct_id"], r["from_version"], r["to_version"], r["change_type"]): dict(r) for r in rows}
    return None  # table exists but no key we can join on -- skip rather than guess


def _adjudication_for(adj_index: tuple[str, dict] | None, finding_row: sqlite3.Row) -> dict | None:
    if adj_index is None:
        return None
    strategy, lookup = adj_index
    key = (
        finding_row["finding_id"]
        if strategy == "finding_id"
        else (finding_row["nct_id"], finding_row["from_version"], finding_row["to_version"], finding_row["change_type"])
    )
    return lookup.get(key)


# ---------------------------------------------------------------------------
# Cache-sourced display metadata: brief/official title and per-version overall
# status aren't in the sqlite schema (extract.py doesn't persist identificationModule
# or per-version statusModule.overallStatus). They're sitting right there in the raw
# cached snapshots ingest.py already fetched, so read them from there instead of
# adding columns to a schema owned by another agent's file this run.
# ---------------------------------------------------------------------------


def _protocol_section(nct_id: str, version_no: int) -> dict | None:
    """Best-effort read of one cached snapshot's protocolSection. None on any
    failure (missing file, concurrent ingest write, corrupt gzip) -- this is
    optional display metadata, never worth a 500."""
    path = config.CACHE_DIR / nct_id / f"v{version_no}.json.gz"
    try:
        raw = json.loads(gzip.decompress(path.read_bytes()))
    except (OSError, EOFError, gzip.BadGzipFile, json.JSONDecodeError):
        return None
    study = raw.get("study", raw) or {}
    return study.get("protocolSection") or {}


def _trial_titles(nct_id: str, version_nos: list[int]) -> tuple[str | None, str | None]:
    for v in sorted(version_nos, reverse=True):
        ps = _protocol_section(nct_id, v)
        im = (ps or {}).get("identificationModule") or {}
        if im.get("briefTitle"):
            return im.get("briefTitle"), im.get("officialTitle")
    return None, None


def _overall_status_at(nct_id: str, version_no: int) -> str | None:
    ps = _protocol_section(nct_id, version_no)
    return ((ps or {}).get("statusModule") or {}).get("overallStatus")


def _compare_url(nct_id: str, vfrom: int, vto: int) -> str:
    """Verified against the live site 2026-08-08: clicking two checkboxes + Compare
    on the Record History tab produces ?tab=history&a=<n>&b=<m>#version-content-panel,
    where a/b are 1-indexed display versions (int-API version 0 == displayed
    "Version 1"). ui-brief.md guessed #version-content (no 'a'/'b' params, no
    trailing '-panel'); that guess was flagged unverified in progress.md and
    rechecked here at v0.6 by driving the actual page."""
    return f"{config.CT_GOV_BASE}/study/{nct_id}?tab=history&a={vfrom + 1}&b={vto + 1}#version-content-panel"


def _headline_counts(conn: sqlite3.Connection) -> dict:
    """Global corpus stats -- independent of any /api/trials filters, same numbers
    scripts/headline.py prints."""
    (n_trials,) = conn.execute("SELECT COUNT(*) FROM trials").fetchone()
    (n_signal,) = conn.execute("SELECT COUNT(DISTINCT nct_id) FROM findings WHERE severity='SIGNAL'").fetchone()
    (n_post_completion,) = conn.execute(
        "SELECT COUNT(DISTINCT nct_id) FROM findings WHERE change_type='POST_COMPLETION_CHANGE'"
    ).fetchone()
    return {
        "trials": n_trials,
        "signalTrials": n_signal,
        "postCompletionTrials": n_post_completion,
        "caveat": RESULTS_POSTED_CAVEAT,
    }


def _index_row(r: sqlite3.Row, adj_index: tuple[str, dict] | None) -> dict:
    return {
        "nctId": r["nct_id"],
        "sponsor": _ue(r["lead_sponsor"]),
        "sponsorClass": r["sponsor_class"],
        "phase": r["phase"],
        "conditions": [_ue(c) for c in json.loads(r["conditions"] or "[]")],
        "overallStatus": r["overall_status"],
        "enrolmentCount": r["enrolment_count"],
        "versionCount": r["version_count"],
        "severity": r["severity"],
        "changeType": r["change_type"],
        "daysAfterEnrolment": r["days_after_enrolment"],
        "daysAfterPrimaryCompletion": r["days_after_primary_completion"],
        "rationale": _ue(r["rationale"]),
        "adjudication": _adjudication_for(adj_index, r) if r["change_type"] is not None else None,
    }


@app.get("/healthz")
def healthz():
    return {"status": "ok"}


@app.get("/api/trials")
def list_trials(
    severity: list[str] = Query(default=[]),
    change_type: list[str] = Query(default=[]),
    sponsor_class: list[str] = Query(default=[]),
    phase: list[str] = Query(default=[]),
    q: str = "",
    sort: str = DEFAULT_SORT,
):
    sort_key = sort.lstrip("-")
    if sort_key not in SORT_FIELDS:
        raise HTTPException(status_code=400, detail=f"unknown sort field {sort_key!r}; choose from {sorted(SORT_FIELDS)}")
    sort_col = SORT_FIELDS[sort_key]
    direction = "DESC" if sort.startswith("-") else "ASC"

    where, params = [], []
    if severity:
        where.append(f"hf.severity IN ({','.join('?' * len(severity))})")
        params += severity
    if change_type:
        where.append(f"hf.change_type IN ({','.join('?' * len(change_type))})")
        params += change_type
    if sponsor_class:
        where.append(f"t.sponsor_class IN ({','.join('?' * len(sponsor_class))})")
        params += sponsor_class
    if phase:
        where.append(f"t.phase IN ({','.join('?' * len(phase))})")
        params += phase
    if q:
        where.append("(t.nct_id LIKE ? OR t.lead_sponsor LIKE ? OR t.conditions LIKE ?)")
        like = f"%{q}%"
        params += [like, like, like]
    where_sql = ("WHERE " + " AND ".join(where)) if where else ""

    conn = _connect()
    try:
        rows = conn.execute(
            f"""
            SELECT t.nct_id, t.lead_sponsor, t.sponsor_class, t.phase, t.conditions,
                   t.overall_status, t.enrolment_count, t.version_count,
                   hf.finding_id, hf.from_version, hf.to_version, hf.severity, hf.change_type,
                   hf.days_after_enrolment, hf.days_after_primary_completion, hf.rationale
            FROM trials t
            LEFT JOIN ({HEADLINE_FINDING_SQL}) hf ON hf.nct_id = t.nct_id
            {where_sql}
            ORDER BY {sort_col} {direction} NULLS LAST, t.nct_id ASC
            """,
            params,
        ).fetchall()
        adj_index = _adjudications_index(conn)
        counts = _headline_counts(conn)
    finally:
        conn.close()

    out_rows = [_index_row(r, adj_index) for r in rows]
    return {"rows": out_rows, "total": len(out_rows), "counts": counts}


@app.get("/api/trials/{nct_id}")
def get_trial(nct_id: str):
    conn = _connect()
    try:
        trial = conn.execute("SELECT * FROM trials WHERE nct_id=?", (nct_id,)).fetchone()
        if trial is None:
            raise HTTPException(status_code=404, detail=f"no trial {nct_id}")

        versions = conn.execute(
            "SELECT version_no, version_date, module_labels FROM versions WHERE nct_id=? ORDER BY version_no",
            (nct_id,),
        ).fetchall()

        outcomes_by_version: dict[int, dict[str, list]] = defaultdict(lambda: defaultdict(list))
        for o in conn.execute(
            "SELECT version_no, outcome_type, measure, description, time_frame FROM outcomes "
            "WHERE nct_id=? ORDER BY version_no, outcome_type, ordinal",
            (nct_id,),
        ):
            outcomes_by_version[o["version_no"]][o["outcome_type"]].append(
                {"measure": _ue(o["measure"]), "description": _ue(o["description"]), "timeFrame": _ue(o["time_frame"])}
            )

        anc = compute_anchors(nct_id, conn)

        brief_title, official_title = _trial_titles(nct_id, [v["version_no"] for v in versions])
        brief_title, official_title = _ue(brief_title), _ue(official_title)
        version_objs = [
            {
                "version": v["version_no"],
                "date": v["version_date"],
                "moduleLabels": json.loads(v["module_labels"] or "[]"),
                "overallStatus": _overall_status_at(nct_id, v["version_no"]),
                "primaryOutcomes": outcomes_by_version[v["version_no"]].get("PRIMARY", []),
                "secondaryOutcomes": outcomes_by_version[v["version_no"]].get("SECONDARY", []),
                "otherOutcomes": outcomes_by_version[v["version_no"]].get("OTHER", []),
            }
            for v in versions
        ]

        adj_index = _adjudications_index(conn)
        findings = [
            {
                "findingId": f["finding_id"],
                "fromVersion": f["from_version"],
                "toVersion": f["to_version"],
                "changeType": f["change_type"],
                "severity": f["severity"],
                "beforeMeasure": _ue(f["before_measure"]),
                "afterMeasure": _ue(f["after_measure"]),
                "daysAfterEnrolment": f["days_after_enrolment"],
                "daysAfterPrimaryCompletion": f["days_after_primary_completion"],
                "confidence": f["confidence"],
                "resolvedBy": f["resolved_by"],
                "rationale": _ue(f["rationale"]),
                "compareUrl": _compare_url(nct_id, f["from_version"], f["to_version"]),
                "adjudication": _adjudication_for(adj_index, f),
            }
            for f in conn.execute(
                "SELECT * FROM findings WHERE nct_id=? ORDER BY from_version, to_version, finding_id", (nct_id,)
            )
        ]
    finally:
        conn.close()

    return {
        "nctId": trial["nct_id"],
        "briefTitle": brief_title,
        "officialTitle": official_title,
        "sponsor": _ue(trial["lead_sponsor"]),
        "sponsorClass": trial["sponsor_class"],
        "phase": trial["phase"],
        "studyType": trial["study_type"],
        "overallStatus": trial["overall_status"],
        "conditions": [_ue(c) for c in json.loads(trial["conditions"] or "[]")],
        "enrolmentCount": trial["enrolment_count"],
        "firstPostedDate": trial["first_posted_date"],
        "enrolmentStart": anc.start.isoformat() if anc.start else None,
        "enrolmentStartType": anc.start_type,
        "primaryCompletion": anc.pcd.isoformat() if anc.pcd else None,
        "primaryCompletionType": anc.pcd_type,
        "historyUrl": f"{config.CT_GOV_BASE}/study/{nct_id}?tab=history",
        "versions": version_objs,
        "findings": findings,
    }


@app.get("/", include_in_schema=False)
def index():
    return FileResponse(UI_PATH)
