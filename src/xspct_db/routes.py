# SPDX-License-Identifier: EUPL-1.2
# SPDX-FileCopyrightText: 2026 Carsten Rosenberg <c.rosenberg@heinlein-support.de>

"""aiohttp route handlers."""

from __future__ import annotations

import asyncio
import json
import logging
from typing import TYPE_CHECKING, Any
from urllib.parse import unquote

from aiohttp import web

from xspct_db import cache, stats
from xspct_db.auth import verify_api_key, verify_metrics_auth
from xspct_db.utils import add_rspamd_id, generate_session_id, timer

if TYPE_CHECKING:
    pass

logger = logging.getLogger(__name__)

routes = web.RouteTableDef()


# ---------------------------------------------------------------------------
# Health / utility endpoints
# ---------------------------------------------------------------------------

@routes.get("/")
async def health(request: web.Request) -> web.Response:
    return web.Response(text="Hello, world", headers={"Connection": "Keep-Alive"})


@routes.get("/ping")
async def ping(request: web.Request) -> web.Response:
    return web.Response(text="Pong")


@routes.get("/metrics")
async def metrics_handle(request: web.Request) -> web.Response:
    cfg: dict[str, Any] = request.app["config"]
    s = f"<metrics-{generate_session_id()}>"

    if not verify_metrics_auth(s, request, cfg):
        return web.Response(
            status=401,
            text="401 Unauthorized",
            headers={"WWW-Authenticate": 'Basic realm="xspct-db metrics", charset="UTF-8"'},
        )

    lines: list[str] = []

    def _line(name: str, value: Any, labels: dict[str, str] | None = None) -> None:
        if labels:
            label_str = ",".join(f'{k}="{v}"' for k, v in labels.items())
            lines.append(f"{name}{{{label_str}}} {value}")
        else:
            lines.append(f"{name} {value}")

    lines += [
        "# HELP xspct_db_requests_total Total client requests",
        "# TYPE xspct_db_requests_total counter",
    ]
    _line("xspct_db_requests_total", stats.stats["requests_total"])

    lines += [
        "# HELP xspct_db_requests_known_total Requests where user was found",
        "# TYPE xspct_db_requests_known_total counter",
    ]
    _line("xspct_db_requests_known_total", stats.stats["requests_known"])

    lines += [
        "# HELP xspct_db_requests_unknown_total Requests where user was not found",
        "# TYPE xspct_db_requests_unknown_total counter",
    ]
    _line("xspct_db_requests_unknown_total", stats.stats["requests_unknown"])

    lines += [
        "# HELP xspct_db_redis_hits_total Redis cache hits",
        "# TYPE xspct_db_redis_hits_total counter",
    ]
    _line("xspct_db_redis_hits_total", stats.stats["redis_hits"])

    lines += [
        "# HELP xspct_db_redis_misses_total Redis cache misses",
        "# TYPE xspct_db_redis_misses_total counter",
    ]
    _line("xspct_db_redis_misses_total", stats.stats["redis_misses"])

    lines += [
        "# HELP xspct_db_redis_negative_hits_total Redis negative cache hits",
        "# TYPE xspct_db_redis_negative_hits_total counter",
    ]
    _line("xspct_db_redis_negative_hits_total", stats.stats["redis_negative_hits"])

    if stats.stats["queries"]:
        lines += [
            "# HELP xspct_db_query_requests_total Queries executed per backend",
            "# TYPE xspct_db_query_requests_total counter",
        ]
        for qk, qs in stats.stats["queries"].items():
            _line("xspct_db_query_requests_total", qs["count"], {"query": qk})

        lines += [
            "# HELP xspct_db_query_duration_seconds_total Accumulated query time per backend",
            "# TYPE xspct_db_query_duration_seconds_total counter",
        ]
        for qk, qs in stats.stats["queries"].items():
            _line(
                "xspct_db_query_duration_seconds_total",
                f'{qs["time_total"]:.6f}',
                {"query": qk},
            )

    return web.Response(
        text="\n".join(lines) + "\n",
        headers={"Content-Type": "text/plain; version=0.0.4; charset=utf-8"},
    )


# ---------------------------------------------------------------------------
# Query endpoints
# ---------------------------------------------------------------------------

