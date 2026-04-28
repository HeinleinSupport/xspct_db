# SPDX-License-Identifier: EUPL-1.2
# SPDX-FileCopyrightText: 2026 Carsten Rosenberg <c.rosenberg@heinlein-support.de>

"""LDAP backend using bonsai with connection pooling.

bonsai is an optional dependency.  Import errors are caught at query time so
the service starts even without the ldap extra installed.
"""

from __future__ import annotations

import logging
import timeit
from typing import Any

from xspct_db.backends.base import merge_userdata, split_values_into_list, translate_entries
from xspct_db import stats
from xspct_db.utils import timer

logger = logging.getLogger(__name__)

# Pool registry: query_name → bonsai.asyncio.AIOConnectionPool
_pools: dict[str, Any] = {}


async def create_pools(cfg: dict[str, Any]) -> None:
    """Create one LDAP connection pool per configured ldap query."""
    try:
        import bonsai
        import bonsai.asyncio
    except ImportError:
        logger.error("bonsai is not installed; LDAP backend is unavailable")
        return

    for qk, qv in cfg.get("xspct_db_queries", {}).items():
        if qv.get("db_type") != "ldap":
            continue
        try:
            cli = bonsai.LDAPClient(qv["server"], tls=qv.get("use_tls", False))
            if "ca_cert_dir" in qv:
                cli.set_ca_cert_dir(qv["ca_cert_dir"])
            policy = "try" if qv.get("verify_certs", True) else "never"
            cli.set_cert_policy(policy)
            cli.set_credentials("SIMPLE", user=qv["bind_dn"], password=qv["bind_dn_pw"])
            minconn = int(qv.get("pool_minconn", cfg.get("xspct_db_ldap_pool_minconn", 2)))
            maxconn = int(qv.get("pool_maxconn", cfg.get("xspct_db_ldap_pool_maxconn", 20)))
            _pools[qk] = bonsai.asyncio.AIOConnectionPool(cli, minconn=minconn, maxconn=maxconn)
            logger.info("LDAP pool created for %s: minconn=%d maxconn=%d", qk, minconn, maxconn)
        except Exception as exc:
            logger.exception("Failed to create LDAP pool for %s: %s", qk, exc)


def close_pools() -> None:
    for qk, pool in _pools.items():
        pool.close()
        logger.info("LDAP pool closed for %s", qk)
    _pools.clear()


async def query(
    s: str,
    query_name: str,
    users: list[dict[str, Any]],
    userdata: dict[str, Any],
    user_to_pkey: dict[str, Any],
    cfg: dict[str, Any],
) -> tuple[dict[str, Any], dict[str, Any], str | bool]:
    """Query LDAP for each user and merge results into *userdata*."""
    try:
        import bonsai
        import bonsai.asyncio  # noqa: F401
    except ImportError:
        return userdata, user_to_pkey, "500 bonsai not installed"

    error: str | bool = False
    queries = cfg.get("xspct_db_queries", {})

    if query_name not in queries or queries[query_name].get("db_type") != "ldap":
        logger.error("%s (%s) - (%s) - invalid or missing query config", s, timer(), query_name)
        return userdata, user_to_pkey, "500 invalid query config"

    if query_name not in _pools:
        logger.error("%s (%s) - (%s) - no LDAP pool available", s, timer(), query_name)
        return userdata, user_to_pkey, "500 LDAP pool not initialised"

    query_config = queries[query_name]

    logger.debug("%s (%s) - (%s) - query users: %s", s, timer(), query_name, users)

    try:
        async with _pools[query_name].spawn() as conn:
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
                # fields so search_filter_replace substitution picks them up.
                if force_prim_key is not False:
                    resolved = query_values[0] if isinstance(query_values, list) else str(query_values)
                    u = dict(u)
                    u["username"] = resolved
                    u["address"] = resolved
                    parts = resolved.split("@", 1)
                    u["userpart"] = parts[0]
                    u["domain"] = parts[1] if len(parts) > 1 else u.get("domain", "")

                ldap_filter = query_config["search_filter"]
                if isinstance(query_config.get("search_filter_replace"), dict):
                    for r, field in query_config["search_filter_replace"].items():
                        if field in u:
                            vals = split_values_into_list(s, u[field], cfg=cfg)
                            ldap_filter = ldap_filter.replace(
                                r, bonsai.escape_filter_exp(vals[0])
                            )

                logger.info("%s (%s) - (%s) - searching for: %s base_dn=%s", s, timer(), query_name, ldap_filter, query_config.get("base_dn"))

                attr_list = query_config.get("attr_list")
                t0 = timeit.default_timer()
                try:
                    search = await conn.search(
                        base=query_config["base_dn"],
                        scope=2,
                        filter_exp=ldap_filter,
                        **({"attrlist": attr_list} if attr_list else {}),
                    )
                except bonsai.errors.LDAPError as exc:
                    logger.exception("%s (%s) - (%s) - LDAPError: %s", s, timer(), query_name, exc)
                    error = "500 LDAP query error"
                    search = []

                elapsed = timeit.default_timer() - t0
                stats.update_query_stats(query_name, elapsed)
                logger.info("%s (%s) - (%s) - ldap query took %.5fs, results: %d", s, timer(), query_name, elapsed, len(search))

                for entry in search:
                    pk, entries = translate_entries(s, query_config, entry, cfg, force_prim_key)
                    user_to_pkey[u["username"]] = pk
                    userdata = merge_userdata(s, pk, entries, userdata)

                logger.debug("%s (%s) - (%s) - after search: %s", s, timer(), query_name, u)

    except (bonsai.errors.LDAPError, bonsai.errors.AuthenticationError, Exception) as exc:
        logger.exception("%s (%s) - (%s) - connection error: %s", s, timer(), query_name, exc)
        return userdata, user_to_pkey, "500 LDAP connection error"

    return userdata, user_to_pkey, error
