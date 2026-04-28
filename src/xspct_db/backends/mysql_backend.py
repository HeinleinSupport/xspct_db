# SPDX-License-Identifier: EUPL-1.2
# SPDX-FileCopyrightText: 2026 Carsten Rosenberg <c.rosenberg@heinlein-support.de>

"""Async MySQL backend using aiomysql with connection pooling.

aiomysql is an optional dependency – import errors are caught at query time.
"""

from __future__ import annotations

import logging
import re
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

    logger.debug("%s (%s) - (%s) - query users: %s", s, timer(), query_name, users)

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
                            logger.debug("%s (%s) - (%s) - use_result matched: pkey=%s attr=%s query_values=%s", s, timer(), query_name, pkey, attr, query_values)
                        else:
                            logger.debug("%s (%s) - (%s) - use_result not matched", s, timer(), query_name)
                    else:
                        logger.debug("%s (%s) - (%s) - use_result not matched", s, timer(), query_name)
                elif query_config.get("use_result"):
                    logger.debug("%s (%s) - (%s) - use_result not matched", s, timer(), query_name)

                logger.debug("%s (%s) - (%s) - new query key %s (%s)", s, timer(), query_name, query_values, type(query_values))

                # When use_result resolved new query_values, rebuild user
                # fields so query_replace substitution picks them up.
                if force_prim_key is not False:
                    resolved = query_values[0] if isinstance(query_values, list) else str(query_values)
                    u = dict(u)
                    u["username"] = resolved
                    u["address"] = resolved
                    parts = resolved.split("@", 1)
                    u["userpart"] = parts[0]
                    u["domain"] = parts[1] if len(parts) > 1 else u.get("domain", "")

                sql = query_config["query"]
                params: list[Any] = []
                if isinstance(query_config.get("query_replace"), dict):
                    # Process placeholders in SQL occurrence order so positional %s params align correctly.
                    # Handle quoted placeholders like "%u", '@%d' — strip the SQL quotes and fold
                    # any prefix/suffix text (e.g. "@") into the param value.
                    active = {
                        ph: field
                        for ph, field in query_config["query_replace"].items()
                        if field in u and ph in sql
                    }
                    for placeholder in sorted(active, key=lambda ph: sql.index(ph)):
                        field = active[placeholder]
                        vals = split_values_into_list(s, u[field], cfg=cfg)
                        val = vals[0]
                        # Match optional SQL quoting: ["'](prefix)placeholder(suffix)["']
                        pat = re.compile(
                            r'(["\'])(.*?)' + re.escape(placeholder) + r'(.*?)\1'
                        )
                        new_params: list[str] = []

                        def _replacer(m: re.Match, _val: str = val, _np: list = new_params) -> str:
                            _np.append(f"{m.group(2)}{_val}{m.group(3)}")
                            return "%s"

                        new_sql, n = pat.subn(_replacer, sql)
                        if n:
                            sql = new_sql
                            params.extend(new_params)
                        else:
                            # No surrounding quotes — bare placeholder
                            occurrences = sql.count(placeholder)
                            sql = sql.replace(placeholder, "%s")
                            params.extend([val] * occurrences)

                logger.info("%s (%s) - (%s) - searching for: %s\n params: %s", s, timer(), query_name, sql, params)

                t0 = timeit.default_timer()
                try:
                    async with conn.cursor(aiomysql.DictCursor) as cur:
                        await cur.execute(sql, params)
                        search = await cur.fetchall()
                        logger.info("%s (%s) - (%s) - cursor descr: %s", s, timer(), query_name, cur.description)
                except aiomysql.Error as exc:
                    logger.exception("%s (%s) - (%s) - aiomysql.Error: %s", s, timer(), query_name, exc)
                    error = "500 MySQL query error"
                    break

                elapsed = timeit.default_timer() - t0
                stats.update_query_stats(query_name, elapsed)
                logger.info("%s (%s) - (%s) - mysql query took %.5fs", s, timer(), query_name, elapsed)

                for entry in search:
                    pk, entries = translate_entries(s, query_config, entry, cfg, force_prim_key)
                    user_to_pkey[u["username"]] = pk
                    userdata = merge_userdata(s, pk, entries, userdata)

                logger.debug("%s (%s) - (%s) - after search: %s", s, timer(), query_name, u)

    except Exception as exc:
        logger.exception("%s (%s) - (%s) - pool error: %s", s, timer(), query_name, exc)
        return userdata, user_to_pkey, "500 MySQL connection error"

    return userdata, user_to_pkey, error
