# Development & Testing

## Prerequisites

- Python 3.10 or newer
- A virtual environment is strongly recommended

```bash
python3 -m venv .venv
source .venv/bin/activate
```

## Install with dev dependencies

The `[dev]` extra installs pytest and the aiohttp/asyncio test plugins.
Install all optional extras as well so the full test suite can run:

```bash
pip install -e ".[all,dev]"
```

Minimal install (unit tests only):

```bash
pip install -e ".[dev]"
```

## Running the tests

From the repository root:

```bash
pytest
```

Or via the venv explicitly:

```bash
.venv/bin/pytest
```

Useful flags:

| Flag | Effect |
|------|--------|
| `-v` | Verbose — show each test name |
| `-q` | Quiet — one dot per test |
| `-x` | Stop after the first failure |
| `-k EXPR` | Run only tests whose name matches *EXPR* |
| `--tb=short` | Shorter tracebacks |
| `--cov=xspct_db` | Coverage report |

Examples:

```bash
# Run only the route tests
pytest tests/test_routes.py -v

# Run only backend tests
pytest tests/backends/ -v

# Run a single test
pytest tests/backends/test_yaml_backend.py::test_lookup_found -v

# With coverage
pytest --cov=xspct_db --cov-report=term-missing
```

## Test configuration

All pytest settings live in `pyproject.toml`:

```toml
[tool.pytest.ini_options]
asyncio_mode = "auto"     # all async test functions run automatically
testpaths    = ["tests"]
addopts      = "-v --tb=short"
```

`asyncio_mode = "auto"` means you do **not** need to decorate async test
functions with `@pytest.mark.asyncio`.

## Test fixtures

Shared fixtures are defined in `tests/conftest.py`:

| Fixture | Scope | Description |
|---------|-------|-------------|
| `base_cfg` | function | Minimal config with dummy backend, no Redis |
| `yaml_cfg` | function | Config with YAML backend and test user data |
| `app_client` | function | aiohttp `TestClient` wired to dummy backend |
| `yaml_app_client` | function | aiohttp `TestClient` wired to YAML backend |

## Test structure

```
tests/
  conftest.py          # Shared fixtures
  test_auth.py         # API key authentication
  test_cache.py        # Redis cache layer
  test_routes.py       # HTTP endpoints (health, query, rspamd-settings)
  test_utils.py        # Utility helpers
  backends/
    test_base.py       # BaseBackend contract
    test_dummy.py      # Dummy backend
    test_ldap_backend.py  # LDAP backend (requires bonsai)
    test_yaml_backend.py  # YAML file backend
```

## Building the documentation

```bash
pip install -e ".[docs]"
cd docs
sphinx-build -b html . _build/html
# Open docs/_build/html/index.html in a browser
```

## Version bumps

Keep `pyproject.toml` and `src/xspct_db/__init__.py` in sync:

```toml
# pyproject.toml
version = "0.7.0"
```

```python
# src/xspct_db/__init__.py
__version__ = "0.7.0"
```

## Adding a new backend

1. Create `src/xspct_db/backends/<name>_backend.py` (copy `dummy.py` as skeleton).
2. Add the SPDX header (lines 1–2).
3. Subclass `BaseBackend` and implement `lookup()` and `close()`.
4. Register the new type in `config.py` defaults and `server.py` pool init.
5. Add an optional dependency extra in `pyproject.toml` if needed.
6. Write tests in `tests/backends/test_<name>_backend.py`.
7. Add a reference page `docs/reference/backends/<name>_backend.rst`.
