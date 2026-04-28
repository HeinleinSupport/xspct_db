# GitHub Copilot instructions for xspct_db

You are working on **xspct_db** — an async Python HTTP service (aiohttp) that provides a multi-backend database query API with Redis caching and Rspamd integration.

## Project layout

```
src/xspct_db/          # Main package
  __init__.py          # Package version (__version__)
  __main__.py          # Entry point → server.run()
  auth.py              # API key authentication
  cache.py             # Two-layer cache (L1 TTLCache + L2 Redis)
  config.py            # YAML configuration loading
  routes.py            # aiohttp route definitions and handlers
  schemas.py           # Pydantic request/response models
  server.py            # App factory, startup/shutdown, run()
  stats.py             # Metrics and periodic stats logging
  utils.py             # Shared helpers
  backends/
    base.py            # BaseBackend abstract class
    dummy.py           # No-op backend (testing)
    delay.py           # Delay-injecting wrapper
    ldap_backend.py    # LDAP via bonsai
    mysql_backend.py   # MySQL via aiomysql
    yaml_backend.py    # Static YAML file backend
tests/                 # pytest-asyncio test suite
  conftest.py          # Shared fixtures (base_cfg, yaml_cfg, app_client, yaml_app_client)
  backends/            # Per-backend unit tests
LICENSES/EUPL-1.2.txt  # Canonical licence text (REUSE)
REUSE.toml             # REUSE 3.0 compliance manifest
pyproject.toml         # Build metadata (hatchling)
```

## HTTP endpoints

| Method | Path | Auth | Description |
|--------|------|------|-------------|
| GET | `/` | — | Health check |
| GET | `/ping` | — | Returns `Pong` |
| GET | `/metrics` | optional | Prometheus metrics |
| GET | `/v1/query/{user}` | required | Single-user lookup |
| POST | `/v1/query-json` | required | Batch user lookup |
| POST | `/v1/rspamd-settings` | required | Rspamd settings blob |

Legacy path prefixes (`/query/v1/{user}`, `/query-json/v1`, `/rspamd-settings/v1`) are also registered for backwards compatibility.

## Conventions

- **Licence header** on every source file, lines 1–2:
  ```python
  # SPDX-License-Identifier: EUPL-1.2
  # SPDX-FileCopyrightText: <year> Carsten Rosenberg <c.rosenberg@heinlein-support.de>
  ```
- **Version** must be kept in sync: `pyproject.toml` and `src/xspct_db/__init__.py`.
- **Async first** — all I/O uses `async`/`await`; tests use `pytest-asyncio` with `asyncio_mode = "auto"`.
- **New backends** subclass `backends.base.BaseBackend`; use `dummy.py` as the minimal skeleton.
- **Rspamd protocol** — the `text/plain; version=0.0.4` content-type in `routes.py` is protocol-mandated; do not change it.
- **Cache config** — L1 (in-process `TTLCache`) is configured under `xspct_db_local_cache`; L2 (Redis) is configured under `xspct_db_redis_cache`. Do not mix keys between these dicts.

## Constraints

- Do NOT add synchronous blocking I/O.
- Do NOT skip SPDX headers on new files.
- Do NOT change the response schema of existing HTTP endpoints.
- Only touch `REUSE.toml` for non-source assets needing explicit annotation.
- **Test email addresses** must use `@mailexample.de` as the domain. Do not use any other domain in tests.
