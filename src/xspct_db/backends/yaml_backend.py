# SPDX-License-Identifier: EUPL-1.2
# SPDX-FileCopyrightText: 2026 Carsten Rosenberg <c.rosenberg@heinlein-support.de>

"""YAML static-data backend."""

from __future__ import annotations

import logging
from typing import Any

from xspct_db.backends.base import merge_userdata, split_values_into_list, translate_entries
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
    """Search the configured YAML data source for each user."""
    error: str | bool = False

    queries = cfg.get("xspct_db_queries", {})
    if query_name not in queries or queries[query_name].get("db_type") != "yaml":
        logger.error("%s (%s) - (%s) - invalid or missing query config", s, timer(), query_name)
        return userdata, user_to_pkey, "500 invalid query config"

    query_config = queries[query_name]

    try:
        yaml_root = query_config.get("yaml_root", query_name)
        yaml_data: dict[str, Any] = cfg.get("xspct_db_yaml_data", {}).get(yaml_root, {})
    except (KeyError, Exception) as exc:
        logger.exception("%s (%s) - (%s) - Exception accessing yaml_data: %s", s, timer(), query_name, exc)
        return userdata, user_to_pkey, "500 YAML data access error"

    search_filter_set = set(query_config.get("search_filter", []))

    try:
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

            yaml_keys: list[str] = []
            if yaml_data:
                for qv in split_values_into_list(s, query_values, cfg=cfg):
                    if qv in yaml_data:
                        yaml_keys.append(qv)
                    else:
                        for dk, dv in yaml_data.items():
                            for sk in set(dv) & search_filter_set:
                                val = dv[sk]
                                if (isinstance(val, str) and qv == val) or (
                                    isinstance(val, list) and qv in val
                                ):
                                    yaml_keys.append(dk)

            for yk in yaml_keys:
                if yk in yaml_data:
                    primary_key, entries = translate_entries(
                        s, query_config, yaml_data[yk], cfg, force_prim_key
                    )
                    user_to_pkey[u["username"]] = primary_key
                    userdata = merge_userdata(s, primary_key, entries, userdata)

    except Exception as exc:
        logger.exception("%s (%s) - (%s) - Exception: %s", s, timer(), query_name, exc)
        return userdata, user_to_pkey, "500 YAML processing error"

    return userdata, user_to_pkey, error
