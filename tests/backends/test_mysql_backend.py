# SPDX-License-Identifier: EUPL-1.2
# SPDX-FileCopyrightText: 2026 Carsten Rosenberg <c.rosenberg@heinlein-support.de>

"""Tests for the MySQL backend using mocked aiomysql."""

from __future__ import annotations

import sys
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from xspct_db import stats
from xspct_db.backends import mysql_backend

# ---------------------------------------------------------------------------
# Shared config
# ---------------------------------------------------------------------------

MYSQL_CFG: dict[str, Any] = {
    "xspct_db_key_translation": {},
    "xspct_db_value_split": {},
    "xspct_db_queries": {
        "mysql_users": {
            "db_type": "mysql",
            "server": "127.0.0.1",
            "port": 3306,
            "user": "test",
            "password": "test",
            "database": "testdb",
            "primary_key": "uid",
            "query": 'SELECT uid, mail FROM users WHERE uid="%u"',
            "query_replace": {"%u": "username"},
        }
    },
    "xspct_db_mysql_pool_minconn": 1,
    "xspct_db_mysql_pool_maxconn": 5,
}

_USER_A = {"username": "alice@mailexample.de", "address": "alice@mailexample.de", "userpart": "alice", "domain": "mailexample.de"}
_USER_B = {"username": "bob@mailexample.de", "address": "bob@mailexample.de", "userpart": "bob", "domain": "mailexample.de"}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_aiomysql_mock() -> MagicMock:
    mock = MagicMock()
    mock.Error = type("Error", (Exception,), {})
    mock.DictCursor = MagicMock()
    return mock


def _make_pool(cursor_results: list[dict]) -> tuple[MagicMock, MagicMock]:
    """Return ``(pool, cursor_mock)`` where ``cursor_mock.fetchall`` returns *cursor_results*."""
    cursor = AsyncMock()
    cursor.execute = AsyncMock()
    cursor.fetchall = AsyncMock(return_value=cursor_results)
    cursor.description = ()

    cursor_cm = AsyncMock()
    cursor_cm.__aenter__ = AsyncMock(return_value=cursor)
    cursor_cm.__aexit__ = AsyncMock(return_value=None)

    conn = AsyncMock()
    conn.cursor = MagicMock(return_value=cursor_cm)

    conn_cm = AsyncMock()
    conn_cm.__aenter__ = AsyncMock(return_value=conn)
    conn_cm.__aexit__ = AsyncMock(return_value=None)

    pool = MagicMock()
    pool.acquire = MagicMock(return_value=conn_cm)
    return pool, cursor


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture(autouse=True)
def reset_pools():
    mysql_backend._pools.clear()
    yield
    mysql_backend._pools.clear()


@pytest.fixture(autouse=True)
def reset_stats():
    stats.reset()


# ---------------------------------------------------------------------------
# Tests – error paths
# ---------------------------------------------------------------------------

async def test_query_aiomysql_not_installed():
    with pytest.MonkeyPatch().context() as mp:
        mp.setitem(sys.modules, "aiomysql", None)
        _, _, error = await mysql_backend.query("s", "mysql_users", [_USER_A], {"users": {}}, {}, MYSQL_CFG)
    assert isinstance(error, str) and "500" in error


async def test_query_invalid_query_name():
    aiomysql_mock = _make_aiomysql_mock()
    with pytest.MonkeyPatch().context() as mp:
        mp.setitem(sys.modules, "aiomysql", aiomysql_mock)
        _, _, error = await mysql_backend.query("s", "nonexistent", [_USER_A], {"users": {}}, {}, MYSQL_CFG)
    assert isinstance(error, str) and "500" in error


async def test_query_pool_not_initialised():
    aiomysql_mock = _make_aiomysql_mock()
    with pytest.MonkeyPatch().context() as mp:
        mp.setitem(sys.modules, "aiomysql", aiomysql_mock)
        _, _, error = await mysql_backend.query("s", "mysql_users", [_USER_A], {"users": {}}, {}, MYSQL_CFG)
    assert isinstance(error, str) and "500" in error


