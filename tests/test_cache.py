# SPDX-License-Identifier: EUPL-1.2
# SPDX-FileCopyrightText: 2026 Carsten Rosenberg <c.rosenberg@heinlein-support.de>

"""Tests for the Redis cache layer using fakeredis."""

from __future__ import annotations

from typing import Any

import fakeredis.aioredis
import pytest

from xspct_db import cache

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture(autouse=True)
def reset_cache_state():
    """Reset module-level cache state before and after each test."""
    cache.connection = None
    cache.error_count = 0
    cache.errors = []
    cache._local_clear()
    cache._response_cache_clear()
    yield
    cache.connection = None
    cache.error_count = 0
    cache.errors = []
    cache._local_clear()
    cache._response_cache_clear()


@pytest.fixture
def redis_cfg() -> dict[str, Any]:
    return {
        "xspct_db_redis_cache": {
            "enabled": True,
            "prefix_user": "xspct_db_user_",
            "prefix_alias": "xspct_db_alias_",
            "prefix_negative_alias": "xspct_db_neg_",
            "expire": 60,
            "expire_negative": 60,
            "max_errors": 2,
        },
        "xspct_db_local_cache": {
            "enabled": True,
            "expire": 20,
            "expire_negative": 20,
            "max_entries": 1000,
        },
    }


@pytest.fixture
def disabled_cfg() -> dict[str, Any]:
    return {
        "xspct_db_redis_cache": {
            "enabled": False,
            "max_errors": 2,
        },
        "xspct_db_local_cache": {
            "enabled": False,
        },
    }


@pytest.fixture
async def fake_redis(redis_cfg: dict[str, Any]):
    r = fakeredis.aioredis.FakeRedis(decode_responses=True)
    cache.set_connection(r)
    yield r
    await r.aclose()


# ---------------------------------------------------------------------------
# Tests – is_enabled()
# ---------------------------------------------------------------------------

def test_is_enabled_false_when_disabled(disabled_cfg):
    assert cache.is_enabled(disabled_cfg) is False


def test_is_enabled_true_when_no_errors(redis_cfg):
    assert cache.is_enabled(redis_cfg) is True


def test_is_enabled_false_when_error_limit_exceeded(redis_cfg):
    cache.error_count = 3  # max_errors = 2
    assert cache.is_enabled(redis_cfg) is False


# ---------------------------------------------------------------------------
# Tests – record_error() / reset_errors()
# ---------------------------------------------------------------------------

def test_record_error_increments_counter(redis_cfg):
    count = cache.record_error("oops", redis_cfg)
    assert count == 1
    assert "oops" in cache.errors


def test_reset_errors_clears_state(redis_cfg):
    cache.error_count = 3
    cache.errors = ["e1", "e2", "e3"]
    cache.reset_errors()
    assert cache.error_count == 0
    assert cache.errors == []


def test_reset_errors_noop_when_zero(redis_cfg):
    """reset_errors() does nothing when error_count is already 0."""
    cache.reset_errors()
    assert cache.error_count == 0


# ---------------------------------------------------------------------------
# Tests – get_object()
# ---------------------------------------------------------------------------

async def test_get_object_returns_none_when_disabled(disabled_cfg):
    result = await cache.get_object("s", "user@mailexample.de", disabled_cfg)
    assert result is None


async def test_get_object_cache_miss(fake_redis, redis_cfg):
    result = await cache.get_object("s", "nobody@mailexample.de", redis_cfg)
    assert result is None


async def test_get_object_alias_hit(fake_redis, redis_cfg):
    """Returns the user dict when alias → user key → object are cached."""
    await fake_redis.set("xspct_db_alias_alice@mailexample.de", "alice@mailexample.de")
    await fake_redis.set("xspct_db_user_alice@mailexample.de", '{"mail": ["alice@mailexample.de"], "uid": ["alice"]}')

    result = await cache.get_object("s", "alice@mailexample.de", redis_cfg)
    assert result == {"mail": ["alice@mailexample.de"], "uid": ["alice"]}


async def test_get_object_negative_cache_hit(fake_redis, redis_cfg):
    """Returns False when the user is in the negative cache."""
    await fake_redis.set("xspct_db_neg_ghost@mailexample.de", "1")

    result = await cache.get_object("s", "ghost@mailexample.de", redis_cfg)
    assert result is False


# ---------------------------------------------------------------------------
# Tests – set_cache()
# ---------------------------------------------------------------------------

async def test_set_cache_stores_user_and_aliases(fake_redis, redis_cfg):
    userdata = {
        "users": {
            "alice@mailexample.de": {
                "mail": ["alice@mailexample.de"],
                "uid": ["alice"],
                "aliases": ["a@mailexample.de"],
            }
        }
    }
    user_to_pkey = {"alice@mailexample.de": "alice@mailexample.de"}
    await cache.set_cache("s", userdata, user_to_pkey, redis_cfg)

    assert await fake_redis.get("xspct_db_user_alice@mailexample.de") is not None
    assert await fake_redis.get("xspct_db_alias_a@mailexample.de") == "alice@mailexample.de"
    assert await fake_redis.get("xspct_db_alias_alice@mailexample.de") == "alice@mailexample.de"


