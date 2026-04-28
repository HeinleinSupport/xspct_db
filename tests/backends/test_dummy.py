# SPDX-License-Identifier: EUPL-1.2
# SPDX-FileCopyrightText: 2026 Carsten Rosenberg <c.rosenberg@heinlein-support.de>

"""Tests for the dummy backend."""

from __future__ import annotations

from typing import Any

from xspct_db.backends.dummy import error_query, query


def _cfg(**extra: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "xspct_db_key_translation": {},
        "xspct_db_value_split": {},
        "xspct_db_queries": {
            "test_dummy": {"db_type": "dummy"},
        },
    }
    base.update(extra)
    return base


def test_dummy_returns_uid():
    users = [{"username": "user@mailexample.de"}]
    userdata = query("s", "test_dummy", users, _cfg())
    assert "user@mailexample.de" in userdata["users"]
    assert userdata["users"]["user@mailexample.de"]["uid"] == "user@mailexample.de"


def test_dummy_multiple_users():
    users = [{"username": "a@mailexample.de"}, {"username": "b@mailexample.de"}]
    userdata = query("s", "test_dummy", users, _cfg())
    assert len(userdata["users"]) == 2


def test_dummy_invalid_query_name():
    users = [{"username": "user@mailexample.de"}]
    userdata = query("s", "nonexistent", users, _cfg())
    assert userdata["users"] == {}


def test_error_query_returns_500():
    result = error_query("s", "test_dummy", _cfg())
    assert result.startswith("500")
