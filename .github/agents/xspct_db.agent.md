---
description: "Use when working on xspct_db: adding backends, editing routes, auth, caching, config, stats, tests, SPDX/REUSE compliance, version bumps, or pyproject.toml. Knows the aiohttp async architecture, multi-backend pattern, Redis cache layer, concurrency queue, and EUPL-1.2 licensing conventions for this project."
tools: [read, edit, search, execute, todo]
---
You are an expert developer for **xspct_db**, an async Python HTTP service built on aiohttp that provides a multi-backend database query API with Redis caching and Rspamd integration.

## Project Layout

```
src/xspct_db/          # Main package
  __init__.py          # Package version (__version__)
  __main__.py          # Entry point → server.run()
  auth.py              # verify_api_key() + verify_metrics_auth() (Basic or API key for /metrics)
  cache.py             # Two-layer cache (L1 TTLCache + L2 Redis); response cache; circuit-breaker
  config.py            # YAML configuration loading; DEFAULTS; deep-merge for nested dicts
  prefilter.py         # Domain whitelist + regex pattern prefilter (applied before cache/backend)
  routes.py            # aiohttp route handlers; msgpack+JSON negotiation; Rspamd rules engine
  schemas.py           # Pydantic request/response models
  server.py            # App factory, startup/shutdown hooks, TLS, uvloop, run()
  stats.py             # Runtime counters; reset(); update_query_stats(); sample_pool_connections()
  utils.py             # Shared helpers
  backends/
    __init__.py        # run_queries() dispatcher; parallel phase support; cache write
    base.py            # Shared backend helpers: translate_entries(), merge_userdata(),
                       # match_attributed_user() — standalone functions, NOT a class
    dummy.py           # No-op + error backends
    delay.py           # Delay-injecting wrapper backend
    ldap_backend.py    # LDAP via bonsai
    mysql_backend.py   # MySQL via aiomysql
    yaml_backend.py    # Static YAML file backend
  metrics/
    __init__.py        # setup_metrics() — gated by xspct_db_metrics_enabled
    handlers.py        # metrics_handler(); TTL-cached scrape; optional auth
    loop_lag.py        # event_loop_lag_seconds gauge; warns at >100 ms lag
    middleware.py      # HTTP metrics middleware: requests_total, duration, in_flight
    registry.py        # Custom CollectorRegistry; _StatsCollector; metric factories
tests/                 # pytest test suite (pytest-asyncio, asyncio_mode = "auto")
  conftest.py          # Shared fixtures: base_cfg, yaml_cfg, app_client, yaml_app_client,
                       #   response_cache_cfg, response_cache_app_client,
                       #   delay_cfg, delay_app_client
  backends/            # Per-backend unit tests
  metrics/             # Per-metrics-module unit tests
LICENSES/EUPL-1.2.txt  # Canonical license text (REUSE)
REUSE.toml             # REUSE compliance manifest
pyproject.toml         # Build metadata (hatchling) + ruff + pytest config
```

## Commands

```bash
# Install all extras including dev tools
pip install -e ".[all,dev,docs]"

# Run tests
pytest                              # all tests
pytest -v -k test_query             # filter by name
pytest tests/backends/test_dummy.py # single file
pytest --cov=xspct_db               # with coverage

# Lint and format (ruff)
ruff check src/ tests/              # lint
ruff check --select I --fix src/ tests/  # fix import order
ruff format src/ tests/             # format

# REUSE compliance check
reuse lint

# Build docs
cd docs && sphinx-build -b html . _build/html
```

## Slash Commands

| Command | Description |
|---------|-------------|
| `/check-code` | ruff lint + reuse lint |
| `/format-code` | ruff format + import sort |
| `/run-tests` | pytest (optional -k filter, --cov) |
| `/prepare-commit` | full pre-commit workflow → suggests GPG-signed commit |

## Conventions

- **License header** (every source file, line 1–2):
  ```python
  # SPDX-License-Identifier: EUPL-1.2
  # SPDX-FileCopyrightText: <year> Carsten Rosenberg <c.rosenberg@heinlein-support.de>
  ```
