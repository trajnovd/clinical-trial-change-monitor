"""Read-only FastAPI surface over ctcm.db (Task 10, v0.7). Serves ui/index.html plus
the JSON endpoints its fetch layer consumes (see ui/index.html's getTrial() and
fetchIndex()). Connects with sqlite's mode=ro URI -- a UI user can never write to the
pipeline's data through this process, no matter what the route handlers do.

v0.7 turned the index into a paged view: /api/trials returns one page plus the full
filtered `total`, and /api/meta serves everything the index chrome needs (headline
counts, filter facet counts, notable cases) in one request, so first paint no longer
depends on fetching every row.

Two endpoints serve consumers other than the UI (TECH-PRD "Beyond v1.0"):
/api/export.csv is the filtered index as a file, for systematic reviewers and HTA
bodies who work in a spreadsheet rather than a browser -- same filter/search/sort
params as /api/trials, no pagination. /api/changes is the monitoring feed: the
findings ctcm.monitor stamped with first_seen_at on a monitor pass, newest first.
Only monitor-discovered findings have that stamp -- the historical corpus is NULL by
design (ctcm/monitor.py's docstring) -- so on a corpus no monitor pass has ever run
against, this feed is legitimately empty rather than a backfilled guess at when each
historical change appeared.

Doesn't import ctcm.db: that module's connect() opens read-write and re-runs the
schema DDL on every call, which is the wrong shape for a read-only request handler
and would couple this file to db.py's schema-owning code while it's being edited
elsewhere in parallel. ctcm.timeline is safe to import (pure dataclasses + date math,
not on the do-not-touch list) and is exactly what the brief asks for: timeline
anchors computed via ctcm.timeline.anchors. ctcm.adjudicate is stable (commit
138072b) and its content_hash() is imported rather than reimplemented here, so
the join key has exactly one definition (see the Adjudications section below).
ctcm.publink (Task 13, this file's own agent) is imported for its pure
days_after_change() date-arithmetic helper -- one definition shared with the
publications table it also owns, rather than a second copy of the same math here.
ctcm.reslink is imported for strict_genuine_trial_count() for the same reason:
the strict-genuine predicate has exactly one SQL definition, owned by the module
that owns the results_linked column (and it degrades to None -- key omitted, not
a 500 -- on a db the enrichment has never touched).
"""

import csv
import gzip
import html
import io
import json
import re
import sqlite3
from collections import defaultdict

from fastapi import FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, Response

from ctcm import analytics, config
from ctcm.adjudicate import content_hash
from ctcm.publink import days_after_change
from ctcm.reslink import strict_genuine_trial_count
from ctcm.timeline import anchors as compute_anchors

UI_PATH = config.REPO_ROOT / "ui" / "index.html"

# Reader-facing subtitle copy (Task 13 fix): scripts/headline.py's CLI caveat
# (kept as-is, that's a coordinator-facing terminal tool) used the same selection-bias
# fact but ended in internal-audience process language ("Lead with the
# post-enrolment-primary-change number above instead"). This is the copy the UI's
# index subtitle actually renders to a reader, so it says what the corpus is and why,
# without instructing anyone on which number to lead with.
RESULTS_POSTED_CAVEAT = (
    "This corpus is limited to completed trials that posted results, which makes "
    "post-completion registry edits common; the count above reflects primary-outcome "
    "changes after enrolment began."
)

# One finding per trial, picked the same way case-study candidates are picked:
# severity first, then |days after primary completion| desc, then |days after
# enrolment| desc, then finding_id asc as the final deterministic tiebreak -- the
# row's headline. _headline_key() below replicates this exact ORDER BY in Python
# for _signal_change_date's single-trial use (get_trial has no SQL window function
# handy there); if this ORDER BY ever changes, _headline_key must change with it.
HEADLINE_FINDING_SQL = """
    SELECT nct_id, finding_id, from_version, to_version, change_type, severity,
           days_after_enrolment, days_after_primary_completion, rationale,
           before_measure, after_measure
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

# The index row's columns and its trial-plus-headline-finding join, kept apart so the
# three statements that need them can each take what they need from one definition:
# /api/trials' page (columns + from), its filtered COUNT(*) (from only -- the count
# must be over exactly the same join the page is), /api/meta's facet counts (from
# only) and its case studies (columns + from).
INDEX_COLUMNS_SQL = """
    t.nct_id, t.lead_sponsor, t.sponsor_class, t.phase, t.conditions,
    t.overall_status, t.enrolment_count, t.version_count,
    hf.finding_id, hf.from_version, hf.to_version, hf.severity, hf.change_type,
    hf.days_after_enrolment, hf.days_after_primary_completion, hf.rationale,
    hf.before_measure, hf.after_measure
