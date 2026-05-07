# Installation

## Requirements

- Python 3.10 or newer
- Core dependencies: `aiohttp >= 3.9`, `PyYAML >= 6.0`

## From PyPI

```bash
# Core only
pip install xspct_db

# With LDAP support (bonsai)
pip install "xspct_db[ldap]"

# With MySQL support (aiomysql)
pip install "xspct_db[mysql]"

# With Redis caching
pip install "xspct_db[redis]"

# With msgpack body encoding
pip install "xspct_db[msgpack]"

# With uvloop event loop
pip install "xspct_db[uvloop]"

# Everything
pip install "xspct_db[all]"

# Everything + development dependencies
pip install "xspct_db[all,dev]"
```

## From Source

```bash
git clone https://github.com/HeinleinSupport/xspct_db
cd xspct_db
python -m venv .venv
source .venv/bin/activate
pip install -e ".[all,dev]"
```

## Running

```bash
xspct-db /etc/xspct-db.yml
# or
python -m xspct_db /etc/xspct-db.yml
```
