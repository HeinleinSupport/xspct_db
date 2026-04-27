# SPDX-License-Identifier: EUPL-1.2
# SPDX-FileCopyrightText: 2026 Carsten Rosenberg <c.rosenberg@heinlein-support.de>

"""aiohttp route handlers – OpenAPI-documented via aiohttp-pydantic."""

from __future__ import annotations

import asyncio
import logging
from typing import Any
from urllib.parse import unquote

from aiohttp import web
from aiohttp_pydantic import PydanticView
from aiohttp_pydantic.oas.typing import r200, r401, r500, r504

from xspct_db import cache, stats
from xspct_db.auth import verify_api_key, verify_metrics_auth
from xspct_db.schemas import (
    ErrorResponse,
    QueryJsonRequest,
    QueryResponse,
    RspamdSettingsResponse,
)
from xspct_db.utils import add_rspamd_id, generate_session_id, timer

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _prometheus_lines(s_stats: dict[str, Any]) -> str:
    """Render the current stats dict as a Prometheus text payload."""
    lines: list[str] = []

    def _line(name: str, value: Any, labels: dict[str, str] | None = None) -> None:
        if labels:
            label_str = ",".join(f'{k}="{v}"' for k, v in labels.items())
            lines.append(f"{name}{{{label_str}}} {value}")
        else:
            lines.append(f"{name} {value}")

    for metric, stat_key, help_text, mtype in (
        ("xspct_db_requests_total", "requests_total", "Total client requests", "counter"),
        ("xspct_db_requests_known_total", "requests_known", "Requests where user was found", "counter"),
        ("xspct_db_requests_unknown_total", "requests_unknown", "Requests where user was not found", "counter"),
        ("xspct_db_redis_hits_total", "redis_hits", "Redis cache hits", "counter"),
        ("xspct_db_redis_misses_total", "redis_misses", "Redis cache misses", "counter"),
        ("xspct_db_redis_negative_hits_total", "redis_negative_hits", "Redis negative cache hits", "counter"),
    ):
        lines += [f"# HELP {metric} {help_text}", f"# TYPE {metric} {mtype}"]
        _line(metric, s_stats[stat_key])

    if s_stats["queries"]:
        lines += [
            "# HELP xspct_db_query_requests_total Queries executed per backend",
            "# TYPE xspct_db_query_requests_total counter",
        ]
        for qk, qs in s_stats["queries"].items():
            _line("xspct_db_query_requests_total", qs["count"], {"query": qk})

        lines += [
            "# HELP xspct_db_query_duration_seconds_total Accumulated query time per backend",
            "# TYPE xspct_db_query_duration_seconds_total counter",
        ]
        for qk, qs in s_stats["queries"].items():
            _line(
                "xspct_db_query_duration_seconds_total",
                f'{qs["time_total"]:.6f}',
                {"query": qk},
            )

    return "\n".join(lines) + "\n"


async def _backend_query(
    s: str,
    user: str,
    use_redis: bool,
    users: list[dict[str, Any]],
    userdata: dict[str, Any],
    user_to_pkey: dict[str, Any],
    cfg: dict[str, Any],
    request_timeout: float,
) -> tuple[dict[str, Any], dict[str, Any], str | bool]:
    """Run backend queries with optional per-request timeout."""
    from xspct_db.backends import run_queries

    if request_timeout > 0:
        task = asyncio.create_task(
            run_queries(s, user, use_redis, users, userdata, user_to_pkey, cfg)
        )
        done, _ = await asyncio.wait({task}, timeout=request_timeout)
        if not done:
            task.cancel()
            return userdata, user_to_pkey, "504"
        return task.result()

    return await run_queries(s, user, use_redis, users, userdata, user_to_pkey, cfg)


# ---------------------------------------------------------------------------
# Health endpoints
# ---------------------------------------------------------------------------

class HealthView(PydanticView):
    async def get(self) -> r200[dict]:
        """Service liveness check."""
        return web.Response(text="Hello, world", headers={"Connection": "Keep-Alive"})


class PingView(PydanticView):
    async def get(self) -> r200[dict]:
        """Ping → Pong."""
        return web.Response(text="Pong")


# ---------------------------------------------------------------------------
# Metrics
# ---------------------------------------------------------------------------

class MetricsView(PydanticView):
    async def get(self) -> r200[str] | r401[ErrorResponse]:
        """
        Prometheus metrics.

        Returns Prometheus text format (``text/plain; version=0.0.4``).
        Authentication is optional, controlled by ``xspct_db_metrics_auth``.
        """
        cfg: dict[str, Any] = self.request.app["config"]
        s = f"<metrics-{generate_session_id()}>"

        if not verify_metrics_auth(s, self.request, cfg):
            return web.Response(
                status=401,
                text="401 Unauthorized",
                headers={"WWW-Authenticate": 'Basic realm="xspct-db metrics", charset="UTF-8"'},
            )

        return web.Response(
            text=_prometheus_lines(stats.stats),
            headers={"Content-Type": "text/plain; version=0.0.4; charset=utf-8"},
        )


# ---------------------------------------------------------------------------
# Query endpoints
# ---------------------------------------------------------------------------

