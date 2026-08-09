#!/usr/bin/env python3
"""Benchmark harness: runs the ctcm pipeline over whatever Holst trials are
cached and scores the findings against Holst et al. 2023's within-registry
manual ratings (benchmark/DATA-NOTES.md, benchmark/README.md).

Run from repo root:
    .venv/bin/python benchmark/evaluate.py --split dev

Writes benchmark/results_dev.md. --split heldout is for the single v1.0 run
only (TECH-PRD gate) -- it prints a warning banner, never writes a results
file, and refuses to run at all unless CTCM_RELEASE_EVAL=1 is set (see
benchmark/README.md's incident log for why the env-var gate exists).
"""

import argparse
import csv
import os
import random
import sqlite3
import sys
import time
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from mapping import CHANGE_TYPES, TypeScore, false_positive_rate, score_by_type, trial_ground_truth  # noqa: E402

from ctcm import db  # noqa: E402
from ctcm.extract import load_corpus  # noqa: E402
from ctcm.pipeline import run_pipeline  # noqa: E402

BENCH_DIR = Path(__file__).resolve().parent
CSV_PATH = BENCH_DIR / "data" / "InvisibleOutcomeChanges" / "data" / "processed_history_data_analyses.csv"
SPLIT_DIR = BENCH_DIR / "splits"
RESULTS_DEV_PATH = BENCH_DIR / "results_dev.md"

# Fixed so the split never silently changes across reruns (task requirement).
# Arbitrary choice, documented here and in README -- not tuned against results.
SPLIT_SEED = 20260808


def load_labeled_rows() -> dict[str, dict]:
    """The 559-trial CT.gov within-registry-history labeled subset: registry ==
    ClinicalTrials.gov AND referenceid populated (DATA-NOTES SS4) -- NOT the
    292-trial registry-vs-publication sample (has_publication_rating), which is
    out of scope for this tool (it compares registry-to-registry, not
    registry-to-publication)."""
    with CSV_PATH.open(encoding="latin-1", newline="") as f:
        rows = list(csv.DictReader(f))
    return {
        r["id"]: r
        for r in rows
        if r["registry"] == "ClinicalTrials.gov" and r.get("referenceid") not in (None, "", "NA", "NULL")
    }


def get_or_create_split(labeled_ids: list[str]) -> tuple[list[str], list[str]]:
    """50/50 dev/held-out split over the labeled trial IDs, seeded and persisted
    to benchmark/splits/*.txt on first call. Every call after that reads the
    files back verbatim -- the split is computed once, ever, regardless of
    what order or superset of ids gets passed in later (e.g. a re-fetched CSV
    with a couple more rows)."""
    dev_path = SPLIT_DIR / "dev_ids.txt"
    heldout_path = SPLIT_DIR / "heldout_ids.txt"
    if dev_path.exists() and heldout_path.exists():
        return dev_path.read_text().split(), heldout_path.read_text().split()

    SPLIT_DIR.mkdir(parents=True, exist_ok=True)
    ids = sorted(labeled_ids)
    random.Random(SPLIT_SEED).shuffle(ids)
    mid = len(ids) // 2
    dev, heldout = sorted(ids[:mid]), sorted(ids[mid:])
    dev_path.write_text("\n".join(dev) + "\n")
    heldout_path.write_text("\n".join(heldout) + "\n")
    return dev, heldout


def _retry_on_lock(fn, retries: int = 5, delay: float = 5.0):
    """data/ctcm.db is shared with other agents' concurrent pipeline runs
    (task brief). Both load_corpus() and run_pipeline() are idempotent
    (INSERT OR REPLACE / delete+reinsert), so a transient 'database is locked'
    from a concurrent writer just needs a retry, not a real fix."""
    for attempt in range(retries):
        try:
            return fn()
        except sqlite3.OperationalError as e:
            if "locked" not in str(e).lower() or attempt == retries - 1:
                raise
            print(f"data/ctcm.db locked, retry {attempt + 1}/{retries} in {delay}s ...", flush=True)
            time.sleep(delay)


def run_ctcm_pipeline() -> None:
    _retry_on_lock(load_corpus)
    _retry_on_lock(run_pipeline)


