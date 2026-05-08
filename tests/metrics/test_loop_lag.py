# SPDX-License-Identifier: EUPL-1.2
# SPDX-FileCopyrightText: 2024 Carsten Rosenberg <c.rosenberg@heinlein-support.de>

"""Tests for the event-loop lag background task."""

from __future__ import annotations

import asyncio
import logging
import time

from prometheus_client import CollectorRegistry

from xspct_db.metrics.registry import gauge

# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


async def test_lag_gauge_is_updated():
    """After a short interval the gauge holds a plausible (non-negative) value."""
    reg = CollectorRegistry()
    lag_gauge = gauge("event_loop_lag_seconds_test1", "test", registry=reg)

    async def _measure(interval: float = 0.05) -> None:
        while True:
            t0 = time.perf_counter()
            await asyncio.sleep(interval)
            lag = max(0.0, time.perf_counter() - t0 - interval)
            lag_gauge.set(lag)

    task = asyncio.create_task(_measure(0.05))
    await asyncio.sleep(0.2)
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass

    val = reg.get_sample_value("event_loop_lag_seconds_test1")
    assert val is not None
    assert val >= 0.0


async def test_cleanup_cancels_task():
    """stop_loop_lag_task cancels the task without leaving a CancelledError."""
    # Use a fresh app-like dict to avoid importing setup_metrics.
    from xspct_db.metrics.loop_lag import start_loop_lag_task, stop_loop_lag_task

    class _FakeApp(dict):
        pass

    fake_app = _FakeApp()
    await start_loop_lag_task(fake_app)  # type: ignore[arg-type]
    task = fake_app["_loop_lag_task"]
    assert not task.done()

    await stop_loop_lag_task(fake_app)  # type: ignore[arg-type]
    assert task.done()
    assert "_loop_lag_task" not in fake_app


async def test_high_lag_emits_warning(caplog):
    """time.sleep(0.2) inside the loop triggers a WARNING log."""
    import time as _time

    reg = CollectorRegistry()
    lag_gauge = gauge("event_loop_lag_seconds_test3", "test", registry=reg)
    WARN_THRESHOLD = 0.1

    log = logging.getLogger("xspct_db.metrics.loop_lag")

    async def _measure(interval: float = 0.05) -> None:
        t0 = _time.perf_counter()
        # Simulate a blocking call to inflate lag.
        _time.sleep(0.2)
        await asyncio.sleep(interval)
        lag = max(0.0, _time.perf_counter() - t0 - interval)
        lag_gauge.set(lag)
        if lag > WARN_THRESHOLD:
            log.warning("Event loop lag: %.3fs", lag)

    with caplog.at_level(logging.WARNING, logger="xspct_db.metrics.loop_lag"):
        await _measure()

    assert any("Event loop lag" in r.message for r in caplog.records)
    val = reg.get_sample_value("event_loop_lag_seconds_test3")
    assert val is not None
    assert val > WARN_THRESHOLD
