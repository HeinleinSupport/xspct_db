# SPDX-License-Identifier: EUPL-1.2
# SPDX-FileCopyrightText: 2026 Carsten Rosenberg <c.rosenberg@heinlein-support.de>

"""Unit tests for the prefilter module."""

from __future__ import annotations

import re
import time
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

import xspct_db.prefilter as pf

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _reset_prefilter() -> None:
    """Reset module-level prefilter state between tests."""
    pf._domain_set = frozenset()
    pf._domain_set_active = False
    pf._domain_set_loaded_at = 0.0
    pf._domain_set_expired_logged = False
    pf._file_mtime = 0.0
    pf._patterns = []


def _make_app(cfg: dict[str, Any]) -> MagicMock:
    data = {"config": cfg}
    app = MagicMock()
    app.__getitem__ = lambda self, key: data[key]
    app.__setitem__ = lambda self, key, value: data.update({key: value})
    app.get = lambda key, default=None: data.get(key, default)
    return app


def _base_cfg() -> dict[str, Any]:
    return {
        "xspct_db_prefilter": {"enabled": False},
        "xspct_db_prefilter_domains": {
            "enabled": False,
            "inline": [],
            "file": "",
            "file_reload_interval": 60,
            "redis_key": "",
            "redis_channel": "",
            "redis_reload_interval": 300,
            "min_domains": 0,
            "max_age": 0,
        },
        "xspct_db_prefilter_patterns": {
            "enabled": False,
            "patterns": [],
        },
    }


# ---------------------------------------------------------------------------
# filter_user — master switch
# ---------------------------------------------------------------------------


def test_filter_user_master_disabled_passes_all():
    """When master switch is off, filter_user returns True unconditionally."""
    _reset_prefilter()
    cfg = _base_cfg()
    app = _make_app(cfg)
    assert pf.filter_user("s", "alice@mailexample.de", app) is True
    assert pf.filter_user("s", "unknown@otherdomain.example", app) is True


def test_filter_user_domains_enabled_known_domain():
    """filter_user returns True for a domain in the active set."""
    _reset_prefilter()
    cfg = _base_cfg()
    cfg["xspct_db_prefilter"]["enabled"] = True
    cfg["xspct_db_prefilter_domains"]["enabled"] = True
    pf._domain_set = frozenset(["mailexample.de"])
    pf._domain_set_active = True
    pf._domain_set_loaded_at = time.monotonic()
    app = _make_app(cfg)
    assert pf.filter_user("s", "alice@mailexample.de", app) is True


def test_filter_user_domains_enabled_unknown_domain():
    """filter_user returns False for a domain not in the active set."""
    _reset_prefilter()
    cfg = _base_cfg()
    cfg["xspct_db_prefilter"]["enabled"] = True
    cfg["xspct_db_prefilter_domains"]["enabled"] = True
    pf._domain_set = frozenset(["mailexample.de"])
    pf._domain_set_active = True
    pf._domain_set_loaded_at = time.monotonic()
    app = _make_app(cfg)
    assert pf.filter_user("s", "alice@other.example", app) is False


def test_filter_user_domains_bypass_when_no_set():
    """filter_user passes when domain filter is enabled but no set is loaded (bypass)."""
    _reset_prefilter()
    cfg = _base_cfg()
    cfg["xspct_db_prefilter"]["enabled"] = True
    cfg["xspct_db_prefilter_domains"]["enabled"] = True
    # _domain_set_active is False (no set loaded)
    app = _make_app(cfg)
    assert pf.filter_user("s", "alice@mailexample.de", app) is True


# ---------------------------------------------------------------------------
# filter_user — pattern filter
# ---------------------------------------------------------------------------


def test_filter_user_pattern_match():
    """filter_user returns True when the user matches a compiled pattern."""
    _reset_prefilter()
    cfg = _base_cfg()
    cfg["xspct_db_prefilter"]["enabled"] = True
    cfg["xspct_db_prefilter_patterns"]["enabled"] = True
    pf._patterns = [re.compile(r"@mailexample\.de$")]
    app = _make_app(cfg)
    assert pf.filter_user("s", "alice@mailexample.de", app) is True


