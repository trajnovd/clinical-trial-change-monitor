"""sqlite connection + schema. TECH-PRD §3.4 translated to sqlite: TEXT[] -> JSON text,
VECTOR dropped (# ponytail: sqlite3 for DuckDB, no embedding column until a vector store
earns its keep), findings gets INTEGER PK (UUID PK not needed for a single-writer sqlite file).
"""

import sqlite3

from ctcm import config

SCHEMA = """
CREATE TABLE IF NOT EXISTS trials(nct_id TEXT PRIMARY KEY, study_type TEXT, phase TEXT,
  overall_status TEXT, lead_sponsor TEXT, sponsor_class TEXT, conditions TEXT,
  enrolment_count INT, first_posted_date TEXT, version_count INT);
CREATE TABLE IF NOT EXISTS versions(nct_id TEXT, version_no INT, version_date TEXT,
  module_labels TEXT, content_hash TEXT, PRIMARY KEY(nct_id, version_no));
CREATE TABLE IF NOT EXISTS outcomes(nct_id TEXT, version_no INT, outcome_type TEXT,
  ordinal INT, measure TEXT, description TEXT, time_frame TEXT, measure_norm TEXT,
  PRIMARY KEY(nct_id, version_no, outcome_type, ordinal));
CREATE TABLE IF NOT EXISTS timeline_facts(nct_id TEXT, version_no INT, start_date TEXT,
  start_date_type TEXT, primary_completion_date TEXT, primary_completion_type TEXT,
  completion_date TEXT, PRIMARY KEY(nct_id, version_no));
CREATE TABLE IF NOT EXISTS findings(finding_id INTEGER PRIMARY KEY, nct_id TEXT,
  from_version INT, to_version INT, change_type TEXT, severity TEXT,
  before_measure TEXT, after_measure TEXT, days_after_enrolment INT,
  days_after_primary_completion INT, confidence REAL, resolved_by TEXT, rationale TEXT);
CREATE TABLE IF NOT EXISTS llm_cache(key TEXT PRIMARY KEY, response TEXT);
"""


def connect() -> sqlite3.Connection:
    """Open (creating if needed) the sqlite db with WAL mode and the full schema.
    Idempotent: safe to call from every entry point."""
    config.DATA_DIR.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(config.DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.executescript(SCHEMA)
    conn.commit()
    return conn


if __name__ == "__main__":
    c = connect()
    tables = [r[0] for r in c.execute("SELECT name FROM sqlite_master WHERE type='table'")]
    print(f"db at {config.DB_PATH}: tables={tables}")
    c.close()
