# SPDX-License-Identifier: EUPL-1.2
# SPDX-FileCopyrightText: 2026 Carsten Rosenberg <c.rosenberg@heinlein-support.de>

"""Two-layer cache: in-process TTLCache (L1) + Redis (L2) with circuit-breaker logic."""

from __future__ import annotations

import json
import logging
from typing import Any

from cachetools import TTLCache

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Module-level state
# ---------------------------------------------------------------------------

# Redis connection – injected by server.py at startup.
connection: Any = None

# Circuit-breaker state for Redis.
error_count: int = 0
errors: list[str] = []

# L1 in-process TTLCache instances.  Initialised lazily by _init_local_caches().
_local_aliases: TTLCache | None = None  # alias/address  → canonical user key
_local_users: TTLCache | None = None  # canonical key  → user dict
_local_negative: TTLCache | None = None  # address        → True (absent marker)

# Response cache – pre-serialised JSON bytes keyed by (endpoint, frozenset/tuple).
_response_cache: TTLCache | None = None


# ---------------------------------------------------------------------------
# L1 helpers
# ---------------------------------------------------------------------------


def _init_local_caches(cfg: dict[str, Any]) -> None:
    """Create (or recreate) the three TTLCache instances from *cfg*."""
    global _local_aliases, _local_users, _local_negative
    rcfg = cfg["xspct_db_local_cache"]
    maxsize = int(rcfg.get("max_entries", 10000))
    ttl = float(rcfg.get("expire", 20))
    ttl_neg = float(rcfg.get("expire_negative", 20))
    _local_aliases = TTLCache(maxsize=maxsize, ttl=ttl)
    _local_users = TTLCache(maxsize=maxsize, ttl=ttl)
    _local_negative = TTLCache(maxsize=maxsize, ttl=ttl_neg)


def _local_clear() -> None:
    """Reset all L1 caches to empty (used in tests and server restart)."""
    global _local_aliases, _local_users, _local_negative
    if _local_aliases is not None:
        _local_aliases.clear()
    if _local_users is not None:
        _local_users.clear()
    if _local_negative is not None:
        _local_negative.clear()


def _l1_enabled(cfg: dict[str, Any]) -> bool:
    """Return True when L1 caching is configured to be active."""
    return bool(cfg["xspct_db_local_cache"].get("enabled", True))


def _ensure_l1(cfg: dict[str, Any]) -> bool:
    """Ensure L1 caches exist; return True when L1 is enabled."""
    global _local_aliases, _local_users, _local_negative
    if not _l1_enabled(cfg):
        return False
    if _local_aliases is None:
        _init_local_caches(cfg)
    return True


# ---------------------------------------------------------------------------
# Response cache helpers
# ---------------------------------------------------------------------------


def _init_response_cache(cfg: dict[str, Any]) -> None:
    """Create (or recreate) the response TTLCache from *cfg*."""
    global _response_cache
    rcfg = cfg["xspct_db_response_cache"]
    maxsize = int(rcfg.get("max_entries", 5000))
    ttl = float(rcfg.get("expire", 10))
    _response_cache = TTLCache(maxsize=maxsize, ttl=ttl)


def _response_cache_clear() -> None:
    """Reset the response cache (used in tests and server restart)."""
    global _response_cache
    _response_cache = None


def _response_cache_enabled(cfg: dict[str, Any]) -> bool:
    """Return True when response caching is configured to be active."""
    return bool(cfg["xspct_db_response_cache"].get("enabled", True))


def _ensure_response_cache(cfg: dict[str, Any]) -> bool:
    """Ensure the response cache exists; return True when enabled."""
    global _response_cache
    if not _response_cache_enabled(cfg):
        return False
    if _response_cache is None:
        _init_response_cache(cfg)
    return True


def get_response(key: tuple, cfg: dict[str, Any], s: str = "") -> bytes | None:
    """Return cached response bytes for *key*, or ``None`` on miss/disabled."""
    if not _ensure_response_cache(cfg):
        return None
    result = _response_cache.get(key)  # type: ignore[union-attr]
    if result is not None and logger.isEnabledFor(logging.DEBUG):
        try:
            ttl_remaining = _response_cache._TTLCache__links[key].expires - _response_cache.timer()  # type: ignore[attr-defined]
        except Exception:
            ttl_remaining = -1
        logger.debug(
            "%s - response cache hit  key=%s  ttl_remaining=%.1fs",
            s,
            key[0] if key else "?",
            ttl_remaining,
        )
    return result


