"""Results-linkage classification: is a finding's registry edit part of the sponsor
posting results, rather than a standalone outcome switch? Validated on the live
corpus (v2 scratch analysis, 2026-08-09): 75% of post-completion primary-outcome
changes coincide with results posting to ClinicalTrials.gov -- registry
restructuring, not outcome switching. The defensible strict number -- trials whose
primary outcome changed during the trial, or within 365 days after primary
completion with NO results-posting event nearby -- is ~202/998 (20.2%) vs the
naive 721.

A finding is RESULTS-LINKED if either:
  (a) the first registry version whose moduleLabels include a results-section
      label (RESULTS_SECTION_LABELS, or any label containing "Results") falls in
      the finding's version window: from_version < first_results_version <=
      to_version. Labels are read from data/cache/{nct}/history.json's full
      changes list, NOT the versions table -- versions only holds the cached
      snapshot subset (v0 + outcome-touching + final, ctcm.ingest's fetch rule),
      so the true first results-touching version may not be a row there. A trial
      can't be in the db without history.json (ctcm.extract.load_corpus skips
      such dirs), so this source is complete whenever the cache is intact.
  (b) the to_version's version_date is within RESULTS_PROXIMITY_DAYS (90) of any
      of resultsFirstSubmitDate / resultsFirstSubmitQcDate /
      resultsFirstPostDateStruct.date, read from the newest readable cached
      snapshot's protocolSection.statusModule.

findings.results_linked (guarded ALTER, same shape as ctcm.monitor.ensure_schema):
1 = linked, 0 = determined not linked, NULL = undetermined (no cached data to
decide with -- e.g. CTIS rows, whose cache has neither a CT.gov history.json nor
v*.json.gz snapshots). (a) and (b) combine as a three-valued OR: any True -> 1;
otherwise any undetermined side -> NULL; both determined False -> 0.

Re-run policy: ctcm.pipeline delete+reinserts findings, renumbering finding_ids
and leaving the fresh rows' results_linked NULL. Unlike monitor.py's
first_seen_at -- historical provenance that must be carried across a re-run by
content hash -- results_linked is a pure function of the finding's version window
plus the trial's cache, so there is nothing to carry over: just re-run this
enrichment (scripts/run_reslink.py) after every pipeline pass; recomputation IS
the reconciliation.

STRICT-GENUINE switcher (trial-level): has a SIGNAL finding with
days_after_enrolment > 0 and days_after_primary_completion <= 0 (during the
trial), OR a SIGNAL finding with 0 < days_after_primary_completion <=
STRICT_WINDOW_DAYS (365) that is determined not results-linked
(results_linked = 0 -- NULL is undetermined and deliberately does NOT count).
STRICT_GENUINE_WHERE below is the single SQL definition; ctcm.api imports
strict_genuine_trial_count() rather than restating the predicate.
"""

import argparse
import gzip
import json
import logging
import sqlite3
from collections import Counter
from datetime import date

from ctcm import config

logger = logging.getLogger(__name__)

RESULTS_SECTION_LABELS = {"Participant Flow", "Baseline Characteristics", "Adverse Events"}
RESULTS_PROXIMITY_DAYS = 90
STRICT_WINDOW_DAYS = 365

STRICT_GENUINE_WHERE = (
    "severity='SIGNAL' AND ("
    "(days_after_enrolment > 0 AND days_after_primary_completion <= 0)"
    f" OR (days_after_primary_completion > 0 AND days_after_primary_completion <= {STRICT_WINDOW_DAYS}"
    " AND results_linked = 0))"
)


def ensure_schema(conn: sqlite3.Connection) -> None:
    """Idempotent guarded ALTER, same shape (and same reason for the
    belt-and-braces except) as ctcm.monitor.ensure_schema: data/ctcm.db is
    shared with other agents' concurrent passes."""
    cols = {r["name"] for r in conn.execute("PRAGMA table_info(findings)")}
    if "results_linked" in cols:
        return
    try:
        conn.execute("ALTER TABLE findings ADD COLUMN results_linked INTEGER")
        conn.commit()
    except sqlite3.OperationalError as e:
        if "duplicate column" not in str(e).lower():
            raise


# ---- linkage rules (pure, no I/O) ----------------------------------------------------


def is_results_label(label: str) -> bool:
    return label in RESULTS_SECTION_LABELS or "Results" in label


def first_results_version(changes: list[dict]) -> int | None:
    """Lowest version whose moduleLabels touch a results section, from
    history.json's full changes list. None if the trial never posted one."""
    hits = [c["version"] for c in changes if any(is_results_label(l) for l in c.get("moduleLabels") or [])]
    return min(hits) if hits else None


def in_window(first_results: int | None, from_version: int, to_version: int) -> bool:
    """Rule (a): the results section first appeared inside this finding's version
    window. Strictly after from_version -- a results section that already existed
    at the finding's baseline can't explain the edit."""
    return first_results is not None and from_version < first_results <= to_version


def _parse_date(s: str | None) -> date | None:
    if not s:
        return None
    try:
        return date.fromisoformat(s[:10])
    except ValueError:
        return None


