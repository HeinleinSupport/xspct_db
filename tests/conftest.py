# SPDX-License-Identifier: EUPL-1.2
# SPDX-FileCopyrightText: 2026 Carsten Rosenberg <c.rosenberg@heinlein-support.de>

"""Shared pytest fixtures for the xspct_db test suite."""

from __future__ import annotations

from typing import Any

import pytest
from aiohttp.test_utils import TestClient

from xspct_db import stats
from xspct_db.server import create_app

# ---------------------------------------------------------------------------
# Minimal configurations
# ---------------------------------------------------------------------------


@pytest.fixture
def base_cfg() -> dict[str, Any]:
    """Return a minimal configuration dict with a dummy query backend."""
    return {
        "xspct_db_listen_address": ["127.0.0.1"],
        "xspct_db_listen_port": "11350",
        "xspct_db_listen_backlog": 128,
        "xspct_db_log_level": 50,
        "xspct_db_log_prefix": "Xspct_DB_Test",
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
        "xspct_db_metrics_auth": {
            "enabled": False,
            "api_key": True,
            "basic_auth_users": {},
        },
        "xspct_db_tls": {
            "tls_enabled": False,
            "tls_cert": "",
            "tls_key": "",
        },
        "xspct_db_key_translation": {},
        "xspct_db_value_split": {},
        "xspct_db_queries": {
            "test_dummy": {"db_type": "dummy"},
        },
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
        "xspct_db_rewrite_rules": None,
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


@pytest.fixture
def yaml_cfg(base_cfg: dict[str, Any]) -> dict[str, Any]:
    """Config with a minimal YAML backend and some static data."""
    cfg = dict(base_cfg)
    cfg["xspct_db_queries"] = {
        "users": {
            "db_type": "yaml",
            "primary_key": "mail",
            "attr_list": ["*"],
            "search_filter": ["mail", "aliases"],
        }
    }
    cfg["xspct_db_yaml_data"] = {
        "users": {
            "alice@mailexample.de": {
                "mail": "alice@mailexample.de",
                "uid": "alice",
                "aliases": ["a@mailexample.de"],
            },
            # bob: all feature attributes explicitly disabled; reject_level 5 → Rspamd 13
            "bob@mailexample.de": {
                "mail": "bob@mailexample.de",
                "uid": "bob",
                "aliases": [],
                "greylisting": "FALSE",
                "rbl": "FALSE",
                "mx_checks": "FALSE",
                "banned_bypass": "TRUE",
                "reject_level": "5",
            },
            # carol: no boolean attributes → tests "absent = default (enabled)" behaviour
            "carol@mailexample.de": {
                "mail": "carol@mailexample.de",
                "uid": "carol",
                "aliases": [],
            },
            # dave: greylisting and rbl disabled, reject_level=6 → Rspamd 15
            "dave@mailexample.de": {
                "mail": "dave@mailexample.de",
                "uid": "dave",
                "aliases": [],
                "greylisting": "FALSE",
                "rbl": "FALSE",
                "reject_level": "6",
            },
            # wildcard domain entry — returned as fallback for unknown users at @mailexample.de
            "@mailexample.de": {
                "mail": "@mailexample.de",
                "uid": "wildcard",
                "greylisting": "TRUE",
            },
        }
    }
    return cfg


@pytest.fixture
def wildcard_yaml_cfg(yaml_cfg: dict[str, Any]) -> dict[str, Any]:
    """yaml_cfg with wildcard_domain_query enabled on the users query."""
    cfg = dict(yaml_cfg)
    cfg["xspct_db_queries"] = {
        "users": {
            **yaml_cfg["xspct_db_queries"]["users"],
            "wildcard_domain_query": True,
        }
    }
    return cfg


@pytest.fixture
async def wildcard_yaml_app_client(wildcard_yaml_cfg: dict[str, Any], aiohttp_client: Any) -> TestClient:
    """Return an aiohttp test client with wildcard domain query enabled."""
    stats.reset()
    app = create_app(wildcard_yaml_cfg)
    return await aiohttp_client(app)


@pytest.fixture
def wildcard_pattern_yaml_cfg(yaml_cfg: dict[str, Any]) -> dict[str, Any]:
    """yaml_cfg with wildcard_domain_query + wildcard_key_pattern/replacement that strips one subdomain level.

    Pattern ``.*@[^.]+\\.(.+)`` with replacement ``@\\1`` applied to
    ``user@sub.mailexample.de`` produces ``@mailexample.de``.
    """
    cfg = dict(yaml_cfg)
    cfg["xspct_db_queries"] = {
        "users": {
            **yaml_cfg["xspct_db_queries"]["users"],
            "wildcard_domain_query": True,
            "wildcard_key_pattern": r".*@[^.]+\.(.+)",
            "wildcard_key_replacement": r"@\1",
        }
    }
    return cfg


@pytest.fixture
async def wildcard_pattern_app_client(wildcard_pattern_yaml_cfg: dict[str, Any], aiohttp_client: Any) -> TestClient:
    """Return an aiohttp test client with wildcard_key_pattern configured."""
    stats.reset()
    app = create_app(wildcard_pattern_yaml_cfg)
    return await aiohttp_client(app)


@pytest.fixture
def wildcard_multi_query_cfg(yaml_cfg: dict[str, Any]) -> dict[str, Any]:
    """yaml_cfg with two wildcard-enabled queries that require different key derivations."""
    cfg = dict(yaml_cfg)
    base_query = yaml_cfg["xspct_db_queries"]["users"]
    cfg["xspct_db_queries"] = {
        "realm_users": {
            **base_query,
            "yaml_root": "users",
            "wildcard_domain_query": True,
            "wildcard_key_pattern": r"^.+@realm$",
            "wildcard_key_replacement": r"@mailexample.de",
        },
        "subdomain_users": {
            **base_query,
            "yaml_root": "users",
            "wildcard_domain_query": True,
        },
    }
    return cfg


@pytest.fixture
async def wildcard_multi_query_app_client(wildcard_multi_query_cfg: dict[str, Any], aiohttp_client: Any) -> TestClient:
    """Return an aiohttp test client with multiple wildcard query configurations."""
    stats.reset()
    app = create_app(wildcard_multi_query_cfg)
    return await aiohttp_client(app)


@pytest.fixture
def wildcard_cached_specific_user_cfg(wildcard_yaml_cfg: dict[str, Any]) -> dict[str, Any]:
    """wildcard_yaml_cfg with local cache enabled and a real subdomain user entry."""
    cfg = dict(wildcard_yaml_cfg)
    cfg["xspct_db_local_cache"] = {
        "enabled": True,
        "expire": 20,
        "expire_negative": 20,
        "max_entries": 10000,
    }
    cfg["xspct_db_yaml_data"] = {
        **wildcard_yaml_cfg["xspct_db_yaml_data"],
        "users": {
            **wildcard_yaml_cfg["xspct_db_yaml_data"]["users"],
            "alice@sub.mailexample.de": {
                "mail": "alice@sub.mailexample.de",
                "uid": "alice-sub",
                "aliases": [],
            },
        },
    }
    return cfg


@pytest.fixture
async def wildcard_cached_specific_user_app_client(
    wildcard_cached_specific_user_cfg: dict[str, Any], aiohttp_client: Any
) -> TestClient:
    """Return an aiohttp test client that can reproduce wildcard cache shadowing."""
    stats.reset()
    app = create_app(wildcard_cached_specific_user_cfg)
    return await aiohttp_client(app)


@pytest.fixture
def rewrite_yaml_cfg(yaml_cfg: dict[str, Any]) -> dict[str, Any]:
    """yaml_cfg with a rewrite rule mapping *@relay.mailexample.de -> *@mailexample.de.

    alice@relay.mailexample.de rewrites to alice@mailexample.de before the
    backend is queried, so the existing alice entry is returned keyed under
    the original relay address.
    """
    cfg = dict(yaml_cfg)
    cfg["xspct_db_rewrite_rules"] = [
        {
            "pattern": r"^(.+)@relay\.mailexample\.de$",
            "replacement": r"\1@mailexample.de",
        }
    ]
    return cfg


@pytest.fixture
def rewrite_wildcard_yaml_cfg(wildcard_yaml_cfg: dict[str, Any]) -> dict[str, Any]:
    """wildcard_yaml_cfg with a rewrite rule mapping *@relay.mailexample.de -> *@mailexample.de."""
    cfg = dict(wildcard_yaml_cfg)
    cfg["xspct_db_rewrite_rules"] = [
        {
            "pattern": r"^(.+)@relay\.mailexample\.de$",
            "replacement": r"\1@mailexample.de",
        }
    ]
    return cfg


@pytest.fixture
def rewrite_realm_wildcard_yaml_cfg(wildcard_yaml_cfg: dict[str, Any]) -> dict[str, Any]:
    """wildcard_yaml_cfg with a rewrite that only enables wildcard lookup after rewriting.

    unknown@realm rewrites to unknown@mailexample.de. The original address
    has no wildcard key, but the canonical rewritten address does.
    """
    cfg = dict(wildcard_yaml_cfg)
    cfg["xspct_db_rewrite_rules"] = [
        {
            "pattern": r"^(.+)@realm$",
            "replacement": r"\1@mailexample.de",
        }
    ]
    return cfg


@pytest.fixture
async def rewrite_yaml_app_client(rewrite_yaml_cfg: dict[str, Any], aiohttp_client: Any) -> TestClient:
    """Return an aiohttp test client with address rewrite rules configured."""
    stats.reset()
    app = create_app(rewrite_yaml_cfg)
    return await aiohttp_client(app)


@pytest.fixture
async def rewrite_wildcard_yaml_app_client(rewrite_wildcard_yaml_cfg: dict[str, Any], aiohttp_client: Any) -> TestClient:
    """Return an aiohttp test client with both rewrite rules and wildcard lookup enabled."""
    stats.reset()
    app = create_app(rewrite_wildcard_yaml_cfg)
    return await aiohttp_client(app)


@pytest.fixture
async def rewrite_realm_wildcard_yaml_app_client(
    rewrite_realm_wildcard_yaml_cfg: dict[str, Any], aiohttp_client: Any
) -> TestClient:
    """Return an aiohttp test client where wildcard fallback only works after rewriting."""
    stats.reset()
    app = create_app(rewrite_realm_wildcard_yaml_cfg)
    return await aiohttp_client(app)


# ---------------------------------------------------------------------------
# aiohttp test client
# ---------------------------------------------------------------------------


@pytest.fixture
async def app_client(base_cfg: dict[str, Any], aiohttp_client: Any) -> TestClient:
    """Return an aiohttp test client wired to a fresh app instance."""
    stats.reset()
    app = create_app(base_cfg)
    return await aiohttp_client(app)


@pytest.fixture
async def yaml_app_client(yaml_cfg: dict[str, Any], aiohttp_client: Any) -> TestClient:
    """Return an aiohttp test client configured with the YAML backend."""
    stats.reset()
    app = create_app(yaml_cfg)
    return await aiohttp_client(app)


@pytest.fixture
def response_cache_cfg(base_cfg: dict[str, Any]) -> dict[str, Any]:
    """base_cfg with the response cache enabled."""
    cfg = dict(base_cfg)
    cfg["xspct_db_response_cache"] = {
        "enabled": True,
        "expire": 10,
        "max_entries": 1000,
        "rspamd_key_fields": ["from", "rcpts", "mta-name", "settings-name", "settings-id"],
    }
    return cfg


@pytest.fixture
async def response_cache_app_client(response_cache_cfg: dict[str, Any], aiohttp_client: Any) -> TestClient:
    """Return an aiohttp test client with response caching enabled."""
    from xspct_db import cache as xcache

    xcache._response_cache_clear()
    stats.reset()
    app = create_app(response_cache_cfg)
    client = await aiohttp_client(app)
    yield client
    xcache._response_cache_clear()


@pytest.fixture
def delay_cfg(base_cfg: dict[str, Any]) -> dict[str, Any]:
    """Config with a delay backend and a short request timeout for queue tests."""
    cfg = dict(base_cfg)
    cfg["xspct_db_queries"] = {"slow": {"db_type": "delay", "delay": 1.0}}
    cfg["xspct_db_request_timeout"] = 0.2
    cfg["xspct_db_foreground_slots"] = 2
    cfg["xspct_db_background_slots"] = 1
    return cfg


@pytest.fixture
async def delay_app_client(delay_cfg: dict[str, Any], aiohttp_client: Any) -> TestClient:
    """Return an aiohttp test client using the delay backend."""
    stats.reset()
    app = create_app(delay_cfg)
    return await aiohttp_client(app)
