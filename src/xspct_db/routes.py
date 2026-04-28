# SPDX-License-Identifier: EUPL-1.2
# SPDX-FileCopyrightText: 2026 Carsten Rosenberg <c.rosenberg@heinlein-support.de>

"""aiohttp route handlers – OpenAPI-documented via aiohttp-pydantic."""

from __future__ import annotations

import ast
import asyncio
import json
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
    RspamdSettingsRequest,
    RspamdSettingsResponse,
)
from xspct_db.utils import add_rspamd_id, generate_session_id, timer

logger = logging.getLogger(__name__)


def _build_settings_extra_data(
    userdata: dict[str, Any],
    cfg: dict[str, Any],
) -> dict[str, Any]:
    """Build the structured ``settings_extra_data`` block.

    Returns ``{"users": {...}, "aliases": {...}}`` where ``aliases`` is a
    reverse map of alias-value → primary-key, built from the fields named in
    ``xspct_db_rspamd_alias_fields``.  Returns ``{}`` when no users were found.
    """
    users = userdata.get("users", {})
    if not users:
        return {}
    alias_fields: list[str] = cfg.get("xspct_db_rspamd_alias_fields", ["aliases"])
    aliases: dict[str, str] = {}
    for pkey, udata in users.items():
        for field in alias_fields:
            for val in udata.get(field, []):
                if val != pkey:
                    aliases[val] = pkey
    return {"users": users, "aliases": aliases}


def _parse_body(raw: bytes) -> Any:
    """Return a parsed object from *raw* bytes.

    Tries JSON first; falls back to ``ast.literal_eval`` to handle the case
    where aiohttp_pydantic has cached the body as a Python-repr string
    (single-quoted dict) instead of the original JSON bytes.
    """
    if not raw:
        return None
    try:
        return json.loads(raw)
    except Exception:
        pass
    try:
        return ast.literal_eval(raw.decode(errors="replace"))
    except Exception:
        pass
    return raw.decode(errors="replace")


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


def _log_response(s: str, response: web.Response) -> web.Response:
    """Log response status and body at DEBUG level, then return the response unchanged."""
    if logger.isEnabledFor(logging.DEBUG):
        try:
            body = response.text
        except Exception:
            body = None
        logger.debug("%s ← %d  body=%s", s, response.status, body)
    return response