def set_response(key: tuple, body: bytes, cfg: dict[str, Any]) -> None:
    """Store *body* bytes under *key* in the response cache."""
    if not _ensure_response_cache(cfg):
        return
    _response_cache[key] = body  # type: ignore[index]


# ---------------------------------------------------------------------------
# Redis helpers
# ---------------------------------------------------------------------------


def set_connection(conn: Any) -> None:
    """Inject the Redis connection object (called from server startup)."""
    global connection
    connection = conn


async def ping_redis(cfg: dict[str, Any]) -> None:
    """Proactive Redis health check: PING and reset the circuit-breaker on success.

    Called periodically so a recovered Redis connection is re-enabled without
    waiting for the next real query to succeed.
    """
    if connection is None or not cfg["xspct_db_redis_cache"]["enabled"]:
        return
    try:
        await connection.ping()
        reset_errors()
    except Exception as exc:
        logger.warning("Redis health check failed: %s", exc)
        record_error(str(exc), cfg)


def reset_errors() -> None:
    """Reset circuit-breaker error counters after a successful Redis call."""
    global error_count, errors
    if error_count > 0:
        logger.info("Redis connection recovered, resetting error counter")
        error_count = 0
        errors = []


def record_error(message: str, cfg: dict[str, Any]) -> int:
    """Increment error counter; log once the threshold is exceeded."""
    global error_count, errors
    errors.append(message)
    error_count += 1
    if error_count > int(cfg["xspct_db_redis_cache"]["max_errors"]):
        logger.error("Redis error limit exceeded, caching disabled. Errors: %s", errors)
    return error_count


def is_enabled(cfg: dict[str, Any]) -> bool:
    """Return ``True`` when Redis caching is active and below the error threshold."""
    if not cfg["xspct_db_redis_cache"]["enabled"]:
        return False
    if error_count > int(cfg["xspct_db_redis_cache"]["max_errors"]):
        return False
    return True


async def get_object(s: str, user: str, cfg: dict[str, Any]) -> dict[str, Any] | bool | None:
    """Two-level cache lookup: L1 (in-process TTLCache) → L2 (Redis).

    Returns:
        dict   – cached user data (positive hit)
        False  – negative cache hit (user confirmed absent)
        None   – cache miss on both layers
    """
    result, _source = await get_object_with_source(s, user, cfg)
    return result


async def get_object_with_source(s: str, user: str, cfg: dict[str, Any]) -> tuple[dict[str, Any] | bool | None, str]:
    """Return ``(value, source)`` for a two-level cache lookup.

    ``source`` is one of ``"local"``, ``"redis"``, ``"redis-negative"``,
    or ``"miss"``.
    """
    # --- L1 lookup ---
    if _ensure_l1(cfg):
        canonical = _local_aliases.get(user)  # type: ignore[union-attr]
        if canonical is not None:
            user_obj = _local_users.get(canonical)  # type: ignore[union-attr]
            if user_obj is not None:
                if logger.isEnabledFor(logging.DEBUG):
                    try:
                        ttl_remaining = _local_users._TTLCache__links[canonical].expires - _local_users.timer()  # type: ignore[attr-defined]
                    except Exception:
                        ttl_remaining = -1
                    logger.debug(
                        "%s - L1 cache hit (positive) for %s → canonical=%s  ttl_remaining=%.1fs",
                        s,
                        user,
                        canonical,
                        ttl_remaining,
                    )
                return user_obj, "local"

        if user in _local_negative:  # type: ignore[operator]
            if logger.isEnabledFor(logging.DEBUG):
                try:
                    ttl_remaining = _local_negative._TTLCache__links[user].expires - _local_negative.timer()  # type: ignore[attr-defined]
                except Exception:
                    ttl_remaining = -1
                logger.debug(
                    "%s - L1 cache hit (negative) for %s  ttl_remaining=%.1fs",
                    s,
                    user,
                    ttl_remaining,
                )
            return False, "local"

    # --- L2 (Redis) lookup ---
    if not is_enabled(cfg):
        return None, "miss"

    redis_cfg = cfg["xspct_db_redis_cache"]
    key_alias = redis_cfg["prefix_alias"] + user

    try:
        alias = await connection.get(key_alias)
    except Exception as exc:
        logger.error("%s - error getting redis alias %s: %s", s, key_alias, exc)
        record_error(str(exc), cfg)
        return None, "miss"

    reset_errors()

    if isinstance(alias, str):
        key_obj = redis_cfg["prefix_user"] + alias
        try:
            raw = await connection.get(key_obj)
        except Exception as exc:
            logger.error("%s - error getting redis object %s: %s", s, key_obj, exc)
            record_error(str(exc), cfg)
            return None, "miss"
        if isinstance(raw, str):
            user_obj = json.loads(raw)
            # Backfill L1 from Redis result.
            if _ensure_l1(cfg):
                _local_aliases[user] = alias  # type: ignore[index]
                _local_users[alias] = user_obj  # type: ignore[index]
            if logger.isEnabledFor(logging.DEBUG):
                try:
                    ttl_remaining = await connection.ttl(key_obj)
                except Exception:
                    ttl_remaining = -1
                logger.debug(
                    "%s - L2 Redis cache hit (positive) for %s → alias=%s  ttl_remaining=%ds",
                    s,
                    user,
                    alias,
                    ttl_remaining,
                )
            return user_obj, "redis"
    else:
        key_neg = redis_cfg["prefix_negative_alias"] + user
        try:
            neg = await connection.get(key_neg)
        except Exception as exc:
            logger.error("%s - error getting redis neg alias %s: %s", s, key_neg, exc)
            record_error(str(exc), cfg)
            return None, "miss"
        if isinstance(neg, str):
            # Backfill L1 negative.
            if _ensure_l1(cfg):
                _local_negative[user] = True  # type: ignore[index]
            if logger.isEnabledFor(logging.DEBUG):
                try:
                    ttl_remaining = await connection.ttl(key_neg)
                except Exception:
                    ttl_remaining = -1
                logger.debug(
                    "%s - L2 Redis cache hit (negative) for %s  ttl_remaining=%ds",
                    s,
                    user,
                    ttl_remaining,
                )
            return False, "redis-negative"

    return None, "miss"


