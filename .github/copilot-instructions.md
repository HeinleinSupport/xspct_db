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
- **Cache config** — three separate top-level dicts; do not mix keys between them:
  - `xspct_db_local_cache` — L1 in-process `TTLCache` for object lookups (enabled by default)
  - `xspct_db_redis_cache` — L2 Redis cache for object lookups (optional)
  - `xspct_db_response_cache` — L1 `TTLCache` for full JSON response bytes for `POST /v1/query-json` and `POST /v1/rspamd-settings` (enabled by default)
- **Concurrency config** — two top-level integer keys control the semaphore capacities:
  - `xspct_db_foreground_slots` (default `30`) — concurrent client-blocking query slots
  - `xspct_db_background_slots` (default `5`) — concurrent background-continuation slots
- **Queue app keys** — `app["fg_sem"]`, `app["bg_sem"]`, `app["bg_tasks"]` are created in `server._on_startup()`; do not access them outside `routes.py` helpers.

## Constraints

- Do NOT add synchronous blocking I/O.
- Do NOT skip SPDX headers on new files.
- Do NOT change the response schema of existing HTTP endpoints.
- Only touch `REUSE.toml` for non-source assets needing explicit annotation.
- **Test email addresses** must use `@mailexample.de` as the domain. Do not use any other domain in tests.
- **Queue changes** — when adding or modifying timeout/concurrency behaviour:
  - Keep `_run_with_queues()` as the single entry point; do not add semaphore logic inline in handlers.
  - New stats counters must be added to `stats.py` (`stats` dict + `reset()`), the `_prometheus_lines()` table in `routes.py`, and `conftest.base_cfg` (with a safe default value).
  - Use the `delay` backend (`db_type: delay`, `delay: <seconds>`) to test timeouts; set `xspct_db_request_timeout` below the delay value.
  - Each query handler must catch `_ServiceOverloaded` → 503 and check `timed_out=True` → 504.
  - When adding features that run inside the background task, the inner coroutine must include the cache write so background completions warm the cache.

## Code Quality

Run checks with:
```bash
ruff check src/ tests/              # lint (E, F, W, I rules)
ruff check --select I --fix src/ tests/  # fix import order
ruff format src/ tests/             # format (line-length = 100)
reuse lint                          # SPDX / REUSE compliance
```

Slash commands available in VS Code Copilot chat:
- `/check-code` — ruff lint + reuse lint
- `/format-code` — ruff format + import sort
- `/run-tests` — pytest (optional `-k <filter>` or `--cov`)
- `/prepare-commit` — full pre-commit workflow with GPG reminder

## Commit Convention

All commits must follow the format: **`[Tag] Description`**

| Tag | When to use |
|-----|-------------|
| `[Feature]` | New user-visible feature |
| `[Fix]` | Bug fix |
| `[Minor]` | Small/trivial change (whitespace, nil check, typo) |
| `[Rework]` | Major refactoring |
| `[Conf]` | Configuration change |
| `[Test]` | Test-only change |
| `[Docs]` | Documentation only |
| `[Project]` | Build system, CI, packaging |

**ALL commits and tags must be GPG-signed:**
```bash
git commit -S -m "[Tag] Description"
git tag -s X.Y.Z -m "Tag message"
```

**NEVER** include "generated by", "co-authored by", or any AI attribution in commit messages.