async def test_query_mysql_error_returns_500():
    """aiomysql.Error during execute() is caught and returns a 500 error."""
    aiomysql_mock = _make_aiomysql_mock()

    cursor = AsyncMock()
    cursor.execute = AsyncMock(side_effect=aiomysql_mock.Error("boom"))
    cursor.description = ()
    cursor_cm = AsyncMock()
    cursor_cm.__aenter__ = AsyncMock(return_value=cursor)
    cursor_cm.__aexit__ = AsyncMock(return_value=None)
    conn = AsyncMock()
    conn.cursor = MagicMock(return_value=cursor_cm)
    conn_cm = AsyncMock()
    conn_cm.__aenter__ = AsyncMock(return_value=conn)
    conn_cm.__aexit__ = AsyncMock(return_value=None)
    pool = MagicMock()
    pool.acquire = MagicMock(return_value=conn_cm)
    mysql_backend._pools["mysql_users"] = pool

    with pytest.MonkeyPatch().context() as mp:
        mp.setitem(sys.modules, "aiomysql", aiomysql_mock)
        _, _, error = await mysql_backend.query("s", "mysql_users", [_USER_A], {"users": {}}, {}, MYSQL_CFG)

    assert isinstance(error, str) and "500" in error


# ---------------------------------------------------------------------------
# Tests – single user (baseline)
# ---------------------------------------------------------------------------

async def test_query_single_user_merges_result():
    """Single user: execute called once, result merged, user_to_pkey set."""
    aiomysql_mock = _make_aiomysql_mock()
    pool, cursor = _make_pool([{"uid": "alice@mailexample.de", "mail": "alice@mailexample.de"}])
    mysql_backend._pools["mysql_users"] = pool

    with pytest.MonkeyPatch().context() as mp:
        mp.setitem(sys.modules, "aiomysql", aiomysql_mock)
        ud, u2p, error = await mysql_backend.query("s", "mysql_users", [_USER_A], {"users": {}}, {}, MYSQL_CFG)

    assert error is False
    cursor.execute.assert_called_once()
    assert "alice@mailexample.de" in ud["users"]
    assert u2p["alice@mailexample.de"] == "alice@mailexample.de"


async def test_query_single_user_params_correct():
    """Single user: the SQL param contains the username value."""
    aiomysql_mock = _make_aiomysql_mock()
    pool, cursor = _make_pool([{"uid": "alice@mailexample.de", "mail": "alice@mailexample.de"}])
    mysql_backend._pools["mysql_users"] = pool

    with pytest.MonkeyPatch().context() as mp:
        mp.setitem(sys.modules, "aiomysql", aiomysql_mock)
        await mysql_backend.query("s", "mysql_users", [_USER_A], {"users": {}}, {}, MYSQL_CFG)

    _, params = cursor.execute.call_args[0]
    assert "alice@mailexample.de" in params


# ---------------------------------------------------------------------------
# Tests – multi-user batching
# ---------------------------------------------------------------------------

async def test_query_multiple_users_single_execute():
    """Two users produce exactly one execute() call."""
    aiomysql_mock = _make_aiomysql_mock()
    pool, cursor = _make_pool([
        {"uid": "alice@mailexample.de", "mail": "alice@mailexample.de"},
        {"uid": "bob@mailexample.de", "mail": "bob@mailexample.de"},
    ])
    mysql_backend._pools["mysql_users"] = pool

    with pytest.MonkeyPatch().context() as mp:
        mp.setitem(sys.modules, "aiomysql", aiomysql_mock)
        ud, u2p, error = await mysql_backend.query(
            "s", "mysql_users", [_USER_A, _USER_B], {"users": {}}, {}, MYSQL_CFG
        )

    assert error is False
    cursor.execute.assert_called_once()
    sql, params = cursor.execute.call_args[0]
    assert " OR " in sql
    assert "alice@mailexample.de" in params
    assert "bob@mailexample.de" in params
    assert "alice@mailexample.de" in ud["users"]
    assert "bob@mailexample.de" in ud["users"]


async def test_query_multiple_users_user_to_pkey():
    """user_to_pkey is correctly set for each input user after a batch query."""
    aiomysql_mock = _make_aiomysql_mock()
    pool, cursor = _make_pool([
        {"uid": "alice@mailexample.de", "mail": "alice@mailexample.de"},
        {"uid": "bob@mailexample.de", "mail": "bob@mailexample.de"},
    ])
    mysql_backend._pools["mysql_users"] = pool

    with pytest.MonkeyPatch().context() as mp:
        mp.setitem(sys.modules, "aiomysql", aiomysql_mock)
        ud, u2p, error = await mysql_backend.query(
            "s", "mysql_users", [_USER_A, _USER_B], {"users": {}}, {}, MYSQL_CFG
        )

    assert error is False
    # Each input user's orig_username maps to their result pk.
    assert u2p.get("alice@mailexample.de") == "alice@mailexample.de"
    assert u2p.get("bob@mailexample.de") == "bob@mailexample.de"


