#!/usr/bin/env python3
"""v0.2 checkpoint: the defensible raw count on the ingested corpus.
Run after scripts/run_pipeline.py. Prints how many trials show a
post-enrolment primary change, how many show a post-completion change, and
breakdowns by change_type / severity / sponsor_class."""

from ctcm import db


def main() -> None:
    conn = db.connect()

    (n_trials,) = conn.execute("SELECT COUNT(*) FROM trials").fetchone()
    (n_signal,) = conn.execute("SELECT COUNT(DISTINCT nct_id) FROM findings WHERE severity='SIGNAL'").fetchone()
    (n_post_completion,) = conn.execute(
        "SELECT COUNT(DISTINCT nct_id) FROM findings WHERE change_type='POST_COMPLETION_CHANGE'"
    ).fetchone()

    print(f"corpus: {n_trials} trials")
    print(f"HEADLINE -- trials with >=1 post-enrolment primary change: {n_signal}")
    print(f"trials with >=1 post-completion change: {n_post_completion}")
    print(
        "  caveat: this corpus is results-posted trials only (ingest discovery query "
        "requires ResultsFirstPostDate) -- sponsors routinely add/adjust outcome rows "
        "around results entry as registry housekeeping, not editorial endpoint-switching, "
        "so POST_COMPLETION_CHANGE is an upper bound, not a purity signal. Lead with the "
        "post-enrolment-primary-change number above instead."
    )

    print("\nby change_type:")
    for row in conn.execute("SELECT change_type, COUNT(*) AS n FROM findings GROUP BY change_type ORDER BY n DESC"):
        print(f"  {row['change_type']}: {row['n']}")

    print("\nby severity:")
    for row in conn.execute("SELECT severity, COUNT(*) AS n FROM findings GROUP BY severity ORDER BY n DESC"):
        print(f"  {row['severity']}: {row['n']}")

    print("\nby sponsor_class (SIGNAL findings only):")
    for row in conn.execute(
        "SELECT COALESCE(t.sponsor_class, 'UNKNOWN') AS sponsor_class, COUNT(*) AS n "
        "FROM findings f JOIN trials t ON t.nct_id = f.nct_id WHERE f.severity='SIGNAL' "
        "GROUP BY sponsor_class ORDER BY n DESC"
    ):
        print(f"  {row['sponsor_class']}: {row['n']}")

    conn.close()


if __name__ == "__main__":
    main()
