# SPDX-License-Identifier: EUPL-1.2
# SPDX-FileCopyrightText: 2026 Carsten Rosenberg <c.rosenberg@heinlein-support.de>

"""Per-span latency tracking, rolling percentiles, and anomaly detection.

The module provides a very cheap instrumentation primitive that can be left
enabled on a high-traffic production server:

* :func:`record` / :class:`span` add one timing sample for a named *span*
  (``redis.get``, ``backend.ldap_users``, ``queue.fg_wait``, …).
* Every span keeps a bounded rolling window of samples (a
  :class:`collections.deque`) from which the **median** and the p90 / p99
  percentiles are derived.  The baseline is recomputed at most every
  ``baseline_interval`` seconds, so the per-sample cost stays O(1).
* A sample that exceeds ``median * anomaly_factor`` (and an absolute floor)
  is counted as an *anomaly* and logged at WARNING level — rate-limited to one
  line per span per ``log_interval`` seconds so a backend outage cannot flood
  the log.
* Spans recorded during one request are also collected per request (via a
  :class:`~contextvars.ContextVar`, so concurrent requests never mix).  When
  the total request time is anomalous, a single WARNING line prints the full
  breakdown — which part of the request was actually slow.

Nothing here touches the event loop and no locks are needed: CPython executes
each ``record()`` without an await point, so the counters are consistent.

The tracker is a **process-wide singleton**, like :mod:`xspct_db.cache`,
:mod:`xspct_db.prefilter`, and :mod:`xspct_db.stats` — the spans it records
originate in layers (the cache, the backends) that have no reference to an
:class:`~aiohttp.web.Application`.  A process therefore serves one application:
:func:`configure` replaces the tuning globals and drops all collected samples,
so calling it again (by building a second app) restarts tracking from scratch.
:func:`set_observer` is likewise global; :mod:`xspct_db.metrics` clears it when
an application shuts down so a stale histogram can never outlive its app.

Configuration (``xspct_db_latency``)::

    xspct_db_latency:
      enabled: true              # master switch
      window: 2048               # rolling samples kept per span
      min_samples: 50            # samples needed before anomalies are flagged
      anomaly_factor: 4.0        # sample > median * factor  → anomaly
      anomaly_floor: 0.05        # ignore anomalies faster than this (seconds)
      baseline_interval: 5.0     # min seconds between percentile recomputes
      log_interval: 10.0         # min seconds between anomaly logs per span
      slow_request_factor: 4.0   # request total > median * factor → breakdown log
      slow_request_floor: 0.1    # ignore slow-request logs below this (seconds)
"""

from __future__ import annotations

import contextvars
import logging
from collections import deque
from time import monotonic, perf_counter
from typing import Any, Callable

logger = logging.getLogger(__name__)

# Histogram buckets tuned for this workload: cache hits are tens of
# microseconds, LDAP/MySQL round-trips single-digit milliseconds.
LATENCY_BUCKETS: tuple[float, ...] = (
    0.0005,
    0.001,
    0.0025,
    0.005,
    0.01,
    0.025,
    0.05,
    0.1,
    0.25,
    0.5,
    1.0,
    2.5,
    5.0,
    10.0,
)

# ---------------------------------------------------------------------------
# Tunables – set once by configure(); kept as module globals so the hot path
# does not repeatedly walk the config dict.
# ---------------------------------------------------------------------------

_enabled: bool = False
_window: int = 2048
_min_samples: int = 50
_anomaly_factor: float = 4.0
_anomaly_floor: float = 0.05
_baseline_interval: float = 5.0
_log_interval: float = 10.0
_slow_request_factor: float = 4.0
_slow_request_floor: float = 0.1

# Optional Prometheus sink installed by xspct_db.metrics.setup_metrics().
_observer: Callable[[str, float], None] | None = None

# Per-request span accumulator: {span_name: [total_seconds, count]}
_spans_var: contextvars.ContextVar[dict[str, list[float]] | None] = contextvars.ContextVar(
    "latency_spans",
    default=None,
)

# Per-request log tag (``<sessid-rspamdid>``) so the slow-request line can be
# correlated with the request's other log lines.
_tag_var: contextvars.ContextVar[str] = contextvars.ContextVar("latency_tag", default="")

# Canonical route template → short endpoint name used as the span suffix.
_ENDPOINT_NAMES: dict[str, str] = {
    "/v1/query/{user}": "query",
    "/v1/query/{user}/": "query",
    "/query/v1/{user}": "query",
    "/query/v1/{user}/": "query",
    "/v1/query-json": "query-json",
    "/v1/query-json/": "query-json",
    "/query-json/v1": "query-json",
    "/query-json/v1/": "query-json",
    "/v1/rspamd-settings": "rspamd-settings",
    "/v1/rspamd-settings/": "rspamd-settings",
    "/rspamd-settings/v1": "rspamd-settings",
    "/rspamd-settings/v1/": "rspamd-settings",
}