def test_filter_user_pattern_no_match():
    """filter_user returns False when the user matches no compiled pattern."""
    _reset_prefilter()
    cfg = _base_cfg()
    cfg["xspct_db_prefilter"]["enabled"] = True
    cfg["xspct_db_prefilter_patterns"]["enabled"] = True
    pf._patterns = [re.compile(r"@mailexample\.de$")]
    app = _make_app(cfg)
    assert pf.filter_user("s", "alice@other.example", app) is False


def test_filter_user_pattern_disabled_passes():
    """When pattern filter is disabled, all addresses pass the pattern check."""
    _reset_prefilter()
    cfg = _base_cfg()
    cfg["xspct_db_prefilter"]["enabled"] = True
    # patterns enabled=False
    pf._patterns = [re.compile(r"@mailexample\.de$")]
    app = _make_app(cfg)
    assert pf.filter_user("s", "alice@other.example", app) is True


# ---------------------------------------------------------------------------
# filter_addresses
# ---------------------------------------------------------------------------


def test_filter_addresses_filters_list():
    """filter_addresses drops addresses whose domain is not in the set."""
    _reset_prefilter()
    cfg = _base_cfg()
    cfg["xspct_db_prefilter"]["enabled"] = True
    cfg["xspct_db_prefilter_domains"]["enabled"] = True
    pf._domain_set = frozenset(["mailexample.de"])
    pf._domain_set_active = True
    pf._domain_set_loaded_at = time.monotonic()
    app = _make_app(cfg)
    result = pf.filter_addresses("s", ["alice@mailexample.de", "bob@other.example"], app)
    assert result == ["alice@mailexample.de"]


def test_filter_addresses_master_off_returns_all():
    """filter_addresses returns the full list when master switch is off."""
    _reset_prefilter()
    cfg = _base_cfg()
    app = _make_app(cfg)
    addrs = ["alice@mailexample.de", "bob@other.example"]
    assert pf.filter_addresses("s", addrs, app) == addrs


def test_filter_addresses_empty_input_returns_empty():
    """filter_addresses on an empty input list returns empty."""
    _reset_prefilter()
    cfg = _base_cfg()
    cfg["xspct_db_prefilter"]["enabled"] = True
    cfg["xspct_db_prefilter_domains"]["enabled"] = True
    pf._domain_set = frozenset(["mailexample.de"])
    pf._domain_set_active = True
    pf._domain_set_loaded_at = time.monotonic()
    app = _make_app(cfg)
    assert pf.filter_addresses("s", [], app) == []


# ---------------------------------------------------------------------------
# _load_file
# ---------------------------------------------------------------------------


def test_load_file_reads_domains(tmp_path):
    """_load_file parses domains, strips comments and blank lines."""
    domain_file = tmp_path / "domains.txt"
    domain_file.write_text("mailexample.de\n# comment\n\nexample.org\n  UPPER.COM  \n")
    result = pf._load_file(str(domain_file))
    assert result == frozenset(["mailexample.de", "example.org", "upper.com"])


def test_load_file_missing_returns_empty(tmp_path):
    """_load_file returns frozenset() when the file does not exist."""
    result = pf._load_file(str(tmp_path / "nonexistent.txt"))
    assert result == frozenset()


def test_load_file_inline_domains():
    """_build_domain_set_sync picks up inline domains."""
    _reset_prefilter()
    cfg = _base_cfg()
    cfg["xspct_db_prefilter_domains"]["inline"] = ["mailexample.de", "ANOTHER.EXAMPLE"]
    result = pf._build_domain_set_sync(cfg)
    assert "mailexample.de" in result
    assert "another.example" in result


