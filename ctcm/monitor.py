"""Continuous monitoring: cheap re-check of already-ingested trials for new
registry versions, source-agnostic via the RegistryAdapter protocol below.

check_updates() does, per trial: refetch the history list only (~1 request,
via adapter.list_versions -- cheap), compare its length against the trial's
recorded version_count (trials.version_count, set by ctcm.extract.load_corpus
from the same history "changes" list -- NOT `SELECT COUNT(*) FROM versions`,
which only counts the subset of versions we actually cache under the fetch
rule below, not every registry revision). Trials with a longer history get
their new versions fetched into the existing data/cache/{nct}/ layout (same
fetch rule as ctcm.ingest.fetch_trial: v0 + outcome-touching + final), then
ctcm.extract.load_corpus() and ctcm.pipeline.run_pipeline() re-run.

load_corpus()/run_pipeline() aren't scoped to "just the changed trials" --
they're plain, unmodified, idempotent full-corpus passes (out of scope for
this task to make nct-filterable; see task14-report.md). run_pipeline() does
a delete+reinsert per trial, so EVERY trial's findings get a fresh finding_id
and a NULL first_seen_at on every call, not just the changed ones. To keep
first_seen_at meaningful, check_updates() snapshots every trial's findings by
content hash (ctcm.adjudicate.content_hash -- findings have no stable id
across a re-run) before calling run_pipeline(), and restores each
surviving hash's original first_seen_at afterward; only hashes that are new
AND belong to a trial this pass actually found new versions for count as
"new findings" (logged to data/monitor_log.jsonl and stamped with now()).
run_pipeline(t3_enabled=True, t3_limit=0) -- NOT t3_enabled=False -- so that
reconciliation is exact: ctcm.match.T3Client checks llm_cache before it
checks the call budget (same reason the Makefile's own `pipeline` target
defaults T3_LIMIT=0), so a limit of 0 only blocks a genuinely new `claude -p`
call and never a previously-cached tier decision. t3_enabled=False would
skip T3Client entirely, bypassing that cache lookup, and flip every
previously LLM-resolved pair on EVERY trial in the corpus (not just this
pass's changed ones) to T2_UNRESOLVED -- since run_pipeline() re-diffs
everyone, that would spuriously mint "new" content hashes wholesale. With
t3_limit=0, an unchanged trial's outcomes are byte-identical to last run, so
its candidate pairs replay to the same cache hit (or the same
T2_UNRESOLVED, if uncached) every time -- reconciliation only ever needs to
distinguish genuinely new registry versions from a fully reproducible replay.
"""

import asyncio
import gzip
import json
import logging
import sqlite3
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Protocol

import httpx

from ctcm import config, db, ingest
from ctcm.adjudicate import content_hash
from ctcm.extract import load_corpus
from ctcm.pipeline import run_pipeline

logger = logging.getLogger(__name__)

MAX_CONCURRENCY = ingest.MAX_CONCURRENCY  # reuse ingest's network concurrency bound as-is


# ---- source-agnostic adapter protocol (Task 15/CTIS implements the same shape) -------


class RegistryAdapter(Protocol):
    async def list_versions(self, nct_id: str) -> list[dict]:
        """Every revision the registry has on file for this trial, oldest
        first: [{"version": int, "date": str | None, "labels": list[str]}, ...].
        Must be a live call, not served from local cache -- this is the
        signal check_updates() diffs against what's already on disk."""
        ...

    async def fetch_version(self, nct_id: str, version: int) -> dict:
        """Raw registry response for one version snapshot."""
        ...


class CTGovAdapter:
    """CT.gov RegistryAdapter: wraps ctcm.ingest's existing retry-safe HTTP
    fetch (ingest._get_json -- same endpoints, same 429/5xx backoff) behind
    the protocol above. No change to ctcm/ingest.py itself.

    Fully serialized (one request in flight at a time) plus a MIN_INTERVAL
    floor between dispatches, regardless of caller concurrency: verified
    empirically 2026-08-09 that /api/int/studies/*/history -- unlike the bulk
    endpoints ingest.py normally drives -- starts 429ing within a handful of
    requests at ANY overlapping concurrency (>=2 in flight); a fully
    sequential loop with real network latency between calls did not trip it,
    but pacing dispatch starts alone (while still allowing several requests'
    retries to overlap in flight) still did. Not documented, so this is a
    measured guess, not a published limit.
    # ponytail: a global lock, not a real token bucket -- tune MIN_INTERVAL
    # or swap in a proper rate limiter if a real run still 429s.
    """

    MIN_INTERVAL = 0.4

    def __init__(self, client: httpx.AsyncClient):
        self._client = client
        self._lock = asyncio.Lock()
        self._next_ok = 0.0

    async def _paced_get(self, url: str) -> dict:
        async with self._lock:  # held for the whole request -- see class docstring
            now = asyncio.get_event_loop().time()
            if self._next_ok > now:
                await asyncio.sleep(self._next_ok - now)
            result = await ingest._get_json(self._client, url)
            self._next_ok = asyncio.get_event_loop().time() + self.MIN_INTERVAL
            return result

    async def list_versions(self, nct_id: str) -> list[dict]:
        history = await self._paced_get(f"/api/int/studies/{nct_id}/history")
        return [
            {"version": c["version"], "date": c.get("date"), "labels": c.get("moduleLabels") or []}
            for c in history.get("changes", [])
        ]

    async def fetch_version(self, nct_id: str, version: int) -> dict:
        return await self._paced_get(f"/api/int/studies/{nct_id}/history/{version}")


