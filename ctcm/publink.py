"""v2 Task 13: publication linking. Links each trial's NCT ID to papers that report
it, via three query forms verified live in v2-research.md (2026-08-09) -- used here
exactly as verified, not the doc's untested "recommended" combos:

  - PubMed eutils esearch `term={nct}[si]` -- publisher-asserted databank/secondary-
    source-ID linkage (ICMJE trial-registration metadata). Curated, not text-mined.
    HIGH tier.
  - Europe PMC `ABSTRACT:"{nct}"` -- the NCT ID appears in the paper's own abstract.
    A paper rarely cites another trial's ID in its abstract, so this is a reasonable
    "this paper is substantially about the trial" signal. MEDIUM tier.
  - Europe PMC bare `"{nct}"` free-text -- catches every indexed mention (comparator
    citations, reviews, reference lists), 10-40x noisier than the above per
    v2-research.md. LOW tier -- never present as "the trial's paper," only as
    "mentions this trial."

We do not parse paper content (PRD explicitly defers that) -- only link + tier +
evidence (which query surfaced it).

Table `publications(nct_id, pmid, doi, title, journal, pub_date, oa, tier, source)`,
PRIMARY KEY(nct_id, pmid) -- created here (CREATE TABLE IF NOT EXISTS), not in
db.py (frozen at v1.0). Raw API responses cache at data/cache/publink/{nct}.json,
one file per trial (resumable at trial granularity: file present -> no network at
all for that trial, matching the brief's cache path exactly). Bounded concurrency
(<=4) with exponential backoff on 429/5xx, same shape as ctcm.ingest._get_json.
"""

import argparse
import asyncio
import json
import logging
from collections import Counter
from datetime import date
from pathlib import Path

import httpx

from ctcm import config

logger = logging.getLogger(__name__)

MAX_CONCURRENCY = 4
MAX_RETRIES = 5
LOW_TIER_PAGE_SIZE = 100  # ponytail: single-page cap on the noisy free-text query,
# not full recall (ACTT-1 alone hits 371) -- upgrade path: paginate if a downstream
# consumer needs every low-tier mention rather than the top-ranked ones.

EUTILS_URL = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/esearch.fcgi"
EPMC_URL = "https://www.ebi.ac.uk/europepmc/webservices/rest/search"

TIER_HIGH = "HIGH"
TIER_MEDIUM = "MEDIUM"
TIER_LOW = "LOW"

PUBLICATIONS_SCHEMA = """
CREATE TABLE IF NOT EXISTS publications(
  nct_id TEXT, pmid TEXT, doi TEXT, title TEXT, journal TEXT, pub_date TEXT,
  oa INT, tier TEXT, source TEXT, PRIMARY KEY(nct_id, pmid)
);
"""


def ensure_schema(conn) -> None:
    """Idempotent, like adjudicate.ensure_schema -- safe to call on every entry point."""
    conn.executescript(PUBLICATIONS_SCHEMA)
    conn.commit()


# ---- tier assignment (pure, no network) --------------------------------------------


def _normalize_record(rec: dict) -> dict:
    """One Europe PMC search-result item -> our storage shape. EPMC's own metadata
    (title/journalTitle/firstPublicationDate/doi/isOpenAccess) is already complete
    per-hit -- no separate metadata lookup call needed (verified in v2-research.md's
    sample response). pub_date is left None rather than backfilled from the coarser
    pubYear -- a fabricated day-precision date would silently corrupt the
    days-after-change arithmetic below."""
    oa_raw = rec.get("isOpenAccess")
    oa = 1 if oa_raw == "Y" else (0 if oa_raw == "N" else None)
    return {
        "pmid": rec.get("pmid"),
        "doi": rec.get("doi"),
        "title": rec.get("title"),
        "journal": rec.get("journalTitle"),
        "pub_date": rec.get("firstPublicationDate"),
        "oa": oa,
    }


