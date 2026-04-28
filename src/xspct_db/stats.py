# SPDX-License-Identifier: EUPL-1.2
# SPDX-FileCopyrightText: 2026 Carsten Rosenberg <c.rosenberg@heinlein-support.de>

"""Runtime statistics counters and periodic logging / Prometheus export."""

from __future__ import annotations

import asyncio
import logging
from typing import Any

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Shared stats dict – mutated in-place by route handlers and backends.
# ---------------------------------------------------------------------------

stats: dict[str, Any] = {
    "requests_total": 0,
    "requests_known": 0,
    "requests_unknown": 0,
    "local_cache_hits": 0,
    "local_cache_misses": 0,
    "response_cache_hits": 0,
    "response_cache_misses": 0,
    "redis_hits": 0,
    "redis_misses": 0,
    "redis_negative_hits": 0,
    "foreground_overloaded": 0,
    "requests_timeout": 0,
    "background_completed": 0,
    "background_rejected": 0,
    "background_errors": 0,
    # per-query timing: {qk: {count, time_total, time_min, time_max}}
    "queries": {},
    # per-pool connection samples: {pool_key: {min, max, sum, count, limit}}
    "pool_connections": {},
}


def reset() -> None:
    """Reset all counters and per-interval samples (useful in tests)."""
    stats["requests_total"] = 0
    stats["requests_known"] = 0
    stats["requests_unknown"] = 0
    stats["local_cache_hits"] = 0
    stats["local_cache_misses"] = 0
    stats["response_cache_hits"] = 0
    stats["response_cache_misses"] = 0
    stats["redis_hits"] = 0
    stats["redis_misses"] = 0
    stats["redis_negative_hits"] = 0
    stats["foreground_overloaded"] = 0
    stats["requests_timeout"] = 0
    stats["background_completed"] = 0
    stats["background_rejected"] = 0
    stats["background_errors"] = 0
    stats["queries"].clear()
    stats["pool_connections"].clear()


def update_query_stats(qk: str, elapsed: float) -> None:
    """Record one query execution time for *qk*."""
    entry = stats["queries"].setdefault(
        qk, {"count": 0, "time_total": 0.0, "time_min": float("inf"), "time_max": 0.0}
    )
    entry["count"] += 1
    entry["time_total"] += elapsed
    if elapsed < entry["time_min"]:
        entry["time_min"] = elapsed
    if elapsed > entry["time_max"]:
        entry["time_max"] = elapsed


def sample_pool_connections(cfg: dict[str, Any]) -> None:
    """Sample open connection counts for all pools and accumulate into ``stats['pool_connections']``."""

    def _record(key: str, open_conns: int, limit: int | None = None) -> None:
        if open_conns < 0:
            return
        s = stats["pool_connections"].setdefault(
            key, {"min": float("inf"), "max": 0, "sum": 0.0, "count": 0, "limit": None}
        )
        if s["limit"] is None and limit is not None:
            s["limit"] = limit
        if open_conns < s["min"]:
            s["min"] = open_conns
        if open_conns > s["max"]:
            s["max"] = open_conns
        s["sum"] += open_conns
        s["count"] += 1

    # Redis
    from xspct_db import cache  # local import to avoid circular deps
    if cache.connection is not None:
        pool = cache.connection.connection_pool
        try:
            rc = pool._created_connections
        except AttributeError:
            try:
                rc = len(pool._in_use_connections) + len(pool._available_connections)
            except (AttributeError, TypeError):
                rc = -1
        redis_limit = int(cfg["xspct_db_redis_cache"].get("max_connections", -1))
        _record("redis", rc, limit=redis_limit if redis_limit > 0 else None)

    # LDAP
    if cfg.get("xspct_db_types_enabled", {}).get("ldap"):
        import sys as _sys
        _ldap_mod = _sys.modules.get("xspct_db.backends.ldap_backend")
        if _ldap_mod is not None:
            for qk, pool in _ldap_mod._pools.items():
                try:
                    _record(qk, pool.size, limit=getattr(pool, "maxconn", None))
                except Exception:
                    pass

    # MySQL
    if cfg.get("xspct_db_types_enabled", {}).get("mysql"):
        import sys as _sys
        _mysql_mod = _sys.modules.get("xspct_db.backends.mysql_backend")
        if _mysql_mod is not None:
            for qk, pool in _mysql_mod._pools.items():
                try:
                    _record(qk, pool.size, limit=getattr(pool, "maxsize", None))
                except Exception:
                    pass


