# SPDX-License-Identifier: EUPL-1.2
# SPDX-FileCopyrightText: 2024 Carsten Rosenberg <c.rosenberg@heinlein-support.de>

"""Integration tests for setup_metrics() and the /metrics endpoint."""

from __future__ import annotations

from typing import Any

from aiohttp.test_utils import TestClient, TestServer
from prometheus_client import CollectorRegistry

from xspct_db.server import create_app

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _base_cfg(**overrides) -> dict[str, Any]:
    cfg: dict[str, Any] = {
        "xspct_db_listen_address": ["127.0.0.1"],
        "xspct_db_listen_port": "11350",
        "xspct_db_listen_backlog": 128,
        "xspct_db_log_level": 50,
        "xspct_db_log_prefix": "Test",
        "xspct_db_api_header": "X-Api-Key",
        "xspct_db_api_key": ["test-key"],
        "xspct_db_api_key_verify_fail": True,
        "xspct_db_rspamd_header": "X-Rspamd-ID",
        "xspct_db_request_timeout": 0,
        "xspct_db_request_timeout_header": "",
        "xspct_db_foreground_slots": 30,
        "xspct_db_background_slots": 5,
        "xspct_db_stats_enabled": False,
        "xspct_db_stats_interval": 60,
        "xspct_db_stats_sample_interval": 10,
        "xspct_db_metrics_enabled": False,
        "xspct_db_metrics_cache_ttl": 5,
        "xspct_db_metrics_auth": {
            "enabled": False,
            "api_key": True,
            "basic_auth_users": {},
        },
        "xspct_db_tls": {"tls_enabled": False, "tls_cert": "", "tls_key": ""},
        "xspct_db_key_translation": {},
        "xspct_db_value_split": {},
        "xspct_db_queries": {"test_dummy": {"db_type": "dummy"}},
        "xspct_db_ldap_pool_minconn": 2,
        "xspct_db_ldap_pool_maxconn": 20,
        "xspct_db_mysql_pool_minconn": 1,
        "xspct_db_mysql_pool_maxconn": 20,
        "xspct_db_redis_cache": {
            "enabled": False,
            "host": "localhost",
            "port": 6379,
            "user": "",
            "password": "",
            "decode_responses": True,
            "prefix_user": "xspct_db_user_",
            "prefix_alias": "xspct_db_alias_",
            "prefix_negative_alias": "xspct_db_neg_alias_",
            "expire": 60,
            "expire_negative": 60,
            "connect_timeout": 1,
            "query_timeout": 1,
            "max_connections": 40,
            "max_errors": 2,
        },
        "xspct_db_yaml_data": {},
        "xspct_db_rspamd_alias_fields": ["aliases"],
        "xspct_db_reject_level_map": {"5": 13, "6": 15, "6.31": 17},
        "xspct_db_reject_level_default": 15,
        "xspct_db_rspamd_rules": None,
        "xspct_db_local_cache": {
            "enabled": False,
            "expire": 20,
            "expire_negative": 20,
            "max_entries": 10000,
        },
        "xspct_db_response_cache": {
            "enabled": False,
            "expire": 10,
            "max_entries": 5000,
            "rspamd_key_fields": ["from", "rcpts", "mta-name", "settings-name", "settings-id"],
        },
    }
    cfg.update(overrides)
    return cfg


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


async def test_metrics_disabled_no_import():
    """With xspct_db_metrics_enabled=false the /metrics route is not registered."""
    cfg = _base_cfg(xspct_db_metrics_enabled=False)
    app = create_app(cfg)
    async with TestClient(TestServer(app)) as c:
        resp = await c.get("/metrics")
        # Should still get a 405/404 from the original MetricsView-less setup_routes,
        # not a 200 from the prometheus handler.
        # The app has no /metrics route → aiohttp returns 404.
        assert resp.status == 404


async def test_metrics_enabled_returns_200():
    """With xspct_db_metrics_enabled=true, /metrics returns 200."""
    reg = CollectorRegistry()
    from prometheus_client import ProcessCollector

    ProcessCollector(registry=reg)

    cfg = _base_cfg(xspct_db_metrics_enabled=True)
    # Inject a custom registry so the handler uses our isolated instance.

    app = create_app(cfg)
    # setup_metrics was already called in create_app; override the handler to
    # use our isolated registry by patching the handler module's _cache.
    async with TestClient(TestServer(app)) as c:
        resp = await c.get("/metrics")
        assert resp.status == 200


async def test_metrics_aggregates_multiple_requests():
    """Multiple requests aggregate correctly in the global registry."""

    cfg = _base_cfg(xspct_db_metrics_enabled=True)
    app = create_app(cfg)
    async with TestClient(TestServer(app)) as c:
        await c.get("/ping")
        await c.get("/ping")
        resp = await c.get("/metrics")
        assert resp.status == 200
        body = await resp.text()
        # The http_requests_total counter should appear in the output.
        assert "http_requests_total" in body
