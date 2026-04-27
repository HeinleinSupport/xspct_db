# SPDX-License-Identifier: EUPL-1.2
# SPDX-FileCopyrightText: 2026 Carsten Rosenberg <c.rosenberg@heinlein-support.de>

"""Async MySQL backend using aiomysql with connection pooling.

aiomysql is an optional dependency – import errors are caught at query time.
"""

from __future__ import annotations

import logging
import timeit
from typing import Any

from xspct_db.backends.base import merge_userdata, split_values_into_list, translate_entries
from xspct_db import stats
from xspct_db.utils import timer

logger = logging.getLogger(__name__)

_pools: dict[str, Any] = {}


async def create_pools(cfg: dict[str, Any]) -> None:
    """Create one aiomysql connection pool per configured mysql query."""
    try:
        import aiomysql
    except ImportError:
        logger.error("aiomysql is not installed; MySQL backend is unavailable")
        return

    for qk, qv in cfg.get("xspct_db_queries", {}).items():
        if qv.get("db_type") != "mysql":
            continue
        try:
            minconn = int(qv.get("pool_minconn", cfg.get("xspct_db_mysql_pool_minconn", 1)))
            maxconn = int(qv.get("pool_maxconn", cfg.get("xspct_db_mysql_pool_maxconn", 20)))
            _pools[qk] = await aiomysql.create_pool(
                host=qv["server"],
                port=qv["port"],
                user=qv["user"],
                password=qv["password"],
                db=qv["database"],
                minsize=minconn,
                maxsize=maxconn,
                autocommit=True,
            )
            logger.info("MySQL pool created for %s: minconn=%d maxconn=%d", qk, minconn, maxconn)
        except Exception as exc:
            logger.exception("Failed to create MySQL pool for %s: %s", qk, exc)


async def close_pools() -> None:
    for qk, pool in _pools.items():
        pool.close()
        await pool.wait_closed()
        logger.info("MySQL pool closed for %s", qk)
    _pools.clear()


async def query(
    s: str,
    query_name: str,
    users: list[dict[str, Any]],
    userdata: dict[str, Any],
    user_to_pkey: dict[str, Any],
    cfg: dict[str, Any],
) -> tuple[dict[str, Any], dict[str, Any], str | bool]:
    """Execute parameterised SQL for each user and merge results into *userdata*."""
    try:
        import aiomysql
    except ImportError:
        return userdata, user_to_pkey, "500 aiomysql not installed"

    error: str | bool = False
    queries = cfg.get("xspct_db_queries", {})

    if query_name not in queries or queries[query_name].get("db_type") != "mysql":
        logger.error("%s (%s) - (%s) - invalid or missing query config", s, timer(), query_name)
        return userdata, user_to_pkey, "500 invalid query config"

    if query_name not in _pools:
        logger.error("%s (%s) - (%s) - no MySQL pool available", s, timer(), query_name)
        return userdata, user_to_pkey, "500 MySQL pool not initialised"

    query_config = queries[query_name]

    try:
        async with _pools[query_name].acquire() as conn:
            for u in users:
                query_values = u["username"]
                force_prim_key: Any = False

                if query_config.get("use_result") and query_values in user_to_pkey:
                    pkey = user_to_pkey[query_values]
                    if pkey in userdata["users"]:
                        attr = query_config.get("result_object_attr", "")
                        if attr in userdata["users"][pkey]:
                            force_prim_key = pkey
                            query_values = userdata["users"][pkey][attr]

                sql = query_config["query"]
                params: list[Any] = []
                if isinstance(query_config.get("query_replace"), dict):
                    for placeholder, field in query_config["query_replace"].items():
                        if field in u:
                            vals = split_values_into_list(s, u[field], cfg=cfg)
                            occurrences = sql.count(placeholder)
                            sql = sql.replace(placeholder, "%s")
                            params.extend([vals[0]] * occurrences)

                t0 = timeit.default_timer()
                try:
                    async with conn.cursor(aiomysql.DictCursor) as cur:
                        await cur.execute(sql, params)
                        search = await cur.fetchall()
                except aiomysql.Error as exc:
                    logger.exception("%s (%s) - (%s) - aiomysql.Error: %s", s, timer(), query_name, exc)
                    error = "500 MySQL query error"
                    break

                elapsed = timeit.default_timer() - t0
                stats.update_query_stats(query_name, elapsed)

                for entry in search:
                    pk, entries = translate_entries(s, query_config, entry, cfg, force_prim_key)
                    user_to_pkey[u["username"]] = pk
                    userdata = merge_userdata(s, pk, entries, userdata)

    except Exception as exc:
        logger.exception("%s (%s) - (%s) - pool error: %s", s, timer(), query_name, exc)
        return userdata, user_to_pkey, "500 MySQL connection error"

    return userdata, user_to_pkey, error
