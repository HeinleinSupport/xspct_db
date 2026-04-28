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

from xspct_db import stats
from xspct_db.backends.base import merge_userdata, split_values_into_list, translate_entries
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


def _build_sql_fragment(
    s: str,
    template: str,
    u: dict[str, Any],
    query_config: dict[str, Any],
    cfg: dict[str, Any],
) -> tuple[str, list[Any]]:
    """Apply ``query_replace`` substitution to *template* for user *u*.

    Returns ``(sql_fragment_with_%s_placeholders, params_list)``.
    """
    sql = template
    params: list[Any] = []
    if not isinstance(query_config.get("query_replace"), dict):
        return sql, params

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

    return sql, params


async def query(
    s: str,
    query_name: str,
    users: list[dict[str, Any]],
    userdata: dict[str, Any],
    user_to_pkey: dict[str, Any],
    cfg: dict[str, Any],
) -> tuple[dict[str, Any], dict[str, Any], str | bool]:
    """Execute a single batched SQL for all users and merge results into *userdata*.

    All users are combined into one query using OR-joined WHERE fragments,
    reducing round-trips to one per backend call regardless of user count.
    """
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

    # Split SQL at WHERE so each user's WHERE fragment can be built separately
    # and all fragments combined with OR into a single batched query.
    _where_match = re.search(r'\bWHERE\b', query_config["query"], re.IGNORECASE)
    select_part = query_config["query"][:_where_match.start()].rstrip() if _where_match else query_config["query"]
    where_tmpl: str | None = query_config["query"][_where_match.end():].strip() if _where_match else None

    # Phase 1: resolve each user (use_result + u-dict rebuild) and build
    # the per-user WHERE fragment with positional %s params.
    user_frags: list[dict[str, Any]] = []
    for u in users:
        orig_username = u["username"]
        query_values: Any = orig_username
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

        # Rebuild u if use_result resolved a different value.
        if force_prim_key is not False:
            resolved = query_values[0] if isinstance(query_values, list) else str(query_values)
            u = dict(u)
            u["username"] = resolved
            u["address"] = resolved
            parts = resolved.split("@", 1)
            u["userpart"] = parts[0]
            u["domain"] = parts[1] if len(parts) > 1 else u.get("domain", "")

        if where_tmpl is not None:
            frag_sql, frag_params = _build_sql_fragment(s, where_tmpl, u, query_config, cfg)
        else:
            frag_sql, frag_params = "", []

        user_frags.append({
            "orig_username": orig_username,
            "effective_username": u["username"],
            "force_prim_key": force_prim_key,
            "frag_sql": frag_sql,
            "frag_params": frag_params,
        })

    if not user_frags:
        return userdata, user_to_pkey, error

    # Phase 2: combine all per-user WHERE fragments into a single batched SQL.
    if where_tmpl is not None:
        combined_where = " OR ".join(f"({uf['frag_sql']})" for uf in user_frags)
        combined_sql = f"{select_part} WHERE {combined_where}"
        combined_params: list[Any] = [p for uf in user_frags for p in uf["frag_params"]]
    else:
        combined_sql = select_part
        combined_params = []

    logger.info("%s (%s) - (%s) - searching for: %s\n params: %s", s, timer(), query_name, combined_sql, combined_params)

    # Phase 3: execute the combined query once.
    try:
        async with _pools[query_name].acquire() as conn:
            t0 = timeit.default_timer()
            try:
                async with conn.cursor(aiomysql.DictCursor) as cur:
                    await cur.execute(combined_sql, combined_params)
                    search = await cur.fetchall()
                    logger.info("%s (%s) - (%s) - cursor descr: %s", s, timer(), query_name, cur.description)
            except aiomysql.Error as exc:
                logger.exception("%s (%s) - (%s) - aiomysql.Error: %s", s, timer(), query_name, exc)
                return userdata, user_to_pkey, "500 MySQL query error"

            elapsed = timeit.default_timer() - t0
            stats.update_query_stats(query_name, elapsed)
            logger.info("%s (%s) - (%s) - mysql query took %.5fs", s, timer(), query_name, elapsed)

    except Exception as exc:
        logger.exception("%s (%s) - (%s) - pool error: %s", s, timer(), query_name, exc)
        return userdata, user_to_pkey, "500 MySQL connection error"

    # Phase 4: attribute result rows to input users and merge into userdata.
    # A result row is attributed to the first user whose effective_username
    # appears anywhere in the row's values (handles alias columns containing
    # the input address), with a fallback to a direct primary-key field match.
    pkey_field = query_config.get("primary_key", "mail")
    effective_to_user: dict[str, tuple[str, Any]] = {
        uf["effective_username"]: (uf["orig_username"], uf["force_prim_key"])
        for uf in user_frags
    }

    for entry in search:
        row_values = frozenset(str(v) for v in entry.values() if v is not None)
        orig_username_r: str | None = None
        fpk: Any = False
        for uf in user_frags:
            if uf["effective_username"] in row_values:
                orig_username_r = uf["orig_username"]
                fpk = uf["force_prim_key"]
                break
        if orig_username_r is None:
            pk_raw = str(entry.get(pkey_field, ""))
            if pk_raw in effective_to_user:
                orig_username_r, fpk = effective_to_user[pk_raw]
        if orig_username_r is None:
            # Third fallback: row matched via a catch-all/wildcard param (e.g.
            # "@domain.tld") that is present in row_values but the full user
            # address is not.  Check each user's actual query fragment params.
            for uf in user_frags:
                if any(str(p) in row_values for p in uf["frag_params"]):
                    orig_username_r = uf["orig_username"]
                    fpk = uf["force_prim_key"]
                    break

        pk, entries = translate_entries(s, query_config, entry, cfg, fpk)
        if orig_username_r is not None:
            user_to_pkey[orig_username_r] = pk
        userdata = merge_userdata(s, pk, entries, userdata)

    logger.debug("%s (%s) - (%s) - after search: %d results", s, timer(), query_name, len(search))
    return userdata, user_to_pkey, error
