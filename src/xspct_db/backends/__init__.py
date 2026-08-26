# SPDX-License-Identifier: EUPL-1.2
# SPDX-FileCopyrightText: 2026 Carsten Rosenberg <c.rosenberg@heinlein-support.de>

"""Backend dispatcher: iterate configured queries and call the right backend."""

from __future__ import annotations

import asyncio
import logging
import timeit
from typing import Any

from xspct_db import cache, latency, stats
from xspct_db.backends.base import merge_userdata

logger = logging.getLogger(__name__)

# db_type → dotted module path of the backend implementing it.
_BACKEND_MODULES: dict[str, str] = {
    "dummy": "xspct_db.backends.dummy",
    "yaml": "xspct_db.backends.yaml_backend",
    "ldap": "xspct_db.backends.ldap_backend",
    "mysql": "xspct_db.backends.mysql_backend",
    "delay": "xspct_db.backends.delay",
    "error": "xspct_db.backends.dummy",
}

# Lazily-populated cache of imported backend modules, so the request path does
# not re-enter the import machinery on every query while optional dependencies
# stay optional (a module is only imported when a query of that type runs).
# Modules — not the query functions themselves — are cached, so that replacing
# a backend's ``query`` attribute at runtime still takes effect.
_MODULES: dict[str, Any] = {}


def _resolve_backend(db_type: str) -> Any | None:
    """Return the backend module for *db_type*, importing it on first use."""
    mod = _MODULES.get(db_type)
    if mod is not None:
        return mod

    path = _BACKEND_MODULES.get(db_type)
    if path is None:
        return None

    from importlib import import_module

    mod = import_module(path)
    _MODULES[db_type] = mod
    return mod


async def _execute_query(
    s: str,
    qk: str,
    qv: dict[str, Any],
    users: list[dict[str, Any]],
    userdata: dict[str, Any],
    user_to_pkey: dict[str, Any],
    cfg: dict[str, Any],
) -> tuple[dict[str, Any], dict[str, Any], str | bool]:
    """Execute a single configured query and return its updated state."""
    db_type = qv.get("db_type", "")
    query_error: str | bool = False
    t0 = timeit.default_timer()

    mod = _resolve_backend(db_type)
    if mod is not None:
        if db_type == "dummy":
            userdata = mod.query(s, qk, users, cfg)
        elif db_type == "error":
            query_error = mod.error_query(s, qk, cfg)
        else:
            userdata, user_to_pkey, query_error = await mod.query(s, qk, users, userdata, user_to_pkey, cfg)

    elapsed = timeit.default_timer() - t0
    # Total time for this configured query including pool acquisition — the
    # backend modules additionally record the pure query/pool spans.
    latency.record("backend." + qk, elapsed)
    logger.debug("%s - query[%s] took %.5fs", s, qk, elapsed)
    if db_type not in ("ldap", "mysql"):
        stats.update_query_stats(qk, elapsed)

    return userdata, user_to_pkey, query_error


def _merge_phase_results(
    userdata: dict[str, Any],
    user_to_pkey: dict[str, Any],
    phase_userdata: dict[str, Any],
    phase_user_to_pkey: dict[str, Any],
    phase_error: str | bool,
) -> tuple[dict[str, Any], dict[str, Any], str | bool]:
    """Merge one completed query result into the accumulated dispatcher state."""
    for primary_key, entries in phase_userdata.get("users", {}).items():
        userdata = merge_userdata(s="dispatcher", user=primary_key, data=entries, userdata=userdata)
    user_to_pkey.update(phase_user_to_pkey)
    return userdata, user_to_pkey, phase_error


async def _run_parallel_phase(
    s: str,
    phase: list[tuple[str, dict[str, Any]]],
    users: list[dict[str, Any]],
    userdata: dict[str, Any],
    user_to_pkey: dict[str, Any],
    cfg: dict[str, Any],
) -> tuple[dict[str, Any], dict[str, Any], str | bool]:
    """Run independent queries concurrently and merge their results in config order."""
    tasks = [_execute_query(s, qk, qv, users, {"users": {}}, {}, cfg) for qk, qv in phase]
    results = await asyncio.gather(*tasks)
    query_error: str | bool = False
    for phase_userdata, phase_user_to_pkey, phase_error in results:
        userdata, user_to_pkey, query_error = _merge_phase_results(
            userdata,
            user_to_pkey,
            phase_userdata,
            phase_user_to_pkey,
            phase_error,
        )
        if isinstance(query_error, str):
            logger.error("%s - query error: %s", s, query_error)
            return userdata, user_to_pkey, query_error
    return userdata, user_to_pkey, query_error


async def run_queries(
    s: str,
    user: str,
    use_redis: bool,
    users: list[dict[str, Any]],
    userdata: dict[str, Any],
    user_to_pkey: dict[str, Any],
    cfg: dict[str, Any],
    *,
    wildcard: bool = False,
) -> tuple[dict[str, Any], dict[str, Any], str | bool]:
    """Execute all configured queries and optionally cache the result.

    When *wildcard* is ``True`` only queries that have
    ``wildcard_domain_query: true`` in their config are executed.  This is used
    for the fallback domain-wildcard lookup when a full-address query returns no
    results.
    """
    query_error: str | bool = False
    parallel_phase: list[tuple[str, dict[str, Any]]] = []

    for qk, qv in cfg.get("xspct_db_queries", {}).items():
        if wildcard and not qv.get("wildcard_domain_query"):
            continue

        if qv.get("use_result"):
            if parallel_phase:
                userdata, user_to_pkey, query_error = await _run_parallel_phase(
                    s, parallel_phase, users, userdata, user_to_pkey, cfg
                )
                parallel_phase = []
                if isinstance(query_error, str):
                    return userdata, user_to_pkey, query_error

            userdata, user_to_pkey, query_error = await _execute_query(s, qk, qv, users, userdata, user_to_pkey, cfg)
            if isinstance(query_error, str):
                logger.error("%s - query error: %s", s, query_error)
                return userdata, user_to_pkey, query_error
            continue

        parallel_phase.append((qk, qv))

    if parallel_phase:
        userdata, user_to_pkey, query_error = await _run_parallel_phase(s, parallel_phase, users, userdata, user_to_pkey, cfg)
        if isinstance(query_error, str):
            return userdata, user_to_pkey, query_error

    if use_redis:
        if user in user_to_pkey:
            await cache.set_cache(s, userdata, user_to_pkey, cfg)
        else:
            await cache.set_negative_cache(s, [user], cfg)

    return userdata, user_to_pkey, query_error
