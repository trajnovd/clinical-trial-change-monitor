"""v0.3 semantic matching cascade (TECH-PRD §5). Decides, for a candidate (before,
after) outcome pair, whether they describe the same measured thing and how -- the
`matcher` that classify.py's diff_pair() consumes (see its module docstring for the
contract) is built from this cascade's output rather than from t0_matcher.

Four tiers, cheapest first, each only seeing what the previous one couldn't decide:

  T0 -- measure_norm byte equality. Free, instant, ~90% of pairs (TECH §5.3).
  T1 -- token-set Jaccard on measure_norm (abbreviation-expanded by normalize.norm()
        already), stopword-filtered so connective/boilerplate words ("in", "at",
        "change", "improvement" -- clinical outcome strings are full of these and they
        drown out the content words that actually identify the construct) don't dilute
        the score. >=0.9 -> REWORDED, <=0.35 -> DIFFERENT, else escalate.
        # ponytail: lexical Jaccard stands in for PubMedBERT embeddings; swap in
        # sentence-transformers if T1 accuracy caps out.
  T2 -- deterministic rule discriminator. Runs on the escalated band AND on T1's
        high-similarity band before a SAME/REWORDED is accepted, because high lexical
        similarity is exactly where the dangerous cases hide (TECH §5.1: "all-cause
        mortality" vs "cardiovascular mortality" is near-identical text, opposite
        meaning). Two rules: (a) same stem, different trailing timepoint ->
        TIMEPOINT_CHANGED; (b) qualifier-narrowing lexicon on an identical head noun
        (all-cause -> cardiovascular/cancer/... or the reverse) -> NARROWED/BROADENED.
  T3 -- LLM adjudication (`claude -p`, haiku) for the residual ambiguous pairs T0-T2
        could not decide. Injected as a callable so tests never touch the network or
        the CLI; ANY failure (exception, timeout, malformed JSON) falls back to the
        conservative DIFFERENT/T3_FALLBACK rather than guessing. When no T3 callable
        is supplied (or its budget is exhausted), the same pairs resolve to
        DIFFERENT/T2_UNRESOLVED -- an honest "we didn't look" rather than a fabricated
        answer either way.
        # ponytail: claude CLI as T3; fine-tuned cross-encoder is the scale upgrade.

match_outcomes(before, after) computes the full candidate matrix once, greedy-best-first
assigns the highest-scoring candidates first (so a strong match elsewhere can't be
stolen by a weaker one considered earlier in list order), and returns every matched
pair plus tier_counts for pipeline-wide instrumentation (TECH §8.2: "report cascade
tier attribution").
"""

from __future__ import annotations

import hashlib
import json
import re
import subprocess
from dataclasses import dataclass

from ctcm.classify import OutcomeRow
from ctcm.normalize import norm, split_timepoint

MATCH_RELATIONS = frozenset({"SAME", "REWORDED", "NARROWED", "BROADENED", "TIMEPOINT_CHANGED"})
ALL_RELATIONS = MATCH_RELATIONS | {"DIFFERENT"}

T1_HIGH = 0.9
T1_LOW = 0.35

DEFAULT_MODEL = "claude-haiku-4-5-20251001"


@dataclass(frozen=True)
class Pair:
    b_idx: int | None
    a_idx: int | None
    relation: str
    tier: str
    score: float


@dataclass(frozen=True)
class MatchResult:
    pairs: list[Pair]
    tier_counts: dict[str, int]


# ---- T1: token-set Jaccard -----------------------------------------------------------

# Generic English stopwords plus clinical-outcome-measure boilerplate: phrasing like
# "Change in X from baseline" / "X improvement" / "X assessment" describes the same
# construct in different sponsor house-styles and would otherwise dominate the token
# set over the words that actually identify what's being measured.
_STOPWORDS = frozenset(
    {
        "a", "an", "the", "in", "on", "of", "for", "to", "at", "from", "with", "and", "or", "by",
        "change", "baseline", "improvement", "assessment",
    }
)


def _tokens(measure_norm: str) -> set[str]:
    out = set()
    for w in measure_norm.split():
        if w in _STOPWORDS:
            continue
        out.add(w[:-1] if w.endswith("s") and len(w) > 3 else w)  # crude singularise
    return out


def _jaccard(before_norm: str, after_norm: str) -> float:
    tb, ta = _tokens(before_norm), _tokens(after_norm)
    if not tb or not ta:
        return 0.0
    return len(tb & ta) / len(tb | ta)


# ---- T2: deterministic rule discriminator ---------------------------------------------

_ALL_CAUSE_TOKENS = frozenset({"all", "cause"})
_NARROWING_QUALIFIERS = frozenset(
    {
        "cardiovascular", "cardiac", "cancer", "oncologic", "respiratory",
        "cerebrovascular", "renal", "hepatic", "infectious", "infection", "sepsis",
    }
)


