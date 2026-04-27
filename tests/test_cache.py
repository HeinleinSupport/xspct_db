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
    yield
    cache.connection = None
    cache.error_count = 0
    cache.errors = []


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
        }
    }


@pytest.fixture
def disabled_cfg() -> dict[str, Any]:
    return {
        "xspct_db_redis_cache": {
            "enabled": False,
            "max_errors": 2,
        }
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
    result = await cache.get_object("s", "user@example.com", disabled_cfg)
    assert result is None


async def test_get_object_cache_miss(fake_redis, redis_cfg):
    result = await cache.get_object("s", "nobody@example.com", redis_cfg)
    assert result is None


async def test_get_object_alias_hit(fake_redis, redis_cfg):
    """Returns the user dict when alias → user key → object are cached."""
    await fake_redis.set("xspct_db_alias_alice@example.com", "alice@example.com")
    await fake_redis.set("xspct_db_user_alice@example.com", '{"mail": ["alice@example.com"], "uid": ["alice"]}')

    result = await cache.get_object("s", "alice@example.com", redis_cfg)
    assert result == {"mail": ["alice@example.com"], "uid": ["alice"]}


async def test_get_object_negative_cache_hit(fake_redis, redis_cfg):
    """Returns False when the user is in the negative cache."""
    await fake_redis.set("xspct_db_neg_ghost@example.com", "1")

    result = await cache.get_object("s", "ghost@example.com", redis_cfg)
    assert result is False


# ---------------------------------------------------------------------------
# Tests – set_cache()
# ---------------------------------------------------------------------------

async def test_set_cache_stores_user_and_aliases(fake_redis, redis_cfg):
    userdata = {
        "users": {
            "alice@example.com": {
                "mail": ["alice@example.com"],
                "uid": ["alice"],
                "aliases": ["a@example.com"],
            }
        }
    }
    user_to_pkey = {"alice@example.com": "alice@example.com"}
    await cache.set_cache("s", userdata, user_to_pkey, redis_cfg)

    assert await fake_redis.get("xspct_db_user_alice@example.com") is not None
    assert await fake_redis.get("xspct_db_alias_a@example.com") == "alice@example.com"
    assert await fake_redis.get("xspct_db_alias_alice@example.com") == "alice@example.com"


async def test_set_cache_noop_when_disabled(disabled_cfg):
    """set_cache() does nothing when caching is disabled."""
    await cache.set_cache("s", {"users": {"u": {}}}, {}, disabled_cfg)
    # No error and no connection needed – passes if it doesn't raise.


# ---------------------------------------------------------------------------
# Tests – set_negative_cache()
# ---------------------------------------------------------------------------

async def test_set_negative_cache_marks_absent_users(fake_redis, redis_cfg):
    await cache.set_negative_cache("s", ["ghost@example.com", "void@example.com"], redis_cfg)

    assert await fake_redis.get("xspct_db_neg_ghost@example.com") == "1"
    assert await fake_redis.get("xspct_db_neg_void@example.com") == "1"


async def test_set_negative_cache_noop_for_empty_list(fake_redis, redis_cfg):
    """set_negative_cache() with an empty list writes nothing."""
    await cache.set_negative_cache("s", [], redis_cfg)
    keys = await fake_redis.keys("xspct_db_neg_*")
    assert keys == []


async def test_set_negative_cache_noop_when_disabled(disabled_cfg):
    await cache.set_negative_cache("s", ["u@example.com"], disabled_cfg)
