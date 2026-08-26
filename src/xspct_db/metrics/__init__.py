# SPDX-License-Identifier: EUPL-1.2
# SPDX-FileCopyrightText: 2024 Carsten Rosenberg <c.rosenberg@heinlein-support.de>

"""Prometheus metrics integration for xspct_db.

Call :func:`setup_metrics` from :func:`~xspct_db.server.create_app` to enable
the full Prometheus integration: HTTP middleware, /metrics handler, loop-lag
background task, and a custom collector that exposes the existing
``stats.stats`` counters.

When ``xspct_db_metrics_enabled`` is ``False`` (the default) this function
returns immediately without importing ``prometheus_client``, so the library
remains an optional dependency.
"""

from __future__ import annotations

from typing import Any

from aiohttp import web


def setup_metrics(app: web.Application, registry: Any = None) -> None:
    """Register middleware, /metrics handler, and background tasks.

    Parameters
    ----------
    app:
        The :class:`~aiohttp.web.Application` to configure.
    registry:
        Optional :class:`~prometheus_client.CollectorRegistry` to use instead
        of the module-level default.  Pass a fresh registry in tests for
        isolation.
    """
    from xspct_db import latency

    cfg: dict[str, Any] = app["config"]
    if not cfg.get("xspct_db_metrics_enabled", False):
        # Drop any observer a previously-created app installed, so this app's
        # samples are never fed into another app's Prometheus registry.
        latency.set_observer(None)
        return

    try:
        from prometheus_client import REGISTRY as _DEFAULT_REG  # noqa: F401
    except ImportError:
        import logging

        latency.set_observer(None)
        logging.getLogger(__name__).error(
            "prometheus-client is not installed — metrics endpoint disabled. Install with: pip install 'xspct-db[metrics]'"
        )
        return

    from .handlers import metrics_handler
    from .loop_lag import start_loop_lag_task, stop_loop_lag_task
    from .middleware import make_metrics_middleware
    from .registry import histogram, register_stats_collector

    # Register the stats.stats → Prometheus bridge collector.
    register_stats_collector(registry=registry)

    # Feed every latency sample into a Prometheus histogram as well, so real
    # quantiles can be computed across instances with histogram_quantile().
    if latency.enabled():
        span_hist = histogram(
            "xspct_db_span_duration_seconds",
            "Latency distribution per instrumented span",
            labels=["span"],
            buckets=latency.LATENCY_BUCKETS,
            registry=registry,
        )
        latency.set_observer(lambda name, elapsed: span_hist.labels(span=name).observe(elapsed))
    else:
        latency.set_observer(None)

    async def _clear_observer(_app: web.Application) -> None:
        """Detach this app's histogram when it shuts down."""
        latency.set_observer(None)

    app.on_cleanup.append(_clear_observer)

    # Prepend the HTTP metrics middleware.
    mw = make_metrics_middleware(registry=registry)
    app.middlewares.insert(0, mw)

    # /metrics route (replaces the old MetricsView).
    app.router.add_get("/metrics", metrics_handler)
    app.router.add_get("/metrics/", metrics_handler)

    # Loop-lag background task.
    app.on_startup.append(start_loop_lag_task)
    app.on_cleanup.append(stop_loop_lag_task)
