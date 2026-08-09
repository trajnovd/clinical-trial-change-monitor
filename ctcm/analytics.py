"""Task 16 (v2.4): sponsor/class/phase/year/condition/timing aggregations over the
live schema. Pure SQL + deterministic Python grouping, no model calls (global-
constraints.md). Every function takes an open sqlite3.Connection (row_factory=
sqlite3.Row, as both ctcm.db.connect() and ctcm.api._connect() set it) and returns
a list of plain JSON-ready dicts -- no connection lifecycle here, callers own that
(same split as ctcm.timeline.anchors / ctcm.publink.days_after_change).

"Signal" is defined exactly as ctcm.api._headline_counts already defines it for the
index header: a trial counts once it has >=1 finding with severity='SIGNAL'. That
severity is only ever assigned to a change that touches a PRIMARY outcome (classify.py
filters everything else out before scoring) and is on or after enrolment (pre-enrolment
changes get CONTEXT, see classify.classify) -- so "SIGNAL" already *is* "post-enrolment
primary SIGNAL" by construction; no extra day-count filter is needed here to match the
brief's phrasing.

Every row carries its numerator and denominator alongside the rate (global-constraints
brief: "never a bare rate") so a caller/reader can't be misled by a rounded percentage
detached from how many trials it's actually counting.
"""

import json
import sqlite3
from collections import Counter

MIN_SPONSOR_TRIALS = 5

# days_after_enrolment bucket edges (brief: "<0, 0-90, 91-365, 366-1095, >1095").
_HISTOGRAM_BUCKETS = [
    ("<0", "days_after_enrolment < 0"),
    ("0-90", "days_after_enrolment BETWEEN 0 AND 90"),
    ("91-365", "days_after_enrolment BETWEEN 91 AND 365"),
    ("366-1095", "days_after_enrolment BETWEEN 366 AND 1095"),
    (">1095", "days_after_enrolment > 1095"),
]

# Ordinal, not alphabetical -- LOW/MODERATE/HIGH is a severity scale (ctcm.adjudicate).
_CONCERN_ORDER = ["LOW", "MODERATE", "HIGH"]


def _table_exists(conn: sqlite3.Connection, name: str) -> bool:
    return conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)).fetchone() is not None


def _rate_row(key_name: str, key_value, signal: int, total: int) -> dict:
    return {
        key_name: key_value,
        "signalTrials": signal,
        "totalTrials": total,
        "rate": round(signal / total, 4) if total else 0.0,
    }


def _rate_by(conn: sqlite3.Connection, column: str, key_name: str, min_group_size: int = 1, order: str = "rate_desc") -> list[dict]:
    """Groups trials by `column`, counting how many of each group have >=1 SIGNAL
    finding. The SIGNAL nct_id set is computed once as a subquery rather than a
    LEFT JOIN ... findings, which would fan out one trials row per matching finding
    and inflate COUNT(*) for trials with more than one SIGNAL finding.

    `column` is always a literal string from this module's own call sites below,
    never request input, so building it into the SQL text is safe (contrast
    ctcm.api.SORT_FIELDS, which whitelists a value that *does* come from a query
    param before doing the same thing)."""
    rows = conn.execute(
        f"""
        SELECT {column} AS grp, COUNT(*) AS total,
               SUM(CASE WHEN nct_id IN (SELECT DISTINCT nct_id FROM findings WHERE severity='SIGNAL') THEN 1 ELSE 0 END) AS signal
        FROM trials
        WHERE {column} IS NOT NULL
        GROUP BY {column}
        HAVING COUNT(*) >= ?
        """,
        (min_group_size,),
    ).fetchall()
    out = [_rate_row(key_name, r["grp"], r["signal"], r["total"]) for r in rows]
    if order == "rate_desc":
        out.sort(key=lambda r: (-r["rate"], -r["totalTrials"], str(r[key_name])))
    else:  # "key_asc" -- e.g. by_year, where chronological order is the readable one
        out.sort(key=lambda r: str(r[key_name]))
    return out


