#!/usr/bin/env python3
"""CLI: python scripts/show_history.py NCT04280705
Prints each fetched version's primary outcome(s) + time_frame + version date."""

import sys

from ctcm import db


def main() -> None:
    if len(sys.argv) != 2:
        print("usage: show_history.py <NCT_ID>", file=sys.stderr)
        sys.exit(1)
    nct = sys.argv[1]
    conn = db.connect()

    versions = conn.execute(
        "SELECT version_no, version_date FROM versions WHERE nct_id=? ORDER BY version_no", (nct,)
    ).fetchall()
    if not versions:
        print(f"no extracted versions for {nct} -- has it been ingested and load_corpus()'d?")
        return

    for v in versions:
        outcomes = conn.execute(
            "SELECT measure, time_frame FROM outcomes WHERE nct_id=? AND version_no=? "
            "AND outcome_type='PRIMARY' ORDER BY ordinal",
            (nct, v["version_no"]),
        ).fetchall()
        print(f"v{v['version_no']} ({v['version_date']}):")
        if not outcomes:
            print("  (no primary outcome recorded)")
        for o in outcomes:
            tf = f" [{o['time_frame']}]" if o["time_frame"] else ""
            print(f"  - {o['measure']}{tf}")


if __name__ == "__main__":
    main()
