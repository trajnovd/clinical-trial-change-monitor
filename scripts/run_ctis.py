#!/usr/bin/env python3
"""CLI entry: python scripts/run_ctis.py --bootstrap [--target N] | --snapshot [--limit N]

--bootstrap searches CTIS for ~200 (--target) phase-3, not-yet-reported trials and writes
their ctNumbers to data/ctis_watchlist.txt (ctcm.ctis.search_watchlist/write_watchlist).
--snapshot runs ctcm.ctis.snapshot_pass over the watchlist (optionally capped by --limit),
fetching each trial's current state and keeping/upserting a new snapshot only when its
outcome-relevant hash changed. Both flags can be given together (bootstrap then snapshot).

Findings for CTIS trials come from the SAME `make pipeline` / scripts/run_pipeline.py entry
point already used for CT.gov -- this script only fetches and stores, it does not re-run the
diff/classify pipeline itself (ctcm.pipeline.run_pipeline() already iterates every nct_id in
trials regardless of registry, unmodified).
"""

import argparse
import asyncio

import httpx

from ctcm.ctis import read_watchlist, search_watchlist, snapshot_pass, write_watchlist


async def _bootstrap(target: int) -> None:
    async with httpx.AsyncClient(timeout=30) as client:
        ct_numbers = await search_watchlist(client, target=target)
    write_watchlist(ct_numbers)
    print(f"bootstrap: {len(ct_numbers)} phase-3 trial(s) written to data/ctis_watchlist.txt", flush=True)


async def _snapshot(limit: int | None) -> None:
    ct_numbers = read_watchlist()
    if limit:
        ct_numbers = ct_numbers[:limit]
    if not ct_numbers:
        print("snapshot: no watchlist found -- run --bootstrap first", flush=True)
        return

    async with httpx.AsyncClient(timeout=30) as client:
        result = await snapshot_pass(ct_numbers, client)

    print(
        f"snapshot: checked {result.checked}, stored {len(result.stored)}, "
        f"skipped {len(result.skipped)} (hash unchanged), failed {len(result.failed)}",
        flush=True,
    )
    if result.stored:
        print(f"  stored: {', '.join(result.stored)}")
    if result.failed:
        print(f"  failed: {', '.join(result.failed)}")


async def _main_async(args: argparse.Namespace) -> None:
    if args.bootstrap:
        await _bootstrap(args.target)
    if args.snapshot:
        await _snapshot(args.limit)


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--bootstrap", action="store_true", help="search CTIS, write the starter watchlist")
    ap.add_argument("--snapshot", action="store_true", help="run snapshot_pass over the watchlist")
    ap.add_argument("--target", type=int, default=200, help="--bootstrap: how many trials to collect")
    ap.add_argument("--limit", type=int, default=None, help="--snapshot: cap watchlist trials processed")
    args = ap.parse_args()
    if not args.bootstrap and not args.snapshot:
        ap.error("specify --bootstrap and/or --snapshot")
    asyncio.run(_main_async(args))


if __name__ == "__main__":
    main()
