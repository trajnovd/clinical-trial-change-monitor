"""v0.4 Task 8: multi-agent adjudication. Three sequential `claude -p` calls per
finding -- defence, prosecution, judge (judge sees both) -- turn a T0 SIGNAL
finding into a concern verdict with a rationale and both sides' arguments.

Verdicts cache in their own `adjudications` table (created here, findings.py's
schema in db.py is untouched) keyed by a content hash of (nct_id, from_version,
to_version, change_type, before_measure, after_measure) rather than finding_id --
finding_id is a delete+reinsert PK the pipeline can renumber on any re-run
(pipeline.py does DELETE+INSERT per trial), so keying on it would silently
orphan every adjudication on the next pipeline run. Content hash survives that.

Every prompt+response is logged verbatim to data/adjudication_log.jsonl for audit,
regardless of success/failure -- that log is an internal debug trail, not the
product-facing output the "never say fraud/misconduct" constraint targets, so it
keeps the model's raw text. What gets persisted into the adjudications table (and
returned to callers) is post-filtered instead: see _defuse().
"""

import hashlib
import json
import re
import subprocess
from dataclasses import dataclass
from datetime import datetime, timezone

from ctcm import config

CLAUDE_MODEL = "claude-haiku-4-5-20251001"
LLM_TIMEOUT_S = 90
_CONCERN_LEVELS = {"LOW", "MODERATE", "HIGH"}

ADJUDICATIONS_SCHEMA = """
CREATE TABLE IF NOT EXISTS adjudications(
  content_hash TEXT PRIMARY KEY, finding_id INT, nct_id TEXT,
  severity_confirmed TEXT, confidence REAL, rationale TEXT,
  defence TEXT, prosecution TEXT, model TEXT, created_at TEXT
);
"""

_BANNED_RE = re.compile(r"\bfraud(?:ulent(?:ly)?)?\b|\bmisconduct\b", re.IGNORECASE)
_FENCE_RE = re.compile(r"^```(?:json)?\s*\n?(.*?)\n?```\s*$", re.DOTALL)


@dataclass(frozen=True)
class Adjudication:
    severity_confirmed: str  # "LOW" / "MODERATE" / "HIGH" / "UNREVIEWED" (on any failure)
    confidence: float
    rationale: str
    defence: str
    prosecution: str


def content_hash(nct_id, from_version, to_version, change_type, before_measure, after_measure) -> str:
    raw = "|".join(str(x) for x in (nct_id, from_version, to_version, change_type, before_measure or "", after_measure or ""))
    return hashlib.sha256(raw.encode()).hexdigest()


def ensure_schema(conn) -> None:
    """Idempotent: safe to call on every entry point, like db.connect(). Also
    registers content_hash as a SQL function so callers can join/filter
    findings against adjudications by hash in a single query (see
    scripts/run_adjudicate.py)."""
    conn.executescript(ADJUDICATIONS_SCHEMA)
    conn.create_function("content_hash", 6, content_hash)
    conn.commit()


# ---- evidence assembly -------------------------------------------------------------


def _outcomes_block(conn, nct: str, version_no: int) -> str:
    rows = conn.execute(
        "SELECT outcome_type, measure, time_frame FROM outcomes WHERE nct_id=? AND version_no=? ORDER BY outcome_type, ordinal",
        (nct, version_no),
    ).fetchall()
    if not rows:
        return "  (no outcomes recorded)"
    return "\n".join(
        f"  - [{r['outcome_type']}] {r['measure']}" + (f" (time frame: {r['time_frame']})" if r["time_frame"] else "")
        for r in rows
    )


def _timeline_block(conn, nct: str, version_no: int) -> str:
    r = conn.execute("SELECT * FROM timeline_facts WHERE nct_id=? AND version_no=?", (nct, version_no)).fetchone()
    if not r:
        return "  (no timeline facts recorded)"
    return (
        f"  start_date={r['start_date']} ({r['start_date_type']}), "
        f"primary_completion_date={r['primary_completion_date']} ({r['primary_completion_type']}), "
        f"completion_date={r['completion_date']}"
    )