@routes.get("/query/v1/{user}")
async def query_handle(request: web.Request) -> web.Response:
    timer("start")
    stats.stats["requests_total"] += 1

    cfg: dict[str, Any] = request.app["config"]
    s_id = generate_session_id()
    s = add_rspamd_id(s_id, request.headers.get(cfg["xspct_db_rspamd_header"]))

    if not verify_api_key(s, request.headers.get(cfg["xspct_db_api_header"]), cfg):
        return web.Response(status=401, text="401 Unauthorized")

    user = unquote(request.match_info.get("user", ""))
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
        return web.Response(text=json.dumps(userdata), headers={"Connection": "Keep-Alive"})

    if isinstance(cache_object, bool) and not cache_object:
        stats.stats["requests_unknown"] += 1
        return web.Response(text=json.dumps(userdata), headers={"Connection": "Keep-Alive"})

    # --- Backend query ---
    user_parts = user.split("@", 1)
    user_arr = {
        "username": user,
        "userpart": user_parts[0],
        "domain": user_parts[-1],
    }
    users = [user_arr]

    from xspct_db.backends import run_queries

    request_timeout = float(cfg.get("xspct_db_request_timeout", 0))
    timeout_header = cfg.get("xspct_db_request_timeout_header", "")
    if timeout_header:
        header_val = request.headers.get(timeout_header)
        if header_val is not None:
            try:
                request_timeout = float(header_val)
            except (ValueError, TypeError):
                pass

    if request_timeout > 0:
        task = asyncio.create_task(
            run_queries(s, user, use_redis, users, userdata, user_to_pkey, cfg)
        )
        done, _ = await asyncio.wait({task}, timeout=request_timeout)
        if not done:
            return web.Response(status=504, text="504 Request Timeout")
        userdata, user_to_pkey, query_error = task.result()
    else:
        userdata, user_to_pkey, query_error = await run_queries(
            s, user, use_redis, users, userdata, user_to_pkey, cfg
        )

    if isinstance(query_error, str):
        return web.Response(status=500, text=query_error)

    if user in user_to_pkey:
        stats.stats["requests_known"] += 1
    else:
        stats.stats["requests_unknown"] += 1

    return web.Response(text=json.dumps(userdata), headers={"Connection": "Keep-Alive"})


@routes.post("/query-json/v1")
async def query_json_handle(request: web.Request) -> web.Response:
    """Batch user lookup (experimental)."""
    timer("start")
    cfg: dict[str, Any] = request.app["config"]
    s_id = generate_session_id()
    s = add_rspamd_id(s_id, request.headers.get(cfg["xspct_db_rspamd_header"]))

    if not verify_api_key(s, request.headers.get(cfg["xspct_db_api_header"]), cfg):
        return web.Response(status=401, text="401 Unauthorized")

    data = await request.json()
    userdata: dict[str, Any] = {"users": {}}
    user_to_pkey: dict[str, Any] = {}

    from xspct_db.backends import run_queries

    userdata, user_to_pkey, query_error = await run_queries(
        s, "", False, data[0]["users"], userdata, user_to_pkey, cfg
    )

    if isinstance(query_error, str):
        return web.Response(status=500, text=query_error)

    return web.Response(text=json.dumps(userdata), headers={"Connection": "Keep-Alive"})


@routes.post("/rspamd-settings/v1")
async def rspamd_settings(request: web.Request) -> web.Response:
    """Rspamd settings endpoint (experimental)."""
    timer("start")
    cfg: dict[str, Any] = request.app["config"]
    s_id = generate_session_id()
    s = add_rspamd_id(s_id, request.headers.get(cfg["xspct_db_rspamd_header"]))

    if not verify_api_key(s, request.headers.get(cfg["xspct_db_api_header"]), cfg):
        return web.Response(status=401, text="401 Unauthorized")

    reply = {
        "actions": {"reject": 17, "greylist": 10, "add header": 14},
        "flags": ["skip_process", "no_stat"],
        "groups_disabled": ["antivirus", "external_services"],
        "symbols": ["INCOMING_API_TEST", "INCOMING"],
    }
    return web.Response(text=json.dumps(reply), headers={"Connection": "Keep-Alive"})