def read_pipeline_snapshot(nct_ids: set[str]) -> tuple[set[str], dict[str, frozenset[str]], dict[str, int]]:
    """Which of nct_ids are cache-extracted, their predicted change_types, and
    tier attribution (findings.resolved_by) -- read from ONE connection inside
    ONE explicit read transaction, so all three numbers describe the same
    instant of a shared data/ctcm.db that other agents write to concurrently.

    v05-review.md C1: three separate connections (as this used to be) can each
    land on a different commit mid-run -- e.g. `have_cache` computed before a
    concurrent run_pipeline() write lands, then `predicted`/`tiers` computed
    after -- producing a self-contradictory report (a "T0: 100%" tier table
    next to findings that could only have come from a non-T0 tier). SQLite's
    default Python binding doesn't start a transaction for SELECTs, so without
    an explicit BEGIN each statement gets its own fresh read snapshot even on
    one connection; wrapping the reads in one transaction pins a single
    snapshot for all of them."""
    conn = db.connect()
    conn.execute("BEGIN")
    try:
        have = {r["nct_id"] for r in conn.execute("SELECT DISTINCT nct_id FROM trials")}
        have_cache = nct_ids & have

        predicted: dict[str, set[str]] = {nct: set() for nct in have_cache}
        tiers: Counter[str] = Counter()
        if have_cache:
            placeholders = ",".join("?" * len(have_cache))
            rows = conn.execute(
                f"SELECT nct_id, change_type, resolved_by FROM findings WHERE nct_id IN ({placeholders})",
                tuple(have_cache),
            ).fetchall()
            for r in rows:
                tiers[r["resolved_by"]] += 1
                if r["change_type"] in CHANGE_TYPES:
                    predicted[r["nct_id"]].add(r["change_type"])
        conn.execute("COMMIT")
    finally:
        conn.close()
    return have_cache, {k: frozenset(v) for k, v in predicted.items()}, dict(tiers)


def _fmt(x: float | None) -> str:
    return f"{x:.3f}" if x is not None else "n/a"


def render_report(
    split_name: str,
    split_ids: list[str],
    have_cache: set[str],
    scores: dict[str, TypeScore],
    fp: int,
    fp_denom: int,
    tiers: dict[str, int],
) -> str:
    n_labeled = len(split_ids)
    n_evaluated = len(have_cache)
    n_awaiting = n_labeled - n_evaluated
    lines = [
        f"# Holst benchmark -- {split_name} split results",
        "",
        f"Coverage: {n_evaluated} evaluated / {n_labeled} labeled ({n_evaluated / n_labeled:.0%}), "
        f"{n_awaiting} awaiting ingest.",
        "",
    ]
    if n_evaluated / n_labeled < 0.5:
        lines += [
            "**Ingest coverage is below 50%.** These numbers are a checkpoint on a partial "
            "sample, not the final v1.0 benchmark -- re-run `evaluate.py --split dev` once "
            "`data/ingest_holst.log` reports the full Holst corpus ingested.",
            "",
        ]

    lines += ["## Per change_type (trial-level)", "", "| change_type | TP | FP | FN | TN | precision | recall | F1 |", "|---|---|---|---|---|---|---|---|"]
    for ct in CHANGE_TYPES:
        s = scores[ct]
        lines.append(f"| {ct} | {s.tp} | {s.fp} | {s.fn} | {s.tn} | {_fmt(s.precision)} | {_fmt(s.recall)} | {_fmt(s.f1)} |")
    lines.append("")

    # Flagged mechanically from this run's own numbers (not hardcoded to a
    # specific code) so this section can't go stale the way a hand-picked
    # claim did before (v05-review.md C1/I2) -- every code below this
    # threshold gets equal prominence, whatever the cause turns out to be.
    LOW_PRECISION_THRESHOLD = 0.3
    MIN_PREDICTED_POSITIVES = 3
    low_precision = [
        ct
        for ct in CHANGE_TYPES
        if scores[ct].precision is not None
        and scores[ct].precision < LOW_PRECISION_THRESHOLD
        and (scores[ct].tp + scores[ct].fp) >= MIN_PREDICTED_POSITIVES
    ]
    lines += [
        f"## Low-precision flags (< {LOW_PRECISION_THRESHOLD:.0%}, n >= {MIN_PREDICTED_POSITIVES} predicted-positive trials)",
        "",
    ]
    if low_precision:
        for ct in low_precision:
            s = scores[ct]
            lines.append(f"- **{ct}**: precision {_fmt(s.precision)} ({s.tp} TP / {s.fp} FP).")
        lines.append(
            "Root cause not diagnosed here -- could be a real weakness at whichever tier is "
            "resolving these pairs (see tier attribution below), a mapping edge case, or "
            "small-sample noise at this coverage level. Not safe to treat as reliable without "
            "further investigation; flagged uniformly, not singled out."
        )
    else:
        lines.append("No change_type crosses this threshold on this sample.")
    lines.append("")

    fpr = fp / fp_denom if fp_denom else None
    lines += [
        "## Overall false-positive rate",
        "",
        f"Trials where Holst confirmed no primary-outcome change at all, but we flagged >=1: "
        f"{fp} / {fp_denom} ({_fmt(fpr)}).",
        "",
        "## Tier attribution (findings.resolved_by)",
        "",
    ]
    if tiers:
        total = sum(tiers.values())
        for tier, n in sorted(tiers.items(), key=lambda kv: -kv[1]):
            lines.append(f"- {tier}: {n} ({n / total:.0%})")
    else:
        lines.append("- no findings for evaluated trials")
    lines.append("")

    lines += [
        "## Known limitations",
        "",
        "- Human inter-rater kappa (TECH-PRD SS8.3) requires human coders -- out of scope "
        "for this machine-only benchmark.",
        "- TIMELINE_REVISED and REWORDED have no Holst ground truth and are excluded from "
        "scoring entirely (see benchmark/README.md).",
        "- change_measurement/change_aggregation map to PRIMARY_NARROWED at low confidence "
        "(documented in benchmark/README.md) -- Holst's own severity coding treats them as "
        "categorically milder than the swap/demote/promote group.",
        "- change_timing -> TIMEPOINT_CHANGED is suppressed when another axis-1-producing "
        "flag fires in the same phase, approximating DATA-NOTES' per-primary co-occurrence "
        "caveat at phase granularity (the CSV has no per-primary flags) -- see "
        "benchmark/README.md.",
        "- Findings below come from the full T0-T3 semantic cascade (`ctcm/pipeline.py` -> "
        "`ctcm/match.py`), not just exact-string T0 matching -- see tier attribution above "
        "for the actual mix on this sample.",
        "",
    ]
    return "\n".join(lines)