def assemble_evidence(finding_row, conn) -> str:
    """Evidence package for one finding: both versions' outcomes, both versions'
    timeline facts, sponsor/phase, and the finding row itself. Same text goes into
    all three prompts (defence/prosecution/judge) so every party argues from the
    same facts."""
    nct = finding_row["nct_id"]
    vfrom, vto = finding_row["from_version"], finding_row["to_version"]
    trial = conn.execute("SELECT lead_sponsor, sponsor_class, phase, overall_status FROM trials WHERE nct_id=?", (nct,)).fetchone()
    sponsor_line = (
        f"Sponsor: {trial['lead_sponsor']} ({trial['sponsor_class']}), Phase: {trial['phase']}, Status: {trial['overall_status']}"
        if trial
        else "Sponsor/phase: unknown (no trial row)"
    )

    return (
        f"Trial: {nct}\n"
        f"{sponsor_line}\n\n"
        f"Finding: {finding_row['change_type']} (v{vfrom} -> v{vto})\n"
        f"Before measure: {finding_row['before_measure'] or '(none)'}\n"
        f"After measure: {finding_row['after_measure'] or '(none)'}\n"
        f"Days after enrolment: {finding_row['days_after_enrolment']}\n"
        f"Days after primary completion: {finding_row['days_after_primary_completion']}\n"
        f"Detector rationale: {finding_row['rationale']}\n\n"
        f"Outcomes as of v{vfrom}:\n{_outcomes_block(conn, nct, vfrom)}\n\n"
        f"Outcomes as of v{vto}:\n{_outcomes_block(conn, nct, vto)}\n\n"
        f"Timeline as of v{vfrom}:\n{_timeline_block(conn, nct, vfrom)}\n"
        f"Timeline as of v{vto}:\n{_timeline_block(conn, nct, vto)}\n"
    )


# ---- prompts ------------------------------------------------------------------------
# None of these ever name "fraud" or "misconduct" -- the global constraint bans them
# from every product output, and inviting the word in the prompt is the easiest way
# to get it back in the response.


def _defence_prompt(evidence: str) -> str:
    return (
        "You are DEFENCE counsel reviewing a change to a clinical trial's registered outcome measures. "
        "Argue that the change is legitimate, using only the evidence below. Plausible legitimate reasons include "
        "a recruitment shortfall forcing a redesign, a documented regulatory instruction, or a pre-data "
        "clarification of ambiguous wording. Cite the actual dates and version numbers given. Do not speculate "
        "beyond the evidence. Write 3-5 sentences of plain prose, no JSON, no markdown fences.\n\n"
        f"EVIDENCE:\n{evidence}"
    )


def _prosecution_prompt(evidence: str, defence_text: str) -> str:
    return (
        "You are PROSECUTION counsel reviewing the same clinical trial change. Argue that the change is "
        "concerning, using only the evidence below. Relevant concerns include post-hoc timing (the change lands "
        "close to or after primary completion), a change made after results would plausibly have been available, "
        "or a direction of change that would make results look more favourable. Cite the actual dates and version "
        "numbers given. You have the defence's argument below -- rebut it if it doesn't hold up against the "
        "evidence. Do not speculate beyond the evidence. Write 3-5 sentences of plain prose, no JSON, no markdown "
        "fences.\n\n"
        f"EVIDENCE:\n{evidence}\n\nDEFENCE ARGUMENT:\n{defence_text}"
    )


def _judge_prompt(evidence: str, defence_text: str, prosecution_text: str) -> str:
    return (
        "You are an impartial judge weighing a defence and a prosecution argument about a change to a clinical "
        "trial's registered outcome measures. Decide how concerning the change is, using only the evidence and "
        "the two arguments below.\n\n"
        f"EVIDENCE:\n{evidence}\n\nDEFENCE ARGUMENT:\n{defence_text}\n\nPROSECUTION ARGUMENT:\n{prosecution_text}\n\n"
        "Respond with strict JSON only, no other text, matching exactly this shape:\n"
        '{"concern": "low"|"moderate"|"high", "confidence": <number 0-1>, "rationale": "2-4 sentences citing the actual dates"}'
    )


# ---- response cleaning ---------------------------------------------------------------


def _strip_fences(text: str) -> str:
    text = text.strip()
    m = _FENCE_RE.match(text)
    return m.group(1).strip() if m else text


def _defuse(text: str) -> tuple[str, bool]:
    """Belt-and-braces post-filter: the prompts never invite 'fraud'/'misconduct',
    but if the model emits them anyway, they must never reach a persisted output
    (global-constraints.md). Returns (cleaned_text, was_filtered)."""
    cleaned = _BANNED_RE.sub("concerning", text)
    return cleaned, cleaned != text


def _clean_response(raw: str) -> tuple[str, bool]:
    return _defuse(_strip_fences(raw))


# ---- LLM call + logging --------------------------------------------------------------


def _default_llm(prompt: str) -> str:
    result = subprocess.run(
        ["claude", "-p", "--model", CLAUDE_MODEL],
        input=prompt,
        capture_output=True,
        text=True,
        timeout=LLM_TIMEOUT_S,
    )
    if result.returncode != 0:
        raise RuntimeError(f"claude -p exited {result.returncode}: {result.stderr.strip()[:500]}")
    return result.stdout


