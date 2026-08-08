"""Runnable check for ingest.py's non-network logic: 429/5xx backoff retry
and the v0 + outcome-touching + final-version fetch rule. No real HTTP calls
(httpx.MockTransport) and no real sleeping (monkeypatched)."""

import asyncio

import httpx

from ctcm import config, ingest


def test_get_json_retries_on_5xx_then_succeeds(monkeypatch):
    real_sleep = asyncio.sleep
    monkeypatch.setattr(ingest.asyncio, "sleep", lambda _: real_sleep(0))
    calls = {"n": 0}

    def handler(request):
        calls["n"] += 1
        if calls["n"] < 3:
            return httpx.Response(500)
        return httpx.Response(200, json={"ok": True})

    transport = httpx.MockTransport(handler)

    async def run():
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            return await ingest._get_json(client, "/x")

    result = asyncio.run(run())
    assert result == {"ok": True}
    assert calls["n"] == 3


def test_fetch_trial_selects_v0_outcome_versions_and_final(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "CACHE_DIR", tmp_path)
    history = {
        "changes": [
            {"version": 0, "moduleLabels": []},
            {"version": 1, "moduleLabels": ["Study Status"]},
            {"version": 2, "moduleLabels": ["Outcome Measures"]},
            {"version": 3, "moduleLabels": ["Contacts/Locations"]},
        ]
    }

    def handler(request):
        if request.url.path.endswith("/history"):
            return httpx.Response(200, json=history)
        version = request.url.path.rsplit("/", 1)[-1]
        return httpx.Response(200, json={"study": {"protocolSection": {}}, "v": version})

    transport = httpx.MockTransport(handler)

    async def run():
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            await ingest.fetch_trial(client, "NCTFAKE")

    asyncio.run(run())

    fetched = sorted(
        int(p.name[len("v"):-len(".json.gz")]) for p in (tmp_path / "NCTFAKE").glob("v*.json.gz")
    )
    assert fetched == [0, 2, 3]  # v1 (no outcome touch, not final) must be skipped