async def test_query_no_results_returns_empty_userdata():
    """Query returning no rows leaves userdata empty."""
    aiomysql_mock = _make_aiomysql_mock()
    pool, cursor = _make_pool([])
    mysql_backend._pools["mysql_users"] = pool

    with pytest.MonkeyPatch().context() as mp:
        mp.setitem(sys.modules, "aiomysql", aiomysql_mock)
        ud, u2p, error = await mysql_backend.query(
            "s", "mysql_users", [_USER_A, _USER_B], {"users": {}}, {}, MYSQL_CFG
        )

    assert error is False
    assert ud["users"] == {}
    assert u2p == {}


# ---------------------------------------------------------------------------
# Tests – wildcard / catch-all attribution (third fallback)
# ---------------------------------------------------------------------------

# Config whose WHERE includes a domain wildcard param: "@domain.tld".
_WILDCARD_CFG: dict[str, Any] = {
    "xspct_db_key_translation": {},
    "xspct_db_value_split": {},
    "xspct_db_queries": {
        "mysql_wc": {
            "db_type": "mysql",
            "server": "127.0.0.1",
            "port": 3306,
            "user": "test",
            "password": "test",
            "database": "testdb",
            "primary_key": "uid",
            # WHERE produces params [username, username, @domain] per user.
            "query": 'SELECT destination AS uid, email AS mailLocalAddress'
                     ' FROM view_aliases WHERE (destination="%u" OR email="%u" OR email="@%d")',
            "query_replace": {"%u": "username", "%d": "domain"},
        }
    },
}

_USER_WILDCARD = {
    "username": "cr@mailexample.de",
    "address": "cr@mailexample.de",
    "userpart": "cr",
    "domain": "mailexample.de",
}


async def test_query_wildcard_catchall_attribution():
    """Third fallback: row matched by a domain wildcard param is attributed correctly.

    The result row has uid=cr-primary@mailexample.de and mailLocalAddress=@mailexample.de.
    Neither the effective username (cr@mailexample.de) nor the pk field value
    (cr-primary@mailexample.de) appear as a key in effective_to_user, so the first two
    attribution fallbacks miss.  The third fallback detects that the frag
    param @mailexample.de is present in the row values and correctly attributes
    the row to user cr@mailexample.de, setting
    user_to_pkey["cr@mailexample.de"] = "cr-primary@mailexample.de".
    """
    aiomysql_mock = _make_aiomysql_mock()
    # Row: the catch-all alias maps @mailexample.de → cr-primary@mailexample.de.
    pool, cursor = _make_pool([
        {"uid": "cr-primary@mailexample.de", "mailLocalAddress": "@mailexample.de"},
    ])
    mysql_backend._pools["mysql_wc"] = pool

    with pytest.MonkeyPatch().context() as mp:
        mp.setitem(sys.modules, "aiomysql", aiomysql_mock)
        ud, u2p, error = await mysql_backend.query(
            "s", "mysql_wc", [_USER_WILDCARD], {"users": {}}, {}, _WILDCARD_CFG
        )

    assert error is False
    # The row must be merged under its primary key.
    assert "cr-primary@mailexample.de" in ud["users"]
    # The input username must map to the resolved primary key.
    assert u2p.get("cr@mailexample.de") == "cr-primary@mailexample.de"


async def test_query_wildcard_catchall_multiple_users():
    """Third fallback works correctly when multiple users are batched.

    Only the user whose domain param matches the catch-all row should be
    attributed; the other user (with no matching row) must not appear in
    user_to_pkey.
    """
    _USER_OTHER = {
        "username": "test@other.mailexample.de",
        "address": "test@other.mailexample.de",
        "userpart": "test",
        "domain": "other.mailexample.de",
    }
    aiomysql_mock = _make_aiomysql_mock()
    pool, cursor = _make_pool([
        {"uid": "cr-primary@mailexample.de", "mailLocalAddress": "@mailexample.de"},
    ])
    mysql_backend._pools["mysql_wc"] = pool

    with pytest.MonkeyPatch().context() as mp:
        mp.setitem(sys.modules, "aiomysql", aiomysql_mock)
        ud, u2p, error = await mysql_backend.query(
            "s", "mysql_wc", [_USER_OTHER, _USER_WILDCARD], {"users": {}}, {}, _WILDCARD_CFG
        )

    assert error is False
    assert u2p.get("cr@mailexample.de") == "cr-primary@mailexample.de"
    assert "test@other.mailexample.de" not in u2p