async def test_set_cache_noop_when_disabled(disabled_cfg):
    """set_cache() does nothing when caching is disabled."""
    await cache.set_cache("s", {"users": {"u": {}}}, {}, disabled_cfg)
    # No error and no connection needed – passes if it doesn't raise.


# ---------------------------------------------------------------------------
# Tests – set_negative_cache()
# ---------------------------------------------------------------------------

async def test_set_negative_cache_marks_absent_users(fake_redis, redis_cfg):
    await cache.set_negative_cache("s", ["ghost@mailexample.de", "void@mailexample.de"], redis_cfg)

    assert await fake_redis.get("xspct_db_neg_ghost@mailexample.de") == "1"
    assert await fake_redis.get("xspct_db_neg_void@mailexample.de") == "1"


async def test_set_negative_cache_noop_for_empty_list(fake_redis, redis_cfg):
    """set_negative_cache() with an empty list writes nothing."""
    await cache.set_negative_cache("s", [], redis_cfg)
    keys = await fake_redis.keys("xspct_db_neg_*")
    assert keys == []


async def test_set_negative_cache_noop_when_disabled(disabled_cfg):
    await cache.set_negative_cache("s", ["u@mailexample.de"], disabled_cfg)


# ---------------------------------------------------------------------------
# Fixtures for L1-only mode
# ---------------------------------------------------------------------------

@pytest.fixture
def local_only_cfg() -> dict[str, Any]:
    """Config with L1 enabled but Redis disabled (L1-only mode)."""
    return {
        "xspct_db_redis_cache": {
            "enabled": False,
            "max_errors": 2,
        },
        "xspct_db_local_cache": {
            "enabled": True,
            "expire": 20,
            "expire_negative": 20,
            "max_entries": 1000,
        },
    }


# ---------------------------------------------------------------------------
# Tests – L1 local cache
# ---------------------------------------------------------------------------

async def test_local_cache_hit_bypasses_redis(redis_cfg):
    """L1 hit returns the user object without any Redis I/O."""
    cache._init_local_caches(redis_cfg)
    cache._local_users["alice@mailexample.de"] = {"uid": ["alice"]}
    cache._local_aliases["alice@mailexample.de"] = "alice@mailexample.de"

    result = await cache.get_object("s", "alice@mailexample.de", redis_cfg)
    assert result == {"uid": ["alice"]}
    # No Redis connection was needed.
    assert cache.connection is None


async def test_local_cache_negative_hit_bypasses_redis(redis_cfg):
    """L1 negative hit returns False without any Redis I/O."""
    cache._init_local_caches(redis_cfg)
    cache._local_negative["ghost@mailexample.de"] = True

    result = await cache.get_object("s", "ghost@mailexample.de", redis_cfg)
    assert result is False
    assert cache.connection is None


async def test_local_cache_miss_falls_through_to_redis(fake_redis, redis_cfg):
    """L1 miss → L2 hit backfills L1 and returns the user object."""
    await fake_redis.set("xspct_db_alias_alice@mailexample.de", "alice@mailexample.de")
    await fake_redis.set(
        "xspct_db_user_alice@mailexample.de",
        '{"uid": ["alice"], "mail": ["alice@mailexample.de"]}',
    )

    result = await cache.get_object("s", "alice@mailexample.de", redis_cfg)
    assert result == {"uid": ["alice"], "mail": ["alice@mailexample.de"]}

    # L1 must have been backfilled.
    assert cache._local_aliases is not None
    assert cache._local_aliases.get("alice@mailexample.de") == "alice@mailexample.de"
    assert cache._local_users is not None
    assert cache._local_users.get("alice@mailexample.de") is not None


async def test_local_cache_negative_miss_falls_through_to_redis(fake_redis, redis_cfg):
    """L1 negative miss → L2 negative hit backfills L1 and returns False."""
    await fake_redis.set("xspct_db_neg_ghost@mailexample.de", "1")

    result = await cache.get_object("s", "ghost@mailexample.de", redis_cfg)
    assert result is False

    assert cache._local_negative is not None
    assert "ghost@mailexample.de" in cache._local_negative


async def test_set_cache_populates_l1_and_redis(fake_redis, redis_cfg):
    """set_cache() writes to both L1 and Redis."""
    userdata = {
        "users": {
            "alice@mailexample.de": {
                "uid": ["alice"],
                "mail": ["alice@mailexample.de"],
                "aliases": ["a@mailexample.de"],
            }
        }
    }
    user_to_pkey = {"alice@mailexample.de": "alice@mailexample.de"}
    await cache.set_cache("s", userdata, user_to_pkey, redis_cfg)

    # L2 (Redis)
    assert await fake_redis.get("xspct_db_user_alice@mailexample.de") is not None
    assert await fake_redis.get("xspct_db_alias_a@mailexample.de") == "alice@mailexample.de"

    # L1
    assert cache._local_users is not None
    assert cache._local_users.get("alice@mailexample.de") is not None
    assert cache._local_aliases is not None
    assert cache._local_aliases.get("a@mailexample.de") == "alice@mailexample.de"
    assert cache._local_aliases.get("alice@mailexample.de") == "alice@mailexample.de"


