#!/usr/bin/env python3
"""CLI entry: python scripts/run_pipeline.py [--no-t3] [--t3-limit N]"""

import argparse

from ctcm.pipeline import run_pipeline

if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument(
        "--no-t3", action="store_true",
        help="disable T3 LLM escalation entirely; residual ambiguous pairs resolve to DIFFERENT/T2_UNRESOLVED",
    )
    p.add_argument(
        "--t3-limit", type=int, default=200,
        help="max claude -p calls this run (default 200); cache hits are free and don't count against it",
    )
    args = p.parse_args()

    n = run_pipeline(t3_enabled=not args.no_t3, t3_limit=args.t3_limit)
    print(f"pipeline done: {n} findings written")