def _require_release_eval_gate(split: str) -> None:
    """Hard stop for --split heldout: "nothing may iterate against held-out"
    is about the *practice*, not just the results file -- a smoke-test run
    against real held-out labels happened once during Task 9 development even
    though it wrote no file (see the incident log in benchmark/README.md).
    Requires an explicit opt-in env var so a casual/curious invocation can't
    execute the held-out join again by accident; the single v1.0 gate run is
    expected to set this deliberately, not have it on by default."""
    if split == "heldout" and os.environ.get("CTCM_RELEASE_EVAL") != "1":
        raise SystemExit(
            "refusing --split heldout: set CTCM_RELEASE_EVAL=1 to run the single v1.0 gate "
            "evaluation. Nothing may iterate against held-out labels outside that one run -- "
            "see benchmark/README.md."
        )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--split", choices=["dev", "heldout"], default="dev")
    args = parser.parse_args()

    _require_release_eval_gate(args.split)

    if args.split == "heldout":
        banner = "!" * 78
        print(banner)
        print("!!  HELD-OUT SPLIT -- do not iterate against this.")
        print("!!  Single final v1.0 run only. Results are NOT written to disk.")
        print(banner, flush=True)

    labeled = load_labeled_rows()
    dev_ids, heldout_ids = get_or_create_split(list(labeled))
    split_ids = dev_ids if args.split == "dev" else heldout_ids
    truth = {nct: trial_ground_truth(labeled[nct]) for nct in split_ids}

    run_ctcm_pipeline()

    have_cache, predicted, tiers = read_pipeline_snapshot(set(split_ids))
    scores = score_by_type(predicted, truth, change_types=CHANGE_TYPES)
    fp, fp_denom = false_positive_rate(predicted, truth)

    report = render_report(args.split, split_ids, have_cache, scores, fp, fp_denom, tiers)
    print(report)

    if args.split == "dev":
        RESULTS_DEV_PATH.write_text(report)
        print(f"\nwrote {RESULTS_DEV_PATH}")


if __name__ == "__main__":
    main()