"""
INDEX_FROM_SQL = f"""
    FROM trials t
    LEFT JOIN ({HEADLINE_FINDING_SQL}) hf ON hf.nct_id = t.nct_id
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

DEFAULT_LIMIT = 50  # the index's page size (ui/index.html's pager reads `total` for the rest)
MAX_LIMIT = 200

# /api/meta's four facet fields. They name the same columns SORT_FIELDS does, and
# deliberately reuse it rather than re-listing them: a facet count that counted a
# different column than the index sorts and filters on would be a lie in the dropdown.
FILTER_FACETS = ("severity", "changeType", "sponsorClass", "phase")

# /api/export.csv's header row, in order. snake_case, not the JSON API's camelCase: this
# is a file destined for a spreadsheet or an R/pandas import, where snake_case column
# names are what a reader can use unquoted. `conditions` collapses the JSON array to a
# "; "-joined string (a CSV cell can't hold a list) and the two adjudication fields
# flatten the nested object /api/trials nests under `adjudication`.
EXPORT_COLUMNS = (
    "nct_id",
    "sponsor",
    "sponsor_class",
    "phase",
    "conditions",
    "overall_status",
    "severity",
    "change_type",
    "days_after_enrolment",
    "days_after_primary_completion",
    "from_version",
    "to_version",
    "rationale",
    "adjudication_concern",
    "adjudication_rationale",
    "registry_history_url",
)

# The subset of a `findings` entry /api/trials/{nct_id} republishes as headlineFinding
# -- what the trial view's banner needs to state the finding and its provenance,
# without the diff payload (beforeMeasure/afterMeasure, compareUrl) the findings list
# already carries for the same finding.
HEADLINE_FINDING_FIELDS = (
    "severity",
    "changeType",
    "daysAfterEnrolment",
    "daysAfterPrimaryCompletion",
    "rationale",
    "adjudication",
    "fromVersion",
    "toVersion",
)

# One trimmed row, so /docs shows what a row actually looks like without pasting a
# full 721-trial page into the schema. Illustrative, not a contract -- the contract
# is _index_row().
TRIALS_EXAMPLE = {
    "rows": [
        {
            "nctId": "NCT01234567",
            "sponsor": "Example Pharma",
            "sponsorClass": "INDUSTRY",
            "phase": "PHASE3",
            "conditions": ["Type 2 Diabetes"],
            "overallStatus": "COMPLETED",
            "enrolmentCount": 420,
            "versionCount": 12,
            "severity": "SIGNAL",
            "changeType": "POST_COMPLETION_CHANGE",
            "daysAfterEnrolment": 900,
            "daysAfterPrimaryCompletion": 120,
            "rationale": "PRIMARY_REPLACED: 'HbA1c at week 24' -> 'HbA1c at week 52' (900 days after enrolment, v4->v5).",
            "adjudication": {"concern": "HIGH", "confidence": 0.9, "rationale": "The primary endpoint was replaced after data lock."},
        }
    ],
    "total": 721,
    "counts": {"trials": 1000, "signalTrials": 721, "postCompletionTrials": 616, "caveat": RESULTS_POSTED_CAVEAT},
}

app = FastAPI(
    title="Redline — Clinical Trial Registry Change Monitor API",
    version="0.7",
    description=(
        "Read-only JSON over a corpus of completed, results-posted trials and the "
        "primary-outcome changes found in their registry version history. No auth: "
        "every route is a `mode=ro` sqlite read."
    ),
)
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])


