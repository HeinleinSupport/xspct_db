# SPDX-License-Identifier: EUPL-1.2
# SPDX-FileCopyrightText: 2026 Carsten Rosenberg <c.rosenberg@heinlein-support.de>

"""Configuration loading, validation, and default values."""

from __future__ import annotations

import logging
import os
import sys
from typing import Any

import yaml

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Default configuration
# ---------------------------------------------------------------------------

DEFAULTS: dict[str, Any] = {
    "xspct_db_listen_address": ["127.0.0.1", "::1"],
    "xspct_db_listen_port": "11350",
    "xspct_db_listen_backlog": 256,
    # 10:DEBUG, 20:INFO, 30:WARNING, 40:ERROR, 50:CRITICAL
    "xspct_db_log_level": 30,
    "xspct_db_log_prefix": "Xspct_DB",
    "xspct_db_api_header": "X-Api-Key",
    "xspct_db_api_key": "changeme",
    "xspct_db_api_key_verify_fail": True,
    "xspct_db_client_max_size": 1048576,  # 1 MiB
    "xspct_db_rspamd_header": "X-Rspamd-ID",
    "xspct_db_request_timeout": 0,
    "xspct_db_request_timeout_header": "",
    "xspct_db_request_timeout_header_max": 120,
    "xspct_db_query_json_max_users": 500,
    "xspct_db_foreground_slots": 30,
    "xspct_db_background_slots": 5,
    "xspct_db_stats_enabled": True,
    "xspct_db_stats_interval": 60,
    "xspct_db_stats_sample_interval": 10,
    "xspct_db_metrics_auth": {
        "enabled": False,
        "api_key": True,
        "basic_auth_users": {},
    },
    "xspct_db_tls": {
        "tls_enabled": False,
        "tls_cert": "",
        "tls_key": "",
    },
    "xspct_db_key_translation": {},
    "xspct_db_value_split": {},
    "xspct_db_queries": {},
    "xspct_db_ldap_pool_minconn": 2,
    "xspct_db_ldap_pool_maxconn": 20,
    "xspct_db_mysql_pool_minconn": 1,
    "xspct_db_mysql_pool_maxconn": 20,
    "xspct_db_redis_cache": {
        "enabled": False,
        "host": "localhost",
        "port": 6379,
        "user": "",
        "password": "",
        "decode_responses": True,
        "prefix_user": "xspct_db_user_",
        "prefix_alias": "xspct_db_alias_",
        "prefix_negative_alias": "xspct_db_neg_alias_",
        "expire": 60,
        "expire_negative": 60,
        "connect_timeout": 1,
        "query_timeout": 1,
        "max_connections": 40,
        "max_errors": 2,
    },
    "xspct_db_yaml_data": {},
    "xspct_db_rspamd_alias_fields": ["aliases"],
    "xspct_db_local_cache": {
        "enabled": True,
        "expire": 20,
        "expire_negative": 20,
        "max_entries": 10000,
    },
    "xspct_db_response_cache": {
        "enabled": True,
        "expire": 10,
        "max_entries": 5000,
        "rspamd_key_fields": ["from", "rcpts", "mta-name", "settings-name", "settings-id"],
    },
}

# Keys whose sub-dicts are deep-merged instead of replaced wholesale.
_DEEP_MERGE_KEYS = (
    "xspct_db_redis_cache",
    "xspct_db_tls",
    "xspct_db_metrics_auth",
    "xspct_db_local_cache",
    "xspct_db_response_cache",
)


def load(config_path: str) -> dict[str, Any]:
    """Load configuration from *config_path* and merge it over the defaults.

    Raises ``SystemExit`` on unrecoverable errors (missing file, YAML parse
    errors) so the process exits cleanly without a traceback.
    """
    cfg: dict[str, Any] = dict(DEFAULTS)
    # Deep-copy the nested dicts so mutations don't bleed between calls.
    for key in _DEEP_MERGE_KEYS:
        cfg[key] = dict(DEFAULTS[key])

    if not os.path.isfile(config_path):
        print(f"Config file not found: {config_path}", file=sys.stderr)
        sys.exit(1)

    try:
        with open(config_path) as fh:
            extra: dict[str, Any] = yaml.safe_load(fh) or {}
    except PermissionError as exc:
        logger.exception("PermissionError reading config: %s", exc)
        sys.exit(1)
    except yaml.YAMLError as exc:
        logger.exception("YAML parse error in config: %s", exc)
        sys.exit(1)

    for key in _DEEP_MERGE_KEYS:
        if key in extra:
            merged = dict(cfg[key])
            merged.update(extra.pop(key))
            cfg[key] = merged

    cfg.update(extra)

    # Normalise api key to a list for uniform iteration in auth checks.
    if isinstance(cfg["xspct_db_api_key"], str):
        cfg["xspct_db_api_key"] = [cfg["xspct_db_api_key"]]

    return cfg
