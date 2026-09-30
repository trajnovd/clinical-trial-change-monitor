#!/usr/bin/env python3
"""CLI entry: python scripts/run_reslink.py -- re-run after every pipeline pass
(ctcm/reslink.py's module docstring explains why recomputation is the whole
reconciliation story)."""

from ctcm.reslink import main

if __name__ == "__main__":
    main()