def _connect() -> sqlite3.Connection:
    conn = sqlite3.connect(f"file:{config.DB_PATH}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    # Registers as a SQL function (not a table write -- fine on a mode=ro connection)
    # so `sponsor` filtering (list_trials, fix round 1 Critical-1) can match the
    # HTML-entity-decoded name the UI displays/sends against the raw-encoded value
    # actually stored in lead_sponsor, without re-escaping the query param (unreliable:
    # _ue's docstring notes the source registry uses non-standard entities like
    # '&#x2F;', which html.escape() wouldn't reproduce on the way back in).
    conn.create_function("html_unescape", 1, _ue)
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
# Adjudications: keyed by content_hash, not finding_id. finding_id is a
# delete+reinsert PK that ctcm/pipeline.py renumbers on every pipeline re-run
# (by design -- see ctcm/adjudicate.py's own module docstring), so an
# adjudications row's finding_id goes stale the moment the pipeline re-runs
# again after it was written; a join on it is a silent no-op against any live
# corpus that has been re-run since (confirmed against data/ctcm.db: every one
# of its finding_id values was orphaned). content_hash is computed from the
# finding's actual content -- (nct_id, from_version, to_version, change_type,
# before_measure, after_measure) -- which survives renumbering. Imported from
# ctcm.adjudicate rather than reimplemented here so there's exactly one
# definition of the hash.
# ---------------------------------------------------------------------------

_SENTENCE_END_RE = re.compile(r"(?<=[.!?])\s+")


def _first_sentence(text: str | None) -> str | None:
    """Judge rationale is written as 2-4 sentences (see adjudicate.py's
    _judge_prompt); case cards need a one-line excerpt, not the full verdict."""
    return _SENTENCE_END_RE.split(text.strip(), maxsplit=1)[0] if text else text


def _adjudications_index(conn: sqlite3.Connection) -> dict | None:
    """{content_hash: row_dict} if an adjudications table in ctcm.adjudicate's
    shape exists, else None. Still checks defensively (table may not exist yet,
    or may exist in some other shape) rather than assuming -- same philosophy
    as the rest of this module's cache/schema reads."""
    if not _table_exists(conn, "adjudications"):
        return None
    cols = {row[1] for row in conn.execute("PRAGMA table_info(adjudications)")}
    if "content_hash" not in cols:
        return None  # table exists but not in adjudicate.py's shape -- skip rather than guess
    rows = conn.execute("SELECT * FROM adjudications").fetchall()
    return {r["content_hash"]: dict(r) for r in rows}


def _adjudication_for(adj_index: dict | None, finding_row: sqlite3.Row) -> dict | None:
    if adj_index is None:
        return None
    chash = content_hash(
        finding_row["nct_id"], finding_row["from_version"], finding_row["to_version"],
        finding_row["change_type"], finding_row["before_measure"], finding_row["after_measure"],
    )
    row = adj_index.get(chash)
    # UNREVIEWED means the LLM chain itself failed (timeout, bad JSON, ...) --
    # its "rationale" is an internal error string, not a judged verdict, so it's
    # not a real adjudication to surface (adjudicate.py's Adjudication dataclass
    # documents UNREVIEWED as the on-any-failure sentinel, not a concern level).
    if row is None or row["severity_confirmed"] == "UNREVIEWED":
        return None
    return {
        "concern": row["severity_confirmed"],
        "confidence": row["confidence"],
        "rationale": _first_sentence(_ue(row["rationale"])),
    }


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


def _publications(conn: sqlite3.Connection, nct_id: str, signal_change_date: str | None) -> list[dict]:
    """Published reports linked to this trial (Task 13, ctcm.publink), highest tier
    first. `signal_change_date` is the to_version's version_date of the trial's most
    significant SIGNAL finding (None if it has none) -- when a publication's pub_date
    postdates it, `daysAfterSignalChange`/`timingNote` carry the factual "published N
    days after the primary outcome changed" line. This is date arithmetic only: no
    claim about what the paper says, low-tier hits included (UI is responsible for
    badging them as "mentions this trial," never "the trial's paper")."""
    if not _table_exists(conn, "publications"):
        return []
    rows = conn.execute(
        "SELECT pmid, doi, title, journal, pub_date, oa, tier, source FROM publications WHERE nct_id=? "
        "ORDER BY CASE tier WHEN 'HIGH' THEN 0 WHEN 'MEDIUM' THEN 1 ELSE 2 END, pub_date DESC",
        (nct_id,),
    ).fetchall()
    out = []
    for r in rows:
        days = days_after_change(r["pub_date"], signal_change_date)
        out.append(
            {
                "pmid": r["pmid"],
                "doi": r["doi"],
                "title": _ue(r["title"]),
                "journal": _ue(r["journal"]),
                "pubDate": r["pub_date"],
                "oa": None if r["oa"] is None else bool(r["oa"]),
                "tier": r["tier"],
                "source": r["source"],
                "daysAfterSignalChange": days,
                "timingNote": f"published {days} days after the primary outcome changed" if days else None,
            }
        )
    return out


def _headline_key(f: dict) -> tuple[int, int, int, int]:
    """Sort key replicating HEADLINE_FINDING_SQL's ORDER BY exactly (see the comment
    above that SQL string) -- the single definition both "pick this trial's headline
    finding" call sites key off, so they cannot quietly diverge: severity (SIGNAL
    first), then |days after primary completion| desc, then |days after enrolment|
    desc, then finding_id asc. min() over this tuple picks the same row rn=1 would."""
    return (
        0 if f["severity"] == "SIGNAL" else 1,
        -abs(f["daysAfterPrimaryCompletion"] or 0),
        -abs(f["daysAfterEnrolment"] or 0),
        f["findingId"],
    )


def _signal_change_date(findings: list[dict], version_dates: dict[int, str | None]) -> str | None:
    """to_version's version_date of the trial's headline SIGNAL finding (_headline_key,
    same ORDER BY as HEADLINE_FINDING_SQL). None if the trial has no SIGNAL finding."""
    signal = [f for f in findings if f["severity"] == "SIGNAL"]
    if not signal:
        return None
    chosen = min(signal, key=_headline_key)
    return version_dates.get(chosen["toVersion"])


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
    scripts/headline.py prints. versionsRead/outcomeRecords/completedTrials are the
    scope numbers: how much registry record was actually read to produce the flags,
    which is what makes the flagged count meaningful."""
    (n_trials,) = conn.execute("SELECT COUNT(*) FROM trials").fetchone()
    (n_completed,) = conn.execute("SELECT COUNT(*) FROM trials WHERE overall_status='COMPLETED'").fetchone()
    (n_versions,) = conn.execute("SELECT COUNT(*) FROM versions").fetchone()
    (n_outcomes,) = conn.execute("SELECT COUNT(*) FROM outcomes").fetchone()
    (n_signal,) = conn.execute("SELECT COUNT(DISTINCT nct_id) FROM findings WHERE severity='SIGNAL'").fetchone()
    (n_post_completion,) = conn.execute(
        "SELECT COUNT(DISTINCT nct_id) FROM findings WHERE change_type='POST_COMPLETION_CHANGE'"
    ).fetchone()
    counts = {
        "trials": n_trials,
        "completedTrials": n_completed,
        "versionsRead": n_versions,
        "outcomeRecords": n_outcomes,
        "signalTrials": n_signal,
        "postCompletionTrials": n_post_completion,
        "caveat": RESULTS_POSTED_CAVEAT,
    }
    # Strict-genuine switchers (ctcm.reslink): SIGNAL during the trial, or post-
    # completion with no results-posting event nearby. None means the results_linked
    # column doesn't exist yet (run_reslink.py never ran on this db) -- omit the key
    # so the UI skips the stat, rather than 500ing or showing a fabricated zero.
    n_strict = strict_genuine_trial_count(conn)
    if n_strict is not None:
        counts["strictGenuineTrials"] = n_strict
    return counts


def _facet_counts(conn: sqlite3.Connection, column: str) -> list[dict]:
    """Trial counts per distinct value of one index column, over the same join
    /api/trials filters against -- which is what makes a facet's count equal to that
    filter's `total` rather than an approximation of it. `column` comes from
    SORT_FIELDS (interpolated, so it must stay whitelisted, same reason as ORDER BY).
    Null and empty values are dropped: the UI can't offer them as a dropdown option."""
    return [
        {"value": value, "count": n}
        for value, n in conn.execute(
            f"""
            SELECT {column} AS value, COUNT(*) AS n
            {INDEX_FROM_SQL}
            WHERE {column} IS NOT NULL AND {column} != ''
            GROUP BY {column}
            ORDER BY n DESC, value ASC
            """
        )
    ]


def _index_where(
    severity: list[str], change_type: list[str], sponsor_class: list[str], phase: list[str], sponsor: str, q: str
) -> tuple[str, list]:
    """The index's filter contract as one (where_sql, params) pair, shared by every
    statement that has to honour it: /api/trials' page and its COUNT, and
    /api/export.csv's unpaged pull. Two copies of this would let the export quietly
    filter differently from the screen it was exported from.

    Repeated values within a group OR; groups AND. `sponsor` is exact (see below);
    `q` is a deliberate substring search across three columns."""
    where, params = [], []
    for column, values in (
        ("hf.severity", severity),
        ("hf.change_type", change_type),
        ("t.sponsor_class", sponsor_class),
        ("t.phase", phase),
    ):
        if values:
            where.append(f"{column} IN ({','.join('?' * len(values))})")
            params += values
    if sponsor:
        # Exact match (fix round 1, task16-review.md Critical-1): `q` below is a
        # deliberate substring search across three columns, wrong for a drill-down
        # that promises "this row's own trials" -- "Pfizer" must not also pull in
        # "Wyeth is now a wholly owned subsidiary of Pfizer". html_unescape (_connect)
        # decodes the stored value before comparing, since the UI sends the decoded
        # display name, not the registry's raw HTML-entity-escaped one.
        where.append("html_unescape(t.lead_sponsor) = ?")
        params.append(sponsor)
    if q:
        where.append("(t.nct_id LIKE ? OR t.lead_sponsor LIKE ? OR t.conditions LIKE ?)")
        like = f"%{q}%"
        params += [like, like, like]
    return ("WHERE " + " AND ".join(where)) if where else "", params


def _index_order_by(sort: str) -> str:
    """`sort` (a SORT_FIELDS key, optionally '-' prefixed) as an ORDER BY clause, with
    t.nct_id as the final tiebreak so paging is stable. 400 on an unknown field: unlike
    an out-of-range limit there's no sane value to clamp to, and silently falling back
    to the default sort would serve rows in an order the caller didn't ask for."""
    sort_key = sort.lstrip("-")
    if sort_key not in SORT_FIELDS:
        raise HTTPException(status_code=400, detail=f"unknown sort field {sort_key!r}; choose from {sorted(SORT_FIELDS)}")
    direction = "DESC" if sort.startswith("-") else "ASC"
    return f"ORDER BY {SORT_FIELDS[sort_key]} {direction} NULLS LAST, t.nct_id ASC"


def _has_column(conn: sqlite3.Connection, table: str, column: str) -> bool:
    return column in {r[1] for r in conn.execute(f"PRAGMA table_info({table})")}


def _history_url(nct_id: str, registry: str) -> str:
    """The registry's own human-readable version-history page for a trial -- the export's
    provenance column, so a reviewer can check any exported row against the source.

    CT.gov only. CTIS rows (ctcm/ctis.py stores their ctNumber in the same nct_id column,
    told apart by trials.registry) get an empty cell: ctcm.ctis talks to
    euclinicaltrials.eu's JSON API, and no per-trial public page URL has been verified
    against the live site the way _compare_url's was. An unverified guess in a column a
    reviewer would cite is worse than a blank."""
    return f"{config.CT_GOV_BASE}/study/{nct_id}?tab=history" if registry == "ctgov" else ""


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


@app.get("/healthz", summary="Liveness probe")
def healthz():
    return {"status": "ok"}


@app.get(
    "/api/trials",
    summary="One page of index rows, plus the full filtered total",
    responses={200: {"content": {"application/json": {"example": TRIALS_EXAMPLE}}}},
)
def list_trials(
    severity: list[str] = Query(default=[]),
    change_type: list[str] = Query(default=[]),
    sponsor_class: list[str] = Query(default=[]),
    phase: list[str] = Query(default=[]),
    sponsor: str = "",
    q: str = "",
    sort: str = DEFAULT_SORT,
    limit: int = DEFAULT_LIMIT,
    offset: int = 0,
):
    """Every trial in the corpus with its headline finding (the one finding
    HEADLINE_FINDING_SQL ranks first), filtered, sorted and paged.

    Repeated `severity`/`change_type`/`sponsor_class`/`phase` params OR within a group
    and AND across groups. `sponsor` is an exact lead-sponsor match (the analytics
    drill-down); `q` is a substring search over NCT id, sponsor and conditions.
    `total` is the count of all matching trials, not of the returned page -- offset
    past the end returns empty `rows` with that same total, never a 404.
    """
    # Clamped, not rejected: a stale bookmark or a hand-edited ?limit= should still
    # render the index. The 400 below is a different case -- an unknown sort field
    # has no sane fallback to clamp to, an out-of-range page size does.
    limit = max(1, min(limit, MAX_LIMIT))
    offset = max(0, offset)

    order_by = _index_order_by(sort)
    where_sql, params = _index_where(severity, change_type, sponsor_class, phase, sponsor, q)

    conn = _connect()
    try:
        rows = conn.execute(
            f"""
            SELECT {INDEX_COLUMNS_SQL}
            {INDEX_FROM_SQL}
            {where_sql}
            {order_by}
            LIMIT ? OFFSET ?
            """,
            [*params, limit, offset],
        ).fetchall()
        # Same WHERE, same join, no LIMIT: `total` is what the pager counts through,
        # so it has to be the size of the whole filtered result, not of this page.
        (total,) = conn.execute(f"SELECT COUNT(*) {INDEX_FROM_SQL} {where_sql}", params).fetchone()
        adj_index = _adjudications_index(conn)
        counts = _headline_counts(conn)
    finally:
        conn.close()

    return {"rows": [_index_row(r, adj_index) for r in rows], "total": total, "counts": counts}


@app.get(
    "/api/export.csv",
    summary="The whole filtered index as a CSV file",
    response_class=Response,
    responses={
        200: {
            "description": "CSV, one row per matching trial, header row first.",
            "content": {"text/csv": {"example": ",".join(EXPORT_COLUMNS) + "\n"}},
        }
    },
)
def export_csv(
    severity: list[str] = Query(default=[]),
    change_type: list[str] = Query(default=[]),
    sponsor_class: list[str] = Query(default=[]),
    phase: list[str] = Query(default=[]),
    sponsor: str = "",
    q: str = "",
    sort: str = DEFAULT_SORT,
):
    """The same rows /api/trials serves, as a downloadable file for systematic
    reviewers and HTA bodies (TECH-PRD "Beyond v1.0": the public-API audience).

    Takes exactly the filter/search/sort params /api/trials takes -- they share one
    WHERE builder, so an export can't disagree with the screen it was exported from --
    but deliberately no `limit`/`offset`: a filtered export that silently stopped at
    the first page would be a wrong denominator in someone's review. Columns are
    snake_case (EXPORT_COLUMNS); `conditions` is "; "-joined and the adjudication
    object is flattened into concern + rationale.
    """
    order_by = _index_order_by(sort)
    where_sql, params = _index_where(severity, change_type, sponsor_class, phase, sponsor, q)

    conn = _connect()
    try:
        # Pre-Task-15 dbs have no trials.registry (ctcm.ctis adds it via a guarded
        # ALTER); every row in one of those is CT.gov by construction.
        registry_col = "t.registry" if _has_column(conn, "trials", "registry") else "'ctgov'"
        rows = conn.execute(
            f"""
            SELECT {INDEX_COLUMNS_SQL}, {registry_col} AS registry
            {INDEX_FROM_SQL}
            {where_sql}
            {order_by}
            """,
            params,
        ).fetchall()
        adj_index = _adjudications_index(conn)
    finally:
        conn.close()

    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow(EXPORT_COLUMNS)
    for r in rows:
        row = _index_row(r, adj_index)
        adj = row["adjudication"] or {}
        writer.writerow(
            [
                row["nctId"],
                row["sponsor"],
                row["sponsorClass"],
                row["phase"],
                "; ".join(row["conditions"]),
                row["overallStatus"],
                row["severity"],
                row["changeType"],
                row["daysAfterEnrolment"],
                row["daysAfterPrimaryCompletion"],
                r["from_version"],
                r["to_version"],
                row["rationale"],
                adj.get("concern"),
                adj.get("rationale"),
                _history_url(row["nctId"], r["registry"]),
            ]
        )
    return Response(
        buf.getvalue(),
        media_type="text/csv",
        headers={"Content-Disposition": 'attachment; filename="redline-trials.csv"'},
    )


@app.get("/api/meta", summary="Everything the index chrome needs before it has any rows")
def get_meta():
    """Headline counts, filter facet counts and the notable cases, in one request.

    The index's first paint is this call plus one page of /api/trials -- before v0.7
    it had to pull every row just to learn how many there were and what to put in the
    filter chips. Facet counts are trial counts over the same headline-finding join
    the index table shows, so `filters.severity[SIGNAL].count` is exactly the `total`
    of `/api/trials?severity=SIGNAL`; null and empty values are left out (they'd be an
    unpickable dropdown option). `caseStudies` are the 5 SIGNAL rows whose primary
    outcome moved furthest from primary completion, in /api/trials row shape.
    """
    conn = _connect()
    try:
        counts = _headline_counts(conn)
        filters = {facet: _facet_counts(conn, SORT_FIELDS[facet]) for facet in FILTER_FACETS}
        case_rows = conn.execute(
            f"""
            SELECT {INDEX_COLUMNS_SQL}
            {INDEX_FROM_SQL}
            WHERE hf.severity = 'SIGNAL'
            ORDER BY ABS(hf.days_after_primary_completion) DESC NULLS LAST, t.nct_id ASC
            LIMIT 5
            """
        ).fetchall()
        adj_index = _adjudications_index(conn)
    finally:
        conn.close()
    return {"counts": counts, "filters": filters, "caseStudies": [_index_row(r, adj_index) for r in case_rows]}


@app.get("/api/trials/{nct_id}", summary="One trial: every version, every finding, its headline finding and linked publications")
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
                # bool | null: true = the edit is part of results posting (ctcm.reslink),
                # false = determined not, null = undetermined OR the column doesn't
                # exist yet (guarded via keys() -- SELECT * on a pre-reslink db).
                "resultsLinked": (
                    None if "results_linked" not in f.keys() or f["results_linked"] is None
                    else bool(f["results_linked"])
                ),
                "compareUrl": _compare_url(nct_id, f["from_version"], f["to_version"]),
                "adjudication": _adjudication_for(adj_index, f),
            }
            for f in conn.execute(
                "SELECT * FROM findings WHERE nct_id=? ORDER BY from_version, to_version, finding_id", (nct_id,)
            )
        ]

        version_dates = {v["version_no"]: v["version_date"] for v in versions}
        publications = _publications(conn, nct_id, _signal_change_date(findings, version_dates))
    finally:
        conn.close()

    # Same finding the index row shows for this trial (min over _headline_key == the
    # SQL's rn=1). Served rather than left to the UI so the banner and the index row
    # can't disagree about which finding is the headline -- a ranking that lives in
    # two languages is a ranking that drifts.
    headline = min(findings, key=_headline_key) if findings else None

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
        "headlineFinding": {k: headline[k] for k in HEADLINE_FINDING_FIELDS} if headline else None,
        "versions": version_objs,
        "findings": findings,
        "publications": publications,
    }


