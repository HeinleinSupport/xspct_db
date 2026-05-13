# GitHub Copilot instructions for xspct_db

You are working on **xspct_db** — an async Python HTTP service (aiohttp) that provides a multi-backend database query API with Redis caching and Rspamd integration.

## Project layout

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
tests/                 # pytest-asyncio test suite (asyncio_mode = "auto")
  conftest.py          # Fixtures: base_cfg, yaml_cfg, app_client, yaml_app_client,
                       #   response_cache_cfg, response_cache_app_client,
                       #   delay_cfg, delay_app_client
  backends/            # Per-backend unit tests
  metrics/             # Per-metrics-module unit tests
LICENSES/EUPL-1.2.txt  # Canonical licence text (REUSE)
REUSE.toml             # REUSE 3.0 compliance manifest
pyproject.toml         # Build metadata (hatchling)
```

## HTTP endpoints

| Method | Path | Auth | Description |
|--------|------|------|-------------|
| GET | `/` | — | Health check |
| GET | `/ping` | — | Returns `Pong` |
| GET | `/metrics` | optional | Prometheus metrics — registered by `setup_metrics()`, not `setup_routes()` |
| GET | `/v1/query/{user}` | required | Single-user lookup; prefiltered; L1/L2 cache; 503/504 |
| POST | `/v1/query-json` | required | Batch user lookup (max `xspct_db_query_json_max_users`); prefiltered; response cache; 503/504 |
| POST | `/v1/rspamd-settings` | required | Rspamd settings blob; rules engine; prefiltered; response cache; 503/504 |

Legacy path prefixes (`/query/v1/{user}`, `/query-json/v1`, `/rspamd-settings/v1`) are also registered for backwards compatibility.

## Conventions

- **Licence header** on every source file, lines 1–2:
  ```python
  # SPDX-License-Identifier: EUPL-1.2
  # SPDX-FileCopyrightText: <year> Carsten Rosenberg <c.rosenberg@heinlein-support.de>
  ```
- **Version** must be kept in sync: `pyproject.toml` and `src/xspct_db/__init__.py`.
- **Async first** — all I/O uses `async`/`await`; tests use `pytest-asyncio` with `asyncio_mode = "auto"`.
- **New backends** — add a `query()` function (see `dummy.py` as minimal skeleton); register the `db_type` string in `backends/__init__.py`; no class required.
- **`backends/base.py`** — contains standalone helper functions (`translate_entries`, `merge_userdata`, `match_attributed_user`), not a class. Import them directly.
- **Response format** — query endpoints serve `application/json` or `application/msgpack` (negotiated via `Accept`/`Content-Type`). msgpack is an optional extra (`pip install 'xspct-db[msgpack]'`); without it all responses fall back to JSON.
- **Cache config** — three separate top-level dicts; do not mix keys between them:
  - `xspct_db_local_cache` — L1 in-process `TTLCache` for object lookups (enabled by default)
  - `xspct_db_redis_cache` — L2 Redis cache for object lookups (optional)
  - `xspct_db_response_cache` — L1 `TTLCache` for full serialised response bytes for POST endpoints (enabled by default)
- **Deep-merge config keys** — these nested dicts are merged (not replaced) when loading YAML: `xspct_db_redis_cache`, `xspct_db_tls`, `xspct_db_metrics_auth`, `xspct_db_local_cache`, `xspct_db_response_cache`.
- **Concurrency config** — two top-level integer keys control the semaphore capacities:
  - `xspct_db_foreground_slots` (default `30`) — concurrent client-blocking query slots
  - `xspct_db_background_slots` (default `5`) — concurrent background-continuation slots
- **Queue app keys** — `app["fg_sem"]`, `app["bg_sem"]`, `app["bg_tasks"]` are created in `server._on_startup()`; do not access them outside `routes.py` helpers.
- **Prefilter** — `prefilter.filter_user()` / `prefilter.filter_addresses()` are called in every query handler before any cache or backend lookup.
- **Metrics** — `setup_metrics(app)` is called from `create_app()`; the `/metrics` route is added there, not in `setup_routes()`. New stats counters must be added to `metrics/registry.py` `_StatsCollector.collect()`.

## Constraints

- Do NOT add synchronous blocking I/O.
- Do NOT skip SPDX headers on new files.
- Do NOT change the response schema of existing HTTP endpoints.
- Only touch `REUSE.toml` for non-source assets needing explicit annotation.
- **Test email addresses** must use `@mailexample.de` as the domain. Do not use any other domain in tests.
- **Queue changes** — when adding or modifying timeout/concurrency behaviour:
  - Keep `_run_with_queues()` as the single entry point; do not add semaphore logic inline in handlers.
  - New stats counters must be added to `stats.py` (`stats` dict + `reset()`), `metrics/registry.py` `_StatsCollector.collect()`, and `conftest.py` `base_cfg` (with a safe default value).
  - Use the `delay` backend (`db_type: delay`, `delay: <seconds>`) to test timeouts; set `xspct_db_request_timeout` below the delay value.
  - Each query handler must catch `_ServiceOverloaded` → 503 and check `timed_out=True` → 504.
  - When adding features that run inside the background task, the inner coroutine must include the cache write so background completions warm the cache.

## Stats counters (`stats.stats`)

| Key | Description |
|-----|-------------|
| `requests_total` | All requests |
| `requests_known` / `requests_unknown` | User found / not found |
| `local_cache_hits` / `local_cache_misses` | L1 cache |
| `response_cache_hits` / `response_cache_misses` | POST response cache |
| `redis_hits` / `redis_misses` / `redis_negative_hits` | L2 Redis cache |
| `foreground_overloaded` | fg_sem acquire timeout → 503 |
| `requests_timeout` | Requests exceeding timeout → 504 |
| `background_completed` / `background_rejected` / `background_errors` | Background task lifecycle |
| `prefilter_domain_count` | Current domain set size (gauge, not counter) |
| `prefilter_domain_hits` / `prefilter_domain_misses` | Domain filter pass/reject |
| `prefilter_pattern_hits` / `prefilter_pattern_misses` | Pattern filter pass/reject |
| `queries` | Per-query timing `{qk: {count, time_total, time_min, time_max}}` |
| `pool_connections` | Per-pool connection samples `{key: {min, max, sum, count, limit}}` |

## Key config keys

| Key | Default | Note |
|-----|---------|------|
| `xspct_db_api_key_verify_fail` | `True` | `False` = permissive mode (WARNING logged) |
| `xspct_db_client_max_size` | `1048576` | Max request body in bytes |
| `xspct_db_query_json_max_users` | `500` | Batch size limit for `/v1/query-json` |
| `xspct_db_request_timeout_header` | `""` | Header name clients use to request a custom timeout |
| `xspct_db_request_timeout_header_max` | `120` | Upper bound on header-provided timeout |
| `xspct_db_metrics_enabled` | `False` | Enable full Prometheus integration |
| `xspct_db_metrics_cache_ttl` | `5` | TTL (s) for `/metrics` scrape output |
| `xspct_db_metrics_auth` | `{enabled: false, api_key: true, basic_auth_users: {}}` | `/metrics` auth |
| `xspct_db_tls` | `{tls_enabled: false, tls_cert: "", tls_key: ""}` | TLS config |
| `xspct_db_rspamd_alias_fields` | `["aliases"]` | Fields used to build `settings_data.aliases` reverse-map |
| `xspct_db_reject_level_map` | `{"5": 13, "6": 15, "6.31": 17}` | SA score → Rspamd reject score |
| `xspct_db_reject_level_default` | `15` | Reference value; `actions.reject` omitted when equal |
| `xspct_db_rspamd_rules` | `null` | Override built-in `_DEFAULT_RSPAMD_RULES` entirely |
| `xspct_db_prefilter` | `{enabled: false}` | Master prefilter switch |
| `xspct_db_prefilter_domains` | — | Domain whitelist config |
| `xspct_db_prefilter_patterns` | — | Regex pattern config |

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
