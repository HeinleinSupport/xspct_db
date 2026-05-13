# SPDX-License-Identifier: EUPL-1.2
# SPDX-FileCopyrightText: 2024 Carsten Rosenberg <c.rosenberg@heinlein-support.de>

"""Tests for the /metrics HTTP handler."""

from __future__ import annotations

from typing import Any

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer
from prometheus_client import CollectorRegistry

# ---------------------------------------------------------------------------
# Minimal app factory for handler tests
# ---------------------------------------------------------------------------


def _make_metrics_app(cfg: dict[str, Any], registry: CollectorRegistry) -> web.Application:
    """Build a minimal app that serves /metrics with the given config."""
    from xspct_db.metrics.handlers import metrics_handler

    app = web.Application()
    app["config"] = cfg
    app.router.add_get("/metrics", metrics_handler)
    return app


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def reg() -> CollectorRegistry:
    return CollectorRegistry()


@pytest.fixture
def base_cfg() -> dict[str, Any]:
    return {
        "xspct_db_api_header": "X-Api-Key",
        "xspct_db_api_key": ["test-key"],
        "xspct_db_api_key_verify_fail": True,
        "xspct_db_metrics_auth": {
            "enabled": False,
            "api_key": True,
            "basic_auth_users": {},
        },
        "xspct_db_metrics_cache_ttl": 5,
    }


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


async def test_metrics_handler_returns_200(base_cfg: dict[str, Any], reg: CollectorRegistry):
    """Handler returns 200 with valid Prometheus content-type."""
    # Register the loop-lag gauge so there's something to output.
    from xspct_db.metrics.registry import gauge

    gauge("event_loop_lag_seconds_h1", "test", registry=reg)

    app = _make_metrics_app(base_cfg, reg)
    async with TestClient(TestServer(app)) as c:
        resp = await c.get("/metrics")
        assert resp.status == 200
        ct = resp.headers.get("Content-Type", "")
        assert "text/plain" in ct


async def test_metrics_handler_body_contains_process_metrics(base_cfg: dict[str, Any], reg: CollectorRegistry):
    """Body includes process_resident_memory_bytes from ProcessCollector."""
    from prometheus_client import ProcessCollector

    ProcessCollector(registry=reg)

    app = _make_metrics_app(base_cfg, reg)
    async with TestClient(TestServer(app)) as c:
        resp = await c.get("/metrics")
        body = await resp.text()
        assert "process_resident_memory_bytes" in body


async def test_metrics_handler_unauthorized_when_auth_enabled(reg: CollectorRegistry):
    """Returns 401 when metrics auth is enabled and no credentials are provided."""
    cfg: dict[str, Any] = {
        "xspct_db_api_header": "X-Api-Key",
        "xspct_db_api_key": ["test-key"],
        "xspct_db_api_key_verify_fail": True,
        "xspct_db_metrics_auth": {
            "enabled": True,
            "api_key": True,
            "basic_auth_users": {},
        },
        "xspct_db_metrics_cache_ttl": 5,
    }

    app = _make_metrics_app(cfg, reg)
    async with TestClient(TestServer(app)) as c:
        resp = await c.get("/metrics")
        assert resp.status == 401


async def test_metrics_handler_authorized_with_token(reg: CollectorRegistry):
    """Returns 200 when correct API key is supplied and auth is enabled."""
    cfg: dict[str, Any] = {
        "xspct_db_api_header": "X-Api-Key",
        "xspct_db_api_key": ["test-key"],
        "xspct_db_api_key_verify_fail": True,
        "xspct_db_metrics_auth": {
            "enabled": True,
            "api_key": True,
            "basic_auth_users": {},
        },
        "xspct_db_metrics_cache_ttl": 5,
    }

    app = _make_metrics_app(cfg, reg)
    async with TestClient(TestServer(app)) as c:
        resp = await c.get("/metrics", headers={"X-Api-Key": "test-key"})
        assert resp.status == 200


async def test_metrics_handler_body_contains_wildcard_counters(base_cfg: dict[str, Any], reg: CollectorRegistry):
    """Body includes wildcard fallback counters exported from stats."""
    from xspct_db import stats as xstats
    from xspct_db.metrics.registry import register_stats_collector

    xstats.reset()
    xstats.stats["wildcard_domain_hits"] = 2
    xstats.stats["wildcard_domain_misses"] = 1
    register_stats_collector()

    app = _make_metrics_app(base_cfg, reg)
    async with TestClient(TestServer(app)) as c:
        resp = await c.get("/metrics")
        body = await resp.text()
        assert "xspct_db_wildcard_domain_hits_total 2.0" in body
        assert "xspct_db_wildcard_domain_misses_total 1.0" in body
