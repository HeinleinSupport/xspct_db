# SPDX-License-Identifier: EUPL-1.2
# SPDX-FileCopyrightText: 2024 Carsten Rosenberg <c.rosenberg@heinlein-support.de>

"""Prometheus registry and metric-factory helpers.

All symbols in this module guard the ``prometheus_client`` import with a
try/except so that the rest of the package can be imported safely even when
``prometheus_client`` is not installed.  When the library is absent every
factory function returns a no-op stub instead of a real metric object.
"""

from __future__ import annotations

from typing import Any

DEFAULT_BUCKETS = (0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0, 10.0)

try:
    from prometheus_client import (
        CollectorRegistry,
        Counter,
        Gauge,
        Histogram,
        PlatformCollector,
        ProcessCollector,
    )
    from prometheus_client.metrics_core import (
        CounterMetricFamily,
        GaugeMetricFamily,
    )

    _HAS_PROMETHEUS = True
except ImportError:  # pragma: no cover
    _HAS_PROMETHEUS = False
    CollectorRegistry = None  # type: ignore[assignment,misc]
    Counter = None  # type: ignore[assignment,misc]
    Gauge = None  # type: ignore[assignment,misc]
    Histogram = None  # type: ignore[assignment,misc]
    PlatformCollector = None  # type: ignore[assignment,misc]
    ProcessCollector = None  # type: ignore[assignment,misc]
    CounterMetricFamily = None  # type: ignore[assignment,misc]
    GaugeMetricFamily = None  # type: ignore[assignment,misc]


# ---------------------------------------------------------------------------
# No-op stubs
# ---------------------------------------------------------------------------


class _NoOpLabels:
    """Returned by _NoOpMetric.labels() — also a no-op."""

    def inc(self, amount: float = 1) -> None:  # noqa: ARG002
        pass

    def set(self, value: float) -> None:  # noqa: ARG002
        pass

    def observe(self, amount: float) -> None:  # noqa: ARG002
        pass


class _NoOpMetric:
    """Silent no-op metric used when prometheus_client is not installed."""

    def inc(self, amount: float = 1) -> None:  # noqa: ARG002
        pass

    def set(self, value: float) -> None:  # noqa: ARG002
        pass

    def observe(self, amount: float) -> None:  # noqa: ARG002
        pass

    def labels(self, **kwargs: Any) -> "_NoOpLabels":  # noqa: ARG002
        return _NoOpLabels()


# ---------------------------------------------------------------------------
# Global registry (None when prometheus_client absent)
# ---------------------------------------------------------------------------

if _HAS_PROMETHEUS:
    REGISTRY: Any = CollectorRegistry()
    ProcessCollector(registry=REGISTRY)
    PlatformCollector(registry=REGISTRY)
else:  # pragma: no cover
    REGISTRY = None


# ---------------------------------------------------------------------------
# Idempotent metric factories
# ---------------------------------------------------------------------------


def _get_existing(registry: Any, name: str) -> Any:
    """Return an already-registered collector by name, or None."""
    try:
        return registry._names_to_collectors.get(name)
    except AttributeError:
        return None


def counter(
    name: str,
    doc: str,
    labels: list[str] | None = None,
    registry: Any = None,
) -> Any:
    """Create (or return existing) Counter in *registry*.

    Returns a :class:`_NoOpMetric` when ``prometheus_client`` is not installed.
    """
    if not _HAS_PROMETHEUS:  # pragma: no cover
        return _NoOpMetric()
    reg = registry if registry is not None else REGISTRY
    existing = _get_existing(reg, name + "_total")
    if existing is not None:
        return existing
    try:
        return Counter(name, doc, labels or [], registry=reg)
    except ValueError:
        return _get_existing(reg, name + "_total") or _NoOpMetric()


def gauge(
    name: str,
    doc: str,
    labels: list[str] | None = None,
    registry: Any = None,
) -> Any:
    """Create (or return existing) Gauge in *registry*.

    Returns a :class:`_NoOpMetric` when ``prometheus_client`` is not installed.
    """
    if not _HAS_PROMETHEUS:  # pragma: no cover
        return _NoOpMetric()
    reg = registry if registry is not None else REGISTRY
    existing = _get_existing(reg, name)
    if existing is not None:
        return existing
    try:
        return Gauge(name, doc, labels or [], registry=reg)
    except ValueError:
        return _get_existing(reg, name) or _NoOpMetric()


def histogram(
    name: str,
    doc: str,
    labels: list[str] | None = None,
    buckets: tuple[float, ...] = DEFAULT_BUCKETS,
    registry: Any = None,
) -> Any:
    """Create (or return existing) Histogram in *registry*.

    Returns a :class:`_NoOpMetric` when ``prometheus_client`` is not installed.
    """
    if not _HAS_PROMETHEUS:  # pragma: no cover
        return _NoOpMetric()
    reg = registry if registry is not None else REGISTRY
    existing = _get_existing(reg, name + "_bucket")
    if existing is None:
        existing = _get_existing(reg, name + "_count")
    if existing is not None:
        return existing
    try:
        return Histogram(name, doc, labels or [], buckets=buckets, registry=reg)
    except ValueError:
        return _get_existing(reg, name + "_count") or _NoOpMetric()


