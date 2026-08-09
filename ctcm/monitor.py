"""Continuous monitoring: cheap re-check of already-ingested trials for new
registry versions, source-agnostic via the RegistryAdapter protocol below.

check_updates() does, per trial: refetch the history list only (~1 request,
via adapter.list_versions -- cheap), compare its length against the trial's
recorded version_count (trials.version_count, set by ctcm.extract.load_corpus
from the same history "changes" list -- NOT `SELECT COUNT(*) FROM versions`,
which only counts the subset of versions we actually cache under the fetch
rule below, not every registry revision). Trials with a longer history get
their new versions fetched into the existing data/cache/{nct}/ layout (same
fetch rule as ctcm.ingest.fetch_trial: v0 + outcome-touching + final); only
once every wanted snapshot for a trial is confirmed cached on disk does
history.json get refreshed, atomically (see _fetch_new_snapshots) -- a
mid-trial fetch failure leaves the old history.json untouched, so the next
pass sees the same version_count and retries instead of silently losing the
missing version forever (task14-review.md Critical #1).

ctcm.extract.load_corpus() and ctcm.pipeline.run_pipeline() then re-run
scoped to just this pass's changed trials (both take an optional nct_ids
filter added for this: default None keeps every other caller's original
whole-corpus behaviour unchanged). Scoping also shrinks Critical #1's blast
radius to nothing: an nct whose fetch failed is never in `changed`, so it's
never touched here regardless.

run_pipeline() does a delete+reinsert per trial it's scoped to, so a changed
trial's findings get fresh finding_ids and NULL first_seen_at every call.
check_updates() snapshots each changed trial's pre-existing findings by
content hash (ctcm.adjudicate.content_hash -- findings have no stable id
across a re-run) as a per-hash *list* of prior first_seen_at values, not a
set/dict keyed by hash alone: two findings can legitimately share an
identical content hash within one trial (duplicate/near-duplicate outcome
text is real registry data), and a set would collapse them, letting one
silently inherit the other's first_seen_at (task14-review.md Important #3).
Reconciliation consumes each hash's queue of prior values in order as
matching post-rerun rows are seen; once a hash's queue is empty, further rows
with that hash are the surplus -- i.e. a count increase -- and are genuinely
new: stamped with now() and logged to data/monitor_log.jsonl.

run_pipeline(t3_enabled=True, t3_limit=0) -- NOT t3_enabled=False -- so that
reconciliation is exact: ctcm.match.T3Client checks llm_cache before it
checks the call budget (same reason the Makefile's own `pipeline` target
defaults T3_LIMIT=0), so a limit of 0 only blocks a genuinely new `claude -p`
call and never a previously-cached tier decision. t3_enabled=False would
skip T3Client entirely, bypassing that cache lookup, and flip every
previously LLM-resolved pair on a changed trial to T2_UNRESOLVED, minting
spurious "new" content hashes. With t3_limit=0, an unchanged candidate pair
replays to the same cache hit (or the same T2_UNRESOLVED, if uncached) every
time -- reconciliation only ever needs to distinguish genuinely new registry
content from a fully reproducible replay.
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
    """Fetches every wanted-but-not-yet-cached snapshot for one trial, then --
    only once every one of them has landed on disk -- refreshes history.json.
    If fetch_version raises partway through (network error, anything), the
    exception propagates before history.json is touched at all: the old
    history.json (and its smaller version count) is left exactly as it was,
    so the next check_updates() pass sees the same known count and retries
    the missing version, instead of load_corpus() later adopting the new,
    larger count for a trial whose snapshot was never actually cached
    (task14-review.md Critical #1 -- reproduced and fixed)."""
    trial_dir = config.CACHE_DIR / nct
    trial_dir.mkdir(parents=True, exist_ok=True)

    for v in sorted(_versions_to_fetch(fresh)):
        snap_path = trial_dir / f"v{v}.json.gz"
        if snap_path.exists():
            continue
        snap = await adapter.fetch_version(nct, v)
        snap_path.write_bytes(gzip.compress(json.dumps(snap).encode()))

    # Every wanted version is now confirmed cached -- safe to advance history.json.
    # Atomic write (temp file + os.replace, via Path.replace) so a crash mid-write
    # can't leave a half-written history.json either.
    history = {"changes": [{"version": v["version"], "date": v["date"], "moduleLabels": v["labels"]} for v in fresh]}
    tmp_path = trial_dir / "history.json.tmp"
    tmp_path.write_text(json.dumps(history))
    tmp_path.replace(trial_dir / "history.json")


# ---- content-hash based new-finding detection -------------------------------------------


def _findings_multiset(conn: sqlite3.Connection, nct: str) -> dict[str, list[str | None]]:
    """content_hash -> list of first_seen_at values, one entry per finding
    currently stored for this trial (module docstring: a list, not a set/dict
    keyed by hash alone, because two findings can legitimately share an
    identical content hash within one trial)."""
    out: dict[str, list[str | None]] = {}
    for r in conn.execute(
        "SELECT from_version, to_version, change_type, before_measure, after_measure, first_seen_at "
        "FROM findings WHERE nct_id=? ORDER BY finding_id",
        (nct,),
    ):
        h = content_hash(nct, r["from_version"], r["to_version"], r["change_type"], r["before_measure"], r["after_measure"])
        out.setdefault(h, []).append(r["first_seen_at"])
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


def _reconcile_trial(conn: sqlite3.Connection, nct: str, before: dict[str, list[str | None]], now: str) -> list[sqlite3.Row]:
    """Restores first_seen_at for every finding run_pipeline() just
    reinserted for this trial, and returns the rows that are genuinely new
    (for the caller to log + count). Multiset diff, not a set: each hash's
    pre-existing first_seen_at values are consumed in order as matching
    post-rerun rows are seen; once a hash's queue is empty, further rows with
    that hash are the surplus -- a count increase -- and are genuinely new."""
    queues = {h: list(vals) for h, vals in before.items()}
    new_rows = []
    for r in conn.execute(
        "SELECT finding_id, nct_id, from_version, to_version, change_type, severity, before_measure, after_measure "
        "FROM findings WHERE nct_id=? ORDER BY finding_id",
        (nct,),
    ).fetchall():
        h = content_hash(nct, r["from_version"], r["to_version"], r["change_type"], r["before_measure"], r["after_measure"])
        q = queues.get(h)
        if q:
            first_seen_at = q.pop(0)
        else:
            first_seen_at = now
            new_rows.append(r)
        conn.execute("UPDATE findings SET first_seen_at=? WHERE finding_id=?", (first_seen_at, r["finding_id"]))
    return new_rows


# ---- top-level entry point ---------------------------------------------------------------


@dataclass(frozen=True)
class MonitorResult:
    checked: int
    changed: list[str]
    new_findings: int


async def check_updates(nct_ids: list[str], adapter: RegistryAdapter) -> MonitorResult:
    """Cheap re-check pass over nct_ids: refetch each trial's history list
    (~1 request/trial), fetch only genuinely new snapshots for trials whose
    history grew, then re-run load_corpus()+run_pipeline() scoped to just
    those changed trials and detect+log new findings by content hash (see
    module docstring)."""
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
        before_all = {nct: _findings_multiset(conn, nct) for nct in changed}
        conn.close()  # load_corpus()/run_pipeline() open their own connections

        _retry_on_lock(lambda: load_corpus(nct_ids=changed))
        _retry_on_lock(lambda: run_pipeline(t3_enabled=True, t3_limit=0, nct_ids=changed))

        conn = db.connect()
        ensure_schema(conn)  # cheap idempotent re-check; guards a concurrent agent's race
        now = datetime.now(timezone.utc).isoformat()
        for nct in changed:
            new_rows = _reconcile_trial(conn, nct, before_all[nct], now)
            for row in new_rows:
                _log_new_finding(now, row)
            new_findings_total += len(new_rows)
        conn.commit()

    conn.close()
    return MonitorResult(checked=len(nct_ids), changed=changed, new_findings=new_findings_total)
