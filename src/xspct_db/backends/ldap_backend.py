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


def _build_ldap_filter(
    s: str,
    u: dict[str, Any],
    query_config: dict[str, Any],
    cfg: dict[str, Any],
    bonsai: Any,
) -> str:
    """Build an LDAP search filter for user *u* by applying ``search_filter_replace``."""
    ldap_filter = query_config["search_filter"]
    if isinstance(query_config.get("search_filter_replace"), dict):
        for r, field in query_config["search_filter_replace"].items():
            if field in u:
                vals = split_values_into_list(s, u[field], cfg=cfg)
                ldap_filter = ldap_filter.replace(
                    r, bonsai.escape_filter_exp(vals[0])
                )
    return ldap_filter


async def query(
    s: str,
    query_name: str,
    users: list[dict[str, Any]],
    userdata: dict[str, Any],
    user_to_pkey: dict[str, Any],
    cfg: dict[str, Any],
) -> tuple[dict[str, Any], dict[str, Any], str | bool]:
    """Query LDAP for all users in a single batched search and merge results into *userdata*.

    Multiple users are combined into one ``(|filter1 filter2 …)`` OR filter,
    reducing round-trips to one per backend call regardless of user count.
    """
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

    # Phase 1: resolve each user (use_result + u-dict rebuild) and build
    # the per-user LDAP filter.
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

        ldap_filter = _build_ldap_filter(s, u, query_config, cfg, bonsai)
        # Collect the actual values substituted into the filter so Phase 4 can
        # fall back to them when a wildcard/catch-all matches.
        frag_values: list[str] = (
            [str(v) for v in query_values]
            if isinstance(query_values, list)
            else [str(query_values)]
        )
        user_frags.append({
            "orig_username": orig_username,
            "effective_username": u["username"],
            "force_prim_key": force_prim_key,
            "ldap_filter": ldap_filter,
            "frag_values": frag_values,
        })

    if not user_frags:
        return userdata, user_to_pkey, error

    # Phase 2: combine per-user filters into a single OR filter.
    if len(user_frags) == 1:
        combined_filter = user_frags[0]["ldap_filter"]
    else:
        combined_filter = "(|" + "".join(uf["ldap_filter"] for uf in user_frags) + ")"

    logger.info("%s (%s) - (%s) - searching for: %s base_dn=%s", s, timer(), query_name, combined_filter, query_config.get("base_dn"))

    attr_list = query_config.get("attr_list")

    # Phase 3: execute the combined search once.
    try:
        async with _pools[query_name].spawn() as conn:
            t0 = timeit.default_timer()
            try:
                search = await conn.search(
                    base=query_config["base_dn"],
                    scope=2,
                    filter_exp=combined_filter,
                    **({"attrlist": attr_list} if attr_list else {}),
                )
            except bonsai.errors.LDAPError as exc:
                logger.exception("%s (%s) - (%s) - LDAPError: %s", s, timer(), query_name, exc)
                error = "500 LDAP query error"
                search = []

            elapsed = timeit.default_timer() - t0
            stats.update_query_stats(query_name, elapsed)
            logger.info("%s (%s) - (%s) - ldap query took %.5fs, results: %d", s, timer(), query_name, elapsed, len(search))

            # Phase 4: attribute result rows to input users and merge into userdata.
            pkey_field = query_config.get("primary_key", "mail")
            effective_to_user: dict[str, tuple[str, Any]] = {
                uf["effective_username"]: (uf["orig_username"], uf["force_prim_key"])
                for uf in user_frags
            }

            for entry in search:
                # Flatten entry values (LDAP attributes may be lists).
                row_values: set[str] = set()
                for v in entry.values():
                    if isinstance(v, list):
                        row_values.update(str(item) for item in v if item is not None)
                    elif v is not None:
                        row_values.add(str(v))

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
                    # Third fallback: row matched via a catch-all/wildcard value
                    # (e.g. "@domain.tld") present in the row but not the full
                    # user address.  Check each user's actual filter values.
                    for uf in user_frags:
                        if any(fv in row_values for fv in uf["frag_values"]):
                            orig_username_r = uf["orig_username"]
                            fpk = uf["force_prim_key"]
                            break

                pk, entries = translate_entries(s, query_config, entry, cfg, fpk)
                if orig_username_r is not None:
                    user_to_pkey[orig_username_r] = pk
                userdata = merge_userdata(s, pk, entries, userdata)

            logger.debug("%s (%s) - (%s) - after search: %d results", s, timer(), query_name, len(search))

    except (bonsai.errors.LDAPError, bonsai.errors.AuthenticationError, Exception) as exc:
        logger.exception("%s (%s) - (%s) - connection error: %s", s, timer(), query_name, exc)
        return userdata, user_to_pkey, "500 LDAP connection error"

    return userdata, user_to_pkey, error
