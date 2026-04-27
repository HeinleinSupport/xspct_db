# xspct_db

A multi-backend database query service with Redis caching and Rspamd integration.

Provides an async HTTP API (aiohttp) for querying user data from LDAP, MySQL, and YAML backends,
with Redis caching, API-key authentication, Prometheus metrics, and TLS support.

## Installation

```bash
pip install xspct_db                        # core (aiohttp + PyYAML)
pip install "xspct_db[ldap]"               # + bonsai LDAP support
pip install "xspct_db[mysql]"              # + aiomysql support
pip install "xspct_db[redis]"              # + Redis caching
pip install "xspct_db[uvloop]"             # + uvloop event loop
pip install "xspct_db[all]"               # everything
pip install "xspct_db[all,dev]"           # + dev/test dependencies
```

## Usage

```bash
xspct-db /etc/xspct-db.yml
# or
python -m xspct_db /etc/xspct-db.yml
```

## API Endpoints

| Method | Path | Auth | Description |
|--------|------|------|-------------|
| GET | `/` | — | Health check |
| GET | `/ping` | — | Ping/Pong |
| GET | `/metrics` | optional | Prometheus metrics |
| GET | `/query/v1/{user}` | API key | Single user lookup |
| POST | `/query-json/v1` | API key | Batch user lookup |
| POST | `/rspamd-settings/v1` | API key | Rspamd settings |

## Development

```bash
git clone ...
cd xspct_db
pip install -e ".[all,dev]"
pytest
pytest --cov=xspct_db
```

## License

European Union Public Licence v. 1.2 (EUPL-1.2)
