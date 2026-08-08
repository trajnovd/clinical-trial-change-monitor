#!/usr/bin/env python3
"""Benchmark harness: runs the ctcm pipeline over whatever Holst trials are
cached and scores the findings against Holst et al. 2023's within-registry
manual ratings (benchmark/DATA-NOTES.md, benchmark/README.md).

Run from repo root:
    .venv/bin/python benchmark/evaluate.py --split dev

Writes benchmark/results_dev.md. --split heldout is for the single v1.0 run
only (TECH-PRD gate) -- it prints a warning banner and never writes a results
file, so a stray run can't leak held-out numbers into the repo early.
"""

import argparse
import csv
import random
import sqlite3
import sys
import time
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


def cached_ids(nct_ids: set[str]) -> set[str]:
    """Which of nct_ids made it into the trials table, i.e. have >=1 extracted
    version -- the real proxy for 'ingested and pipeline-ready', not just a
    cache directory existing."""
    conn = db.connect()
    have = {r["nct_id"] for r in conn.execute("SELECT DISTINCT nct_id FROM trials")}
    conn.close()
    return nct_ids & have


def predicted_types_by_trial(nct_ids: set[str]) -> dict[str, frozenset[str]]:
    """Distinct scored change_type values per trial from findings, restricted to
    nct_ids (the evaluated set) and to CHANGE_TYPES (excludes REWORDED/
    TIMELINE_REVISED, which have no Holst ground truth -- see mapping.py)."""
    if not nct_ids:
        return {}
    conn = db.connect()
    placeholders = ",".join("?" * len(CHANGE_TYPES))
    rows = conn.execute(
        f"SELECT DISTINCT nct_id, change_type FROM findings WHERE change_type IN ({placeholders})",
        CHANGE_TYPES,
    ).fetchall()
    conn.close()
    by_trial: dict[str, set[str]] = {nct: set() for nct in nct_ids}
    for r in rows:
        if r["nct_id"] in by_trial:
            by_trial[r["nct_id"]].add(r["change_type"])
    return {k: frozenset(v) for k, v in by_trial.items()}


def resolved_by_counts(nct_ids: set[str]) -> dict[str, int]:
    if not nct_ids:
        return {}
    conn = db.connect()
    placeholders = ",".join("?" * len(nct_ids))
    rows = conn.execute(
        f"SELECT resolved_by, COUNT(*) c FROM findings WHERE nct_id IN ({placeholders}) GROUP BY resolved_by",
        tuple(nct_ids),
    ).fetchall()
    conn.close()
    return {r["resolved_by"]: r["c"] for r in rows}


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
        "- omitted_measurement/omitted_aggregation/omitted_timing (Holst's 'broadened' "
        "sub-flags) have no corresponding change_type in our taxonomy and are excluded from "
        "per-type scoring; trials whose only Holst signal is one of these are also excluded "
        "from the false-positive-rate denominator (can't call our finding there a clean FP "
        "or TN when Holst did observe *something*).",
        "- change_measurement/change_aggregation map to PRIMARY_NARROWED at low confidence "
        "(documented in benchmark/README.md) -- Holst's own severity coding treats them as "
        "categorically milder than the swap/demote/promote group.",
        "- PRIMARY_NARROWED recall is structurally 0 at T0: `t0_matcher` (ctcm/classify.py) "
        "only ever returns SAME or None, never NARROWED, so this code cannot be predicted at "
        "all until v0.3's fuzzy-matching tiers land -- every PRIMARY_NARROWED FN below is "
        "expected, not a bug.",
        "",
    ]
    return "\n".join(lines)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--split", choices=["dev", "heldout"], default="dev")
    args = parser.parse_args()

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

    have_cache = cached_ids(set(split_ids))
    predicted = predicted_types_by_trial(have_cache)
    scores = score_by_type(predicted, truth, change_types=CHANGE_TYPES)
    fp, fp_denom = false_positive_rate(predicted, truth)
    tiers = resolved_by_counts(have_cache)

    report = render_report(args.split, split_ids, have_cache, scores, fp, fp_denom, tiers)
    print(report)

    if args.split == "dev":
        RESULTS_DEV_PATH.write_text(report)
        print(f"\nwrote {RESULTS_DEV_PATH}")


if __name__ == "__main__":
    main()