def _qualifier_relation(before_stem_norm: str, after_stem_norm: str) -> str | None:
    tb, ta = before_stem_norm.split(), after_stem_norm.split()
    if not tb or not ta or tb[-1] != ta[-1]:
        return None  # no shared head noun -> not a qualifier-narrowing case at all
    head = tb[-1]
    qualifiers_b, qualifiers_a = set(tb[:-1]), set(ta[:-1])
    if qualifiers_b == _ALL_CAUSE_TOKENS and qualifiers_a & _NARROWING_QUALIFIERS:
        return "NARROWED"
    if qualifiers_a == _ALL_CAUSE_TOKENS and qualifiers_b & _NARROWING_QUALIFIERS:
        return "BROADENED"
    if qualifiers_b < qualifiers_a:  # after inserted extra qualifier word(s)
        return "NARROWED"
    if qualifiers_a < qualifiers_b:  # after dropped qualifier word(s) before had
        return "BROADENED"
    return None


def _t2_rules(before: OutcomeRow, after: OutcomeRow) -> str | None:
    stem_b, tp_b = split_timepoint(before.measure)
    stem_a, tp_a = split_timepoint(after.measure)
    stem_b_norm, stem_a_norm = norm(stem_b), norm(stem_a)

    if tp_b != tp_a and stem_b_norm == stem_a_norm:
        return "TIMEPOINT_CHANGED"

    return _qualifier_relation(stem_b_norm, stem_a_norm)


# ---- T3: LLM adjudication (injectable) -------------------------------------------------


class T3BudgetExceeded(Exception):
    """Raised by a T3 callable that tracks a call budget once it's exhausted. Treated
    the same as T3 being disabled (DIFFERENT/T2_UNRESOLVED) -- an honest "chose not to
    look", distinct from T3_FALLBACK's "looked and failed"."""


_FENCE_RE = re.compile(r"^```(?:json)?\s*|\s*```\s*$")


def _strip_fences(text: str) -> str:
    return _FENCE_RE.sub("", text.strip()).strip()


def _prompt(before: OutcomeRow, after: OutcomeRow) -> str:
    before_tf = f" (time frame: {before.time_frame})" if before.time_frame else ""
    after_tf = f" (time frame: {after.time_frame})" if after.time_frame else ""
    return (
        "Classify the relationship between two clinical trial outcome measure descriptions "
        "taken from consecutive registry versions of the same trial.\n\n"
        f'Before: "{before.measure}"{before_tf}\n'
        f'After: "{after.measure}"{after_tf}\n\n'
        "One of:\n"
        "SAME - identical measured construct, wording differs only trivially.\n"
        "REWORDED - same construct, meaningfully different wording, but equivalent.\n"
        "NARROWED - after measures a stricter/more specific subset of before "
        "(e.g. all-cause mortality -> cardiovascular mortality).\n"
        "BROADENED - the reverse of NARROWED.\n"
        "TIMEPOINT_CHANGED - same construct, the measurement timepoint changed.\n"
        "DIFFERENT - a genuinely different construct.\n\n"
        "Respond with strict JSON only, no markdown fences, no prose outside the JSON:\n"
        '{"relation": "<ONE_OF_THE_ABOVE>", "confidence": <0-1 float>, "reasoning": "<one sentence>"}'
    )


class T3Client:
    """Default T3 callable: `claude -p` (haiku), sqlite-cached by sha256(prompt), with
    an optional call budget (--t3-limit). Cache hits are free and never count against
    the budget -- that's what makes re-runs free (global-constraints.md)."""

    def __init__(self, conn, limit: int | None = None, model: str = DEFAULT_MODEL):
        self.conn = conn
        self.limit = limit
        self.calls_made = 0
        self.model = model

    def __call__(self, before: OutcomeRow, after: OutcomeRow) -> dict:
        prompt = _prompt(before, after)
        key = hashlib.sha256(prompt.encode()).hexdigest()

        row = self.conn.execute("SELECT response FROM llm_cache WHERE key=?", (key,)).fetchone()
        if row:
            return json.loads(row["response"])

        if self.limit is not None and self.calls_made >= self.limit:
            raise T3BudgetExceeded()
        self.calls_made += 1

        result = subprocess.run(
            ["claude", "-p", "--model", self.model],
            input=prompt,
            capture_output=True,
            text=True,
            timeout=60,
        )
        if result.returncode != 0:
            raise RuntimeError(f"claude -p exited {result.returncode}: {result.stderr[:200]}")
        parsed = json.loads(_strip_fences(result.stdout))
        if parsed.get("relation") not in ALL_RELATIONS:
            raise ValueError(f"unexpected relation in T3 response: {parsed.get('relation')!r}")

        self.conn.execute(
            "INSERT OR REPLACE INTO llm_cache(key, response) VALUES (?, ?)", (key, json.dumps(parsed))
        )
        self.conn.commit()
        return parsed


