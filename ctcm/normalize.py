"""Cheap deterministic matching prep (TECH-PRD §4): normalise measure strings so T0
exact-match resolves the large majority of outcome pairs, and split trailing
timepoints the registry didn't put in time_frame. No model calls.
"""

import re

# >=40 curated entries. Expansion runs BEFORE punctuation stripping (see norm()) so
# both hyphenated ("ham-d") and concatenated ("hamd") spellings are given as separate
# keys -- whichever spelling appears in the raw text, the literal hyphen still exists
# at match time. hr/rr kept to their least-ambiguous clinical reading (heart rate,
# respiratory rate) -- oncology's "hazard ratio"/"relative risk" readings are a known
# false-expansion risk. # ponytail: static dict; promote to corpus-frequency-mined list
# if T0 exact-match rate falls short of the ~90% TECH §5.3 target.
ABBREV: dict[str, str] = {
    "ham-d": "hamilton depression rating scale",
    "hamd": "hamilton depression rating scale",
    "madrs": "montgomery asberg depression rating scale",
    "phq-9": "patient health questionnaire 9",
    "phq9": "patient health questionnaire 9",
    "os": "overall survival",
    "pfs": "progression free survival",
    "orr": "objective response rate",
    "dfs": "disease free survival",
    "efs": "event free survival",
    "ttp": "time to progression",
    "6mwd": "six minute walk distance",
    "6mwt": "six minute walk test",
    "hba1c": "glycated hemoglobin a1c",
    "fev1": "forced expiratory volume in one second",
    "bmi": "body mass index",
    "dbp": "diastolic blood pressure",
    "sbp": "systolic blood pressure",
    "vas": "visual analog scale",
    "sae": "serious adverse event",
    "ae": "adverse event",
    "cgi": "clinical global impression",
    "ymrs": "young mania rating scale",
    "panss": "positive and negative syndrome scale",
    "auc": "area under the curve",
    "cmax": "maximum plasma concentration",
    "dlt": "dose limiting toxicity",
    "mtd": "maximum tolerated dose",
    "egfr": "estimated glomerular filtration rate",
    "ldl": "low density lipoprotein",
    "hdl": "high density lipoprotein",
    "crp": "c reactive protein",
    "nyha": "new york heart association",
    "acr20": "american college of rheumatology 20 percent response",
    "pasi": "psoriasis area and severity index",
    "edss": "expanded disability status scale",
    "mmse": "mini mental state examination",
    "adas-cog": "alzheimers disease assessment scale cognitive subscale",
    "adascog": "alzheimers disease assessment scale cognitive subscale",
    "updrs": "unified parkinsons disease rating scale",
    "qol": "quality of life",
    "sf-36": "short form 36 health survey",
    "sf36": "short form 36 health survey",
    "eq-5d": "euroqol five dimension questionnaire",
    "eq5d": "euroqol five dimension questionnaire",
    "hr": "heart rate",
    "rr": "respiratory rate",
}

_ABBREV_RE = [(re.compile(rf"\b{re.escape(k)}\b"), v) for k, v in ABBREV.items()]
_PUNCT_RE = re.compile(r"[^\w\s]")
_WS_RE = re.compile(r"\s+")

_TIMEPOINT_RE = re.compile(
    r"\s*(?:at|after|through|during)\s+"
    r"(?:(?:day|week|month|year)s?\s*\d+|\d+\s*(?:day|week|month|year)s?)\s*$",
    re.IGNORECASE,
)


def norm(s: str) -> str:
    """Lowercase, expand curated abbreviations, strip punctuation, collapse whitespace.
    Two measure strings describing the same thing should come out norm()-equal."""
    s = s.lower()
    for pattern, expansion in _ABBREV_RE:
        s = pattern.sub(expansion, s)
    s = _PUNCT_RE.sub(" ", s)
    return _WS_RE.sub(" ", s).strip()


def split_timepoint(measure: str) -> tuple[str, str | None]:
    """Split a trailing timepoint phrase off a measure string, e.g.
    'Overall survival at 24 months' -> ('Overall survival', 'at 24 months').
    Returns (measure, None) when no trailing timepoint is present."""
    m = _TIMEPOINT_RE.search(measure)
    if not m:
        return measure, None
    return measure[: m.start()].rstrip(), measure[m.start() :].strip()


def fill_measure_norm(conn) -> int:
    """Backfill outcomes.measure_norm for every row in the corpus. Idempotent:
    always recomputes from the current `measure` text, safe to re-run after
    ingest adds more rows. Returns the number of rows updated."""
    rows = conn.execute("SELECT nct_id, version_no, outcome_type, ordinal, measure FROM outcomes").fetchall()
    for r in rows:
        conn.execute(
            "UPDATE outcomes SET measure_norm=? WHERE nct_id=? AND version_no=? AND outcome_type=? AND ordinal=?",
            (norm(r["measure"]), r["nct_id"], r["version_no"], r["outcome_type"], r["ordinal"]),
        )
    conn.commit()
    return len(rows)


if __name__ == "__main__":
    from ctcm import db

    c = db.connect()
    n = fill_measure_norm(c)
    print(f"measure_norm filled for {n} outcome rows")
    c.close()
