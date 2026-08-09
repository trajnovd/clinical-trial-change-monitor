.PHONY: venv ingest load pipeline headline adjudicate benchmark serve test all show-history

# All paths below are relative to this Makefile's own directory (make's default
# recipe cwd) and never `cd` into an absolute path -- the repo lives under a
# directory name with a space in it ("Clincal Trial Change Monitor"), and an
# unquoted absolute `cd` there breaks. Relative paths sidestep the problem
# entirely: run `make` from the repo root.
PY := .venv/bin/python

# T3 LLM escalation budget for `pipeline`/`all`. Defaults to 0 so both stay
# network-free and byte-for-byte reproducible from whatever's already cached
# in llm_cache: ctcm.match.T3Client checks the cache before checking the
# budget, so a limit of 0 only blocks a genuinely new `claude -p` call, never
# a previously-cached tier decision (see docs/methodology.md). For live
# escalation of the residual T2_UNRESOLVED pairs, override explicitly:
#   make pipeline T3_LIMIT=200
T3_LIMIT ?= 0

venv:
	uv venv
	uv pip install -e .

# Fetches registry version history into data/cache/ (network; resumable, skips
# what's already cached). LIMIT overrides the discovery cap (default 800).
ingest:
	$(PY) scripts/run_ingest.py --limit $${LIMIT:-800}

# Walks data/cache/ into data/ctcm.db (trials/versions/outcomes/timeline_facts).
# Local, deterministic, idempotent -- no network.
load:
	$(PY) -m ctcm.extract

# Runs the T0-T3 diff/classify cascade over the loaded corpus and writes
# findings. Network-free at the default T3_LIMIT=0 (see above).
pipeline:
	$(PY) scripts/run_pipeline.py --t3-limit $(T3_LIMIT)

# Prints the headline count + change_type/severity/sponsor_class breakdown.
headline:
	$(PY) scripts/headline.py

# Multi-agent (defence/prosecution/judge) adjudication of SIGNAL findings via
# `claude -p` -- hits the LLM/CLI every uncached call. Not part of `all`.
adjudicate:
	$(PY) scripts/run_adjudicate.py --limit 25

# Scores the pipeline's findings against the Holst et al. 2023 dev split.
# Runs load_corpus()+run_pipeline() itself (see benchmark/README.md); the
# default T3 budget there can hit the LLM/CLI. Not part of `all`.
benchmark:
	$(PY) benchmark/evaluate.py --split dev

# Serves the read-only API + UI (ui/index.html) at http://127.0.0.1:8742/.
serve:
	.venv/bin/uvicorn ctcm.api:app --port 8742

test:
	$(PY) -m pytest -q

show-history:
	$(PY) scripts/show_history.py $(NCT)

# Fresh-clone reproducibility check (docs/methodology.md "Reproducing the
# headline number"): from an already-ingested cache, this reproduces the
# headline number with no network access. Does NOT include adjudicate/
# benchmark -- both call the LLM/CLI and are run separately, on purpose.
all: load pipeline headline