# ---------------------------------------------------------------------------
# Custom collector: bridges stats.stats dict → Prometheus exposition
# ---------------------------------------------------------------------------


class _StatsCollector:
    """A prometheus_client Collector that reads xspct_db stats on demand."""

    def describe(self) -> list[Any]:
        # Return empty to avoid metric name conflicts; collect() is authoritative.
        return []

    def collect(self) -> list[Any]:  # type: ignore[return]
        if not _HAS_PROMETHEUS:  # pragma: no cover
            return []
        try:
            from xspct_db import stats as _stats
        except ImportError:  # pragma: no cover
            return []

        s = _stats.stats
        metrics: list[Any] = []

        _simple: list[tuple[str, str, str, str]] = [
            ("xspct_db_requests_total", "counter", "Total client requests", "requests_total"),
            ("xspct_db_requests_known_total", "counter", "Requests where user was found", "requests_known"),
            ("xspct_db_requests_unknown_total", "counter", "Requests where user was not found", "requests_unknown"),
            ("xspct_db_local_cache_hits_total", "counter", "Local in-process cache hits", "local_cache_hits"),
            ("xspct_db_local_cache_misses_total", "counter", "Local in-process cache misses", "local_cache_misses"),
            (
                "xspct_db_response_cache_hits_total",
                "counter",
                "Response cache hits",
                "response_cache_hits",
            ),
            (
                "xspct_db_response_cache_misses_total",
                "counter",
                "Response cache misses",
                "response_cache_misses",
            ),
            ("xspct_db_redis_hits_total", "counter", "Redis cache hits", "redis_hits"),
            ("xspct_db_redis_misses_total", "counter", "Redis cache misses", "redis_misses"),
            (
                "xspct_db_redis_negative_hits_total",
                "counter",
                "Redis negative cache hits",
                "redis_negative_hits",
            ),
            (
                "xspct_db_foreground_overloaded_total",
                "counter",
                "Foreground slot acquire failures",
                "foreground_overloaded",
            ),
            (
                "xspct_db_requests_timeout_total",
                "counter",
                "Requests that exceeded timeout",
                "requests_timeout",
            ),
            (
                "xspct_db_background_completed_total",
                "counter",
                "Background tasks completed",
                "background_completed",
            ),
            (
                "xspct_db_background_rejected_total",
                "counter",
                "Background tasks rejected",
                "background_rejected",
            ),
            (
                "xspct_db_background_errors_total",
                "counter",
                "Background tasks that raised errors",
                "background_errors",
            ),
            (
                "xspct_db_wildcard_domain_hits_total",
                "counter",
                "Wildcard domain fallback hits",
                "wildcard_domain_hits",
            ),
            (
                "xspct_db_wildcard_domain_misses_total",
                "counter",
                "Wildcard domain fallback misses",
                "wildcard_domain_misses",
            ),
            (
                "xspct_db_prefilter_domain_count",
                "gauge",
                "Current number of domains in the prefilter set",
                "prefilter_domain_count",
            ),
            (
                "xspct_db_prefilter_domain_hits_total",
                "counter",
                "Addresses allowed by domain filter",
                "prefilter_domain_hits",
            ),
            (
                "xspct_db_prefilter_domain_misses_total",
                "counter",
                "Addresses blocked by domain filter",
                "prefilter_domain_misses",
            ),
            (
                "xspct_db_prefilter_pattern_hits_total",
                "counter",
                "Addresses allowed by pattern filter",
                "prefilter_pattern_hits",
            ),
            (
                "xspct_db_prefilter_pattern_misses_total",
                "counter",
                "Addresses blocked by pattern filter",
                "prefilter_pattern_misses",
            ),
        ]

        for prom_name, mtype, doc, stat_key in _simple:
            val = s.get(stat_key, 0)
            if mtype == "counter":
                m = CounterMetricFamily(prom_name, doc)
                m.add_metric([], float(val))
            else:
                m = GaugeMetricFamily(prom_name, doc)
                m.add_metric([], float(val))
            metrics.append(m)

        # Per-query timing
        queries = s.get("queries", {})
        if queries:
            count_m = CounterMetricFamily(
                "xspct_db_query_requests_total",
                "Queries executed per backend",
                labels=["query"],
            )
            duration_m = CounterMetricFamily(
                "xspct_db_query_duration_seconds_total",
                "Accumulated query time per backend",
                labels=["query"],
            )
            for qk, qs in queries.items():
                count_m.add_metric([qk], float(qs.get("count", 0)))
                duration_m.add_metric([qk], float(qs.get("time_total", 0.0)))
            metrics.append(count_m)
            metrics.append(duration_m)

        return metrics


# Module-level singleton; registered when setup_metrics() is called.
_stats_collector_instance: "_StatsCollector | None" = None


def register_stats_collector(registry: Any = None) -> None:
    """Register the :class:`_StatsCollector` with *registry* (default: REGISTRY)."""
    global _stats_collector_instance
    if not _HAS_PROMETHEUS:  # pragma: no cover
        return
    reg = registry if registry is not None else REGISTRY
    if _stats_collector_instance is None:
        _stats_collector_instance = _StatsCollector()
    try:
        reg.register(_stats_collector_instance)
    except Exception:
        pass  # already registered
