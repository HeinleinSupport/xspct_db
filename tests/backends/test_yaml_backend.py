# SPDX-License-Identifier: EUPL-1.2
# SPDX-FileCopyrightText: 2026 Carsten Rosenberg <c.rosenberg@heinlein-support.de>

"""Tests for the YAML backend."""

from __future__ import annotations

from typing import Any

from xspct_db.backends.yaml_backend import query


def _cfg(yaml_data: dict[str, Any] | None = None) -> dict[str, Any]:
    return {
        "xspct_db_key_translation": {},
        "xspct_db_value_split": {},
        "xspct_db_queries": {
            "users": {
                "db_type": "yaml",
                "primary_key": "mail",
                "attr_list": ["*"],
                "search_filter": ["mail", "aliases"],
            }
        },
        "xspct_db_yaml_data": {
            "users": yaml_data
            or {
                "alice@mailexample.de": {
                    "mail": "alice@mailexample.de",
                    "uid": "alice",
                    "aliases": ["a@mailexample.de"],
                },
            }
        },
    }


async def test_yaml_known_user():
    users = [{"username": "alice@mailexample.de"}]
    ud, pkey, err = await query("s", "users", users, {"users": {}}, {}, _cfg())
    assert err is False
    assert len(ud["users"]) == 1


async def test_yaml_unknown_user_returns_empty():
    users = [{"username": "nobody@mailexample.de"}]
    ud, pkey, err = await query("s", "users", users, {"users": {}}, {}, _cfg())
    assert err is False
    assert ud["users"] == {}


async def test_yaml_alias_lookup():
    users = [{"username": "a@mailexample.de"}]
    ud, pkey, err = await query("s", "users", users, {"users": {}}, {}, _cfg())
    assert err is False
    assert len(ud["users"]) == 1


async def test_yaml_invalid_query_name():
    users = [{"username": "alice@mailexample.de"}]
    _, _, err = await query("s", "nonexistent", users, {"users": {}}, {}, _cfg())
    assert isinstance(err, str) and err.startswith("500")


async def test_yaml_missing_yaml_root_returns_empty():
    """Missing yaml_root key returns empty results without error."""
    cfg = _cfg()
    cfg["xspct_db_yaml_data"] = {}  # data missing entirely
    users = [{"username": "alice@mailexample.de"}]
    ud, _, err = await query("s", "users", users, {"users": {}}, {}, cfg)
    assert err is False
    assert ud["users"] == {}
