# SPDX-License-Identifier: EUPL-1.2
# SPDX-FileCopyrightText: 2026 Carsten Rosenberg <c.rosenberg@heinlein-support.de>

"""Delay backend – sleep for a configurable duration (for timeout/cache testing)."""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from xspct_db.utils import timer

logger = logging.getLogger(__name__)


async def query(
    s: str,
    query_name: str,
    users: list[dict[str, Any]],
    userdata: dict[str, Any],
    user_to_pkey: dict[str, Any],
    cfg: dict[str, Any],
) -> tuple[dict[str, Any], dict[str, Any], str | bool]:
    """Sleep for the configured delay and return userdata unchanged."""
    queries = cfg.get("xspct_db_queries", {})
    if query_name not in queries or queries[query_name].get("db_type") != "delay":
        logger.error("%s (%s) - (%s) - invalid or missing query config", s, timer(), query_name)
        return userdata, user_to_pkey, "500 invalid query config"

    delay_seconds = float(queries[query_name].get("delay", 1.0))
    logger.debug("%s (%s) - (%s) - sleeping for %ss", s, timer(), query_name, delay_seconds)
    await asyncio.sleep(delay_seconds)
    return userdata, user_to_pkey, False