def assign_tiers(si_pmids: set[str], abstract_records: list[dict], fulltext_records: list[dict]) -> list[dict]:
    """Merges the three query results into one row per pmid, highest tier wins.
    Records with no pmid are dropped -- the schema's PK needs one (ponytail: rare,
    preprints/non-MEDLINE hits; upgrade path: key by (source, id) if that population
    starts to matter). A pmid that's [si]-linked but never showed up in either EPMC
    query still gets a HIGH-tier stub row (pmid only, metadata None) -- the linkage
    itself is the evidence, metadata is best-effort."""
    by_pmid: dict[str, dict] = {}

    for rec in fulltext_records:
        norm = _normalize_record(rec)
        if not norm["pmid"]:
            continue
        by_pmid[norm["pmid"]] = {**norm, "tier": TIER_LOW, "source": "epmc_fulltext"}

    for rec in abstract_records:
        norm = _normalize_record(rec)
        if not norm["pmid"]:
            continue
        by_pmid[norm["pmid"]] = {**norm, "tier": TIER_MEDIUM, "source": "epmc_abstract"}

    for pmid in si_pmids:
        if pmid in by_pmid:
            row = by_pmid[pmid]
            row["tier"] = TIER_HIGH
            row["source"] = row["source"] + "+pubmed_si"
        else:
            by_pmid[pmid] = {
                "pmid": pmid, "doi": None, "title": None, "journal": None, "pub_date": None, "oa": None,
                "tier": TIER_HIGH, "source": "pubmed_si",
            }

    return list(by_pmid.values())


def records_for_trial(raw: dict) -> list[dict]:
    """raw: {"si": <eutils esearch json>, "abstract": <epmc search json>, "fulltext": <epmc search json>}."""
    si_pmids = set(((raw.get("si") or {}).get("esearchresult") or {}).get("idlist") or [])
    abstract_records = ((raw.get("abstract") or {}).get("resultList") or {}).get("result") or []
    fulltext_records = ((raw.get("fulltext") or {}).get("resultList") or {}).get("result") or []
    return assign_tiers(si_pmids, abstract_records, fulltext_records)


def days_after_change(pub_date: str | None, change_date: str | None) -> int | None:
    """Whole days between a publication's pub_date and a SIGNAL finding's change date
    (the to_version's version_date) -- None if either date is missing, or if the
    publication does not postdate the change (the UI only ever shows the "published
    N days after" framing, never a "before" one)."""
    if not pub_date or not change_date:
        return None
    pub = date.fromisoformat(pub_date[:10])
    change = date.fromisoformat(change_date[:10])
    delta = (pub - change).days
    return delta if delta > 0 else None


# ---- persistence ---------------------------------------------------------------------


def upsert_publications(conn, nct_id: str, records: list[dict]) -> None:
    ensure_schema(conn)
    conn.executemany(
        "INSERT INTO publications(nct_id, pmid, doi, title, journal, pub_date, oa, tier, source) "
        "VALUES (?,?,?,?,?,?,?,?,?) "
        "ON CONFLICT(nct_id, pmid) DO UPDATE SET doi=excluded.doi, title=excluded.title, journal=excluded.journal, "
        "pub_date=excluded.pub_date, oa=excluded.oa, tier=excluded.tier, source=excluded.source",
        [
            (nct_id, r["pmid"], r["doi"], r["title"], r["journal"], r["pub_date"], r["oa"], r["tier"], r["source"])
            for r in records
        ],
    )
    conn.commit()


# ---- fetch + cache ---------------------------------------------------------------------


async def _get_json(client: httpx.AsyncClient, url: str, params: dict) -> dict:
    """GET url as JSON with exponential backoff on 429/5xx, up to MAX_RETRIES attempts
    -- same shape as ctcm.ingest._get_json, duplicated rather than imported: that
    function is bound to ingest.py's single-base-url client, this one takes full URLs
    across two different hosts (eutils + EPMC)."""
    delay = 1.0
    for attempt in range(MAX_RETRIES):
        resp = await client.get(url, params=params)
        if resp.status_code == 429 or resp.status_code >= 500:
            if attempt == MAX_RETRIES - 1:
                resp.raise_for_status()
            await asyncio.sleep(delay)
            delay *= 2
            continue
        resp.raise_for_status()
        return resp.json()
    raise RuntimeError(f"unreachable: retries exhausted for {url}")  # pragma: no cover


def _cache_path(nct: str) -> Path:
    return config.CACHE_DIR / "publink" / f"{nct}.json"


async def _fetch_raw(client: httpx.AsyncClient, nct: str) -> dict:
    si = await _get_json(client, EUTILS_URL, {"db": "pubmed", "term": f"{nct}[si]", "retmode": "json"})
    abstract = await _get_json(client, EPMC_URL, {"query": f'ABSTRACT:"{nct}"', "format": "json", "pageSize": 100})
    fulltext = await _get_json(client, EPMC_URL, {"query": f'"{nct}"', "format": "json", "pageSize": LOW_TIER_PAGE_SIZE})
    return {"si": si, "abstract": abstract, "fulltext": fulltext}


