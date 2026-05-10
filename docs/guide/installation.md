# Installation

## Requirements

- Python 3.10 or newer
- Core dependencies: `aiohttp >= 3.9`, `PyYAML >= 6.0`

## From GitHub

```bash
# Core only
pip install "xspct_db @ git+https://github.com/HeinleinSupport/xspct_db.git"

# With LDAP support (bonsai)
pip install "xspct_db[ldap] @ git+https://github.com/HeinleinSupport/xspct_db.git"

# With MySQL support (aiomysql)
pip install "xspct_db[mysql] @ git+https://github.com/HeinleinSupport/xspct_db.git"

# With Redis caching
pip install "xspct_db[redis] @ git+https://github.com/HeinleinSupport/xspct_db.git"

# With msgpack body encoding
pip install "xspct_db[msgpack] @ git+https://github.com/HeinleinSupport/xspct_db.git"

# With uvloop event loop
pip install "xspct_db[uvloop] @ git+https://github.com/HeinleinSupport/xspct_db.git"

# Everything
pip install "xspct_db[all] @ git+https://github.com/HeinleinSupport/xspct_db.git"

# Everything + development dependencies
pip install "xspct_db[all,dev] @ git+https://github.com/HeinleinSupport/xspct_db.git"
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