# ---- the cascade itself -----------------------------------------------------------------


def match_outcomes(before: list[OutcomeRow], after: list[OutcomeRow], t3=None) -> MatchResult:
    """Match every `before` outcome against every `after` outcome via the T0-T3
    cascade, greedy-best-first by T1 score. `t3(before_row, after_row) -> dict` is
    called only for pairs T0-T2 could not decide (LLM budget discipline); pass None to
    run with T3 disabled entirely (escalations resolve to DIFFERENT/T2_UNRESOLVED)."""
    used_b: set[int] = set()
    used_a: set[int] = set()
    tier_counts: dict[str, int] = {}
    pairs: list[Pair] = []
    final_tier: dict[tuple[int, int], str] = {}
    final_score: dict[tuple[int, int], float] = {}

    def bump(tier: str) -> None:
        tier_counts[tier] = tier_counts.get(tier, 0) + 1

    # Pass A: score every candidate (cheap: exact-match check or Jaccard only), then
    # walk highest-score-first, resolving T0/T1/T2 as each candidate is reached.
    scored = []
    for bi, b in enumerate(before):
        for ai, a in enumerate(after):
            exact = bool(b.measure_norm) and b.measure_norm == a.measure_norm
            score = 1.0 if exact else _jaccard(b.measure_norm, a.measure_norm)
            scored.append((score, bi, ai, exact))
    scored.sort(key=lambda c: (-c[0], c[1], c[2]))

    escalated: list[tuple[float, int, int]] = []
    for score, bi, ai, exact in scored:
        if bi in used_b or ai in used_a:
            continue
        b, a = before[bi], after[ai]
        if exact:
            pairs.append(Pair(bi, ai, "SAME", "T0", 1.0))
            used_b.add(bi)
            used_a.add(ai)
            bump("T0")
            continue

        t2_relation = _t2_rules(b, a)
        if score >= T1_HIGH:
            relation, tier = (t2_relation, "T2") if t2_relation else ("REWORDED", "T1")
            pairs.append(Pair(bi, ai, relation, tier, score))
            used_b.add(bi)
            used_a.add(ai)
            bump(tier)
        elif score <= T1_LOW:
            final_tier[(bi, ai)] = "T1"
            final_score[(bi, ai)] = score
        elif t2_relation:
            pairs.append(Pair(bi, ai, t2_relation, "T2", score))
            used_b.add(bi)
            used_a.add(ai)
            bump("T2")
        else:
            escalated.append((score, bi, ai))  # T0-T2 could not decide -- T3's job

    # Pass B: T3 sees only the residual escalated candidates, and only for rows still
    # unmatched by the time we get to them (best-first order).
    for score, bi, ai in escalated:
        if bi in used_b or ai in used_a:
            continue
        b, a = before[bi], after[ai]
        final_score[(bi, ai)] = score

        if t3 is None:
            final_tier[(bi, ai)] = "T2_UNRESOLVED"
            bump("T2_UNRESOLVED")
            continue
        try:
            result = t3(b, a)
        except T3BudgetExceeded:
            final_tier[(bi, ai)] = "T2_UNRESOLVED"
            bump("T2_UNRESOLVED")
            continue
        except Exception:
            final_tier[(bi, ai)] = "T3_FALLBACK"
            bump("T3_FALLBACK")
            continue

        relation = result.get("relation") if result else None
        if relation not in ALL_RELATIONS:
            final_tier[(bi, ai)] = "T3_FALLBACK"
            bump("T3_FALLBACK")
            continue

        bump("T3")
        if relation in MATCH_RELATIONS:
            conf = result.get("confidence", score)
            pairs.append(Pair(bi, ai, relation, "T3", float(conf) if conf is not None else score))
            used_b.add(bi)
            used_a.add(ai)
        else:
            final_tier[(bi, ai)] = "T3"

    # Pass C: whatever's left is a genuine leftover. An unambiguous single leftover on
    # each side is worth naming as an explicit DIFFERENT pair (mirrors diff_pair's own
    # "only collapse the unambiguous 1:1 case" philosophy); anything else stays
    # independent rather than inventing a specific cross-pairing.
    unmatched_b = [bi for bi in range(len(before)) if bi not in used_b]
    unmatched_a = [ai for ai in range(len(after)) if ai not in used_a]
    if len(unmatched_b) == 1 and len(unmatched_a) == 1:
        bi, ai = unmatched_b[0], unmatched_a[0]
        tier = final_tier.get((bi, ai), "T1")
        score = final_score.get((bi, ai), 0.0)
        pairs.append(Pair(bi, ai, "DIFFERENT", tier, score))
    else:
        for bi in unmatched_b:
            pairs.append(Pair(bi, None, "DIFFERENT", "T1", 0.0))
        for ai in unmatched_a:
            pairs.append(Pair(None, ai, "DIFFERENT", "T1", 0.0))

    return MatchResult(pairs, tier_counts)