async def test_set_negative_cache_populates_l1_and_redis(fake_redis, redis_cfg):
    """set_negative_cache() writes to both L1 and Redis."""
    await cache.set_negative_cache("s", ["ghost@mailexample.de"], redis_cfg)

    assert await fake_redis.get("xspct_db_neg_ghost@mailexample.de") == "1"
    assert cache._local_negative is not None
    assert "ghost@mailexample.de" in cache._local_negative


async def test_local_cache_works_without_redis(local_only_cfg):
    """L1 serves hits and stores misses when Redis is disabled (connection is None)."""
    userdata = {
        "users": {
            "bob@mailexample.de": {
                "uid": ["bob"],
                "mail": ["bob@mailexample.de"],
                "aliases": [],
            }
        }
    }
    user_to_pkey = {"bob@mailexample.de": "bob@mailexample.de"}

    await cache.set_cache("s", userdata, user_to_pkey, local_only_cfg)

    result = await cache.get_object("s", "bob@mailexample.de", local_only_cfg)
    assert result == {"uid": ["bob"], "mail": ["bob@mailexample.de"], "aliases": []}
    assert cache.connection is None


async def test_local_cache_negative_without_redis(local_only_cfg):
    """set_negative_cache() writes to L1 and get_object() reads from L1 without Redis."""
    await cache.set_negative_cache("s", ["nobody@mailexample.de"], local_only_cfg)
    result = await cache.get_object("s", "nobody@mailexample.de", local_only_cfg)
    assert result is False
    assert cache.connection is None


async def test_local_cache_disabled_falls_through(disabled_cfg):
    """When L1 is explicitly disabled and Redis is disabled, get_object() returns None."""
    result = await cache.get_object("s", "alice@mailexample.de", disabled_cfg)
    assert result is None


# ---------------------------------------------------------------------------
# Fixtures for response cache tests
# ---------------------------------------------------------------------------

@pytest.fixture
def response_cache_cfg() -> dict[str, Any]:
    return {
        "xspct_db_response_cache": {
            "enabled": True,
            "expire": 10,
            "max_entries": 100,
            "rspamd_key_fields": ["from", "rcpts", "mta-name", "settings-name", "settings-id"],
        },
    }


@pytest.fixture
def response_cache_disabled_cfg() -> dict[str, Any]:
    return {
        "xspct_db_response_cache": {
            "enabled": False,
            "expire": 10,
            "max_entries": 100,
        },
    }


# ---------------------------------------------------------------------------
# Tests – response cache
# ---------------------------------------------------------------------------

def test_response_cache_miss(response_cache_cfg):
    """get_response() returns None on a cold cache."""
    key = ("query-json", frozenset(["alice@mailexample.de"]))
    result = cache.get_response(key, response_cache_cfg)
    assert result is None


def test_response_cache_hit(response_cache_cfg):
    """set_response() then get_response() returns the stored bytes."""
    key = ("query-json", frozenset(["alice@mailexample.de"]))
    body = b'{"users": {"alice@mailexample.de": {}}}'
    cache.set_response(key, body, response_cache_cfg)
    assert cache.get_response(key, response_cache_cfg) == body


def test_response_cache_key_order_independent(response_cache_cfg):
    """Different ordering of users produces the same frozenset key → same hit."""
    body = b'{"users": {}}'
    key1 = ("query-json", frozenset(["alice@mailexample.de", "bob@mailexample.de"]))
    key2 = ("query-json", frozenset(["bob@mailexample.de", "alice@mailexample.de"]))
    cache.set_response(key1, body, response_cache_cfg)
    assert cache.get_response(key2, response_cache_cfg) == body


def test_response_cache_different_endpoints(response_cache_cfg):
    """query-json and rspamd-settings keys do not collide."""
    body_qj = b'{"users": {}}'
    body_rs = b'{"actions": {}}'
    key_qj = ("query-json", frozenset(["alice@mailexample.de"]))
    key_rs = ("rspamd-settings", "alice@mailexample.de", frozenset(["bob@mailexample.de"]), None, None, None)
    cache.set_response(key_qj, body_qj, response_cache_cfg)
    cache.set_response(key_rs, body_rs, response_cache_cfg)
    assert cache.get_response(key_qj, response_cache_cfg) == body_qj
    assert cache.get_response(key_rs, response_cache_cfg) == body_rs


def test_response_cache_disabled(response_cache_disabled_cfg):
    """get_response() returns None when the response cache is disabled."""
    key = ("query-json", frozenset(["alice@mailexample.de"]))
    cache.set_response(key, b"data", response_cache_disabled_cfg)
    assert cache.get_response(key, response_cache_disabled_cfg) is None
