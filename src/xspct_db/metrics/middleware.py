# SPDX-License-Identifier: EUPL-1.2
# SPDX-FileCopyrightText: 2024 Carsten Rosenberg <c.rosenberg@heinlein-support.de>

"""aiohttp middleware that records HTTP request metrics via prometheus_client."""

from __future__ import annotations

import time
from typing import Any

from aiohttp import web

from .registry import DEFAULT_BUCKETS, counter, gauge, histogram

# Paths that should not be measured (the metrics endpoint itself).
_SKIP_PATHS = frozenset({"/metrics", "/metrics/"})


def make_metrics_middleware(registry: Any = None) -> Any:
    """Return an aiohttp middleware coroutine that records HTTP metrics.

    Three metrics are recorded:

    * ``http_requests_total{method, route, status}`` — Counter
    * ``http_request_duration_seconds{method, route}`` — Histogram
    * ``http_requests_in_flight`` — Gauge

    *registry* is forwarded to the metric factories; pass a custom
    :class:`~prometheus_client.CollectorRegistry` in tests for isolation.
    """
    _requests_total = counter(
        "http_requests_total",
        "Total HTTP requests",
        labels=["method", "route", "status"],
        registry=registry,
    )
    _duration = histogram(
        "http_request_duration_seconds",
        "HTTP request duration in seconds",
        labels=["method", "route"],
        buckets=DEFAULT_BUCKETS,
        registry=registry,
    )
    _in_flight = gauge(
        "http_requests_in_flight",
        "Number of HTTP requests currently being processed",
        registry=registry,
    )

    @web.middleware
    async def _metrics_middleware(
        request: web.Request,
        handler: Any,
    ) -> web.StreamResponse:
        # Do not record metrics for the /metrics endpoint itself.
        if request.path in _SKIP_PATHS:
            return await handler(request)

        # Determine the route pattern (canonical path template).
        try:
            route = request.match_info.route.resource.canonical
        except AttributeError:
            route = "<unmatched>"
        if not route:
            route = "<unmatched>"

        method = request.method
        _in_flight.inc()
        t0 = time.perf_counter()
        status = "500"
        try:
            response = await handler(request)
            status = str(response.status)
            return response
        except web.HTTPException as exc:
            status = str(exc.status)
            raise
        except Exception:
            status = "500"
            raise
        finally:
            elapsed = time.perf_counter() - t0
            _in_flight.dec()
            _requests_total.labels(method=method, route=route, status=status).inc()
            _duration.labels(method=method, route=route).observe(elapsed)

    return _metrics_middleware


# Module-level default instance used by setup_metrics().
metrics_middleware = make_metrics_middleware()