def _versions_to_fetch(versions: list[dict]) -> set[int]:
    """Same fetch rule as ctcm.ingest.fetch_trial (its module docstring): v0,
    every version whose labels touch outcomes, and the final version. Reuses
    ingest.OUTCOME_MODULE_LABEL; duplicated as a set-comprehension rather than
    calling fetch_trial's internals because it operates on the protocol's
    generic {version, labels} shape, not ingest's raw CT.gov {version,
    moduleLabels} change dicts -- this is what keeps check_updates() itself
    source-agnostic."""
    if not versions:
        return set()
    wanted = {0, versions[-1]["version"]}
    wanted.update(v["version"] for v in versions if ingest.OUTCOME_MODULE_LABEL in (v.get("labels") or []))
    return wanted


# ---- schema guard: first_seen_at column ------------------------------------------------


def ensure_schema(conn: sqlite3.Connection) -> None:
    """Idempotent: sqlite has no 'ADD COLUMN IF NOT EXISTS', so guard the
    ALTER with a PRAGMA check (and a belt-and-braces except, in case a
    concurrent agent's monitor pass wins the race between the check and the
    ALTER -- data/ctcm.db is shared, per task brief)."""
    cols = {r["name"] for r in conn.execute("PRAGMA table_info(findings)")}
    if "first_seen_at" in cols:
        return
    try:
        conn.execute("ALTER TABLE findings ADD COLUMN first_seen_at TEXT")
        conn.commit()
    except sqlite3.OperationalError as e:
        if "duplicate column" not in str(e).lower():
            raise


def _retry_on_lock(fn, retries: int = 5, delay: float = 5.0):
    """data/ctcm.db is shared with other agents' concurrent runs (task
    brief) -- a transient 'database is locked' just needs a retry, not a
    real fix (same pattern as benchmark/evaluate.py's _retry_on_lock)."""
    for attempt in range(retries):
        try:
            return fn()
        except sqlite3.OperationalError as e:
            if "locked" not in str(e).lower() or attempt == retries - 1:
                raise
            logger.warning("data/ctcm.db locked, retry %d/%d in %ss ...", attempt + 1, retries, delay)
            time.sleep(delay)


# ---- new-snapshot fetch into the existing cache layout ---------------------------------


async def _fetch_new_snapshots(adapter: RegistryAdapter, nct: str, fresh: list[dict]) -> None:
    trial_dir = config.CACHE_DIR / nct
    trial_dir.mkdir(parents=True, exist_ok=True)

    # Refresh the cached history.json so load_corpus() (which reads it for
    # each version's date/moduleLabels and derives trials.version_count from
    # len(changes)) sees this pass's fresh counts, not a stale prior fetch.
    history = {"changes": [{"version": v["version"], "date": v["date"], "moduleLabels": v["labels"]} for v in fresh]}
    (trial_dir / "history.json").write_text(json.dumps(history))

    for v in sorted(_versions_to_fetch(fresh)):
        snap_path = trial_dir / f"v{v}.json.gz"
        if snap_path.exists():
            continue
        snap = await adapter.fetch_version(nct, v)
        snap_path.write_bytes(gzip.compress(json.dumps(snap).encode()))


# ---- content-hash based new-finding detection -------------------------------------------


def _all_findings_snapshot(conn: sqlite3.Connection) -> dict[str, dict[str, str | None]]:
    """nct_id -> {content_hash: first_seen_at} for every finding currently in
    the db, in one query. Covers every trial with existing findings (not just
    this pass's changed set) because run_pipeline() delete+reinserts globally
    -- see module docstring."""
    out: dict[str, dict[str, str | None]] = {}
    for r in conn.execute(
        "SELECT nct_id, from_version, to_version, change_type, before_measure, after_measure, first_seen_at "
        "FROM findings"
    ):
        h = content_hash(r["nct_id"], r["from_version"], r["to_version"], r["change_type"], r["before_measure"], r["after_measure"])
        out.setdefault(r["nct_id"], {})[h] = r["first_seen_at"]
    return out