async def fetch_raw_cached(client: httpx.AsyncClient, nct: str) -> dict:
    """Cache hit -> no network at all for this trial (resumable, per global-constraints.md)."""
    path = _cache_path(nct)
    if path.exists():
        return json.loads(path.read_text())
    raw = await _fetch_raw(client, nct)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(raw))
    return raw


# ---- trial selection (SIGNAL + adjudicated trials first) ---------------------------------


def _table_exists(conn, name: str) -> bool:
    return conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)).fetchone() is not None


def select_nct_ids(conn, limit: int) -> list[str]:
    """Priority order: (0) trials with a SIGNAL finding that's been adjudicated
    (non-UNREVIEWED) -- the case-study population, gets links first; (1) other SIGNAL
    trials; (2) everything else, alphabetically for determinism. Computed in Python
    against ctcm.adjudicate's content_hash() rather than registering it as a SQL
    function -- same join key as ctcm/api.py, no schema coupling either way."""
    from ctcm.adjudicate import content_hash

    trial_ids = [r[0] for r in conn.execute("SELECT nct_id FROM trials ORDER BY nct_id")]
    signal_findings = conn.execute(
        "SELECT nct_id, from_version, to_version, change_type, before_measure, after_measure "
        "FROM findings WHERE severity='SIGNAL'"
    ).fetchall()

    adjudicated_hashes: set[str] = set()
    if _table_exists(conn, "adjudications"):
        adjudicated_hashes = {
            r[0] for r in conn.execute("SELECT content_hash FROM adjudications WHERE severity_confirmed != 'UNREVIEWED'")
        }

    signal_ncts: set[str] = set()
    adjudicated_ncts: set[str] = set()
    for f in signal_findings:
        signal_ncts.add(f["nct_id"])
        chash = content_hash(
            f["nct_id"], f["from_version"], f["to_version"], f["change_type"], f["before_measure"], f["after_measure"]
        )
        if chash in adjudicated_hashes:
            adjudicated_ncts.add(f["nct_id"])

    def priority(nct: str) -> tuple[int, str]:
        if nct in adjudicated_ncts:
            return (0, nct)
        if nct in signal_ncts:
            return (1, nct)
        return (2, nct)

    return sorted(trial_ids, key=priority)[:limit]


# ---- orchestration ---------------------------------------------------------------------


async def link_all(nct_ids: list[str], conn) -> Counter:
    """Fetch+cache+tier+upsert publications for each trial, bounded concurrency,
    one trial's failure never aborts the batch (same shape as ctcm.ingest.ingest).
    Safe to share one sqlite conn across concurrent tasks: asyncio is single-threaded,
    so upsert_publications' execute+commit (no internal await) never interleaves."""
    sem = asyncio.Semaphore(MAX_CONCURRENCY)
    tier_totals: Counter[str] = Counter()
    done = 0
    lock = asyncio.Lock()
    total = len(nct_ids)

    async def _one(client: httpx.AsyncClient, nct: str) -> None:
        nonlocal done
        async with sem:
            try:
                raw = await fetch_raw_cached(client, nct)
                records = records_for_trial(raw)
                upsert_publications(conn, nct, records)
                tier_totals.update(r["tier"] for r in records)
            except Exception:
                logger.exception("failed to link publications for %s", nct)
        async with lock:
            done += 1
            if done % 10 == 0 or done == total:
                print(f"publink progress: {done}/{total}", flush=True)

    async with httpx.AsyncClient(timeout=30) as client:
        await asyncio.gather(*(_one(client, nct) for nct in nct_ids))

    return tier_totals


async def _main_async(limit: int, nct: str | None = None) -> None:
    from ctcm import db

    conn = db.connect()
    nct_ids = [nct] if nct else select_nct_ids(conn, limit)
    print(f"linking publications for {len(nct_ids)} trials", flush=True)
    tier_totals = await link_all(nct_ids, conn)
    conn.close()

    decided = sum(tier_totals.values())
    print(f"done: {decided} publication link(s)")
    for tier, n in sorted(tier_totals.items(), key=lambda kv: -kv[1]):
        print(f"  {tier}: {n}")


def main() -> None:
    parser = argparse.ArgumentParser(description="Link trials to publications via PubMed [si] + Europe PMC")
    parser.add_argument("--limit", type=int, default=50)
    parser.add_argument("--nct", default=None, help="force-link one trial regardless of priority ordering")
    args = parser.parse_args()
    asyncio.run(_main_async(args.limit, args.nct))


if __name__ == "__main__":
    main()
