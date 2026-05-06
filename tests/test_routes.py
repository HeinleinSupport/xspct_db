# SPDX-License-Identifier: EUPL-1.2
# SPDX-FileCopyrightText: 2026 Carsten Rosenberg <c.rosenberg@heinlein-support.de>

"""Tests for aiohttp HTTP routes."""

from __future__ import annotations

import json

import pytest

# ---------------------------------------------------------------------------
# Health / utility
# ---------------------------------------------------------------------------

async def test_health_endpoint(app_client):
    resp = await app_client.get("/")
    assert resp.status == 200
    text = await resp.text()
    assert "Hello" in text


async def test_ping_endpoint(app_client):
    resp = await app_client.get("/ping")
    assert resp.status == 200
    assert await resp.text() == "Pong"


async def test_metrics_unauthenticated(app_client):
    resp = await app_client.get("/metrics")
    assert resp.status == 200
    text = await resp.text()
    assert "xspct_db_requests_total" in text


# ---------------------------------------------------------------------------
# /v1/query/{user}
# ---------------------------------------------------------------------------

async def test_query_missing_api_key(app_client):
    resp = await app_client.get("/v1/query/user@mailexample.de")
    assert resp.status == 401


async def test_query_wrong_api_key(app_client):
    resp = await app_client.get(
        "/v1/query/user@mailexample.de", headers={"X-Api-Key": "wrong"}
    )
    assert resp.status == 401


async def test_query_dummy_backend(app_client):
    resp = await app_client.get(
        "/v1/query/user@mailexample.de", headers={"X-Api-Key": "test-key"}
    )
    assert resp.status == 200
    data = json.loads(await resp.text())
    assert "users" in data


# ---------------------------------------------------------------------------
# /v1/query/{user} with YAML backend
# ---------------------------------------------------------------------------

async def test_query_yaml_known_user(yaml_app_client):
    resp = await yaml_app_client.get(
        "/v1/query/alice@mailexample.de", headers={"X-Api-Key": "test-key"}
    )
    assert resp.status == 200
    data = json.loads(await resp.text())
    assert "alice@mailexample.de" in data["users"] or any(
        "alice" in str(v) for v in data["users"].values()
    )


async def test_query_yaml_unknown_user_returns_empty(yaml_app_client):
    resp = await yaml_app_client.get(
        "/v1/query/nobody@mailexample.de", headers={"X-Api-Key": "test-key"}
    )
    assert resp.status == 200
    data = json.loads(await resp.text())
    assert data["users"] == {}


async def test_query_alias_lookup(yaml_app_client):
    """Query by alias should resolve to the canonical user."""
    resp = await yaml_app_client.get(
        "/v1/query/a@mailexample.de", headers={"X-Api-Key": "test-key"}
    )
    assert resp.status == 200


# ---------------------------------------------------------------------------
# /v1/rspamd-settings
# ---------------------------------------------------------------------------

async def test_rspamd_settings_auth_required(app_client):
    resp = await app_client.post(
        "/v1/rspamd-settings",
        data=json.dumps({}),
        headers={"Content-Type": "application/json"},
    )
    assert resp.status == 401


async def test_rspamd_settings_valid(app_client):
    resp = await app_client.post(
        "/v1/rspamd-settings",
        data=json.dumps({}),
        headers={"Content-Type": "application/json", "X-Api-Key": "test-key"},
    )
    assert resp.status == 200
    data = json.loads(await resp.text())
    assert "actions" in data
    assert "flags" in data


async def test_rspamd_settings_empty_body_has_settings_extra_data(app_client):
    resp = await app_client.post(
        "/v1/rspamd-settings",
        data=json.dumps({}),
        headers={"Content-Type": "application/json", "X-Api-Key": "test-key"},
    )
    assert resp.status == 200
    data = json.loads(await resp.text())
    assert "settings_extra_data" in data
    assert data["settings_extra_data"] == {}


async def test_rspamd_settings_with_body(yaml_app_client):
    """from + rcpts addresses are looked up and returned in settings_extra_data."""
    resp = await yaml_app_client.post(
        "/v1/rspamd-settings",
        data=json.dumps({
            "uid": "abc123",
            "from": "alice@mailexample.de",
            "rcpts": ["nobody@mailexample.de"],
        }),
        headers={"Content-Type": "application/json", "X-Api-Key": "test-key"},
    )
    assert resp.status == 200
    data = json.loads(await resp.text())
    assert "settings_extra_data" in data
    # alice should be found; nobody should not appear or be empty
    extra = data["settings_extra_data"]
    assert "users" in extra
    assert any("alice" in str(v) for v in extra["users"].values())