def _log_new_finding(ts: str, row: sqlite3.Row) -> None:
    config.DATA_DIR.mkdir(parents=True, exist_ok=True)
    entry = {
        "ts": ts,
        "nct_id": row["nct_id"],
        "change_type": row["change_type"],
        "severity": row["severity"],
        "from_version": row["from_version"],
        "to_version": row["to_version"],
    }
    with open(config.DATA_DIR / "monitor_log.jsonl", "a") as f:
        f.write(json.dumps(entry) + "\n")


def _reconcile_findings(
    conn: sqlite3.Connection, before_all: dict[str, dict[str, str | None]], changed: set[str], now: str
) -> int:
    """Restores first_seen_at across every trial run_pipeline() just
    reinserted, and logs+stamps genuinely new findings for `changed` trials
    only. Returns the count of logged new findings."""
    ensure_schema(conn)
    to_reconcile = set(before_all) | changed
    rows_by_nct: dict[str, list[sqlite3.Row]] = {}
    if to_reconcile:
        placeholders = ",".join("?" * len(to_reconcile))
        query = (
            "SELECT finding_id, nct_id, from_version, to_version, change_type, severity, before_measure, after_measure "
            f"FROM findings WHERE nct_id IN ({placeholders})"
        )
        for r in conn.execute(query, tuple(to_reconcile)):
            rows_by_nct.setdefault(r["nct_id"], []).append(r)

    new_count = 0
    for nct in to_reconcile:
        before = before_all.get(nct, {})
        new_rows = []
        for r in rows_by_nct.get(nct, []):
            h = content_hash(nct, r["from_version"], r["to_version"], r["change_type"], r["before_measure"], r["after_measure"])
            if h in before:
                first_seen_at = before[h]
            else:
                first_seen_at = now
                new_rows.append(r)
            conn.execute("UPDATE findings SET first_seen_at=? WHERE finding_id=?", (first_seen_at, r["finding_id"]))

        if nct in changed:
            for row in new_rows:
                _log_new_finding(now, row)
            new_count += len(new_rows)
        elif new_rows:
            # run_pipeline() ran with t3_limit=0, which only ever replays a cache hit
            # or the same T2_UNRESOLVED fallback (module docstring), so an unchanged
            # trial's content hashes should never shift. Surface it loudly rather than
            # silently dropping the mismatch (CLAUDE.md SS6).
            logger.warning(
                "nct %s produced %d new-hash finding(s) with no detected version change", nct, len(new_rows)
            )
    conn.commit()
    return new_count


# ---- top-level entry point ---------------------------------------------------------------


@dataclass(frozen=True)
class MonitorResult:
    checked: int
    changed: list[str]
    new_findings: int


async def check_updates(nct_ids: list[str], adapter: RegistryAdapter) -> MonitorResult:
    """Cheap re-check pass over nct_ids: refetch each trial's history list
    (~1 request/trial), fetch only genuinely new snapshots for trials whose
    history grew, then re-run load_corpus()+run_pipeline() once for the whole
    corpus (idempotent -- see module docstring for why this isn't scoped to
    just the changed trials) and detect+log new findings by content hash."""
    conn = db.connect()
    ensure_schema(conn)

    sem = asyncio.Semaphore(MAX_CONCURRENCY)
    changed: list[str] = []
    changed_lock = asyncio.Lock()

    async def _one(nct: str) -> None:
        async with sem:
            try:
                fresh = await adapter.list_versions(nct)
            except Exception:
                logger.exception("failed to check history for %s", nct)
                return
            row = conn.execute("SELECT version_count FROM trials WHERE nct_id=?", (nct,)).fetchone()
            known = row["version_count"] if row and row["version_count"] is not None else 0
            if len(fresh) <= known:
                return
            try:
                await _fetch_new_snapshots(adapter, nct, fresh)
            except Exception:
                logger.exception("failed to fetch new snapshots for %s", nct)
                return
            async with changed_lock:
                changed.append(nct)

    await asyncio.gather(*(_one(nct) for nct in nct_ids))

    new_findings_total = 0
    if changed:
        before_all = _all_findings_snapshot(conn)
        conn.close()  # load_corpus()/run_pipeline() open their own connections

        _retry_on_lock(load_corpus)
        _retry_on_lock(lambda: run_pipeline(t3_enabled=True, t3_limit=0))

        conn = db.connect()
        new_findings_total = _reconcile_findings(conn, before_all, set(changed), datetime.now(timezone.utc).isoformat())

    conn.close()
    return MonitorResult(checked=len(nct_ids), changed=changed, new_findings=new_findings_total)
