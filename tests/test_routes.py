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
# /query/v1/{user}
# ---------------------------------------------------------------------------

async def test_query_missing_api_key(app_client):
    resp = await app_client.get("/query/v1/user@example.com")
    assert resp.status == 401


async def test_query_wrong_api_key(app_client):
    resp = await app_client.get(
        "/query/v1/user@example.com", headers={"X-Api-Key": "wrong"}
    )
    assert resp.status == 401


async def test_query_dummy_backend(app_client):
    resp = await app_client.get(
        "/query/v1/user@example.com", headers={"X-Api-Key": "test-key"}
    )
    assert resp.status == 200
    data = json.loads(await resp.text())
    assert "users" in data


# ---------------------------------------------------------------------------
# /query/v1/{user} with YAML backend
# ---------------------------------------------------------------------------

async def test_query_yaml_known_user(yaml_app_client):
    resp = await yaml_app_client.get(
        "/query/v1/alice@example.com", headers={"X-Api-Key": "test-key"}
    )
    assert resp.status == 200
    data = json.loads(await resp.text())
    assert "alice@example.com" in data["users"] or any(
        "alice" in str(v) for v in data["users"].values()
    )


async def test_query_yaml_unknown_user_returns_empty(yaml_app_client):
    resp = await yaml_app_client.get(
        "/query/v1/nobody@example.com", headers={"X-Api-Key": "test-key"}
    )
    assert resp.status == 200
    data = json.loads(await resp.text())
    assert data["users"] == {}


async def test_query_alias_lookup(yaml_app_client):
    """Query by alias should resolve to the canonical user."""
    resp = await yaml_app_client.get(
        "/query/v1/a@example.com", headers={"X-Api-Key": "test-key"}
    )
    assert resp.status == 200


# ---------------------------------------------------------------------------
# /rspamd-settings/v1
# ---------------------------------------------------------------------------

async def test_rspamd_settings_auth_required(app_client):
    resp = await app_client.post(
        "/rspamd-settings/v1",
        data=json.dumps({}),
        headers={"Content-Type": "application/json"},
    )
    assert resp.status == 401


async def test_rspamd_settings_valid(app_client):
    resp = await app_client.post(
        "/rspamd-settings/v1",
        data=json.dumps({}),
        headers={"Content-Type": "application/json", "X-Api-Key": "test-key"},
    )
    assert resp.status == 200
    data = json.loads(await resp.text())
    assert "actions" in data
    assert "flags" in data
