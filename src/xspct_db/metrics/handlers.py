# SPDX-License-Identifier: EUPL-1.2
# SPDX-FileCopyrightText: 2024 Carsten Rosenberg <c.rosenberg@heinlein-support.de>

"""HTTP handler for the /metrics Prometheus endpoint."""

from __future__ import annotations

from typing import Any

from aiohttp import web
from cachetools import TTLCache

from ..auth import verify_metrics_auth
from ..utils import generate_session_id


async def metrics_handler(request: web.Request) -> web.Response:
    """Serve Prometheus text metrics with optional auth and TTL caching."""
    from prometheus_client import CONTENT_TYPE_LATEST, generate_latest

    from .registry import REGISTRY

    cfg: dict[str, Any] = request.app["config"]
    s = f"<metrics-{generate_session_id()}>"

    if not verify_metrics_auth(s, request, cfg):
        return web.Response(
            status=401,
            text="401 Unauthorized",
            headers={"WWW-Authenticate": 'Basic realm="xspct-db metrics", charset="UTF-8"'},
        )

    ttl = int(cfg.get("xspct_db_metrics_cache_ttl", 5))

    # Per-app cache stored in app context to avoid cross-test contamination.
    cache: TTLCache | None = request.app.get("_metrics_cache")
    if cache is None or cache.ttl != ttl:
        cache = TTLCache(maxsize=1, ttl=ttl)
        request.app["_metrics_cache"] = cache

    body: bytes | None = cache.get("latest")
    if body is None:
        body = generate_latest(REGISTRY)
        cache["latest"] = body

    return web.Response(
        body=body,
        headers={"Content-Type": CONTENT_TYPE_LATEST},
    )
