# SPDX-License-Identifier: EUPL-1.2
# SPDX-FileCopyrightText: 2026 Carsten Rosenberg <c.rosenberg@heinlein-support.de>

"""YAML static-data backend."""

from __future__ import annotations

import logging
from typing import Any

from xspct_db.backends.base import merge_userdata, split_values_into_list, translate_entries
from xspct_db.utils import timer

logger = logging.getLogger(__name__)


def _ensure_yaml_indexes(cfg: dict[str, Any]) -> dict[str, dict[str, dict[str, tuple[str, ...]]]]:
    """Build and cache reverse indexes for YAML roots on the config dict."""
    cached = cfg.get("_xspct_db_yaml_indexes")
    if isinstance(cached, dict):
        return cached

    indexes: dict[str, dict[str, dict[str, tuple[str, ...]]]] = {}
    for yaml_root, yaml_data in cfg.get("xspct_db_yaml_data", {}).items():
        root_index: dict[str, dict[str, list[str]]] = {}
        if isinstance(yaml_data, dict):
            for primary_key, entry in yaml_data.items():
                if not isinstance(entry, dict):
                    continue
                for field, value in entry.items():
                    values = value if isinstance(value, list) else [value]
                    field_index = root_index.setdefault(field, {})
                    for item in values:
                        if item is None:
                            continue
                        sval = str(item)
                        field_index.setdefault(sval, []).append(primary_key)

        indexes[yaml_root] = {
            field: {value: tuple(dict.fromkeys(primary_keys)) for value, primary_keys in values.items()}
            for field, values in root_index.items()
        }

    cfg["_xspct_db_yaml_indexes"] = indexes
    return indexes


def _lookup_yaml_keys(
    s: str,
    query_values: Any,
    yaml_data: dict[str, Any],
    search_filter_set: set[str],
    query_name: str,
    query_config: dict[str, Any],
    cfg: dict[str, Any],
) -> list[str]:
    """Resolve *query_values* to YAML primary keys using a cached reverse index."""
    yaml_root = query_config.get("yaml_root", query_name)
    root_index = _ensure_yaml_indexes(cfg).get(yaml_root, {})
    matched_keys: list[str] = []

    for qv in split_values_into_list(s, query_values, cfg=cfg):
        if qv in yaml_data:
            matched_keys.append(qv)
            continue

        qv_str = str(qv)
        for field in search_filter_set:
            matched_keys.extend(root_index.get(field, {}).get(qv_str, ()))

    return list(dict.fromkeys(matched_keys))


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
        logger.error(
            "%s (%s) - (%s) - invalid or missing query config",
            s,
            timer(),
            query_name,
        )
        return userdata, user_to_pkey, "500 invalid query config"

    query_config = queries[query_name]

    try:
        yaml_root = query_config.get("yaml_root", query_name)
        yaml_data: dict[str, Any] = cfg.get("xspct_db_yaml_data", {}).get(yaml_root, {})
    except (KeyError, Exception) as exc:
        logger.exception(
            "%s (%s) - (%s) - Exception accessing yaml_data: %s",
            s,
            timer(),
            query_name,
            exc,
        )
        return userdata, user_to_pkey, "500 YAML data access error"

    search_filter_set = set(query_config.get("search_filter", []))

    logger.debug("%s (%s) - (%s) - query users: %s", s, timer(), query_name, users)

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
                        logger.debug(
                            "%s (%s) - (%s) - use_result matched: pkey=%s attr=%s query_values=%s",
                            s,
                            timer(),
                            query_name,
                            pkey,
                            attr,
                            query_values,
                        )
                    else:
                        logger.debug("%s (%s) - (%s) - use_result not matched", s, timer(), query_name)
                else:
                    logger.debug("%s (%s) - (%s) - use_result not matched", s, timer(), query_name)
            elif query_config.get("use_result"):
                logger.debug("%s (%s) - (%s) - use_result not matched", s, timer(), query_name)

            logger.debug(
                "%s (%s) - (%s) - new query key %s (%s)",
                s,
                timer(),
                query_name,
                query_values,
                type(query_values),
            )
            logger.info("%s (%s) - (%s) - searching for: %s", s, timer(), query_name, query_values)

            yaml_keys: list[str] = []
            if yaml_data:
                yaml_keys = _lookup_yaml_keys(
                    s,
                    query_values,
                    yaml_data,
                    search_filter_set,
                    query_name,
                    query_config,
                    cfg,
                )

            logger.debug(
                "%s (%s) - (%s) - yaml_keys found: %s",
                s,
                timer(),
                query_name,
                yaml_keys,
            )

            for yk in yaml_keys:
                if yk in yaml_data:
                    primary_key, entries = translate_entries(s, query_config, yaml_data[yk], cfg, force_prim_key)
                    user_to_pkey[u["username"]] = primary_key
                    userdata = merge_userdata(s, primary_key, entries, userdata)

            logger.debug("%s (%s) - (%s) - after search: %s", s, timer(), query_name, u)

    except Exception as exc:
        logger.exception(
            "%s (%s) - (%s) - Exception: %s",
            s,
            timer(),
            query_name,
            exc,
        )
        return userdata, user_to_pkey, "500 YAML processing error"

    return userdata, user_to_pkey, error
