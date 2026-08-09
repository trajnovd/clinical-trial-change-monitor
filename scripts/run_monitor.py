#!/usr/bin/env python3
"""CLI entry: python scripts/run_monitor.py --watch corpus|path/to/ids.txt --once [--limit N]

Single pass, cron-friendly -- no daemon mode (see Makefile's `monitor` target
for a crontab line). --watch corpus checks every trial already in the db
(data/ctcm.db, populated by a prior `make ingest`+`make load`, or `make ctis`),
optionally capped by --limit to the N most-recently-updated (by latest
version_date). --watch path/to/ids.txt checks the ids listed there, one per line.

Mixed-registry aware (task15-review.md Important #1): `trials` can hold both
CT.gov (NCT ids) and CTIS (ctNumber) rows; check_updates() routes each id to
the right check by its trials.registry column, so this script doesn't need to
know or care which registry any given id belongs to -- it just opens a client
for each and hands both over.
"""

import argparse
import asyncio

import httpx

from ctcm import config, db
from ctcm.ctis import CTISAdapter
from ctcm.monitor import CTGovAdapter, check_updates


def _corpus_ids(conn, limit: int | None) -> list[str]:
    if limit is None:
        return [r["nct_id"] for r in conn.execute("SELECT nct_id FROM trials ORDER BY nct_id")]
    rows = conn.execute(
        "SELECT nct_id FROM versions GROUP BY nct_id ORDER BY MAX(version_date) DESC LIMIT ?", (limit,)
    ).fetchall()
    return [r["nct_id"] for r in rows]


def _file_ids(path: str, limit: int | None) -> list[str]:
    ids = [line.strip() for line in open(path) if line.strip()]
    return ids[:limit] if limit is not None else ids  # limit=0 means zero, not unlimited (consistent with _corpus_ids)


async def _main_async(args: argparse.Namespace) -> None:
    conn = db.connect()
    nct_ids = _corpus_ids(conn, args.limit) if args.watch == "corpus" else _file_ids(args.watch, args.limit)
    conn.close()

    print(f"monitor: checking {len(nct_ids)} trial(s) ({args.watch})", flush=True)
    async with (
        httpx.AsyncClient(base_url=config.CT_GOV_BASE, timeout=30) as ctgov_client,
        httpx.AsyncClient(timeout=30) as ctis_client,
    ):
        adapters = {"ctgov": CTGovAdapter(ctgov_client), "ctis": CTISAdapter()}
        result = await check_updates(nct_ids, adapters, ctis_client=ctis_client)

    print(
        f"monitor done: checked {result.checked}, {len(result.changed)} with new version(s), "
        f"{result.new_findings} new finding(s)",
        flush=True,
    )
    if result.changed:
        print(f"  changed: {', '.join(result.changed)}")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--watch", required=True, help="'corpus' or a path to a text file of NCT IDs, one per line")
    ap.add_argument(
        "--once", action="store_true", required=True,
        help="single pass (required -- there is no daemon/loop mode yet)",
    )
    ap.add_argument(
        "--limit", type=int, default=None,
        help="cap trial count; in corpus mode, the N most-recently-updated trials",
    )
    args = ap.parse_args()
    asyncio.run(_main_async(args))


if __name__ == "__main__":
    main()
