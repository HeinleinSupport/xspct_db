# SPDX-License-Identifier: EUPL-1.2
# SPDX-FileCopyrightText: 2026 Carsten Rosenberg <c.rosenberg@heinlein-support.de>

"""Tests for the LDAP backend using mocked bonsai."""

from __future__ import annotations

import sys
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from xspct_db.backends import ldap_backend
from xspct_db import stats

# ---------------------------------------------------------------------------
# Shared config
# ---------------------------------------------------------------------------

LDAP_CFG: dict[str, Any] = {
    "xspct_db_key_translation": {},
    "xspct_db_value_split": {},
    "xspct_db_queries": {
        "ldap_users": {
            "db_type": "ldap",
            "server": "ldap://localhost",
            "bind_dn": "cn=admin,dc=example,dc=com",
            "bind_dn_pw": "secret",
            "base_dn": "ou=users,dc=example,dc=com",
            "search_filter": "(mail=alice@example.com)",
            "primary_key": "mail",
            "attr_list": ["mail", "uid"],
        }
    },
    "xspct_db_ldap_pool_minconn": 2,
    "xspct_db_ldap_pool_maxconn": 20,
}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_bonsai_mock() -> MagicMock:
    """Return a minimal mock of the bonsai module."""
    mock = MagicMock()
    # Make LDAPError and AuthenticationError actual Exception subclasses so
    # they work as exception types in except clauses.
    mock.errors = MagicMock()
    mock.errors.LDAPError = type("LDAPError", (Exception,), {})
    mock.errors.AuthenticationError = type("AuthenticationError", (Exception,), {})
    mock.escape_filter_exp = lambda s: s
    return mock


def _make_pool(conn: AsyncMock) -> MagicMock:
    """Return a mock pool whose spawn() context manager yields *conn*."""
    cm = AsyncMock()
    cm.__aenter__ = AsyncMock(return_value=conn)
    cm.__aexit__ = AsyncMock(return_value=None)
    pool = MagicMock()
    pool.spawn.return_value = cm
    return pool


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture(autouse=True)
def reset_pools():
    ldap_backend._pools.clear()
    yield
    ldap_backend._pools.clear()


@pytest.fixture(autouse=True)
def reset_stats():
    stats.reset()


# ---------------------------------------------------------------------------
# Tests – query()
# ---------------------------------------------------------------------------

async def test_query_bonsai_not_installed():
    """query() returns a 500 error string when bonsai cannot be imported."""
    with pytest.MonkeyPatch().context() as mp:
        mp.setitem(sys.modules, "bonsai", None)
        mp.setitem(sys.modules, "bonsai.asyncio", None)
        _, _, error = await ldap_backend.query(
            "s", "ldap_users",
            [{"username": "alice@example.com"}],
            {"users": {}}, {}, LDAP_CFG,
        )
    assert isinstance(error, str) and "500" in error


async def test_query_invalid_query_name():
    """query() returns 500 when the query name is absent from config."""
    bonsai_mock = _make_bonsai_mock()
    with pytest.MonkeyPatch().context() as mp:
        mp.setitem(sys.modules, "bonsai", bonsai_mock)
        mp.setitem(sys.modules, "bonsai.asyncio", bonsai_mock)
        _, _, error = await ldap_backend.query(
            "s", "nonexistent",
            [{"username": "alice@example.com"}],
            {"users": {}}, {}, LDAP_CFG,
        )
    assert isinstance(error, str) and "500" in error


async def test_query_pool_not_initialised():
    """query() returns 500 when no pool has been created for the query name."""
    bonsai_mock = _make_bonsai_mock()
    with pytest.MonkeyPatch().context() as mp:
        mp.setitem(sys.modules, "bonsai", bonsai_mock)
        mp.setitem(sys.modules, "bonsai.asyncio", bonsai_mock)
        _, _, error = await ldap_backend.query(
            "s", "ldap_users",
            [{"username": "alice@example.com"}],
            {"users": {}}, {}, LDAP_CFG,
        )
    assert isinstance(error, str) and "500" in error