def near_results_date(version_date: str | None, results_dates: list[str]) -> bool | None:
    """Rule (b): to_version's date within RESULTS_PROXIMITY_DAYS of any
    results-posting date. [] means the snapshot was readable and carried no
    results dates -- determined False, the trial never posted results through the
    QC pipeline. None only when the comparison itself is impossible (results
    dates exist but the version date is missing/unparseable)."""
    if not results_dates:
        return False
    vd = _parse_date(version_date)
    if vd is None:
        return None
    deltas = [abs((vd - rd).days) for rd in map(_parse_date, results_dates) if rd is not None]
    return min(deltas) <= RESULTS_PROXIMITY_DAYS if deltas else None


def classify(window: bool | None, near: bool | None) -> int | None:
    """Three-valued OR of the two rules, as the results_linked column value:
    any True -> 1; else any None (undetermined) -> None; both False -> 0."""
    if window or near:
        return 1
    if window is None or near is None:
        return None
    return 0


# ---- cache reads ---------------------------------------------------------------------


def _changes(nct_id: str) -> list[dict] | None:
    """history.json's full changes list; None on any failure (missing file --
    e.g. a CTIS trial's cache dir -- or corrupt JSON) -> rule (a) undetermined."""
    path = config.CACHE_DIR / nct_id / "history.json"
    try:
        return json.loads(path.read_text()).get("changes", [])
    except (OSError, json.JSONDecodeError):
        return None


def _results_dates(nct_id: str, version_nos: list[int]) -> list[str] | None:
    """Results-posting dates from the newest readable cached snapshot's
    protocolSection.statusModule. Same gzip-read shape as ctcm.api's
    _protocol_section, duplicated rather than imported: api.py is the read-only
    serving edge and this enrichment must not depend on it. The newest readable
    snapshot decides -- results dates only accumulate over a trial's history, so
    older snapshots are never consulted once one reads. None when no snapshot
    reads at all -> rule (b) undetermined."""
    for v in sorted(version_nos, reverse=True):
        path = config.CACHE_DIR / nct_id / f"v{v}.json.gz"
        try:
            raw = json.loads(gzip.decompress(path.read_bytes()))
        except (OSError, EOFError, gzip.BadGzipFile, json.JSONDecodeError):
            continue
        study = raw.get("study", raw) or {}
        sm = ((study.get("protocolSection") or {}).get("statusModule")) or {}
        dates = [
            sm.get("resultsFirstSubmitDate"),
            sm.get("resultsFirstSubmitQcDate"),
            (sm.get("resultsFirstPostDateStruct") or {}).get("date"),
        ]
        return [d for d in dates if d]
    return None


# ---- enrichment entry point ----------------------------------------------------------


def enrich_all(conn: sqlite3.Connection) -> Counter:
    """UPDATEs every finding's results_linked from the cache (module docstring
    rules). Idempotent, and the whole reconciliation story after a pipeline
    re-run -- see the re-run policy above."""
    ensure_schema(conn)
    totals: Counter[str] = Counter()
    trial_ids = [r["nct_id"] for r in conn.execute("SELECT DISTINCT nct_id FROM findings ORDER BY nct_id")]
    for nct in trial_ids:
        changes = _changes(nct)
        frv = first_results_version(changes) if changes is not None else None
        version_rows = conn.execute(
            "SELECT version_no, version_date FROM versions WHERE nct_id=?", (nct,)
        ).fetchall()
        version_dates = {r["version_no"]: r["version_date"] for r in version_rows}
        results_dates = _results_dates(nct, list(version_dates))

        for f in conn.execute(
            "SELECT finding_id, from_version, to_version FROM findings WHERE nct_id=?", (nct,)
        ).fetchall():
            window = None if changes is None else in_window(frv, f["from_version"], f["to_version"])
            near = None if results_dates is None else near_results_date(version_dates.get(f["to_version"]), results_dates)
            linked = classify(window, near)
            conn.execute("UPDATE findings SET results_linked=? WHERE finding_id=?", (linked, f["finding_id"]))
            totals["linked" if linked == 1 else "not_linked" if linked == 0 else "undetermined"] += 1
    conn.commit()
    return totals


def strict_genuine_trial_count(conn: sqlite3.Connection) -> int | None:
    """Trials with a strict-genuine SIGNAL finding (STRICT_GENUINE_WHERE). None
    when findings.results_linked doesn't exist yet (enrichment never ran on this
    db) -- callers omit the number rather than fabricate one."""
    cols = {r["name"] for r in conn.execute("PRAGMA table_info(findings)")}
    if "results_linked" not in cols:
        return None
    (n,) = conn.execute(f"SELECT COUNT(DISTINCT nct_id) FROM findings WHERE {STRICT_GENUINE_WHERE}").fetchone()
    return n


def main() -> None:
    argparse.ArgumentParser(description="Classify findings as results-linked; recompute after every pipeline pass").parse_args()
    from ctcm import db

    conn = db.connect()
    totals = enrich_all(conn)
    strict = strict_genuine_trial_count(conn)
    conn.close()

    print(f"done: {sum(totals.values())} finding(s) classified")
    for outcome, n in sorted(totals.items(), key=lambda kv: -kv[1]):
        print(f"  {outcome}: {n}")
    print(f"strict genuine switcher trials: {strict}")


if __name__ == "__main__":
    main()
