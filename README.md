# xspct_db

A multi-backend database query service with two-layer caching, concurrency control, and Rspamd integration.

Provides an async HTTP API (aiohttp) for querying user data from LDAP, MySQL, and YAML backends,
with a two-layer object cache (in-process L1 `TTLCache` + optional Redis L2), a response-level
cache for batch POST endpoints, foreground/background query queues with semaphore-based concurrency
control, API-key authentication, Prometheus metrics, and TLS support.

## Installation

Requires Python 3.10 or newer.

```bash
pip install xspct_db                   # core (aiohttp + PyYAML)
pip install "xspct_db[ldap]"          # + bonsai LDAP support
pip install "xspct_db[mysql]"         # + aiomysql support
pip install "xspct_db[redis]"         # + Redis caching
pip install "xspct_db[uvloop]"        # + uvloop event loop
pip install "xspct_db[all]"           # all optional backends
pip install "xspct_db[all,dev]"       # + dev/test dependencies
pip install "xspct_db[all,dev,docs]"  # + Sphinx documentation
```

## Usage

```bash
xspct-db /etc/xspct-db.yml
# or
python -m xspct_db /etc/xspct-db.yml
```

Configuration is a single YAML file. All keys are optional; see
[docs/guide/configuration.md](docs/guide/configuration.md) for the full reference,
including examples for [multiple databases with result merging](docs/guide/configuration.md#multiple-databases-and-result-merging)
and [chained queries with `use_result`](docs/guide/configuration.md#use_result--chained-queries).

## HTTP API

| Method | Path | Auth | Description |
|--------|------|------|-------------|
| GET | `/` | — | Health / liveness check |
| GET | `/ping` | — | Ping → Pong |
| GET | `/metrics` | optional | Prometheus metrics |
| GET | `/v1/query/{user}` | API key | Single user lookup |
| POST | `/v1/query-json` | API key | Batch user lookup |
| POST | `/v1/rspamd-settings` | API key | Rspamd settings blob |

Legacy path prefixes (`/query/v1/{user}`, `/query-json/v1`, `/rspamd-settings/v1`) are also
accepted for backwards compatibility.

Authentication uses the `X-Api-Key` header (configurable).
See [docs/guide/api.md](docs/guide/api.md) for request/response details and all exposed metrics.

## Backends

| Backend | Extra | Description |
|---------|-------|-------------|
| `yaml` | — | Static data from the config file |
| `dummy` | — | No-op backend (returns the username as-is) |
| `delay` | — | Artificial-delay backend for testing |
| `ldap` | `[ldap]` | LDAP via bonsai with connection pooling |
| `mysql` | `[mysql]` | MySQL via aiomysql with connection pooling |

## Caching

xspct_db has three independent cache layers, all using `TTLCache` from `cachetools`:

| Layer | Config key | Scope |
|-------|-----------|-------|
| L1 object cache | `xspct_db_local_cache` | Per-user lookup; zero latency, in-process |
| L2 object cache | `xspct_db_redis_cache` | Per-user lookup; shared across workers via Redis |
| Response cache | `xspct_db_response_cache` | Full JSON response body for `POST /v1/query-json` and `POST /v1/rspamd-settings` |

On a `GET /v1/query/{user}` request, lookups flow: L1 → L2 (Redis) → backend.
On a `POST` request, the response cache is checked first; on a miss the backend is queried
and the serialised response is stored for reuse.

## Concurrency

When `xspct_db_request_timeout` is greater than `0`, every query endpoint is guarded by two
`asyncio.Semaphore` instances:

- **Foreground slots** (`xspct_db_foreground_slots`, default `30`) — limit concurrent
  client-blocking queries.  When all slots are busy a new request immediately receives
  **503 Service Overloaded**.
- **Background slots** (`xspct_db_background_slots`, default `5`) — when a query exceeds the
  timeout the client receives **504 Request Timeout** but the backend task is promoted to a
  background slot so it can complete and warm the cache for subsequent requests.  If no
  background slot is free the task is cancelled.

See [docs/guide/configuration.md](docs/guide/configuration.md) for all options.

## Development

```bash
git clone https://github.com/heinlein-support/xspct_db
cd xspct_db
python -m venv .venv && source .venv/bin/activate
pip install -e ".[all,dev]"

# Run tests
pytest

# Run tests with coverage
pytest --cov

# Build documentation
pip install -e ".[docs]"
python -m sphinx -b html docs docs/_build/html
```

## License

European Union Public Licence v. 1.2 (EUPL-1.2) — see [LICENSES/EUPL-1.2.txt](LICENSES/EUPL-1.2.txt).
