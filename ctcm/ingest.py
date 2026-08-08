"""Discovery + version-history fetch -> local cache.

Cache layout: data/cache/{nct}/history.json (version list),
data/cache/{nct}/v{n}.json.gz (gzipped snapshot per fetched version).
Everything is keyed by (nct_id, version) and skipped if already on disk,
so re-running is free and ingestion is resumable (global-constraints.md).
"""

import argparse
import asyncio
import gzip
import json
import logging
from pathlib import Path

import httpx

from ctcm import config

logger = logging.getLogger(__name__)

MAX_CONCURRENCY = 8
MAX_RETRIES = 5
OUTCOME_MODULE_LABEL = "Outcome Measures"

DISCOVERY_QUERY_TERM = (
    "AREA[StudyType]INTERVENTIONAL AND "
    "AREA[ResultsFirstPostDate]RANGE[2008-01-01,MAX] AND "
    "AREA[StartDate]RANGE[2008-01-01,MAX]"
)


async def _get_json(client: httpx.AsyncClient, url: str, params: dict | None = None) -> dict:
    """GET url as JSON with exponential backoff on 429/5xx, up to MAX_RETRIES attempts."""
    delay = 1.0
    for attempt in range(MAX_RETRIES):
        resp = await client.get(url, params=params)
        if resp.status_code == 429 or resp.status_code >= 500:
            if attempt == MAX_RETRIES - 1:
                resp.raise_for_status()
            await asyncio.sleep(delay)
            delay *= 2
            continue
        resp.raise_for_status()
        return resp.json()
    raise RuntimeError(f"unreachable: retries exhausted for {url}")  # pragma: no cover


async def discover(limit: int) -> list[str]:
    """NCT IDs for the working corpus: interventional, completed, results posted, start >=2008
    (global-constraints.md). Verified empirically 2026-08-08 that query.term supports AND'ing
    AREA[StartDate] alongside AREA[ResultsFirstPostDate] -- both filters run server-side."""
    nct_ids: list[str] = []
    params = {
        "filter.overallStatus": "COMPLETED",
        "query.term": DISCOVERY_QUERY_TERM,
        "fields": "NCTId",
        "pageSize": 1000,
    }
    async with httpx.AsyncClient(base_url=config.CT_GOV_BASE, timeout=30) as client:
        while len(nct_ids) < limit:
            data = await _get_json(client, "/api/v2/studies", params=params)
            for study in data.get("studies", []):
                nct_ids.append(study["protocolSection"]["identificationModule"]["nctId"])
                if len(nct_ids) >= limit:
                    break
            token = data.get("nextPageToken")
            if not token:
                break
            params["pageToken"] = token
    return nct_ids[:limit]


def _trial_dir(nct: str) -> Path:
    d = config.CACHE_DIR / nct
    d.mkdir(parents=True, exist_ok=True)
    return d


async def fetch_trial(client: httpx.AsyncClient, nct: str) -> None:
    """Cache the version history + selected snapshots for one trial.
    Fetch rule: v0 + every version with 'Outcome Measures' in moduleLabels + final version.
    # ponytail: skips non-outcome versions; date-revision detection limited to fetched
    # set -- fetch all versions if TIMELINE_REVISED recall matters
    """
    trial_dir = _trial_dir(nct)
    history_path = trial_dir / "history.json"
    if history_path.exists():
        history = json.loads(history_path.read_text())
    else:
        history = await _get_json(client, f"/api/int/studies/{nct}/history")
        history_path.write_text(json.dumps(history))

    changes = history.get("changes", [])
    if not changes:
        return

    versions_to_fetch = {0, changes[-1]["version"]}
    for c in changes:
        if OUTCOME_MODULE_LABEL in (c.get("moduleLabels") or []):
            versions_to_fetch.add(c["version"])

    for v in sorted(versions_to_fetch):
        snap_path = trial_dir / f"v{v}.json.gz"
        if snap_path.exists():
            continue
        snap = await _get_json(client, f"/api/int/studies/{nct}/history/{v}")
        snap_path.write_bytes(gzip.compress(json.dumps(snap).encode()))


async def ingest(nct_ids: list[str]) -> None:
    """Fetch every trial's history + selected snapshots, bounded concurrency, with
    a progress line every 50 trials. One trial's failure never aborts the batch."""
    sem = asyncio.Semaphore(MAX_CONCURRENCY)
    done = 0
    lock = asyncio.Lock()
    total = len(nct_ids)

    async def _one(client: httpx.AsyncClient, nct: str) -> None:
        nonlocal done
        async with sem:
            try:
                await fetch_trial(client, nct)
            except Exception:
                logger.exception("failed to fetch %s", nct)
        async with lock:
            done += 1
            if done % 50 == 0 or done == total:
                print(f"ingest progress: {done}/{total}", flush=True)

    async with httpx.AsyncClient(base_url=config.CT_GOV_BASE, timeout=30) as client:
        await asyncio.gather(*(_one(client, nct) for nct in nct_ids))


async def _main_async(limit: int) -> None:
    nct_ids = await discover(limit)
    print(f"discovered {len(nct_ids)} trials", flush=True)
    await ingest(nct_ids)
    print("ingest done", flush=True)


def main() -> None:
    parser = argparse.ArgumentParser(description="Ingest trial version histories into the cache")
    parser.add_argument("--limit", type=int, default=config.CORPUS_LIMIT)
    args = parser.parse_args()
    asyncio.run(_main_async(args.limit))


if __name__ == "__main__":
    main()
