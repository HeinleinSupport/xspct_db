# SPDX-License-Identifier: EUPL-1.2
# SPDX-FileCopyrightText: 2026 Carsten Rosenberg <c.rosenberg@heinlein-support.de>

"""aiohttp application factory, startup / shutdown hooks, and run()."""

from __future__ import annotations

import asyncio
import logging
import ssl
import sys
from typing import Any

from aiohttp import web

from aiohttp_pydantic import oas

from xspct_db import cache, stats
from xspct_db import config as cfg_mod
from xspct_db import __version__
from xspct_db.routes import setup_routes

try:
    import uvloop
except ImportError:
    uvloop = None  # type: ignore[assignment]

logger = logging.getLogger(__name__)


async def _on_startup(app: web.Application) -> None:
    cfg: dict[str, Any] = app["config"]

    if cfg["xspct_db_redis_cache"]["enabled"]:
        try:
            import redis.asyncio as redis

            pool = redis.ConnectionPool(
                host=cfg["xspct_db_redis_cache"]["host"],
                port=cfg["xspct_db_redis_cache"]["port"],
                username=cfg["xspct_db_redis_cache"]["user"],
                password=cfg["xspct_db_redis_cache"]["password"],
                decode_responses=True,
                socket_timeout=cfg["xspct_db_redis_cache"]["query_timeout"],
                socket_connect_timeout=cfg["xspct_db_redis_cache"]["connect_timeout"],
                max_connections=int(cfg["xspct_db_redis_cache"]["max_connections"]),
            )
            cache.set_connection(redis.Redis(connection_pool=pool))
            logger.info("Redis connection pool created")
        except Exception as exc:
            logger.error("Error creating Redis connection pool: %s", exc)

    if cfg.get("xspct_db_types_enabled", {}).get("ldap"):
        from xspct_db.backends.ldap_backend import create_pools as create_ldap_pools
        await create_ldap_pools(cfg)

    if cfg.get("xspct_db_types_enabled", {}).get("mysql"):
        from xspct_db.backends.mysql_backend import create_pools as create_mysql_pools
        await create_mysql_pools(cfg)

    asyncio.create_task(stats.log_stats_periodically(cfg))


async def _on_shutdown(app: web.Application) -> None:
    cfg: dict[str, Any] = app["config"]

    if cfg.get("xspct_db_types_enabled", {}).get("ldap"):
        from xspct_db.backends.ldap_backend import close_pools as close_ldap_pools
        close_ldap_pools()

    if cfg.get("xspct_db_types_enabled", {}).get("mysql"):
        from xspct_db.backends.mysql_backend import close_pools as close_mysql_pools
        await close_mysql_pools()


def create_app(config: dict[str, Any]) -> web.Application:
    """Build and return the aiohttp :class:`~aiohttp.web.Application`."""
    app = web.Application()
    app["config"] = config
    setup_routes(app)
    oas.setup(
        app,
        title_spec="xspct_db",
        version_spec=__version__,
    )
    app.on_startup.append(_on_startup)
    app.on_shutdown.append(_on_shutdown)
    return app


def run(config_path: str) -> None:
    """Load config, configure logging, then start the aiohttp server."""
    config = cfg_mod.load(config_path)

    logging.basicConfig(
        stream=sys.stdout,
        level=int(config["xspct_db_log_level"]),
        format=config["xspct_db_log_prefix"] + " %(levelname)s %(funcName)s %(message)s",
    )

    if uvloop is not None:
        asyncio.set_event_loop_policy(uvloop.EventLoopPolicy())
        logger.info("uvloop event loop policy active")

    async def _run() -> None:
        app = create_app(config)
        runner = web.AppRunner(app, backlog=int(config["xspct_db_listen_backlog"]))
        await runner.setup()

        ssl_ctx: ssl.SSLContext | None = None
        if config["xspct_db_tls"]["tls_enabled"]:
            ssl_ctx = ssl.create_default_context(ssl.Purpose.CLIENT_AUTH)
            ssl_ctx.load_cert_chain(
                config["xspct_db_tls"]["tls_cert"],
                config["xspct_db_tls"]["tls_key"],
            )
            logger.info("TLS enabled")

        addrs = config["xspct_db_listen_address"]
        if isinstance(addrs, str):
            addrs = [addrs]
        port = int(config["xspct_db_listen_port"])
        for addr in addrs:
            site = web.TCPSite(runner, addr, port, ssl_context=ssl_ctx)
            await site.start()
            logger.info("Listening on %s:%d  TLS: %s", addr, port, ssl_ctx is not None)

        try:
            while True:
                await asyncio.sleep(3600)
        except (asyncio.CancelledError, KeyboardInterrupt):
            pass
        finally:
            await runner.cleanup()

    try:
        asyncio.run(_run())
    except KeyboardInterrupt:
        pass
    except Exception as exc:
        logger.exception("Fatal error: %s", exc)
        sys.exit(1)
