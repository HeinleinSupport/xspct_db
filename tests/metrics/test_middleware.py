# SPDX-License-Identifier: EUPL-1.2
# SPDX-FileCopyrightText: 2024 Carsten Rosenberg <c.rosenberg@heinlein-support.de>

"""Tests for the metrics HTTP middleware."""

from __future__ import annotations

import pytest
import pytest_asyncio
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer
from prometheus_client import CollectorRegistry

from xspct_db.metrics.middleware import make_metrics_middleware

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_app(handler, registry: CollectorRegistry) -> web.Application:
    """Build a minimal aiohttp app with the metrics middleware and a single route."""
    mw = make_metrics_middleware(registry=registry)
    app = web.Application(middlewares=[mw])
    app.router.add_get("/users/{id}", handler)
    return app


async def _ok_handler(request: web.Request) -> web.Response:
    return web.Response(text="ok")


async def _not_found_handler(request: web.Request) -> web.Response:
    raise web.HTTPNotFound()


async def _error_handler(request: web.Request) -> web.Response:
    raise RuntimeError("boom")


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


@pytest.fixture
def reg() -> CollectorRegistry:
    return CollectorRegistry()


@pytest_asyncio.fixture
async def client(reg: CollectorRegistry) -> TestClient:
    app = _make_app(_ok_handler, reg)
    server = TestServer(app)
    client = TestClient(server)
    await client.start_server()
    yield client
    await client.close()


async def test_successful_request_increments_counter(reg: CollectorRegistry):
    """Counter incremented, histogram observed, in_flight back to 0 after request."""
    app = _make_app(_ok_handler, reg)
    async with TestClient(TestServer(app)) as c:
        resp = await c.get("/users/1")
        assert resp.status == 200

    # Counter must have been incremented exactly once
    counter_val = reg.get_sample_value(
        "http_requests_total",
        labels={"method": "GET", "route": "/users/{id}", "status": "200"},
    )
    assert counter_val == 1.0

    # Histogram bucket must have an observation
    bucket_val = reg.get_sample_value(
        "http_request_duration_seconds_count",
        labels={"method": "GET", "route": "/users/{id}"},
    )
    assert bucket_val == 1.0

    # in_flight must be back at 0
    in_flight = reg.get_sample_value("http_requests_in_flight")
    assert in_flight == 0.0


async def test_two_ids_share_route_pattern(reg: CollectorRegistry):
    """/users/1 and /users/42 both emit the same route label."""
    app = _make_app(_ok_handler, reg)
    async with TestClient(TestServer(app)) as c:
        await c.get("/users/1")
        await c.get("/users/42")

    counter_val = reg.get_sample_value(
        "http_requests_total",
        labels={"method": "GET", "route": "/users/{id}", "status": "200"},
    )
    assert counter_val == 2.0


async def test_http_not_found_uses_unmatched_label(reg: CollectorRegistry):
    """Requests to routes that match nothing get route='<unmatched>'."""
    mw = make_metrics_middleware(registry=reg)
    app = web.Application(middlewares=[mw])
    # No routes registered → every request is unmatched.
    async with TestClient(TestServer(app)) as c:
        resp = await c.get("/does/not/exist")
        assert resp.status == 404

    counter_val = reg.get_sample_value(
        "http_requests_total",
        labels={"method": "GET", "route": "<unmatched>", "status": "404"},
    )
    assert counter_val == 1.0


async def test_http_exception_records_status_label(reg: CollectorRegistry):
    """HTTPNotFound raised from handler → status label is '404'."""
    app = _make_app(_not_found_handler, reg)
    async with TestClient(TestServer(app)) as c:
        resp = await c.get("/users/1")
        assert resp.status == 404

    counter_val = reg.get_sample_value(
        "http_requests_total",
        labels={"method": "GET", "route": "/users/{id}", "status": "404"},
    )
    assert counter_val == 1.0

    # in_flight must be 0 after re-raise
    assert reg.get_sample_value("http_requests_in_flight") == 0.0


async def test_generic_exception_records_500_and_decrements_in_flight(reg: CollectorRegistry):
    """Generic exception → status '500', in_flight correctly decremented."""
    app = _make_app(_error_handler, reg)
    async with TestClient(TestServer(app)) as c:
        resp = await c.get("/users/1")
        # aiohttp converts uncaught exceptions to 500
        assert resp.status == 500

    counter_val = reg.get_sample_value(
        "http_requests_total",
        labels={"method": "GET", "route": "/users/{id}", "status": "500"},
    )
    assert counter_val == 1.0

    assert reg.get_sample_value("http_requests_in_flight") == 0.0


async def test_metrics_path_not_recorded(reg: CollectorRegistry):
    """/metrics requests are excluded from middleware measurements."""

    async def _metrics_handler(request: web.Request) -> web.Response:
        return web.Response(text="# fake metrics\n")

    mw = make_metrics_middleware(registry=reg)
    app = web.Application(middlewares=[mw])
    app.router.add_get("/metrics", _metrics_handler)

    async with TestClient(TestServer(app)) as c:
        resp = await c.get("/metrics")
        assert resp.status == 200

    # No samples should have been recorded for /metrics.
    samples = list(reg.collect())
    for metric in samples:
        for sample in metric.samples:
            if sample.labels.get("route") == "/metrics":
                pytest.fail(f"Unexpected metric sample for /metrics route: {sample}")