async def test_rspamd_settings_deduplication(yaml_app_client):
    """Sender appearing in rcpts is only looked up once."""
    resp = await yaml_app_client.post(
        "/v1/rspamd-settings",
        data=json.dumps({
            "from": "alice@mailexample.de",
            "rcpts": ["alice@mailexample.de", "nobody@mailexample.de"],
        }),
        headers={"Content-Type": "application/json", "X-Api-Key": "test-key"},
    )
    assert resp.status == 200
    data = json.loads(await resp.text())
    # alice must appear at most once in settings_extra_data
    alice_keys = [k for k in data["settings_extra_data"].get("users", {}) if "alice" in k]
    assert len(alice_keys) <= 1


# ---------------------------------------------------------------------------
# Response cache integration tests
# ---------------------------------------------------------------------------

async def test_query_json_response_cache_hit(response_cache_app_client):
    """Second identical POST /v1/query-json is served from the response cache."""
    from xspct_db import stats as xstats
    payload = json.dumps({"users": ["alice@mailexample.de"]})
    headers = {"Content-Type": "application/json", "X-Api-Key": "test-key"}

    resp1 = await response_cache_app_client.post("/v1/query-json", data=payload, headers=headers)
    assert resp1.status == 200

    resp2 = await response_cache_app_client.post("/v1/query-json", data=payload, headers=headers)
    assert resp2.status == 200
    assert xstats.stats["response_cache_hits"] == 1
    assert json.loads(await resp1.text()) == json.loads(await resp2.text())


async def test_rspamd_settings_response_cache_hit(response_cache_app_client):
    """Second identical POST /v1/rspamd-settings is served from the response cache."""
    from xspct_db import stats as xstats
    payload = json.dumps({"from": "alice@mailexample.de", "rcpts": ["bob@mailexample.de"]})
    headers = {"Content-Type": "application/json", "X-Api-Key": "test-key"}

    resp1 = await response_cache_app_client.post("/v1/rspamd-settings", data=payload, headers=headers)
    assert resp1.status == 200

    resp2 = await response_cache_app_client.post("/v1/rspamd-settings", data=payload, headers=headers)
    assert resp2.status == 200
    assert xstats.stats["response_cache_hits"] == 1
    assert json.loads(await resp1.text()) == json.loads(await resp2.text())


# ---------------------------------------------------------------------------
# Queue / timeout behaviour
# ---------------------------------------------------------------------------

async def test_query_returns_504_on_timeout(delay_app_client):
    """GET /v1/query/{user} returns 504 when the backend exceeds the timeout."""
    from xspct_db import stats as xstats
    headers = {"X-Api-Key": "test-key"}
    resp = await delay_app_client.get("/v1/query/user@mailexample.de", headers=headers)
    assert resp.status == 504
    assert xstats.stats["requests_timeout"] == 1


async def test_query_json_returns_504_on_timeout(delay_app_client):
    """POST /v1/query-json returns 504 when the backend exceeds the timeout."""
    from xspct_db import stats as xstats
    payload = json.dumps({"users": ["user@mailexample.de"]})
    headers = {"Content-Type": "application/json", "X-Api-Key": "test-key"}
    resp = await delay_app_client.post("/v1/query-json", data=payload, headers=headers)
    assert resp.status == 504
    assert xstats.stats["requests_timeout"] >= 1


async def test_rspamd_settings_returns_504_on_timeout(delay_app_client):
    """POST /v1/rspamd-settings returns 504 when the backend exceeds the timeout."""
    from xspct_db import stats as xstats
    payload = json.dumps({"from": "user@mailexample.de", "rcpts": ["bob@mailexample.de"]})
    headers = {"Content-Type": "application/json", "X-Api-Key": "test-key"}
    resp = await delay_app_client.post("/v1/rspamd-settings", data=payload, headers=headers)
    assert resp.status == 504
    assert xstats.stats["requests_timeout"] >= 1


async def test_foreground_overload_returns_503(delay_app_client):
    """When all foreground slots are busy, new requests get 503."""
    import asyncio

    from xspct_db import stats as xstats
    headers = {"X-Api-Key": "test-key"}
    # Saturate the 2 fg slots + 1 bg slot, then the 4th should get 503
    tasks = [
        asyncio.ensure_future(delay_app_client.get("/v1/query/u1@mailexample.de", headers=headers))
        for _ in range(4)
    ]
    results = await asyncio.gather(*tasks)
    statuses = sorted(r.status for r in results)
    # At least one 503 expected
    assert 503 in statuses
    assert xstats.stats["foreground_overloaded"] >= 1