def test_load_file_combined_with_inline(tmp_path):
    """_build_domain_set_sync merges inline + file sources."""
    domain_file = tmp_path / "domains.txt"
    domain_file.write_text("example.org\n")
    cfg = _base_cfg()
    cfg["xspct_db_prefilter_domains"]["inline"] = ["mailexample.de"]
    cfg["xspct_db_prefilter_domains"]["file"] = str(domain_file)
    result = pf._build_domain_set_sync(cfg)
    assert frozenset(["mailexample.de", "example.org"]).issubset(result)


# ---------------------------------------------------------------------------
# _validate_and_apply — min_domains guard
# ---------------------------------------------------------------------------


def test_validate_apply_accepts_valid_set():
    """_validate_and_apply updates state when the new set meets min_domains."""
    _reset_prefilter()
    cfg = _base_cfg()
    cfg["xspct_db_prefilter_domains"]["min_domains"] = 2
    new_set = frozenset(["mailexample.de", "example.org"])
    pf._validate_and_apply(new_set, cfg)
    assert pf._domain_set_active is True
    assert pf._domain_set == new_set


def test_validate_apply_rejects_below_min_domains_first_load():
    """On first load, a set below min_domains leaves filter in bypass mode."""
    _reset_prefilter()
    cfg = _base_cfg()
    cfg["xspct_db_prefilter_domains"]["min_domains"] = 5
    pf._validate_and_apply(frozenset(["mailexample.de"]), cfg)
    assert pf._domain_set_active is False
    assert pf._domain_set == frozenset()


def test_validate_apply_keeps_previous_on_reload_failure():
    """A defunct reload keeps the previous valid set (last-known-good)."""
    _reset_prefilter()
    cfg = _base_cfg()
    cfg["xspct_db_prefilter_domains"]["min_domains"] = 2
    good_set = frozenset(["mailexample.de", "example.org"])
    pf._validate_and_apply(good_set, cfg)
    assert pf._domain_set_active is True

    # Now a bad reload (only 1 domain, below min_domains=2)
    pf._validate_and_apply(frozenset(["mailexample.de"]), cfg)
    assert pf._domain_set_active is True
    assert pf._domain_set == good_set  # kept last-known-good


def test_validate_apply_empty_set_no_min_domains():
    """With min_domains=0, an empty set is accepted (no guard)."""
    _reset_prefilter()
    cfg = _base_cfg()
    cfg["xspct_db_prefilter_domains"]["min_domains"] = 0
    pf._validate_and_apply(frozenset(), cfg)
    assert pf._domain_set_active is True
    assert pf._domain_set == frozenset()


# ---------------------------------------------------------------------------
# max_age expiry
# ---------------------------------------------------------------------------


def test_check_expiry_active_within_max_age():
    """_check_expiry returns True when set is active and not expired."""
    _reset_prefilter()
    cfg = _base_cfg()
    cfg["xspct_db_prefilter_domains"]["max_age"] = 60
    pf._domain_set = frozenset(["mailexample.de"])
    pf._domain_set_active = True
    pf._domain_set_loaded_at = time.monotonic()
    assert pf._check_expiry(cfg) is True


def test_check_expiry_drops_expired_set():
    """_check_expiry switches to bypass when the set has exceeded max_age."""
    _reset_prefilter()
    cfg = _base_cfg()
    cfg["xspct_db_prefilter_domains"]["max_age"] = 1  # 1 second
    pf._domain_set = frozenset(["mailexample.de"])
    pf._domain_set_active = True
    pf._domain_set_loaded_at = time.monotonic() - 5  # 5 seconds old
    assert pf._check_expiry(cfg) is False
    assert pf._domain_set_active is False
    assert pf._domain_set == frozenset()


def test_filter_user_bypasses_after_max_age_expiry():
    """After max_age expiry, filter_user passes the address (bypass mode)."""
    _reset_prefilter()
    cfg = _base_cfg()
    cfg["xspct_db_prefilter"]["enabled"] = True
    cfg["xspct_db_prefilter_domains"]["enabled"] = True
    cfg["xspct_db_prefilter_domains"]["max_age"] = 1
    pf._domain_set = frozenset(["mailexample.de"])
    pf._domain_set_active = True
    pf._domain_set_loaded_at = time.monotonic() - 10  # expired
    app = _make_app(cfg)
    # unknown@other.example would be blocked if set were active; bypass passes it
    assert pf.filter_user("s", "unknown@other.example", app) is True


