# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Commands

### Install (development)
```bash
pip install -e ".[all,dev,docs]"
```

### Run tests
```bash
pytest                              # all tests
pytest -v -k test_query             # tests matching name
pytest tests/backends/test_dummy.py # single file
pytest --cov=xspct_db               # with coverage
```

### Run the daemon
```bash
xspct-db /etc/xspct-db.yml
# or from source:
python -m xspct_db /etc/xspct-db.yml
```

### Query the running daemon
```bash
curl -s -H "X-Api-Key: your-key" http://localhost:11350/query/v1/user@example.com | python3 -m json.tool
```

### Build docs
```bash
cd docs && sphinx-build -b html . _build/html
```

## Architecture

The package lives in `src/xspct_db/` and is split into focused modules.
Entry point is `src/xspct_db/__main__.py` → `server.run()`.

### Request flow

1. `routes.py` receives the HTTP request and validates the API key via `auth.py`.
2. The route handler resolves the backend from `app["config"]` and calls its `lookup()` method.
3. The backend (LDAP / MySQL / YAML / dummy) performs the query and returns a user dict.
4. `cache.py` optionally wraps the lookup in a Redis cache keyed by username.
5. `stats.py` counters are incremented; the response is JSON-serialised and returned.

### Module overview

| Module | Purpose |
|--------|---------|
| `server.py` | `create_app()` factory, startup/shutdown hooks, `run()` |
| `routes.py` | aiohttp route definitions and HTTP handlers |
| `auth.py` | API key validation (`X-Api-Key` header) |
| `cache.py` | Redis cache layer; `set_connection()` + `get()` / `set()` |
| `config.py` | YAML config loading with defaults |
| `schemas.py` | Pydantic request/response models for aiohttp-pydantic |
| `stats.py` | Prometheus-style counters; periodic log output |
| `utils.py` | Shared helpers |
| `backends/base.py` | `BaseBackend` abstract class |
| `backends/dummy.py` | No-op backend (testing / health checks) |
| `backends/delay.py` | Delay-injecting wrapper backend |
| `backends/ldap_backend.py` | LDAP lookup via bonsai |
| `backends/mysql_backend.py` | MySQL lookup via aiomysql |
| `backends/yaml_backend.py` | Static YAML file backend |

### HTTP endpoints

| Method | Path | Auth | Description |
|--------|------|------|-------------|
| GET | `/` | — | Health check |
| GET | `/ping` | — | Returns `Pong` |
| GET | `/metrics` | optional | Prometheus metrics |
| GET | `/query/v1/{user}` | required | Single-user lookup |
| POST | `/query-json/v1` | required | Batch user lookup |
| POST | `/rspamd-settings/v1` | required | Rspamd settings blob |

## Conventions

- **License header** (every source file, lines 1–2):
  ```python
  # SPDX-License-Identifier: EUPL-1.2
  # SPDX-FileCopyrightText: <year> Carsten Rosenberg <c.rosenberg@heinlein-support.de>
  ```
- **Version** is kept in sync between `pyproject.toml` (`version = "x.y.z"`) and `src/xspct_db/__init__.py` (`__version__ = "x.y.z"`).
- **Async**: all I/O uses `async`/`await`; tests use `pytest-asyncio` (`asyncio_mode = "auto"`).
- **New backends** must subclass `backends.base.BaseBackend`; copy `dummy.py` as the minimal skeleton.
- **Dependencies**: core in `[project.dependencies]`; optional extras (`ldap`, `mysql`, `redis`, `uvloop`, `all`) in `[project.optional-dependencies]`.
- DO NOT add blocking I/O — always use async equivalents.
- DO NOT skip SPDX headers on new files.
- DO NOT change the `text/plain; version=0.0.4` content-type in `routes.py` — it is protocol-mandated by Rspamd.
- ONLY touch `REUSE.toml` when adding non-source assets that need explicit annotation.
