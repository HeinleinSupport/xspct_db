# Configuration Reference

xspct_db is configured via a single YAML file passed as the first argument:

```bash
xspct-db /etc/xspct-db.yml
```

All keys are optional; unspecified keys fall back to the defaults shown below.

## Server

| Key | Default | Description |
|-----|---------|-------------|
| `xspct_db_listen_address` | `["127.0.0.1", "::1"]` | List of IP addresses to bind |
| `xspct_db_listen_port` | `"11350"` | TCP port |
| `xspct_db_listen_backlog` | `256` | TCP accept backlog |
| `xspct_db_log_level` | `30` | Python log level (10=DEBUG … 50=CRITICAL) |
| `xspct_db_log_prefix` | `"Xspct_DB"` | String prepended to log messages |
| `xspct_db_request_timeout` | `0` | Per-request timeout in seconds; `0` = disabled |
| `xspct_db_request_timeout_header` | `""` | Header name to read per-request timeout from |

## Authentication

| Key | Default | Description |
|-----|---------|-------------|
| `xspct_db_api_header` | `"X-Api-Key"` | HTTP header carrying the API key |
| `xspct_db_api_key` | `"changeme"` | Accepted API key or list of keys |
| `xspct_db_api_key_verify_fail` | `true` | Return `403` on bad key; `false` = allow through |
| `xspct_db_rspamd_header` | `"X-Rspamd-ID"` | Header used to propagate the Rspamd request ID |

## TLS

```yaml
xspct_db_tls:
  tls_enabled: false
  tls_cert: /etc/ssl/certs/xspct-db.crt
  tls_key:  /etc/ssl/private/xspct-db.key
```

## Metrics

```yaml
xspct_db_metrics_auth:
  enabled: false        # require auth on /metrics
  api_key: true         # accept the API key as bearer
  basic_auth_users:     # map of user: password (plain-text)
    monitor: secret
```

## Statistics

| Key | Default | Description |
|-----|---------|-------------|
| `xspct_db_stats_enabled` | `true` | Emit periodic stats log lines |
| `xspct_db_stats_interval` | `60` | Interval between stats log lines (seconds) |
| `xspct_db_stats_sample_interval` | `10` | Pool connection sampling interval (seconds) |

## Local (L1) Cache

xspct_db maintains an in-process `TTLCache` (provided by `cachetools`) that sits in front of
Redis and serves as a zero-latency first layer.  It is **enabled by default** and works even
when Redis is not configured.

```yaml
xspct_db_local_cache:
  enabled: true        # set to false to disable the L1 cache entirely
  expire: 20           # TTL for positive cache entries (seconds)
  expire_negative: 20  # TTL for negative (not-found) entries (seconds)
  max_entries: 10000   # maximum number of entries across each cache bucket
```

When both L1 and Redis (L2) are enabled, a lookup follows this order:

1. Check L1 — return immediately on hit (no network I/O).
2. Check Redis (L2) — on hit, backfill L1 and return.
3. Query the backend — write result to both L1 and L2.

Disabling L1 (`enabled: false`) falls through directly to Redis or the backend on every request.

## Redis Cache

```yaml
xspct_db_redis_cache:
  enabled: false
  host: localhost
  port: 6379
  user: ""
  password: ""
  decode_responses: true
  prefix_user: "xspct_db_user_"
  prefix_alias: "xspct_db_alias_"
  prefix_negative_alias: "xspct_db_neg_alias_"
  expire: 60           # TTL for positive cache entries (seconds)
  expire_negative: 60  # TTL for negative cache entries (seconds)
  connect_timeout: 1
  query_timeout: 1
  max_connections: 40
  max_errors: 2        # disable cache after this many consecutive errors
```

## Queries

Each entry under `xspct_db_queries` defines one named query.
The `db_type` key selects the backend.

### YAML backend

```yaml
xspct_db_queries:
  users:
    db_type: yaml
    primary_key: mail
    attr_list: ["*"]
    search_filter: [mail, aliases]

xspct_db_yaml_data:
  users:
    alice@mailexample.de:
      mail: alice@mailexample.de
      uid: alice
      aliases: [a@mailexample.de]
```

### LDAP backend

```yaml
xspct_db_queries:
  ldap_users:
    db_type: ldap
    server: ldap://ldap.example.com
    use_tls: false
    verify_certs: true
    ca_cert_dir: /etc/ssl/certs
    bind_dn: cn=reader,dc=example,dc=com
    bind_dn_pw: secret
    base_dn: ou=users,dc=example,dc=com
    search_filter: "(mail={MAIL})"
    search_filter_replace:
      "{MAIL}": username
    primary_key: mail
    attr_list: [mail, uid, cn]
    pool_minconn: 2
    pool_maxconn: 20

xspct_db_ldap_pool_minconn: 2   # global default
xspct_db_ldap_pool_maxconn: 20
```

### MySQL backend

```yaml
xspct_db_queries:
  mysql_users:
    db_type: mysql
    host: 127.0.0.1
    port: 3306
    user: xspct
    password: secret
    db: maildb
    query: "SELECT mail, uid FROM users WHERE mail = %s"
    primary_key: mail
    pool_minconn: 1
    pool_maxconn: 20

xspct_db_mysql_pool_minconn: 1  # global default
xspct_db_mysql_pool_maxconn: 20
```

## Key Translation and Value Splitting

```yaml
# Rename attribute keys in the result
xspct_db_key_translation:
  cn: displayname

# Split string values on a delimiter
xspct_db_value_split:
  aliases: ","
```