async def test_prometheus_includes_queue_metrics(app_client):
    """GET /metrics includes the five queue-related counter lines."""
    from xspct_db import stats as xstats
    xstats.reset()
    resp = await app_client.get("/metrics")
    body = await resp.text()
    for metric in (
        "xspct_db_foreground_overloaded_total",
        "xspct_db_requests_timeout_total",
        "xspct_db_background_completed_total",
        "xspct_db_background_rejected_total",
        "xspct_db_background_errors_total",
    ):
        assert metric in body, f"{metric} missing from /metrics output"


# ---------------------------------------------------------------------------
# msgpack encoding
# ---------------------------------------------------------------------------

msgpack = pytest.importorskip("msgpack", reason="msgpack not installed")


async def test_query_json_msgpack_content_type(app_client):
    """POST /v1/query-json with msgpack body + Content-Type returns msgpack response."""
    payload = msgpack.packb({"users": ["user@mailexample.de"]}, use_bin_type=True)
    headers = {"Content-Type": "application/msgpack", "X-Api-Key": "test-key"}
    resp = await app_client.post("/v1/query-json", data=payload, headers=headers)
    assert resp.status == 200
    assert "msgpack" in resp.headers.get("Content-Type", "")
    data = msgpack.unpackb(await resp.read(), raw=False)
    assert "users" in data


async def test_query_json_accept_msgpack(app_client):
    """POST /v1/query-json with JSON body + Accept: application/msgpack returns msgpack response."""
    payload = json.dumps({"users": ["user@mailexample.de"]})
    headers = {
        "Content-Type": "application/json",
        "Accept": "application/msgpack",
        "X-Api-Key": "test-key",
    }
    resp = await app_client.post("/v1/query-json", data=payload, headers=headers)
    assert resp.status == 200
    assert "msgpack" in resp.headers.get("Content-Type", "")
    data = msgpack.unpackb(await resp.read(), raw=False)
    assert "users" in data


async def test_rspamd_settings_msgpack(app_client):
    """POST /v1/rspamd-settings with msgpack body returns msgpack response."""
    payload = msgpack.packb({}, use_bin_type=True)
    headers = {"Content-Type": "application/msgpack", "X-Api-Key": "test-key"}
    resp = await app_client.post("/v1/rspamd-settings", data=payload, headers=headers)
    assert resp.status == 200
    assert "msgpack" in resp.headers.get("Content-Type", "")
    data = msgpack.unpackb(await resp.read(), raw=False)
    assert "actions" in data
    assert "flags" in data


async def test_query_get_accept_msgpack(yaml_app_client):
    """GET /v1/query/{user} with Accept: application/msgpack returns msgpack response."""
    headers = {"Accept": "application/msgpack", "X-Api-Key": "test-key"}
    resp = await yaml_app_client.get("/v1/query/alice@mailexample.de", headers=headers)
    assert resp.status == 200
    assert "msgpack" in resp.headers.get("Content-Type", "")
    data = msgpack.unpackb(await resp.read(), raw=False)
    assert "users" in data


async def test_query_json_msgpack_cache_hit(response_cache_app_client):
    """Second identical msgpack POST /v1/query-json is served from the response cache."""
    from xspct_db import stats as xstats
    payload = msgpack.packb({"users": ["alice@mailexample.de"]}, use_bin_type=True)
    headers = {"Content-Type": "application/msgpack", "X-Api-Key": "test-key"}

    resp1 = await response_cache_app_client.post("/v1/query-json", data=payload, headers=headers)
    assert resp1.status == 200

    resp2 = await response_cache_app_client.post("/v1/query-json", data=payload, headers=headers)
    assert resp2.status == 200
    assert xstats.stats["response_cache_hits"] == 1
    assert msgpack.unpackb(await resp1.read(), raw=False) == msgpack.unpackb(
        await resp2.read(), raw=False
    )


async def test_query_json_msgpack_and_json_cache_separate(response_cache_app_client):
    """msgpack and JSON responses for the same query use separate cache entries."""
    from xspct_db import stats as xstats
    users_payload = {"users": ["alice@mailexample.de"]}

    # First: JSON request
    resp_json = await response_cache_app_client.post(
        "/v1/query-json",
        data=json.dumps(users_payload),
        headers={"Content-Type": "application/json", "X-Api-Key": "test-key"},
    )
    assert resp_json.status == 200

    # Second: same users but msgpack request — must be a cache miss (separate key)
    resp_mp = await response_cache_app_client.post(
        "/v1/query-json",
        data=msgpack.packb(users_payload, use_bin_type=True),
        headers={"Content-Type": "application/msgpack", "X-Api-Key": "test-key"},
    )
    assert resp_mp.status == 200
    # Only the very first request triggered a miss; the second also misses (different fmt key)
    assert xstats.stats["response_cache_hits"] == 0
