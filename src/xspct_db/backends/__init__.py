# SPDX-License-Identifier: EUPL-1.2
# SPDX-FileCopyrightText: 2026 Carsten Rosenberg <c.rosenberg@heinlein-support.de>

"""Backend dispatcher: iterate configured queries and call the right backend."""

from __future__ import annotations

import logging
import timeit
from typing import Any

from xspct_db import cache, stats

logger = logging.getLogger(__name__)


async def run_queries(
    s: str,
    user: str,
    use_redis: bool,
    users: list[dict[str, Any]],
    userdata: dict[str, Any],
    user_to_pkey: dict[str, Any],
    cfg: dict[str, Any],
) -> tuple[dict[str, Any], dict[str, Any], str | bool]:
    """Execute all configured queries and optionally cache the result."""
    query_error: str | bool = False

    for qk, qv in cfg.get("xspct_db_queries", {}).items():
        db_type = qv.get("db_type")
        t0 = timeit.default_timer()

        if db_type == "dummy":
            from xspct_db.backends.dummy import query as dummy_query
            userdata = dummy_query(s, qk, users, cfg)

        elif db_type == "yaml":
            from xspct_db.backends.yaml_backend import query as yaml_query
            userdata, user_to_pkey, query_error = await yaml_query(
                s, qk, users, userdata, user_to_pkey, cfg
            )

        elif db_type == "ldap":
            from xspct_db.backends.ldap_backend import query as ldap_query
            userdata, user_to_pkey, query_error = await ldap_query(
                s, qk, users, userdata, user_to_pkey, cfg
            )

        elif db_type == "mysql":
            from xspct_db.backends.mysql_backend import query as mysql_query
            userdata, user_to_pkey, query_error = await mysql_query(
                s, qk, users, userdata, user_to_pkey, cfg
            )

        elif db_type == "delay":
            from xspct_db.backends.delay import query as delay_query
            userdata, user_to_pkey, query_error = await delay_query(
                s, qk, users, userdata, user_to_pkey, cfg
            )

        elif db_type == "error":
            from xspct_db.backends.dummy import error_query
            query_error = error_query(s, qk, cfg)

        elapsed = timeit.default_timer() - t0
        logger.info("%s - query[%s] took %.5fs", s, qk, elapsed)
        if db_type not in ("ldap", "mysql"):
            stats.update_query_stats(qk, elapsed)

        if isinstance(query_error, str):
            logger.error("%s - query error: %s", s, query_error)
            return userdata, user_to_pkey, query_error

    if use_redis:
        if user in user_to_pkey:
            await cache.set_cache(s, userdata, user_to_pkey, cfg)
        else:
            await cache.set_negative_cache(s, [user], cfg)

    return userdata, user_to_pkey, query_error