async def set_cache(
    s: str,
    userdata: dict[str, Any],
    user_to_pkey: dict[str, Any],
    cfg: dict[str, Any],
) -> None:
    """Write user data, aliases, and email mappings to both L1 and L2 (Redis)."""
    # --- L1 write ---
    if _ensure_l1(cfg):
        for k, v in userdata.get("users", {}).items():
            _local_users[k] = v  # type: ignore[index]
            for av in v.get("aliases", []):
                _local_aliases[av] = k  # type: ignore[index]
            for mv in v.get("mail", []):
                _local_aliases[mv] = k  # type: ignore[index]
        for k, v in user_to_pkey.items():
            _local_aliases[k] = v  # type: ignore[index]

    # --- L2 write ---
    if not is_enabled(cfg):
        return

    redis_cfg = cfg["xspct_db_redis_cache"]
    expire = redis_cfg["expire"]
    prefix_user = redis_cfg["prefix_user"]
    prefix_alias = redis_cfg["prefix_alias"]

    try:
        async with connection.pipeline(transaction=False) as pipe:
            for k, v in userdata.get("users", {}).items():
                pipe.setex(prefix_user + k, expire, json.dumps(v))
                for av in v.get("aliases", []):
                    pipe.setex(prefix_alias + av, expire, k)
                for mv in v.get("mail", []):
                    pipe.setex(prefix_alias + mv, expire, k)
            for k, v in user_to_pkey.items():
                pipe.setex(prefix_alias + k, expire, v)
            await pipe.execute()
    except Exception as exc:
        logger.error("%s - error in set_cache pipeline: %s", s, exc)
        record_error(str(exc), cfg)


async def set_negative_cache(s: str, neg_users: list[str], cfg: dict[str, Any]) -> None:
    """Mark a list of users as absent in both L1 and L2."""
    if not neg_users:
        return

    # --- L1 write ---
    if _ensure_l1(cfg):
        for user in neg_users:
            _local_negative[user] = True  # type: ignore[index]

    # --- L2 write ---
    if not is_enabled(cfg):
        return

    redis_cfg = cfg["xspct_db_redis_cache"]
    prefix = redis_cfg["prefix_negative_alias"]
    expire = redis_cfg["expire_negative"]

    try:
        async with connection.pipeline(transaction=False) as pipe:
            for user in neg_users:
                pipe.setex(prefix + user, expire, 1)
            await pipe.execute()
    except Exception as exc:
        logger.error("%s - error in set_negative_cache pipeline: %s", s, exc)
        record_error(str(exc), cfg)
