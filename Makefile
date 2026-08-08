.PHONY: venv ingest test show-history

venv:
	uv venv
	uv pip install -e .

ingest:
	.venv/bin/python scripts/run_ingest.py --limit $${LIMIT:-800}

test:
	.venv/bin/python -m pytest -q

show-history:
	.venv/bin/python scripts/show_history.py $(NCT)
