# xspct_db

**xspct_db** is an async HTTP service that queries multiple database backends
(LDAP, MySQL, YAML) and merges user information for integration with
[Rspamd](https://rspamd.com/) and other mail-security pipelines.

## Quick start

```bash
pip install "xspct_db[all]"
xspct-db /etc/xspct-db.yml
```

Query a user:

```bash
curl -s -H "X-Api-Key: your-key" \
  http://localhost:11350/query/v1/user@mailexample.de | python3 -m json.tool
```

```{toctree}
:maxdepth: 2
:caption: User Guide

guide/installation
guide/configuration
guide/api
guide/development
```

```{toctree}
:maxdepth: 2
:caption: API Reference

reference/server
reference/routes
reference/auth
reference/cache
reference/config
reference/stats
reference/utils
reference/backends/index
```

```{toctree}
:maxdepth: 1
:caption: Project

changelog
license
```