def test_check_expiry_disabled_when_max_age_zero():
    """When max_age=0, _check_expiry never expires the set."""
    _reset_prefilter()
    cfg = _base_cfg()
    cfg["xspct_db_prefilter_domains"]["max_age"] = 0
    pf._domain_set = frozenset(["mailexample.de"])
    pf._domain_set_active = True
    pf._domain_set_loaded_at = time.monotonic() - 9999  # very old
    assert pf._check_expiry(cfg) is True


# ---------------------------------------------------------------------------
# Redis source (graceful skip when connection is None)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_load_redis_no_connection(monkeypatch):
    """_load_redis returns frozenset() gracefully when cache.connection is None."""
    from xspct_db import cache

    monkeypatch.setattr(cache, "connection", None)
    cfg = _base_cfg()
    cfg["xspct_db_prefilter_domains"]["redis_key"] = "domains"
    result = await pf._load_redis(cfg)
    assert result == frozenset()


@pytest.mark.asyncio
async def test_build_domain_set_full_no_redis(monkeypatch, tmp_path):
    """_build_domain_set_full works when Redis is not available."""
    from xspct_db import cache

    monkeypatch.setattr(cache, "connection", None)
    domain_file = tmp_path / "domains.txt"
    domain_file.write_text("mailexample.de\n")
    cfg = _base_cfg()
    cfg["xspct_db_prefilter_domains"]["file"] = str(domain_file)
    cfg["xspct_db_prefilter_domains"]["redis_key"] = "domains"
    result = await pf._build_domain_set_full(cfg)
    assert "mailexample.de" in result


@pytest.mark.asyncio
async def test_load_redis_with_connection(monkeypatch):
    """_load_redis fetches members from a mock Redis connection."""
    from xspct_db import cache

    mock_conn = AsyncMock()
    mock_conn.smembers = AsyncMock(return_value={"mailexample.de", "example.org"})
    monkeypatch.setattr(cache, "connection", mock_conn)
    cfg = _base_cfg()
    cfg["xspct_db_prefilter_domains"]["redis_key"] = "domains"
    result = await pf._load_redis(cfg)
    assert result == frozenset(["mailexample.de", "example.org"])


# ---------------------------------------------------------------------------
# _compile_patterns
# ---------------------------------------------------------------------------


def test_compile_patterns_valid():
    """_compile_patterns returns compiled patterns for valid regex strings."""
    cfg = _base_cfg()
    cfg["xspct_db_prefilter_patterns"]["patterns"] = [r"@mailexample\.de$", r"^admin@"]
    patterns = pf._compile_patterns(cfg)
    assert len(patterns) == 2


def test_compile_patterns_invalid_skipped():
    """_compile_patterns skips invalid patterns without raising."""
    cfg = _base_cfg()
    cfg["xspct_db_prefilter_patterns"]["patterns"] = [r"[invalid", r"@mailexample\.de$"]
    patterns = pf._compile_patterns(cfg)
    assert len(patterns) == 1  # only the valid one


# ---------------------------------------------------------------------------
# Route integration: empty result when all addresses filtered
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_query_single_filtered_returns_empty(aiohttp_client, base_cfg):
    """GET /v1/query/{user} returns 200 with empty users when prefiltered."""
    from xspct_db import stats as xstats
    from xspct_db.server import create_app

    cfg = dict(base_cfg)
    cfg["xspct_db_prefilter"] = {"enabled": True}
    cfg["xspct_db_prefilter_domains"] = {
        "enabled": True,
        "inline": ["mailexample.de"],
        "file": "",
        "file_reload_interval": 60,
        "redis_key": "",
        "redis_channel": "",
        "redis_reload_interval": 300,
        "min_domains": 0,
        "max_age": 0,
    }
    cfg["xspct_db_prefilter_patterns"] = {"enabled": False, "patterns": []}

    xstats.reset()
    _reset_prefilter()
    app = create_app(cfg)
    client = await aiohttp_client(app)

    resp = await client.get(
        "/v1/query/alice@blocked.example",
        headers={"X-Api-Key": "test-key"},
    )
    assert resp.status == 200
    data = await resp.json()
    assert data == {"users": {}}


