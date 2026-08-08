#!/usr/bin/env python3
"""CLI entry: python scripts/run_adjudicate.py [--limit N] [--nct NCT12345678]

Adjudicates SIGNAL findings via ctcm.adjudicate.adjudicate(), skipping any whose
content hash is already cached in the adjudications table. Ordered by
|days_after_primary_completion| desc so post-completion changes get reviewed first.
"""

import argparse

from ctcm import db
from ctcm.adjudicate import adjudicate, ensure_schema


def _pending_signal_findings(conn, limit: int, nct: str | None):
    where = ["severity='SIGNAL'"]
    params: list = []
    if nct:
        where.append("nct_id=?")
        params.append(nct)
    # NOT EXISTS via the content_hash() SQL function ensure_schema() registers --
    # single query does the SIGNAL filter, the already-adjudicated skip, and the
    # ordering together instead of a per-row Python round trip.
    where.append(
        "NOT EXISTS (SELECT 1 FROM adjudications a WHERE a.content_hash = "
        "content_hash(f.nct_id, f.from_version, f.to_version, f.change_type, f.before_measure, f.after_measure))"
    )
    sql = (
        f"SELECT f.* FROM findings f WHERE {' AND '.join(where)} "
        "ORDER BY ABS(COALESCE(f.days_after_primary_completion, 0)) DESC LIMIT ?"
    )
    params.append(limit)
    return conn.execute(sql, params).fetchall()


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--limit", type=int, default=25)
    ap.add_argument("--nct", default=None, help="force adjudication of one trial's SIGNAL findings")
    args = ap.parse_args()

    conn = db.connect()
    ensure_schema(conn)
    pending = _pending_signal_findings(conn, args.limit, args.nct)
    print(f"{len(pending)} pending SIGNAL finding(s) to adjudicate")

    counts: dict[str, int] = {}
    for i, row in enumerate(pending, 1):
        adj = adjudicate(row, conn)
        counts[adj.severity_confirmed] = counts.get(adj.severity_confirmed, 0) + 1
        print(f"[{i}/{len(pending)}] {row['nct_id']} finding {row['finding_id']} ({row['change_type']}) -> {adj.severity_confirmed}")

    conn.close()
    print(f"done: {counts}")


if __name__ == "__main__":
    main()