@app.get("/api/analytics", summary="Corpus aggregations by sponsor, class, phase, year, condition and timing")
def get_analytics():
    """Task 16: sponsor/class/phase/year/condition/timing aggregations (ctcm.analytics),
    all sharing the /api/trials index header's caveat -- this corpus is completed,
    results-posted trials only, which makes post-completion registry edits common.

    Every trials-table aggregate excludes Task 15's CTIS rows (see ctcm.analytics'
    module docstring, Critical-2 fix): they're prospective-only and structurally can't
    have a finding yet, so including them would dilute every rate with denominator-only
    trials. `ctisExcludedCount` reports how many, so the UI can disclose it rather than
    just silently dropping them."""
    conn = _connect()
    try:
        by_sponsor = analytics.signal_rate_by_sponsor(conn)
        top_conditions = analytics.top_conditions(conn)
        payload = {
            "signalRateBySponsor": by_sponsor,
            "bySponsorClass": analytics.by_sponsor_class(conn),
            "byPhase": analytics.by_phase(conn),
            "byYear": analytics.by_year(conn),
            "topConditions": top_conditions,
            "timingHistogram": analytics.timing_histogram(conn),
            "adjudicationConcernMix": analytics.adjudication_concern_mix(conn),
            "ctisExcludedCount": analytics.excluded_ctis_count(conn),
            "caveat": RESULTS_POSTED_CAVEAT,
        }
    finally:
        conn.close()
    # HTML-entity decode at the API boundary (see _ue's docstring above) -- sponsor and
    # condition are the only two free-text registry fields analytics.py's aggregations
    # group by; sponsor_class/phase/year/bucket/concern are all our own fixed vocabulary.
    for row in by_sponsor:
        row["sponsor"] = _ue(row["sponsor"])
    for row in top_conditions:
        row["condition"] = _ue(row["condition"])
    return payload


