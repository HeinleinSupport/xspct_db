# SPDX-License-Identifier: EUPL-1.2
# SPDX-FileCopyrightText: 2026 Carsten Rosenberg <c.rosenberg@heinlein-support.de>

"""Dummy / error backends for development and testing."""

from __future__ import annotations

import logging
from typing import Any

from xspct_db.backends.base import merge_userdata
from xspct_db.utils import timer

logger = logging.getLogger(__name__)


def query(
    s: str,
    query_name: str,
    users: list[dict[str, Any]],
    cfg: dict[str, Any],
) -> dict[str, Any]:
    """Return static dummy data for each user in *users*."""
    userdata: dict[str, Any] = {"users": {}}

    qcfg = cfg.get("xspct_db_queries", {}).get(query_name, {})
    if qcfg.get("db_type") != "dummy":
        logger.error("%s (%s) - (%s) - invalid or missing query config", s, timer(), query_name)
        return userdata

    for u in users:
        userdata = merge_userdata(
            s,
            u["username"],
            {"uid": u["username"], "comment": "dummy reply"},
            userdata,
        )
    return userdata


def error_query(s: str, query_name: str, cfg: dict[str, Any]) -> str:
    """Always return a 500 error string (used to test error-handling paths)."""
    logger.debug("%s (%s) - (%s) - raise custom error", s, timer(), query_name)
    return "500 raise custom error"