class SpanStats:
    """Rolling-window statistics for a single named span."""

    __slots__ = (
        "name",
        "samples",
        "count",
        "total",
        "max",
        "median",
        "p90",
        "p99",
        "anomalies",
        "_baseline_at",
        "_logged_at",
    )

    def __init__(self, name: str) -> None:
        self.name = name
        self.samples: deque[float] = deque(maxlen=_window)
        self.count: int = 0
        self.total: float = 0.0
        self.max: float = 0.0
        self.median: float = 0.0
        self.p90: float = 0.0
        self.p99: float = 0.0
        self.anomalies: int = 0
        self._baseline_at: float = 0.0
        self._logged_at: float = 0.0

    # -- baseline ---------------------------------------------------------

    def recompute(self) -> None:
        """Recompute median / p90 / p99 from the current rolling window."""
        n = len(self.samples)
        if n == 0:
            return
        ordered = sorted(self.samples)
        self.median = ordered[n // 2]
        self.p90 = ordered[min(n - 1, int(n * 0.90))]
        self.p99 = ordered[min(n - 1, int(n * 0.99))]
        self._baseline_at = monotonic()

    def observe(self, value: float) -> bool:
        """Add one sample; return ``True`` when it is an anomaly."""
        self.samples.append(value)
        self.count += 1
        self.total += value
        if value > self.max:
            self.max = value

        now = monotonic()
        if now - self._baseline_at >= _baseline_interval and len(self.samples) >= _min_samples:
            self.recompute()

        median = self.median
        if median <= 0.0 or len(self.samples) < _min_samples:
            return False
        if value < _anomaly_floor or value <= median * _anomaly_factor:
            return False

        self.anomalies += 1
        if now - self._logged_at >= _log_interval:
            self._logged_at = now
            logger.warning(
                "LATENCY anomaly span=%s value=%.5fs median=%.5fs p99=%.5fs factor=%.1fx (anomalies=%d/%d)",
                self.name,
                value,
                median,
                self.p99,
                value / median,
                self.anomalies,
                self.count,
            )
        return True

    def snapshot(self) -> dict[str, float]:
        """Return a plain-dict view for logging / Prometheus export."""
        return {
            "count": self.count,
            "avg": (self.total / self.count) if self.count else 0.0,
            "median": self.median,
            "p90": self.p90,
            "p99": self.p99,
            "max": self.max,
            "anomalies": self.anomalies,
            "window": len(self.samples),
        }


# span name → SpanStats
_stats: dict[str, SpanStats] = {}


# ---------------------------------------------------------------------------
# Lifecycle
# ---------------------------------------------------------------------------


def configure(cfg: dict[str, Any]) -> None:
    """Apply the ``xspct_db_latency`` config block; called at server startup."""
    global _enabled, _window, _min_samples, _anomaly_factor, _anomaly_floor
    global _baseline_interval, _log_interval, _slow_request_factor, _slow_request_floor

    lcfg: dict[str, Any] = cfg.get("xspct_db_latency", {}) or {}
    _enabled = bool(lcfg.get("enabled", True))
    _window = max(16, int(lcfg.get("window", 2048)))
    _min_samples = max(1, int(lcfg.get("min_samples", 50)))
    _anomaly_factor = float(lcfg.get("anomaly_factor", 4.0))
    _anomaly_floor = float(lcfg.get("anomaly_floor", 0.05))
    _baseline_interval = float(lcfg.get("baseline_interval", 5.0))
    _log_interval = float(lcfg.get("log_interval", 10.0))
    _slow_request_factor = float(lcfg.get("slow_request_factor", 4.0))
    _slow_request_floor = float(lcfg.get("slow_request_floor", 0.1))
    reset()
    logger.info(
        "latency tracing enabled=%s window=%d min_samples=%d anomaly=%.1fx/%.3fs slow_request=%.1fx/%.3fs",
        _enabled,
        _window,
        _min_samples,
        _anomaly_factor,
        _anomaly_floor,
        _slow_request_factor,
        _slow_request_floor,
    )


def set_observer(fn: Callable[[str, float], None] | None) -> None:
    """Install a sink that receives every ``(span, seconds)`` sample.

    Used by :mod:`xspct_db.metrics` to feed a Prometheus histogram without
    making this module depend on ``prometheus_client``.
    """
    global _observer
    _observer = fn


def enabled() -> bool:
    """Return ``True`` when latency tracing is active."""
    return _enabled


def reset() -> None:
    """Drop all collected samples (used by tests and on reconfiguration)."""
    _stats.clear()
    _spans_var.set(None)
    _tag_var.set("")


def snapshot() -> dict[str, dict[str, float]]:
    """Return ``{span: {count, avg, median, p90, p99, max, anomalies}}``."""
    return {name: st.snapshot() for name, st in _stats.items()}


# ---------------------------------------------------------------------------
# Recording
# ---------------------------------------------------------------------------


def record(name: str, elapsed: float) -> None:
    """Record one timing sample of *elapsed* seconds for span *name*."""
    if not _enabled:
        return

    spans = _spans_var.get()
    if spans is not None:
        entry = spans.get(name)
        if entry is None:
            spans[name] = [elapsed, 1]
        else:
            entry[0] += elapsed
            entry[1] += 1

    st = _stats.get(name)
    if st is None:
        st = _stats[name] = SpanStats(name)
    st.observe(elapsed)

    if _observer is not None:
        _observer(name, elapsed)


class span:
    """Context manager timing a block of code as span *name*.

    Works both synchronously and with ``async with``::

        with latency.span("cache.l1"):
            ...
        async with latency.span("redis.get"):
            ...
    """

    __slots__ = ("name", "_t0")

    def __init__(self, name: str) -> None:
        self.name = name
        self._t0 = 0.0

    def __enter__(self) -> "span":
        self._t0 = perf_counter()
        return self

    def __exit__(self, *exc: Any) -> bool:
        record(self.name, perf_counter() - self._t0)
        return False

    async def __aenter__(self) -> "span":
        self._t0 = perf_counter()
        return self

    async def __aexit__(self, *exc: Any) -> bool:
        record(self.name, perf_counter() - self._t0)
        return False


# ---------------------------------------------------------------------------
# Per-request lifecycle
# ---------------------------------------------------------------------------


def start_request() -> float:
    """Begin per-request span collection; returns the start timestamp."""
    if _enabled:
        _spans_var.set({})
        _tag_var.set("")
    return perf_counter()


def set_tag(s: str) -> None:
    """Attach the request's log tag so slow-request lines can be correlated."""
    if _enabled:
        _tag_var.set(s)


def spans() -> dict[str, list[float]] | None:
    """Return the current request's span map (``None`` when not collecting)."""
    return _spans_var.get()


def _format_breakdown(collected: dict[str, list[float]]) -> str:
    """Render ``{span: [total, count]}`` as ``name=0.0123s(2)`` pairs."""
    return " ".join(
        f"{name}={total:.5f}s({count})"
        for name, (total, count) in sorted(collected.items(), key=lambda kv: kv[1][0], reverse=True)
    )


def finish_request(endpoint: str, t0: float) -> float:
    """Close out a request: record the total span and log slow outliers.

    *endpoint* is a short route identifier (``query``, ``query-json``,
    ``rspamd-settings``).  Returns the elapsed wall time in seconds.
    """
    elapsed = perf_counter() - t0
    if not _enabled:
        return elapsed

    name = "request." + endpoint
    record(name, elapsed)

    st = _stats.get(name)
    collected = _spans_var.get()
    _spans_var.set(None)

    if st is None or collected is None:
        return elapsed
    median = st.median
    if (
        median > 0.0
        and len(st.samples) >= _min_samples
        and elapsed >= _slow_request_floor
        and elapsed > median * _slow_request_factor
    ):
        logger.warning(
            "%s SLOW %s total=%.5fs median=%.5fs (%.1fx) breakdown: %s",
            _tag_var.get(),
            endpoint,
            elapsed,
            median,
            elapsed / median,
            _format_breakdown(collected),
        )
    return elapsed


# ---------------------------------------------------------------------------
# Periodic logging (called from stats.log_stats)
# ---------------------------------------------------------------------------


def log_summary() -> None:
    """Emit one INFO line per span with the current rolling percentiles."""
    if not _enabled:
        return
    for name, st in sorted(_stats.items()):
        if st.count == 0:
            continue
        # Make sure the printed baseline reflects the newest samples.
        st.recompute()
        logger.info(
            "STATS latency[%s] n=%d avg=%.5fs median=%.5fs p90=%.5fs p99=%.5fs max=%.5fs anomalies=%d",
            name,
            st.count,
            st.total / st.count,
            st.median,
            st.p90,
            st.p99,
            st.max,
            st.anomalies,
        )


# ---------------------------------------------------------------------------
# aiohttp middleware
# ---------------------------------------------------------------------------


def make_latency_middleware() -> Any:
    """Return a middleware that brackets every query request with span collection.

    Non-query routes (health, ping, /metrics, the OpenAPI spec) are passed
    through untouched so they neither pollute the percentiles nor pay the
    (small) instrumentation cost.
    """
    from aiohttp import web

    @web.middleware
    async def _latency_middleware(request: "web.Request", handler: Any) -> Any:
        if not _enabled:
            return await handler(request)

        try:
            route = request.match_info.route.resource.canonical
        except AttributeError:
            route = ""
        endpoint = _ENDPOINT_NAMES.get(route or "")
        if endpoint is None:
            return await handler(request)

        t0 = start_request()
        try:
            return await handler(request)
        finally:
            finish_request(endpoint, t0)

    return _latency_middleware
