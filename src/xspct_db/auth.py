# SPDX-License-Identifier: EUPL-1.2
# SPDX-FileCopyrightText: 2026 Carsten Rosenberg <c.rosenberg@heinlein-support.de>

"""API-key and HTTP Basic auth verification for query and metrics endpoints."""

from __future__ import annotations

import base64
import hmac
import logging
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from aiohttp.web import Request

logger = logging.getLogger(__name__)


def verify_api_key(s: str, key: str | None, cfg: dict[str, Any]) -> bool:
    """Return ``True`` when *key* matches any configured API key.

    All configured keys are always checked with ``hmac.compare_digest`` to
    prevent timing-based side-channel leakage.  The key value is never logged.

    If ``xspct_db_api_key_verify_fail`` is ``False`` the function returns
    ``True`` unconditionally (permissive / dev mode).
    """
    provided = str(key or "")
    valid = False
    for k in cfg["xspct_db_api_key"]:
        valid |= hmac.compare_digest(provided, str(k))

    if valid:
        logger.debug("%s - api key verification success", s)
        return True
    if not cfg["xspct_db_api_key_verify_fail"]:
        logger.debug("%s - api key verification failed – not fatal (permissive mode)", s)
        return True
    logger.error("%s - api key verification failed", s)
    return False


def verify_metrics_auth(s: str, request: "Request", cfg: dict[str, Any]) -> bool:
    """Return ``True`` when the /metrics request passes authentication.

    When ``xspct_db_metrics_auth.enabled`` is ``False`` (the default) the
    endpoint is unauthenticated.  When enabled, either a valid API key header
    or valid HTTP Basic auth credentials are accepted.
    """
    auth_cfg: dict[str, Any] = cfg.get("xspct_db_metrics_auth", {})
    if not auth_cfg.get("enabled", False):
        return True

    # --- API key ---
    if auth_cfg.get("api_key", True):
        provided = str(request.headers.get(cfg["xspct_db_api_header"]) or "")
        for k in cfg["xspct_db_api_key"]:
            if hmac.compare_digest(provided, str(k)):
                logger.debug("%s - metrics api key verification success", s)
                return True

    # --- HTTP Basic auth ---
    basic_users: dict[str, str] = auth_cfg.get("basic_auth_users", {})
    if basic_users:
        auth_header = request.headers.get("Authorization", "")
        if auth_header.startswith("Basic "):
            try:
                decoded = base64.b64decode(auth_header[6:]).decode("utf-8", errors="strict")
                username, _, password = decoded.partition(":")
                if username in basic_users:
                    if hmac.compare_digest(password, str(basic_users[username])):
                        logger.debug("%s - metrics basic auth success for user: %s", s, username)
                        return True
            except Exception:
                pass

    logger.warning("%s - metrics auth failed", s)
    return False