def _log_entry(chash: str, nct_id: str, role: str, prompt: str, raw_response: str, error: str | None, filtered: bool) -> None:
    config.DATA_DIR.mkdir(parents=True, exist_ok=True)
    entry = {
        "ts": datetime.now(timezone.utc).isoformat(),
        "content_hash": chash,
        "nct_id": nct_id,
        "role": role,
        "prompt": prompt,
        "response": raw_response,
        "error": error,
        "filtered": filtered,
    }
    with open(config.DATA_DIR / "adjudication_log.jsonl", "a") as f:
        f.write(json.dumps(entry) + "\n")


def _call_and_log(llm_fn, role: str, chash: str, nct_id: str, prompt: str) -> str:
    error = None
    raw = ""
    try:
        raw = llm_fn(prompt)
    except Exception as e:  # subprocess timeout, non-zero exit, or a fake llm raising in tests
        error = str(e)
    cleaned, filtered = ("", False) if error else _clean_response(raw)
    _log_entry(chash, nct_id, role, prompt, raw, error, filtered)
    if error:
        raise RuntimeError(f"{role} call failed: {error}")
    return cleaned


# ---- top-level interface --------------------------------------------------------------


def _load_cached(conn, chash: str) -> Adjudication | None:
    row = conn.execute(
        "SELECT severity_confirmed, confidence, rationale, defence, prosecution FROM adjudications WHERE content_hash=?",
        (chash,),
    ).fetchone()
    if row is None:
        return None
    return Adjudication(row["severity_confirmed"], row["confidence"], row["rationale"], row["defence"], row["prosecution"])


def _save(conn, chash: str, finding_row, adj: Adjudication) -> None:
    conn.execute(
        "INSERT INTO adjudications(content_hash, finding_id, nct_id, severity_confirmed, confidence, rationale, "
        "defence, prosecution, model, created_at) VALUES (?,?,?,?,?,?,?,?,?,?) "
        "ON CONFLICT(content_hash) DO UPDATE SET finding_id=excluded.finding_id, "
        "severity_confirmed=excluded.severity_confirmed, confidence=excluded.confidence, rationale=excluded.rationale, "
        "defence=excluded.defence, prosecution=excluded.prosecution, model=excluded.model, created_at=excluded.created_at",
        (
            chash, finding_row["finding_id"], finding_row["nct_id"], adj.severity_confirmed, adj.confidence,
            adj.rationale, adj.defence, adj.prosecution, CLAUDE_MODEL, datetime.now(timezone.utc).isoformat(),
        ),
    )
    conn.commit()


def adjudicate(finding_row, conn, llm=None) -> Adjudication:
    """Runs the defence -> prosecution -> judge chain for one finding, or returns
    the cached verdict if this exact (nct_id, from_version, to_version, change_type,
    before_measure, after_measure) has already been adjudicated. `llm` is an
    injectable `prompt: str -> response: str` callable (defaults to `claude -p`);
    tests pass a fake so no subprocess/network is touched.

    On any failure (LLM error, timeout, malformed JSON, unexpected concern value)
    returns severity_confirmed="UNREVIEWED" instead of raising -- the failing
    prompt+response is still on data/adjudication_log.jsonl for debugging."""
    ensure_schema(conn)
    chash = content_hash(
        finding_row["nct_id"], finding_row["from_version"], finding_row["to_version"],
        finding_row["change_type"], finding_row["before_measure"], finding_row["after_measure"],
    )
    cached = _load_cached(conn, chash)
    if cached is not None:
        return cached

    llm_fn = llm or _default_llm
    nct = finding_row["nct_id"]
    evidence = assemble_evidence(finding_row, conn)

    try:
        defence = _call_and_log(llm_fn, "defence", chash, nct, _defence_prompt(evidence))
        prosecution = _call_and_log(llm_fn, "prosecution", chash, nct, _prosecution_prompt(evidence, defence))
        judge_text = _call_and_log(llm_fn, "judge", chash, nct, _judge_prompt(evidence, defence, prosecution))

        verdict = json.loads(judge_text)
        concern = str(verdict["concern"]).strip().upper()
        if concern not in _CONCERN_LEVELS:
            raise ValueError(f"judge returned unrecognised concern: {verdict.get('concern')!r}")
        confidence = float(verdict["confidence"])
        rationale, _ = _defuse(str(verdict["rationale"]))

        adj = Adjudication(concern, confidence, rationale, defence, prosecution)
    except Exception as e:
        adj = Adjudication("UNREVIEWED", 0.0, f"adjudication failed: {e}", "", "")

    _save(conn, chash, finding_row, adj)
    return adj