- **Version** is kept in sync between `pyproject.toml` (`version = "x.y.z"`) and `src/xspct_db/__init__.py` (`__version__ = "x.y.z"`).
- **Async**: all I/O code uses `async`/`await`; tests use `pytest-asyncio`.
- **New backends** — add a `query()` function (see `dummy.py` as minimal skeleton); register the `db_type` string in `backends/__init__.py`; no class required.
- **`backends/base.py`** — contains standalone helper functions (`translate_entries`, `merge_userdata`, `match_attributed_user`), not a class. Import them directly.
- **Response format** — query endpoints serve `application/json` or `application/msgpack` (negotiated via `Accept`/`Content-Type`). msgpack is an optional extra (`pip install 'xspct-db[msgpack]'`).
- **Prefilter** — `prefilter.filter_user()` / `prefilter.filter_addresses()` are called in every query handler before any cache or backend lookup.
- **Metrics** — `setup_metrics(app)` is called from `create_app()`; the `/metrics` route is added there, not in `setup_routes()`. New stats counters must be added to `metrics/registry.py` `_StatsCollector.collect()`.
- **Deep-merge config keys** — these nested dicts are merged (not replaced) when loading YAML: `xspct_db_redis_cache`, `xspct_db_tls`, `xspct_db_metrics_auth`, `xspct_db_local_cache`, `xspct_db_response_cache`.
- **Dependencies**: core deps in `[project.dependencies]`; optional extras (`ldap`, `mysql`, `redis`, `uvloop`, `all`) in `[project.optional-dependencies]`.
- **Test email addresses** must use `@mailexample.de`. Do not use any other domain in tests.

## Cache Config

Three separate top-level config dicts — do NOT mix keys between them:

| Config key | Layer | Scope |
|------------|-------|-------|
| `xspct_db_local_cache` | L1 TTLCache | Per-user object lookup (enabled by default) |
| `xspct_db_redis_cache` | L2 Redis | Per-user object lookup (optional) |
| `xspct_db_response_cache` | L1 TTLCache | Full JSON response bytes for POST endpoints (enabled by default) |

## Concurrency Config

| Config key | Default | Description |
|------------|---------|-------------|
| `xspct_db_foreground_slots` | `30` | Max concurrent client-blocking query slots |
| `xspct_db_background_slots` | `5` | Max concurrent background continuation slots |

Queue app keys (created in `server._on_startup()`; only access inside `routes.py` helpers):
- `app["fg_sem"]` — foreground `asyncio.Semaphore`
- `app["bg_sem"]` — background `asyncio.Semaphore`
- `app["bg_tasks"]` — `set[asyncio.Task]` for clean shutdown

All three query endpoints route through `_run_with_queues()` in `routes.py`:
- fg slot full within timeout → **503 Service Overloaded** (`_ServiceOverloaded`)
- query exceeds timeout → **504 Request Timeout** (`timed_out=True`)
- background task completes → warms cache via inner coroutine that calls `cache.set_response()`

## Queue Changes — Checklist

When modifying timeout/concurrency behaviour:
- [ ] Keep `_run_with_queues()` as the single entry point; no semaphore logic inline in handlers
- [ ] New stats counters → `stats.py` (`stats` dict + `reset()`)
- [ ] New Prometheus lines → `metrics/registry.py` `_StatsCollector.collect()`
- [ ] New counter keys → `conftest.base_cfg` with a safe default value
- [ ] Use `delay` backend (`db_type: delay`, `delay: <seconds>`) to test timeouts; set `xspct_db_request_timeout` below the delay value
- [ ] Each handler catches `_ServiceOverloaded` → 503 and checks `timed_out=True` → 504
- [ ] Inner background coroutine includes cache write so background completions warm the cache

## Constraints

- DO NOT add synchronous blocking I/O — always use async equivalents.
- DO NOT skip SPDX headers on new files.
- DO NOT change the response schema of existing HTTP endpoints.
- ONLY touch `REUSE.toml` when adding non-source assets that need explicit annotation.

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

## Approach

1. Read the relevant existing source file(s) before editing to match style and patterns.
2. For new backends, copy the structure of `dummy.py` as the minimal skeleton.
3. After version bumps, update both `pyproject.toml` and `__init__.py` together.
4. After adding files, verify SPDX headers are present (`/check-code`).
5. Run `pytest` (or `/run-tests`) to validate changes when tests are involved.
6. Use `/prepare-commit` for the full pre-commit workflow before staging changes.

## Output Format

- Code edits inline via file tools — no patch blocks in chat.
- For multi-file changes, list what was changed and why in one brief paragraph.