def _log_request(s: str, request: web.Request, body: Any = None) -> None:
    """Emit a DEBUG line with method, URL, all headers (API key masked), and optional body."""
    if not logger.isEnabledFor(logging.DEBUG):
        return
    api_header = "X-Api-Key"
    headers: dict[str, str] = {}
    for name, value in request.headers.items():
        if name.lower() == api_header.lower() and len(value) > 4:
            headers[name] = value[:4] + "…"
        else:
            headers[name] = value
    params = dict(request.rel_url.query)
    try:
        matched_path = request.match_info.route.resource.canonical
    except Exception:
        matched_path = "-"
    logger.debug(
        "%s %s %s (matched: %s)  params=%s  headers=%s  body=%s",
        s,
        request.method,
        request.path,
        matched_path,
        params or "-",
        headers,
        body if body is not None else "-",
    )


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

        **Example curl**::

            curl -s -H "X-Api-Key: your-key" \
                 http://localhost:11350/v1/query/alice@example.com | python3 -m json.tool

        **Example request**::

            GET /v1/query/alice@example.com
            X-Api-Key: your-key

        **Example response (user found)**::

            {
                "users": {
                    "alice@example.com": {
                        "mail": "alice@example.com",
                        "uid": "alice",
                        "aliases": ["a.smith@example.com"]
                    }
                }
            }

        **Example response (user not found)**::

            {"users": {}}

        Returns ``504`` if the backend query exceeds the configured timeout.
        """
        timer("start")
        stats.stats["requests_total"] += 1

        cfg: dict[str, Any] = self.request.app["config"]
        s_id = generate_session_id()
        s = add_rspamd_id(s_id, self.request.headers.get(cfg["xspct_db_rspamd_header"]))

        _log_request(s, self.request)

        if not verify_api_key(s, self.request.headers.get(cfg["xspct_db_api_header"]), cfg):
            return _log_response(s, web.Response(status=401, text="401 Unauthorized"))

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
            return _log_response(s, web.json_response(userdata, headers={"Connection": "Keep-Alive"}))

        if isinstance(cache_object, bool) and not cache_object:
            stats.stats["requests_unknown"] += 1
            return _log_response(s, web.json_response(userdata, headers={"Connection": "Keep-Alive"}))

        # --- Backend query ---
        user_parts = user.split("@", 1)
        users = [{
            "username": user,
            "address": user,
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
            return _log_response(s, web.Response(status=504, text="504 Request Timeout"))
        if isinstance(query_error, str):
            return _log_response(s, web.Response(status=500, text=query_error))

        if user in user_to_pkey:
            stats.stats["requests_known"] += 1
        else:
            stats.stats["requests_unknown"] += 1

        return _log_response(s, web.json_response(userdata, headers={"Connection": "Keep-Alive"}))


class QueryJsonView(PydanticView):
    async def post(self) -> r200[QueryResponse] | r401[ErrorResponse] | r500[ErrorResponse]:
        """
        Batch user lookup.

        Accepts a list of users and queries all configured backends for each.
        Redis cache is **not** consulted or populated on batch requests.

        **Example curl**::

            curl -s -X POST http://localhost:11350/v1/query-json \
                 -H "X-Api-Key: your-key" \
                 -H "Content-Type: application/json" \
                 -d '{"users": ["alice@example.com", "bob@example.com"]}' | python3 -m json.tool

        **Example request**::

            POST /v1/query-json
            Content-Type: application/json
            X-Api-Key: your-key

            {
                "users": [
                    "alice@example.com",
                    "bob@example.com"
                ]
            }

        **Example response**::

            {
                "users": {
                    "alice@example.com": {
                        "mail": "alice@example.com",
                        "uid": "alice",
                        "aliases": ["a.smith@example.com"]
                    },
                    "bob@example.com": {}
                }
            }

        Users not found in any backend are returned with an empty dict.
        """
        timer("start")
        cfg: dict[str, Any] = self.request.app["config"]
        s_id = generate_session_id()
        s = add_rspamd_id(s_id, self.request.headers.get(cfg["xspct_db_rspamd_header"]))

        raw_body = await self.request.read()
        parsed_body: Any = _parse_body(raw_body)

        query_req = QueryJsonRequest.model_validate(
            parsed_body if isinstance(parsed_body, dict) else {}
        )

        _log_request(s, self.request, body=parsed_body)

        if not verify_api_key(s, self.request.headers.get(cfg["xspct_db_api_header"]), cfg):
            return _log_response(s, web.Response(status=401, text="401 Unauthorized"))

        users = [{
            "username": u,
            "address": u,
            "userpart": u.split("@", 1)[0],
            "domain": u.split("@", 1)[-1],
        } for u in query_req.users]
        userdata: dict[str, Any] = {"users": {}}
        user_to_pkey: dict[str, Any] = {}

        from xspct_db.backends import run_queries

        userdata, user_to_pkey, query_error = await run_queries(
            s, "", False, users, userdata, user_to_pkey, cfg
        )

        if isinstance(query_error, str):
            return _log_response(s, web.Response(status=500, text=query_error))

        return _log_response(s, web.json_response(userdata, headers={"Connection": "Keep-Alive"}))


class RspamdSettingsView(PydanticView):
    async def post(self) -> r200[RspamdSettingsResponse] | r401[ErrorResponse]:
        """
        Rspamd settings endpoint.

        Returns an Rspamd settings blob for use with the Rspamd HTTP settings module.
        The response ``Content-Type`` is ``application/json``.

        **Example curl**::

            curl -s -X POST http://localhost:11350/v1/rspamd-settings \
                 -H "X-Api-Key: your-key" \
                 -H "Content-Type: application/json" \
                 -d '{"from": "alice@example.com", "rcpts": ["bob@example.com"]}' | python3 -m json.tool

        **Example request**::

            POST /v1/rspamd-settings
            X-Api-Key: your-key

        **Example response**::

            {
                "actions": {
                    "reject": 15,
                    "greylist": 8,
                    "add header": 13
                },
                "flags": ["skip_process", "no_stat"],
                "groups_disabled": ["antivirus", "external_services"],
                "symbols": ["INCOMING_API_TEST", "INCOMING"]
            }

        """
        timer("start")
        cfg: dict[str, Any] = self.request.app["config"]
        s_id = generate_session_id()

        # Read and parse body first so uid is available for the session tag.
        raw_body = await self.request.read()
        parsed_body: Any = _parse_body(raw_body)

        # Construct the model explicitly from the parsed dict to avoid any
        # alias-resolution issues caused by aiohttp_pydantic's model introspection.
        if isinstance(parsed_body, dict):
            rspamd_req = RspamdSettingsRequest(
                uid=parsed_body.get("uid", ""),
                from_addr=parsed_body.get("from", ""),
                rcpts=parsed_body.get("rcpts", []),
                mta_name=parsed_body.get("mta-name"),
                mta_host=parsed_body.get("mta-host"),
                ip=parsed_body.get("ip"),
                settings_name=parsed_body.get("settings-name"),
                settings_id=parsed_body.get("settings-id"),
            )
        else:
            rspamd_req = RspamdSettingsRequest()

        # Prefer uid from body, fall back to X-Rspamd-ID header
        rspamd_id = rspamd_req.uid or self.request.headers.get(cfg["xspct_db_rspamd_header"])
        s = add_rspamd_id(s_id, rspamd_id)

        _log_request(s, self.request, body=parsed_body)

        if not verify_api_key(s, self.request.headers.get(cfg["xspct_db_api_header"]), cfg):
            return _log_response(s, web.Response(status=401, text="401 Unauthorized"))

        # Look up all addresses from envelope sender + recipients
        addresses = list(dict.fromkeys(
            addr for addr in ([rspamd_req.from_addr] + rspamd_req.rcpts) if addr
        ))
        userdata: dict[str, Any] = {"users": {}}
        user_to_pkey: dict[str, Any] = {}

        if addresses:
            from xspct_db.backends import run_queries
            users = [{
                "username": addr,
                "address": addr,
                "userpart": addr.split("@", 1)[0],
                "domain": addr.split("@", 1)[-1],
            } for addr in addresses]
            userdata, user_to_pkey, _ = await run_queries(
                s, "", False, users, userdata, user_to_pkey, cfg
            )

        reply = RspamdSettingsResponse(
            actions={"reject": 15, "greylist": 8, "add header": 13},
            symbols_disabled=["DKIM_SIGNED"],
            symbols=["SETTINGS_API_TEST_RESPONSE"],
            settings_extra_data=_build_settings_extra_data(userdata, cfg),
            settings_error=[],
        )
        return _log_response(s, web.json_response(reply.model_dump(exclude_none=True), headers={"Connection": "Keep-Alive"}))


# ---------------------------------------------------------------------------
# Route registration
# ---------------------------------------------------------------------------

def setup_routes(app: web.Application) -> None:
    """Register all route views on *app*."""
    _routes: list[tuple[str, type]] = [
        ("/", HealthView),
        ("/ping", PingView),
        ("/ping/", PingView),
        ("/metrics", MetricsView),
        ("/metrics/", MetricsView),
        ("/v1/query/{user}", QueryView),
        ("/v1/query/{user}/", QueryView),
        ("/query/v1/{user}", QueryView),
        ("/query/v1/{user}/", QueryView),
        ("/v1/query-json", QueryJsonView),
        ("/v1/query-json/", QueryJsonView),
        ("/query-json/v1", QueryJsonView),
        ("/query-json/v1/", QueryJsonView),
        ("/v1/rspamd-settings", RspamdSettingsView),
        ("/v1/rspamd-settings/", RspamdSettingsView),
        ("/rspamd-settings/v1", RspamdSettingsView),
        ("/rspamd-settings/v1/", RspamdSettingsView),
    ]
    for path, view in _routes:
        logger.debug("registering route: %s → %s", path, view.__name__)
        app.router.add_view(path, view)
