#!/usr/bin/env python3
"""CLI entry: python scripts/run_pipeline.py"""

from ctcm.pipeline import run_pipeline

if __name__ == "__main__":
    n = run_pipeline()
    print(f"pipeline done: {n} findings written")