@pytest.mark.asyncio
async def test_query_json_all_filtered_returns_empty(aiohttp_client, base_cfg):
    """POST /v1/query-json returns 200 with empty users when all addresses prefiltered."""
    from xspct_db import stats as xstats
    from xspct_db.server import create_app

    cfg = dict(base_cfg)
    cfg["xspct_db_prefilter"] = {"enabled": True}
    cfg["xspct_db_prefilter_domains"] = {
        "enabled": True,
        "inline": ["mailexample.de"],
        "file": "",
        "file_reload_interval": 60,
        "redis_key": "",
        "redis_channel": "",
        "redis_reload_interval": 300,
        "min_domains": 0,
        "max_age": 0,
    }
    cfg["xspct_db_prefilter_patterns"] = {"enabled": False, "patterns": []}

    xstats.reset()
    _reset_prefilter()
    app = create_app(cfg)
    client = await aiohttp_client(app)

    resp = await client.post(
        "/v1/query-json",
        json={"users": ["alice@blocked.example", "bob@blocked.example"]},
        headers={"X-Api-Key": "test-key"},
    )
    assert resp.status == 200
    data = await resp.json()
    assert data == {"users": {}}


@pytest.mark.asyncio
async def test_query_json_partial_filter(aiohttp_client):
    """POST /v1/query-json only queries non-filtered addresses."""
    from xspct_db import stats as xstats
    from xspct_db.server import create_app

    cfg = {
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
        "xspct_db_metrics_auth": {"enabled": False, "api_key": True, "basic_auth_users": {}},
        "xspct_db_tls": {"tls_enabled": False, "tls_cert": "", "tls_key": ""},
        "xspct_db_key_translation": {},
        "xspct_db_value_split": {},
        "xspct_db_queries": {
            "users": {
                "db_type": "yaml",
                "primary_key": "mail",
                "attr_list": ["*"],
                "search_filter": ["mail", "aliases"],
            }
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
        "xspct_db_yaml_data": {
            "users": {
                "alice@mailexample.de": {
                    "mail": "alice@mailexample.de",
                    "uid": "alice",
                    "aliases": [],
                },
            }
        },
        "xspct_db_rspamd_alias_fields": ["aliases"],
        "xspct_db_local_cache": {"enabled": False, "expire": 20, "expire_negative": 20, "max_entries": 10000},
        "xspct_db_response_cache": {
            "enabled": False,
            "expire": 10,
            "max_entries": 5000,
            "rspamd_key_fields": ["from", "rcpts", "mta-name", "settings-name", "settings-id"],
        },
        "xspct_db_prefilter": {"enabled": True},
        "xspct_db_prefilter_domains": {
            "enabled": True,
            "inline": ["mailexample.de"],
            "file": "",
            "file_reload_interval": 60,
            "redis_key": "",
            "redis_channel": "",
            "redis_reload_interval": 300,
            "min_domains": 0,
            "max_age": 0,
        },
        "xspct_db_prefilter_patterns": {"enabled": False, "patterns": []},
    }

    xstats.reset()
    _reset_prefilter()
    app = create_app(cfg)
    client = await aiohttp_client(app)

    resp = await client.post(
        "/v1/query-json",
        json={"users": ["alice@mailexample.de", "bob@blocked.example"]},
        headers={"X-Api-Key": "test-key"},
    )
    assert resp.status == 200
    data = await resp.json()
    # alice should be returned (known domain); bob should have been filtered out
    assert "alice@mailexample.de" in data["users"]
    assert "bob@blocked.example" not in data["users"]


# ---------------------------------------------------------------------------
# Stats counters
# ---------------------------------------------------------------------------


def test_stats_domain_filter_hits():
    """filter_user increments prefilter_domain_hits when address passes."""
    from xspct_db import stats as xstats

    _reset_prefilter()
    xstats.reset()
    cfg = _base_cfg()
    cfg["xspct_db_prefilter"]["enabled"] = True
    cfg["xspct_db_prefilter_domains"]["enabled"] = True
    pf._domain_set = frozenset(["mailexample.de"])
    pf._domain_set_active = True
    pf._domain_set_loaded_at = time.monotonic()
    app = _make_app(cfg)
    assert pf.filter_user("s", "alice@mailexample.de", app) is True
    assert xstats.stats["prefilter_domain_hits"] == 1
    assert xstats.stats["prefilter_domain_misses"] == 0


def test_stats_domain_filter_misses():
    """filter_user increments prefilter_domain_misses when address is blocked."""
    from xspct_db import stats as xstats

    _reset_prefilter()
    xstats.reset()
    cfg = _base_cfg()
    cfg["xspct_db_prefilter"]["enabled"] = True
    cfg["xspct_db_prefilter_domains"]["enabled"] = True
    pf._domain_set = frozenset(["mailexample.de"])
    pf._domain_set_active = True
    pf._domain_set_loaded_at = time.monotonic()
    app = _make_app(cfg)
    assert pf.filter_user("s", "alice@blocked.example", app) is False
    assert xstats.stats["prefilter_domain_hits"] == 0
    assert xstats.stats["prefilter_domain_misses"] == 1


def test_stats_pattern_filter_increments():
    """filter_user increments pattern hit/miss counters correctly."""
    from xspct_db import stats as xstats

    _reset_prefilter()
    xstats.reset()
    cfg = _base_cfg()
    cfg["xspct_db_prefilter"]["enabled"] = True
    cfg["xspct_db_prefilter_patterns"]["enabled"] = True
    pf._patterns = [re.compile(r"@mailexample\.de$")]
    app = _make_app(cfg)

    assert pf.filter_user("s", "alice@mailexample.de", app) is True
    assert xstats.stats["prefilter_pattern_hits"] == 1
    assert xstats.stats["prefilter_pattern_misses"] == 0

    assert pf.filter_user("s", "alice@other.example", app) is False
    assert xstats.stats["prefilter_pattern_hits"] == 1
    assert xstats.stats["prefilter_pattern_misses"] == 1


def test_stats_filter_addresses_batch_counts():
    """filter_addresses increments domain hit/miss counters for the whole batch."""
    from xspct_db import stats as xstats

    _reset_prefilter()
    xstats.reset()
    cfg = _base_cfg()
    cfg["xspct_db_prefilter"]["enabled"] = True
    cfg["xspct_db_prefilter_domains"]["enabled"] = True
    pf._domain_set = frozenset(["mailexample.de"])
    pf._domain_set_active = True
    pf._domain_set_loaded_at = time.monotonic()
    app = _make_app(cfg)

    result = pf.filter_addresses(
        "s",
        ["alice@mailexample.de", "bob@mailexample.de", "eve@blocked.example"],
        app,
    )
    assert result == ["alice@mailexample.de", "bob@mailexample.de"]
    assert xstats.stats["prefilter_domain_hits"] == 2
    assert xstats.stats["prefilter_domain_misses"] == 1


def test_stats_domain_count_gauge():
    """_validate_and_apply updates prefilter_domain_count gauge."""
    from xspct_db import stats as xstats

    _reset_prefilter()
    xstats.reset()
    cfg = _base_cfg()
    new_set = frozenset(["mailexample.de", "example.org", "test.de"])
    pf._validate_and_apply(new_set, cfg)
    assert xstats.stats["prefilter_domain_count"] == 3
