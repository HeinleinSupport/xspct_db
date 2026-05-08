# SPDX-License-Identifier: EUPL-1.2
# SPDX-FileCopyrightText: 2024 Carsten Rosenberg <c.rosenberg@heinlein-support.de>

"""Background asyncio task that periodically measures event-loop lag."""

from __future__ import annotations

import asyncio
import logging
import time

from aiohttp import web

from .registry import gauge

_log = logging.getLogger(__name__)

_LAG_GAUGE = gauge("event_loop_lag_seconds", "Event loop lag in seconds")
_WARN_THRESHOLD = 0.1  # 100 ms


async def _measure_loop_lag(interval: float = 1.0) -> None:
    """Measure event-loop lag in a tight loop and update the gauge."""
    while True:
        t0 = time.perf_counter()
        await asyncio.sleep(interval)
        lag = max(0.0, time.perf_counter() - t0 - interval)
        _LAG_GAUGE.set(lag)
        if lag > _WARN_THRESHOLD:
            _log.warning("Event loop lag: %.3fs", lag)


async def start_loop_lag_task(app: web.Application) -> None:
    """Startup hook: create the loop-lag background task."""
    app["_loop_lag_task"] = asyncio.create_task(_measure_loop_lag())


async def stop_loop_lag_task(app: web.Application) -> None:
    """Cleanup hook: cancel and await the loop-lag task."""
    task: asyncio.Task | None = app.pop("_loop_lag_task", None)
    if task:
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
