# SPDX-License-Identifier: EUPL-1.2
# SPDX-FileCopyrightText: 2026 Carsten Rosenberg <c.rosenberg@heinlein-support.de>

"""Redis cache layer with circuit-breaker logic."""

from __future__ import annotations

import json
import logging
from typing import Any

logger = logging.getLogger(__name__)

# Module-level state – injected / reset by server.py at startup.
connection: Any = None  # redis.asyncio.Redis instance or None
error_count: int = 0
errors: list[str] = []


def set_connection(conn: Any) -> None:
    """Inject the Redis connection object (called from server startup)."""
    global connection
    connection = conn


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


async def get_object(
    s: str, user: str, cfg: dict[str, Any]
) -> dict[str, Any] | bool | None:
    """Two-level cache lookup: alias → user key → user object.

    Returns:
        dict   – cached user data
        False  – negative cache hit (user confirmed absent)
        None   – cache miss
    """
    global connection

    if not is_enabled(cfg):
        return None

    redis_cfg = cfg["xspct_db_redis_cache"]
    key_alias = redis_cfg["prefix_alias"] + user

    try:
        alias = await connection.get(key_alias)
    except Exception as exc:
        logger.error("%s - error getting redis alias %s: %s", s, key_alias, exc)
        record_error(str(exc), cfg)
        return None

    reset_errors()

    if isinstance(alias, str):
        key_obj = redis_cfg["prefix_user"] + alias
        try:
            raw = await connection.get(key_obj)
        except Exception as exc:
            logger.error("%s - error getting redis object %s: %s", s, key_obj, exc)
            record_error(str(exc), cfg)
            return None
        if isinstance(raw, str):
            return json.loads(raw)
    else:
        key_neg = redis_cfg["prefix_negative_alias"] + user
        try:
            neg = await connection.get(key_neg)
        except Exception as exc:
            logger.error("%s - error getting redis neg alias %s: %s", s, key_neg, exc)
            record_error(str(exc), cfg)
            return None
        if isinstance(neg, str):
            return False

    return None


async def set_cache(
    s: str,
    userdata: dict[str, Any],
    user_to_pkey: dict[str, Any],
    cfg: dict[str, Any],
) -> None:
    """Pipeline-write user data, aliases, and email mappings to Redis."""
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


async def set_negative_cache(
    s: str, neg_users: list[str], cfg: dict[str, Any]
) -> None:
    """Mark a list of users as absent in the negative cache."""
    if not is_enabled(cfg) or not neg_users:
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
