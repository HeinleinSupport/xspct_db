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
    "redis_hits": 0,
    "redis_misses": 0,
    "redis_negative_hits": 0,
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
    stats["redis_hits"] = 0
    stats["redis_misses"] = 0
    stats["redis_negative_hits"] = 0
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


def log_stats() -> None:
    """Emit a stats summary at INFO level."""
    logger.info(
        "STATS requests_total=%d requests_known=%d requests_unknown=%d",
        stats["requests_total"],
        stats["requests_known"],
        stats["requests_unknown"],
    )
    for qk, qs in stats["queries"].items():
        if qs["count"] > 0:
            avg = qs["time_total"] / qs["count"]
            qmin = qs["time_min"] if qs["time_min"] != float("inf") else 0.0
            logger.info(
                "STATS query[%s] count=%d avg=%.5fs min=%.5fs max=%.5fs",
                qk,
                qs["count"],
                avg,
                qmin,
                qs["time_max"],
            )


async def log_stats_periodically(cfg: dict[str, Any]) -> None:
    """Background task: sample pools and call :func:`log_stats` on a fixed interval."""
    interval = float(cfg.get("xspct_db_stats_interval", 60))
    sample_interval = float(cfg.get("xspct_db_stats_sample_interval", 10))
    elapsed = 0.0
    while True:
        await asyncio.sleep(sample_interval)
        elapsed += sample_interval
        if elapsed >= interval:
            log_stats()
            elapsed = 0.0