class QueryView(PydanticView):
    async def get(self, user: str, /) -> r200[QueryResponse] | r401[ErrorResponse] | r500[ErrorResponse] | r504[ErrorResponse]:
        """
        Look up a single user across all configured backends.

        The ``user`` path segment is URL-decoded before lookup.
        Redis cache is consulted first when enabled.
        """
        timer("start")
        stats.stats["requests_total"] += 1

        cfg: dict[str, Any] = self.request.app["config"]
        s_id = generate_session_id()
        s = add_rspamd_id(s_id, self.request.headers.get(cfg["xspct_db_rspamd_header"]))

        if not verify_api_key(s, self.request.headers.get(cfg["xspct_db_api_header"]), cfg):
            return web.Response(status=401, text="401 Unauthorized")

        user = unquote(user)
        userdata: dict[str, Any] = {"users": {}}
        user_to_pkey: dict[str, Any] = {}
        use_redis = cfg["xspct_db_redis_cache"]["enabled"] and cache.connection is not None

        # --- Redis cache lookup ---
        cache_object = None
        if use_redis:
            cache_object = await cache.get_object(s, user, cfg)
            if isinstance(cache_object, dict):
                stats.stats["redis_hits"] += 1
            elif isinstance(cache_object, bool) and not cache_object:
                stats.stats["redis_negative_hits"] += 1
            else:
                stats.stats["redis_misses"] += 1

        if isinstance(cache_object, dict):
            stats.stats["requests_known"] += 1
            userdata["users"][user] = cache_object
            return web.json_response(userdata, headers={"Connection": "Keep-Alive"})

        if isinstance(cache_object, bool) and not cache_object:
            stats.stats["requests_unknown"] += 1
            return web.json_response(userdata, headers={"Connection": "Keep-Alive"})

        # --- Backend query ---
        user_parts = user.split("@", 1)
        users = [{
            "username": user,
            "userpart": user_parts[0],
            "domain": user_parts[-1],
        }]

        request_timeout = float(cfg.get("xspct_db_request_timeout", 0))
        timeout_header = cfg.get("xspct_db_request_timeout_header", "")
        if timeout_header:
            header_val = self.request.headers.get(timeout_header)
            if header_val is not None:
                try:
                    request_timeout = float(header_val)
                except (ValueError, TypeError):
                    pass

        userdata, user_to_pkey, query_error = await _backend_query(
            s, user, use_redis, users, userdata, user_to_pkey, cfg, request_timeout
        )

        if query_error == "504":
            return web.Response(status=504, text="504 Request Timeout")
        if isinstance(query_error, str):
            return web.Response(status=500, text=query_error)

        if user in user_to_pkey:
            stats.stats["requests_known"] += 1
        else:
            stats.stats["requests_unknown"] += 1

        return web.json_response(userdata, headers={"Connection": "Keep-Alive"})


class QueryJsonView(PydanticView):
    async def post(self, body: QueryJsonRequest) -> r200[QueryResponse] | r401[ErrorResponse] | r500[ErrorResponse]:
        """
        Batch user lookup.

        Accepts a list of users and queries all configured backends for each.
        Redis cache is **not** consulted or populated on batch requests.
        """
        timer("start")
        cfg: dict[str, Any] = self.request.app["config"]
        s_id = generate_session_id()
        s = add_rspamd_id(s_id, self.request.headers.get(cfg["xspct_db_rspamd_header"]))

        if not verify_api_key(s, self.request.headers.get(cfg["xspct_db_api_header"]), cfg):
            return web.Response(status=401, text="401 Unauthorized")

        users = [{"username": u.username} for u in body.users]
        userdata: dict[str, Any] = {"users": {}}
        user_to_pkey: dict[str, Any] = {}

        from xspct_db.backends import run_queries

        userdata, user_to_pkey, query_error = await run_queries(
            s, "", False, users, userdata, user_to_pkey, cfg
        )

        if isinstance(query_error, str):
            return web.Response(status=500, text=query_error)

        return web.json_response(userdata, headers={"Connection": "Keep-Alive"})


class RspamdSettingsView(PydanticView):
    async def post(self) -> r200[RspamdSettingsResponse] | r401[ErrorResponse]:
        """
        Rspamd settings endpoint.

        Returns an Rspamd settings blob for use with the Rspamd HTTP settings module.
        The response ``Content-Type`` is ``application/json``.
        """
        timer("start")
        cfg: dict[str, Any] = self.request.app["config"]
        s_id = generate_session_id()
        s = add_rspamd_id(s_id, self.request.headers.get(cfg["xspct_db_rspamd_header"]))

        if not verify_api_key(s, self.request.headers.get(cfg["xspct_db_api_header"]), cfg):
            return web.Response(status=401, text="401 Unauthorized")

        reply = RspamdSettingsResponse(
            actions={"reject": 17, "greylist": 10, "add header": 14},
            flags=["skip_process", "no_stat"],
            groups_disabled=["antivirus", "external_services"],
            symbols=["INCOMING_API_TEST", "INCOMING"],
        )
        return web.json_response(reply.model_dump(), headers={"Connection": "Keep-Alive"})


# ---------------------------------------------------------------------------
# Route registration
# ---------------------------------------------------------------------------

def setup_routes(app: web.Application) -> None:
    """Register all route views on *app*."""
    app.router.add_view("/", HealthView)
    app.router.add_view("/ping", PingView)
    app.router.add_view("/metrics", MetricsView)
    app.router.add_view("/query/v1/{user}", QueryView)
    app.router.add_view("/query-json/v1", QueryJsonView)
    app.router.add_view("/rspamd-settings/v1", RspamdSettingsView)
