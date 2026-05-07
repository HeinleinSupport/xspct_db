# SPDX-License-Identifier: EUPL-1.2
# SPDX-FileCopyrightText: 2026 Carsten Rosenberg <c.rosenberg@heinlein-support.de>

"""Tests for the backend dispatcher and query scheduling."""

from __future__ import annotations

import asyncio
import time
from typing import Any

import pytest

from xspct_db.backends import run_queries
from xspct_db.backends.base import merge_userdata

_USER = {
    "username": "alice@mailexample.de",
    "address": "alice@mailexample.de",
    "userpart": "alice",
    "domain": "mailexample.de",
}


async def test_run_queries_parallelises_independent_queries(monkeypatch: pytest.MonkeyPatch):
    """Independent queries run concurrently but merge in config order."""
    from xspct_db.backends import delay as delay_backend

    cfg: dict[str, Any] = {
        "xspct_db_queries": {
            "first": {"db_type": "delay", "delay": 0.06},
            "second": {"db_type": "delay", "delay": 0.06},
        },
    }

    async def fake_delay_query(
        s: str,
        query_name: str,
        users: list[dict[str, Any]],
        userdata: dict[str, Any],
        user_to_pkey: dict[str, Any],
        cfg: dict[str, Any],
    ) -> tuple[dict[str, Any], dict[str, Any], str | bool]:
        await asyncio.sleep(float(cfg["xspct_db_queries"][query_name]["delay"]))
        userdata = merge_userdata(
            s,
            users[0]["username"],
            {"steps": [query_name]},
            userdata,
        )
        user_to_pkey[users[0]["username"]] = users[0]["username"]
        return userdata, user_to_pkey, False

    monkeypatch.setattr(delay_backend, "query", fake_delay_query)

    started = time.perf_counter()
    userdata, user_to_pkey, error = await run_queries("s", _USER["username"], False, [_USER], {"users": {}}, {}, cfg)
    elapsed = time.perf_counter() - started

    assert error is False
    assert elapsed < 0.10
    assert userdata["users"][_USER["username"]]["steps"] == ["first", "second"]
    assert user_to_pkey[_USER["username"]] == _USER["username"]


async def test_run_queries_keeps_use_result_queries_sequential(monkeypatch: pytest.MonkeyPatch):
    """Queries with use_result still see the merged state from prior phases."""
    from xspct_db.backends import yaml_backend

    cfg: dict[str, Any] = {
        "xspct_db_queries": {
            "first": {"db_type": "yaml"},
            "second": {
                "db_type": "yaml",
                "use_result": True,
                "result_object_attr": "domain",
            },
        },
    }
    second_ran = False

    async def fake_yaml_query(
        s: str,
        query_name: str,
        users: list[dict[str, Any]],
        userdata: dict[str, Any],
        user_to_pkey: dict[str, Any],
        cfg: dict[str, Any],
    ) -> tuple[dict[str, Any], dict[str, Any], str | bool]:
        nonlocal second_ran
        if query_name == "first":
            userdata = merge_userdata(
                s,
                users[0]["username"],
                {"domain": ["mailexample.de"], "steps": ["first"]},
                userdata,
            )
            user_to_pkey[users[0]["username"]] = users[0]["username"]
            return userdata, user_to_pkey, False

        second_ran = True
        assert user_to_pkey[users[0]["username"]] == users[0]["username"]
        assert userdata["users"][users[0]["username"]]["domain"] == ["mailexample.de"]
        userdata = merge_userdata(
            s,
            users[0]["username"],
            {"steps": ["second"]},
            userdata,
        )
        return userdata, user_to_pkey, False

    monkeypatch.setattr(yaml_backend, "query", fake_yaml_query)

    userdata, user_to_pkey, error = await run_queries("s", _USER["username"], False, [_USER], {"users": {}}, {}, cfg)

    assert error is False
    assert second_ran is True
    assert userdata["users"][_USER["username"]]["steps"] == ["first", "second"]
    assert user_to_pkey[_USER["username"]] == _USER["username"]