async def test_query_success():
    """query() merges LDAP search results into userdata on success."""
    bonsai_mock = _make_bonsai_mock()
    conn = AsyncMock()
    conn.search = AsyncMock(return_value=[
        {"mail": "alice@example.com", "uid": "alice"},
    ])
    ldap_backend._pools["ldap_users"] = _make_pool(conn)

    with pytest.MonkeyPatch().context() as mp:
        mp.setitem(sys.modules, "bonsai", bonsai_mock)
        mp.setitem(sys.modules, "bonsai.asyncio", bonsai_mock)
        result_ud, result_u2p, error = await ldap_backend.query(
            "s", "ldap_users",
            [{"username": "alice@example.com"}],
            {"users": {}}, {}, LDAP_CFG,
        )

    assert error is False
    assert "alice@example.com" in result_ud["users"]
    assert result_ud["users"]["alice@example.com"]["uid"] == ["alice"]
    assert result_u2p["alice@example.com"] == "alice@example.com"


async def test_query_ldap_error_during_search():
    """LDAPError inside conn.search() is caught; query returns a 500 error."""
    bonsai_mock = _make_bonsai_mock()
    conn = AsyncMock()
    conn.search = AsyncMock(side_effect=bonsai_mock.errors.LDAPError("boom"))
    ldap_backend._pools["ldap_users"] = _make_pool(conn)

    with pytest.MonkeyPatch().context() as mp:
        mp.setitem(sys.modules, "bonsai", bonsai_mock)
        mp.setitem(sys.modules, "bonsai.asyncio", bonsai_mock)
        _, _, error = await ldap_backend.query(
            "s", "ldap_users",
            [{"username": "alice@example.com"}],
            {"users": {}}, {}, LDAP_CFG,
        )

    assert isinstance(error, str) and "500" in error


async def test_query_connection_error():
    """Exception from pool.spawn() is caught; query returns a 500 error."""
    bonsai_mock = _make_bonsai_mock()
    pool = MagicMock()
    cm = AsyncMock()
    cm.__aenter__ = AsyncMock(side_effect=Exception("connection refused"))
    cm.__aexit__ = AsyncMock(return_value=None)
    pool.spawn.return_value = cm
    ldap_backend._pools["ldap_users"] = pool

    with pytest.MonkeyPatch().context() as mp:
        mp.setitem(sys.modules, "bonsai", bonsai_mock)
        mp.setitem(sys.modules, "bonsai.asyncio", bonsai_mock)
        _, _, error = await ldap_backend.query(
            "s", "ldap_users",
            [{"username": "alice@example.com"}],
            {"users": {}}, {}, LDAP_CFG,
        )

    assert isinstance(error, str) and "500" in error


# ---------------------------------------------------------------------------
# Tests – create_pools() / close_pools()
# ---------------------------------------------------------------------------

async def test_create_pools_skips_non_ldap_queries():
    """create_pools() ignores queries whose db_type is not 'ldap'."""
    bonsai_mock = _make_bonsai_mock()
    cfg = {**LDAP_CFG, "xspct_db_queries": {"q": {"db_type": "dummy"}}}
    with pytest.MonkeyPatch().context() as mp:
        mp.setitem(sys.modules, "bonsai", bonsai_mock)
        mp.setitem(sys.modules, "bonsai.asyncio", bonsai_mock)
        await ldap_backend.create_pools(cfg)
    assert ldap_backend._pools == {}


async def test_create_pools_bonsai_not_installed():
    """create_pools() exits silently when bonsai is not importable."""
    with pytest.MonkeyPatch().context() as mp:
        mp.setitem(sys.modules, "bonsai", None)
        mp.setitem(sys.modules, "bonsai.asyncio", None)
        await ldap_backend.create_pools(LDAP_CFG)
    assert ldap_backend._pools == {}


def test_close_pools():
    """close_pools() calls close() on every pool and empties the registry."""
    pool = MagicMock()
    ldap_backend._pools["ldap_users"] = pool
    ldap_backend.close_pools()
    pool.close.assert_called_once()
    assert ldap_backend._pools == {}