@app.get("/api/changes", summary="Monitoring feed: findings a monitor pass discovered, newest first")
def list_changes(limit: int = DEFAULT_LIMIT, offset: int = 0):
    """Registry changes this system saw appear, most recently seen first.

    Only findings ctcm.monitor stamped with `first_seen_at` on a pass appear here.
    The historical corpus is NULL by design (ctcm/monitor.py): those findings were
    extracted from version history that already existed when the trial was first
    ingested, so we know when the registry recorded the change but not when we'd have
    noticed it -- publishing an invented discovery date for them would make a
    backfill look like surveillance. A corpus no monitor pass has run against
    therefore returns `{rows: [], total: 0, asOf: null}`, which is the honest answer,
    not an error.

    `asOf` is the newest `first_seen_at` in the corpus (the last time this feed
    learned anything), and `total` counts every timestamped finding regardless of
    paging. `limit` clamps to 1-200, `offset` to >= 0.
    """
    limit = max(1, min(limit, MAX_LIMIT))
    offset = max(0, offset)

    conn = _connect()
    try:
        # findings.first_seen_at arrives via ctcm.monitor.ensure_schema's guarded ALTER,
        # so it's absent from any db no monitor pass has ever touched -- that's zero
        # timestamped findings, same answer as an empty column, not a 500.
        if not _has_column(conn, "findings", "first_seen_at"):
            return {"rows": [], "total": 0, "asOf": None}
        rows = conn.execute(
            """
            SELECT f.nct_id, f.from_version, f.to_version, f.change_type, f.severity, f.first_seen_at,
                   t.lead_sponsor, t.conditions
            FROM findings f
            LEFT JOIN trials t ON t.nct_id = f.nct_id
            WHERE f.first_seen_at IS NOT NULL
            ORDER BY f.first_seen_at DESC, f.finding_id DESC
            LIMIT ? OFFSET ?
            """,
            (limit, offset),
        ).fetchall()
        (total,) = conn.execute("SELECT COUNT(*) FROM findings WHERE first_seen_at IS NOT NULL").fetchone()
        (as_of,) = conn.execute("SELECT MAX(first_seen_at) FROM findings").fetchone()
    finally:
        conn.close()

    return {
        "rows": [
            {
                "nctId": r["nct_id"],
                "sponsor": _ue(r["lead_sponsor"]),
                "conditions": [_ue(c) for c in json.loads(r["conditions"] or "[]")],
                "changeType": r["change_type"],
                "severity": r["severity"],
                "fromVersion": r["from_version"],
                "toVersion": r["to_version"],
                "firstSeenAt": r["first_seen_at"],
                "compareUrl": _compare_url(r["nct_id"], r["from_version"], r["to_version"]),
            }
            for r in rows
        ],
        "total": total,
        "asOf": as_of,
    }


@app.get("/", include_in_schema=False)
def index():
    return FileResponse(UI_PATH)
