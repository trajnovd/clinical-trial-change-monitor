#!/usr/bin/env python3
"""Bake the live API into a static bundle for dumb-file hosting.

Writes build/static-site/: a copy of ui/index.html with window.REDLINE_STATIC
stamped on (which flips the UI's fetch seam to the pre-baked files), plus every
API response the UI can ask for: api/meta.json, api/analytics.json,
api/changes.json, api/trials-index.json (every index row in one file — the
static host can't filter, so the UI does), one api/trials/{nct}.json per trial,
and redline-trials.csv (the unfiltered export).

Runs the FastAPI app in-process via TestClient — no server needed, and the baked
responses are byte-identical to what the live API would serve.
"""

import json
import re
import shutil
import sys

from fastapi.testclient import TestClient

from ctcm import config
from ctcm.api import MAX_LIMIT, app

OUT = config.REPO_ROOT / "build" / "static-site"
SAFE_ID = re.compile(r"^[A-Za-z0-9._-]+$")


def main() -> None:
    client = TestClient(app)

    def get_json(path: str) -> dict:
        r = client.get(path)
        r.raise_for_status()
        return r.json()

    if OUT.exists():
        shutil.rmtree(OUT)
    (OUT / "api" / "trials").mkdir(parents=True)

    for name, path in [("meta", "/api/meta"), ("analytics", "/api/analytics"), ("changes", "/api/changes")]:
        (OUT / "api" / f"{name}.json").write_text(json.dumps(get_json(path)))

    # Every index row, one file. counts rides along so the static fetchIndex can
    # return the same shape /api/trials does.
    rows, offset = [], 0
    while True:
        page = get_json(f"/api/trials?limit={MAX_LIMIT}&offset={offset}")
        rows.extend(page["rows"])
        offset += MAX_LIMIT
        if offset >= page["total"]:
            counts = page["counts"]
            break
    (OUT / "api" / "trials-index.json").write_text(json.dumps({"rows": rows, "counts": counts}))

    for i, row in enumerate(rows):
        nct = row["nctId"]
        if not SAFE_ID.match(nct):  # a trial id that isn't a safe filename would silently 404 once deployed
            sys.exit(f"unsafe trial id for a filename: {nct!r}")
        (OUT / "api" / "trials" / f"{nct}.json").write_text(json.dumps(get_json(f"/api/trials/{nct}")))
        if (i + 1) % 200 == 0:
            print(f"  {i + 1}/{len(rows)} trials baked")

    csv = client.get("/api/export.csv")
    csv.raise_for_status()
    (OUT / "redline-trials.csv").write_bytes(csv.content)

    html = (config.REPO_ROOT / "ui" / "index.html").read_text()
    stamped = html.replace("<script>", "<script>window.REDLINE_STATIC = 1;</script>\n<script>", 1)
    if stamped == html:
        sys.exit("could not find the <script> tag to stamp REDLINE_STATIC onto")
    (OUT / "index.html").write_text(stamped)

    n_files = sum(1 for p in OUT.rglob("*") if p.is_file())
    size_mb = sum(p.stat().st_size for p in OUT.rglob("*") if p.is_file()) / 1e6
    print(f"baked {len(rows)} trials -> {OUT} ({n_files} files, {size_mb:.1f} MB)")


if __name__ == "__main__":
    main()
