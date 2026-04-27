# SPDX-License-Identifier: EUPL-1.2
# SPDX-FileCopyrightText: 2026 Carsten Rosenberg <c.rosenberg@heinlein-support.de>

"""Tests for xspct_db.auth."""

from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock

from xspct_db.auth import verify_api_key, verify_metrics_auth


def _cfg(
    keys: list[str] | None = None,
    verify_fail: bool = True,
    metrics_enabled: bool = False,
    basic_users: dict[str, str] | None = None,
) -> dict[str, Any]:
    return {
        "xspct_db_api_header": "X-Api-Key",
        "xspct_db_api_key": keys or ["correct-key"],
        "xspct_db_api_key_verify_fail": verify_fail,
        "xspct_db_metrics_auth": {
            "enabled": metrics_enabled,
            "api_key": True,
            "basic_auth_users": basic_users or {},
        },
    }


# ---------------------------------------------------------------------------
# verify_api_key
# ---------------------------------------------------------------------------

def test_correct_key_accepted():
    assert verify_api_key("s", "correct-key", _cfg()) is True


def test_wrong_key_rejected():
    assert verify_api_key("s", "wrong-key", _cfg()) is False


def test_missing_key_rejected():
    assert verify_api_key("s", None, _cfg()) is False


def test_multiple_valid_keys_accepted():
    cfg = _cfg(keys=["key-a", "key-b"])
    assert verify_api_key("s", "key-a", cfg) is True
    assert verify_api_key("s", "key-b", cfg) is True


def test_permissive_mode_accepts_wrong_key():
    cfg = _cfg(verify_fail=False)
    assert verify_api_key("s", "anything", cfg) is True


# ---------------------------------------------------------------------------
# verify_metrics_auth
# ---------------------------------------------------------------------------

def _mock_request(api_key: str | None = None, auth_header: str | None = None) -> MagicMock:
    req = MagicMock()
    headers: dict[str, str] = {}
    if api_key is not None:
        headers["X-Api-Key"] = api_key
    if auth_header is not None:
        headers["Authorization"] = auth_header
    req.headers = headers
    return req


def test_metrics_unauthenticated_by_default():
    cfg = _cfg(metrics_enabled=False)
    assert verify_metrics_auth("s", _mock_request(), cfg) is True


def test_metrics_api_key_accepted():
    cfg = _cfg(metrics_enabled=True)
    req = _mock_request(api_key="correct-key")
    assert verify_metrics_auth("s", req, cfg) is True


def test_metrics_wrong_api_key_rejected():
    cfg = _cfg(metrics_enabled=True)
    req = _mock_request(api_key="bad-key")
    assert verify_metrics_auth("s", req, cfg) is False


def test_metrics_basic_auth_accepted():
    import base64

    cfg = _cfg(metrics_enabled=True, basic_users={"prom": "secret"})
    token = base64.b64encode(b"prom:secret").decode()
    req = _mock_request(auth_header=f"Basic {token}")
    assert verify_metrics_auth("s", req, cfg) is True


def test_metrics_basic_auth_wrong_password():
    import base64

    cfg = _cfg(metrics_enabled=True, basic_users={"prom": "secret"})
    token = base64.b64encode(b"prom:wrong").decode()
    req = _mock_request(auth_header=f"Basic {token}")
    assert verify_metrics_auth("s", req, cfg) is False