def log_stats(cfg: dict[str, Any]) -> None:
    """Emit a stats summary at INFO level."""
    logger.info(
        "STATS requests_total=%d requests_known=%d requests_unknown=%d",
        stats["requests_total"],
        stats["requests_known"],
        stats["requests_unknown"],
    )

    # L1 local cache hit/miss counters
    from xspct_db import cache  # local import to avoid circular deps
    rcfg = cfg.get("xspct_db_local_cache", {})
    if rcfg.get("enabled", False):
        l1_hits = stats["local_cache_hits"]
        l1_misses = stats["local_cache_misses"]
        l1_total = l1_hits + l1_misses
        l1_rate = (l1_hits / l1_total * 100) if l1_total > 0 else 0.0
        logger.info(
            "STATS local_cache_hits=%d local_cache_misses=%d local_hit_rate=%.1f%%",
            l1_hits, l1_misses, l1_rate,
        )

    # Redis hit/miss counters
    if cache.connection is not None:
        redis_hits = stats["redis_hits"]
        redis_misses = stats["redis_misses"]
        redis_neg_hits = stats["redis_negative_hits"]
        total_lookups = redis_hits + redis_misses + redis_neg_hits
        hit_rate = (redis_hits / total_lookups * 100) if total_lookups > 0 else 0.0
        logger.info(
            "STATS redis_hits=%d redis_misses=%d redis_neg_hits=%d redis_hit_rate=%.1f%%",
            redis_hits, redis_misses, redis_neg_hits, hit_rate,
        )

    # Connection snapshot line
    conn_parts: list[str] = []
    if cache.connection is not None:
        pool = cache.connection.connection_pool
        try:
            rc = pool._created_connections
        except AttributeError:
            try:
                rc = len(pool._in_use_connections) + len(pool._available_connections)
            except (AttributeError, TypeError):
                rc = -1
        conn_parts.append(f"redis_connections={rc}")

    if cfg.get("xspct_db_types_enabled", {}).get("ldap"):
        try:
            from xspct_db.backends import ldap_backend
            if ldap_backend._pools:
                ldap_info = {}
                for qk, pool in ldap_backend._pools.items():
                    try:
                        ldap_info[qk] = pool.size
                    except Exception:
                        ldap_info[qk] = -1
                conn_parts.append(f"ldap_pools={ldap_info}")
        except ImportError:
            pass

    if cfg.get("xspct_db_types_enabled", {}).get("mysql"):
        try:
            from xspct_db.backends import mysql_backend
            if mysql_backend._pools:
                mysql_info = {}
                for qk, pool in mysql_backend._pools.items():
                    try:
                        mysql_info[qk] = {"size": pool.size, "free": pool.freesize}
                    except Exception:
                        mysql_info[qk] = -1
                conn_parts.append(f"mysql_pools={mysql_info}")
        except ImportError:
            pass

    if conn_parts:
        logger.info("STATS %s", " ".join(conn_parts))

    # Per-pool connection min/avg/max
    for pk, ps in stats["pool_connections"].items():
        if ps["count"] > 0:
            avg = ps["sum"] / ps["count"]
            pmin = ps["min"] if ps["min"] != float("inf") else 0
            limit = ps.get("limit")
            hint = (
                f" limit={limit} LIMIT_REACHED"
                if limit is not None and ps["max"] >= limit
                else ""
            )
            logger.info(
                "STATS pool[%s] conns min=%d avg=%.1f max=%d%s",
                pk, pmin, avg, ps["max"], hint,
            )
    stats["pool_connections"].clear()

    # Per-query timing
    for qk, qs in stats["queries"].items():
        if qs["count"] > 0:
            avg = qs["time_total"] / qs["count"]
            qmin = qs["time_min"] if qs["time_min"] != float("inf") else 0.0
            logger.info(
                "STATS query[%s] count=%d avg=%.5fs min=%.5fs max=%.5fs",
                qk, qs["count"], avg, qmin, qs["time_max"],
            )


async def log_stats_periodically(cfg: dict[str, Any]) -> None:
    """Background task: sample pools and call :func:`log_stats` on a fixed interval."""
    interval = float(cfg.get("xspct_db_stats_interval", 60))
    sample_interval = float(cfg.get("xspct_db_stats_sample_interval", 10))
    elapsed = 0.0
    while True:
        await asyncio.sleep(sample_interval)
        elapsed += sample_interval
        sample_pool_connections(cfg)
        if elapsed >= interval:
            log_stats(cfg)
            elapsed = 0.0
