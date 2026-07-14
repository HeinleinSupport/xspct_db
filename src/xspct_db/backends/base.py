# SPDX-License-Identifier: EUPL-1.2
# SPDX-FileCopyrightText: 2026 Carsten Rosenberg <c.rosenberg@heinlein-support.de>

"""Shared helpers used by all backends: entry translation and userdata merge."""

from __future__ import annotations

import logging
import sys
from typing import Any

logger = logging.getLogger(__name__)


def split_values_into_list(
    s: str,
    data: Any,
    key: str | None = None,
    query_config: dict[str, Any] | None = None,
    cfg: dict[str, Any] | None = None,
) -> list[Any]:
    """Split *data* into a list according to configured delimiters."""
    value_split: dict[str, str] = {}
    if isinstance(query_config, dict) and "value_split" in query_config:
        value_split = query_config["value_split"]
    elif cfg is not None:
        value_split = cfg.get("xspct_db_value_split", {})

    if isinstance(key, str) and key in value_split:
        sep = value_split[key]
        if isinstance(data, str):
            return data.split(sep)
        if isinstance(data, list):
            result: list[Any] = []
            for item in data:
                result.extend(item.split(sep) if isinstance(item, str) else [item])
            return result
        return data

    if isinstance(data, (str, int)):
        return [str(data)]
    return data


def maybe_list(
    s: str,
    entries: dict[str, Any],
    key: str,
    query_config: dict[str, Any],
    cfg: dict[str, Any],
) -> dict[str, Any]:
    """Ensure *entries[key]* is a list; create an empty one if absent."""
    if key not in entries:
        entries[key] = []
    else:
        entries[key] = split_values_into_list(s, entries[key], key, query_config, cfg)
    return entries


def translate_entries(
    s: str,
    query_config: dict[str, Any],
    data: dict[str, Any],
    cfg: dict[str, Any],
    force_primary_key: Any = False,
) -> tuple[Any, dict[str, Any]]:
    """Translate raw DB entry *data* into a normalised (primary_key, entries) tuple."""
    entries: dict[str, Any] = {}
    primary_key: Any = None

    key = query_config.get("primary_key", "mail")
    key_translation: dict[str, str] = query_config.get("key_translation", cfg.get("xspct_db_key_translation", {}))
    use_attr_filter = "attr_list" in query_config and query_config["attr_list"][0] != "*"

    for k, v in data.items():
        if use_attr_filter and k not in query_config["attr_list"]:
            continue

        if k == key:
            p = split_values_into_list(s, v, k, query_config, cfg)
            primary_key = str(p[0]) if p[0] is not None else None

        translated_key = key_translation.get(k, k)
        entries = maybe_list(s, entries, translated_key, query_config, cfg)
        to_add = split_values_into_list(s, v, k, query_config, cfg)
        if to_add is not None:
            entries[translated_key] = [*entries[translated_key], *to_add]

    # Convert LDAP DN objects to strings when bonsai is loaded.
    if "dn" in entries and "bonsai" in sys.modules:
        import bonsai  # noqa: PLC0415

        if isinstance(entries["dn"], bonsai.ldapdn.LDAPDN):
            entries["dn"] = str(entries["dn"])

    # If the primary_key attribute was absent from the raw entry but the key
    # field ended up in entries via key_translation (e.g. mail → uid), derive
    # primary_key from the translated value rather than returning None.
    if primary_key is None and key in entries and entries[key]:
        primary_key = entries[key][0]

    if force_primary_key:
        return force_primary_key, entries
    return primary_key, entries


def merge_userdata(
    s: str,
    user: str,
    data: dict[str, Any],
    userdata: dict[str, Any],
) -> dict[str, Any]:
    """Merge *data* for *user* into *userdata['users']*."""
    if user not in userdata["users"]:
        userdata["users"][user] = data
    else:
        _merge_mapping_in_place(userdata["users"][user], data)
    return userdata


def _merge_mapping_in_place(target: dict[str, Any], incoming: dict[str, Any]) -> None:
    """Recursively merge *incoming* into *target* without rebuilding *target*."""
    for key, value in incoming.items():
        if key not in target:
            target[key] = value
            continue

        current = target[key]
        if isinstance(current, dict) and isinstance(value, dict):
            _merge_mapping_in_place(current, value)
            continue

        if current == value:
            continue

        if isinstance(current, list):
            if isinstance(value, list):
                current.extend(value)
            else:
                current.append(value)
            continue

        if isinstance(value, list):
            target[key] = [current, *value]
            continue

        target[key] = [current, value]


def match_attributed_user(
    row_values: set[str] | frozenset[str],
    direct_values: dict[str, tuple[str, Any]],
    pkey_value: str,
    fallback_values: dict[str, tuple[str, Any]],
) -> tuple[str | None, Any]:
    """Resolve a result row back to the originating input user.

    Matching precedence is kept identical to the previous backend-local logic:
    1. any row value matching a direct/effective username
    2. the translated primary-key field matching a direct/effective username
    3. any row value matching a fallback fragment value/parameter
    """
    match = next((direct_values[val] for val in row_values if val in direct_values), None)
    if match is not None:
        return match

    if pkey_value in direct_values:
        return direct_values[pkey_value]

    match = next((fallback_values[val] for val in row_values if val in fallback_values), None)
    if match is not None:
        return match

    return None, False
