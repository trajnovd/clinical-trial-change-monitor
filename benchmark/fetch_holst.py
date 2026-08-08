#!/usr/bin/env python3
"""Formalises the manual acquisition steps in benchmark/DATA-NOTES.md SS1: fetch
the Holst et al. 2023 label CSV + codebook if not already on disk, then
(re)derive benchmark/data/nct_ids.txt from the CSV. Idempotent -- skips any
file already present, safe to run repeatedly.

Run from repo root: .venv/bin/python benchmark/fetch_holst.py
"""

import csv
from pathlib import Path

import httpx

BENCH_DIR = Path(__file__).resolve().parent
REPO_DIR = BENCH_DIR / "data" / "InvisibleOutcomeChanges"
CSV_PATH = REPO_DIR / "data" / "processed_history_data_analyses.csv"
CODEBOOK_PATH = REPO_DIR / "ASCERTAIN_Codebook_v3_amendments.docx"
NCT_IDS_PATH = BENCH_DIR / "data" / "nct_ids.txt"

# Exact source URLs per DATA-NOTES SS1.
CSV_URL = "https://raw.githubusercontent.com/Martin-R-H/InvisibleOutcomeChanges/main/data/processed_history_data_analyses.csv"
CODEBOOK_URL = "https://osf.io/download/werxf/"


def _fetch(url: str, dest: Path) -> None:
    if dest.exists():
        print(f"skip (already on disk): {dest}")
        return
    dest.parent.mkdir(parents=True, exist_ok=True)
    print(f"fetching {url} -> {dest}")
    with httpx.Client(follow_redirects=True, timeout=60) as client:
        resp = client.get(url)
        resp.raise_for_status()
        dest.write_bytes(resp.content)


def regenerate_nct_ids() -> int:
    """Unique ClinicalTrials.gov NCT IDs across the full 1,402-trial IntoValue
    cohort (DATA-NOTES SS4) -- not just the 559-trial labeled subset. The FPR /
    negative-class evaluation needs the un-flagged trials too, and this tool is
    CT.gov-only (PRD line 202), so DRKS ids are dropped."""
    with CSV_PATH.open(encoding="latin-1", newline="") as f:
        ids = sorted({r["id"] for r in csv.DictReader(f) if r["registry"] == "ClinicalTrials.gov"})
    NCT_IDS_PATH.parent.mkdir(parents=True, exist_ok=True)
    NCT_IDS_PATH.write_text("\n".join(ids) + "\n")
    return len(ids)


def main() -> None:
    _fetch(CSV_URL, CSV_PATH)
    _fetch(CODEBOOK_URL, CODEBOOK_PATH)
    n = regenerate_nct_ids()
    print(f"wrote {NCT_IDS_PATH} ({n} unique ClinicalTrials.gov NCT IDs)")


if __name__ == "__main__":
    main()
