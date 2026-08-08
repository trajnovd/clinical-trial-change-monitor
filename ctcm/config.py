"""Paths and corpus-wide constants. Everything env-overridable stays env-overridable."""

import os
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = Path(os.environ.get("CTCM_DATA_DIR", REPO_ROOT / "data"))
CACHE_DIR = DATA_DIR / "cache"
DB_PATH = DATA_DIR / "ctcm.db"

# Trial count cap for the working corpus (TECH-PRD §3.3: bounded, expandable).
CORPUS_LIMIT = int(os.environ.get("CTCM_CORPUS_LIMIT", "800"))

CT_GOV_BASE = "https://clinicaltrials.gov"