def signal_rate_by_sponsor(conn: sqlite3.Connection) -> list[dict]:
    """Only sponsors with >=5 trials (brief) -- below that, one or two trials would
    swing a sponsor between 0% and 100%, which reads as a signal it isn't."""
    return _rate_by(conn, "lead_sponsor", "sponsor", min_group_size=MIN_SPONSOR_TRIALS)


def by_sponsor_class(conn: sqlite3.Connection) -> list[dict]:
    return _rate_by(conn, "sponsor_class", "sponsorClass")


def by_phase(conn: sqlite3.Connection) -> list[dict]:
    return _rate_by(conn, "phase", "phase")


def by_year(conn: sqlite3.Connection) -> list[dict]:
    """Grouped by first_posted_date's year. Chronological order (not rate-desc) --
    this table reads as a trend over time, not a ranking."""
    return _rate_by(conn, "substr(first_posted_date, 1, 4)", "year", order="key_asc")


def top_conditions(conn: sqlite3.Connection, limit: int = 20) -> list[dict]:
    """Top `limit` conditions by trial count. trials.conditions is a JSON array
    (ctcm/db.py's TEXT[]->JSON translation) -- exploded in Python with json.loads,
    the same way ctcm/api.py already reads this column, rather than relying on
    sqlite's optional JSON1 extension being compiled in."""
    signal_ids = {r[0] for r in conn.execute("SELECT DISTINCT nct_id FROM findings WHERE severity='SIGNAL'")}
    totals: Counter = Counter()
    signals: Counter = Counter()
    for nct_id, conditions_json in conn.execute("SELECT nct_id, conditions FROM trials"):
        for cond in json.loads(conditions_json or "[]"):
            totals[cond] += 1
            if nct_id in signal_ids:
                signals[cond] += 1
    ranked = sorted(totals.items(), key=lambda kv: (-kv[1], kv[0]))[:limit]
    return [_rate_row("condition", cond, signals[cond], total) for cond, total in ranked]


def timing_histogram(conn: sqlite3.Connection) -> list[dict]:
    """Distribution of every finding's days_after_enrolment across fixed buckets
    (brief: "<0, 0-90, 91-365, 366-1095, >1095"). Unlike the rate tables above, this
    counts findings, not trials, and isn't restricted to severity='SIGNAL' -- it's
    the full timing picture, including the pre-enrolment CONTEXT findings that make
    up the "<0" bucket."""
    select = ", ".join(f"SUM(CASE WHEN {cond} THEN 1 ELSE 0 END) AS b{i}" for i, (_, cond) in enumerate(_HISTOGRAM_BUCKETS))
    row = conn.execute(
        f"SELECT {select}, COUNT(*) AS total FROM findings WHERE days_after_enrolment IS NOT NULL"
    ).fetchone()
    total = row["total"]
    return [
        {"bucket": label, "count": row[f"b{i}"], "total": total, "rate": round(row[f"b{i}"] / total, 4) if total else 0.0}
        for i, (label, _) in enumerate(_HISTOGRAM_BUCKETS)
    ]


def adjudication_concern_mix(conn: sqlite3.Connection) -> list[dict]:
    """Distribution of adjudicated concern levels, excluding UNREVIEWED (that's the
    LLM chain's own on-failure sentinel, not a verdict -- see ctcm.adjudicate's
    module docstring and ctcm.api._adjudication_for, which suppresses it the same
    way). [] if the adjudications table doesn't exist yet -- the common case for a
    fixture db or a corpus that hasn't been through `scripts/run_adjudicate.py`."""
    if not _table_exists(conn, "adjudications"):
        return []
    cols = {r[1] for r in conn.execute("PRAGMA table_info(adjudications)")}
    if "severity_confirmed" not in cols:
        return []  # table exists but not in adjudicate.py's shape -- skip rather than guess
    rows = conn.execute(
        "SELECT severity_confirmed, COUNT(*) AS cnt FROM adjudications WHERE severity_confirmed != 'UNREVIEWED' GROUP BY severity_confirmed"
    ).fetchall()
    counts = {r["severity_confirmed"]: r["cnt"] for r in rows}
    total = sum(counts.values())
    return [
        {"concern": level, "count": counts.get(level, 0), "total": total, "rate": round(counts.get(level, 0) / total, 4) if total else 0.0}
        for level in _CONCERN_ORDER
        if level in counts
    ]
