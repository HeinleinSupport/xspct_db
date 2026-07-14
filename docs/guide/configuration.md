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
| `xspct_db_request_timeout_header_max` | `120` | Maximum timeout (seconds) accepted from the timeout header; values ≤ 0 are ignored |
| `xspct_db_foreground_slots` | `30` | Maximum concurrent foreground (client-blocking) queries |
| `xspct_db_background_slots` | `5` | Maximum concurrent background queries after a timeout |
| `xspct_db_client_max_size` | `1048576` | Maximum accepted request body size in bytes (default 1 MiB) |
| `xspct_db_query_json_max_users` | `500` | Maximum number of users accepted in a single `POST /v1/query-json` request |

## Authentication

| Key | Default | Description |
|-----|---------|-------------|
| `xspct_db_api_header` | `"X-Api-Key"` | HTTP header carrying the API key |
| `xspct_db_api_key` | `"changeme"` | Accepted API key or list of keys |
| `xspct_db_api_key_verify_fail` | `true` | Return `401` on bad key; `false` = allow through (permissive mode — logs a WARNING at startup) |
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
| `xspct_db_stats_sample_interval` | `10` | Interval for pool connection sampling and Redis health-check PING (seconds) |

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

## Response Cache

xspct_db can cache the full serialised JSON response body for `POST /v1/query-json` and
`POST /v1/rspamd-settings` in a dedicated in-process `TTLCache`.  Unlike the object cache,
this layer operates on the **response level** — the backend is not called at all on a cache hit.

```yaml
xspct_db_response_cache:
  enabled: true
  expire: 10           # TTL for cached responses (seconds)
  max_entries: 5000    # maximum number of cached responses
  # Fields of the rspamd-settings request used to build the cache key.
  # Removing fields that are always unique (e.g. settings-id) improves hit rate.
  rspamd_key_fields:
    - from
    - rcpts
    - mta-name
    - settings-name
    - settings-id
```

**Cache key construction:**

- `/v1/query-json` — keyed by the sorted tuple of user addresses in the request.
  Two requests with the same users in different order produce the same cache key.
- `/v1/rspamd-settings` — keyed by the fields listed in `rspamd_key_fields`.
  `rcpts` is stored as a `frozenset` so order does not matter.

Set `enabled: false` to disable the response cache entirely.

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

## Prefilter

The prefilter rejects addresses at the HTTP handler level, *before* any cache or backend
lookup.  Filtered addresses are silently dropped; if all addresses in a request are
filtered the endpoint returns `200` with an empty `{"users": {}}` body.

There are two independent sub-filters, each with its own `enabled` switch, plus a master
switch (`xspct_db_prefilter.enabled`) that disables both sub-filters at once.

### Master switch

```yaml
xspct_db_prefilter:
  enabled: false   # set to true to activate prefiltering
```

### Domain filter

Maintains a `frozenset` of allowed domains.  An address is **kept** only when its
domain part (`user@**domain**`) is in the set.  Plain usernames without `@` are matched
against the whole string.

```yaml
xspct_db_prefilter_domains:
  enabled: false

  # --- Sources ---
  inline: []                # list of domain strings directly in the config
  file: ""                  # path to a plain-text file (one domain per line, # = comment)
  file_reload_interval: 60  # seconds between file mtime checks (0 = disabled)

  redis_key: ""             # Redis SET key to fetch with SMEMBERS
  redis_channel: ""         # Redis pub/sub channel; PUBLISH triggers an immediate reload
  redis_reload_interval: 300  # safety-net interval in seconds (0 = pub/sub only)

  # --- Safety guards ---
  min_domains: 0   # minimum valid set size; 0 = no guard
  max_age: 0       # seconds after which an unrefreshed set is discarded; 0 = never expire
```

**File format** — one domain per line, leading/trailing whitespace stripped,
lines beginning with `#` (after stripping) and blank lines are ignored:

```text
# Allowed local domains
mailexample.de
example.org
```

**Redis setup**

```bash
# Populate the set
redis-cli SADD xspct_db_domains mailexample.de example.org

# Signal a reload (optional pub/sub channel)
redis-cli PUBLISH xspct_db_prefilter_reload 1
```

**State machine**

| State | Condition | Behaviour |
|-------|-----------|-----------|
| **BYPASS** | No valid set loaded yet | All addresses pass (filter inactive) |
| **ACTIVE** | Valid set present | Only addresses with a known domain pass |
| **EXPIRED** | `max_age > 0` and set age exceeds `max_age` | Falls back to BYPASS; WARNING logged once |

A reload that produces a set below `min_domains` is discarded — the previous
valid set is kept (*last-known-good*).  If no previous set exists the filter
stays in BYPASS and logs an ERROR.

> **Warning:** Set `max_age` to at least twice the smallest reload interval
> (`file_reload_interval` or `redis_reload_interval`) to avoid the set expiring
> between refresh cycles.

### Pattern filter

Keeps only addresses that match **at least one** of the configured regular
expressions (Python `re` syntax, applied with `re.search`).

```yaml
xspct_db_prefilter_patterns:
  enabled: false
  patterns:
    - "@mailexample\\.de$"
    - "^postmaster@"
```

Patterns are compiled once at startup.  Invalid patterns are logged and skipped.

## Queries

Each entry under `xspct_db_queries` defines one named query.
The `db_type` key selects the backend.

### YAML backend

Static data embedded directly in the configuration file.  Useful for small, rarely-changing
datasets such as service accounts, shared mailboxes, or test fixtures.

`search_filter` names the fields that are scanned when searching by alias.
`primary_key` is the field whose value becomes the key in the `users` response dict.
`attr_list: ["*"]` returns all fields; set it to a specific list to filter attributes.

`yaml_root` (optional) lets a single `xspct_db_yaml_data` tree serve multiple query entries by
naming a different top-level key.  Defaults to the query name.

```yaml
xspct_db_queries:
  users:
    db_type: yaml
    primary_key: mail
    attr_list: ["*"]
    search_filter: [mail, aliases]
    # yaml_root: users  # default: same as the query name

xspct_db_yaml_data:
  users:
    alice@mailexample.de:
      mail: alice@mailexample.de
      uid: alice
      aliases: [a@mailexample.de, alice.smith@mailexample.de]
    bob@mailexample.de:
      mail: bob@mailexample.de
      uid: bob
      aliases: []
```

### Dummy backend

The dummy backend returns a minimal synthetic object for every queried address without
consulting any real data source.  Each result contains the address as `uid` and a static
`comment` field.

Use it as a health-check backend, as a placeholder while a real backend is being set up,
or as a no-op stand-in in development.

```yaml
xspct_db_queries:
  noop:
    db_type: dummy
```

### Delay backend

The delay backend sleeps for a configurable duration and then returns the unchanged result.
It is intended for **testing timeout and background-continuation behaviour**: combine it with
`xspct_db_request_timeout` set to a value lower than `delay` to reliably trigger 504 responses
and exercise the background-slot path.

| Key | Default | Description |
|-----|---------|-------------|
| `delay` | `1.0` | Seconds to sleep before returning |

```yaml
xspct_db_request_timeout: 1   # return 504 after 1 second

xspct_db_queries:
  slow_backend:
    db_type: delay
    delay: 3.0    # always exceeds the timeout → 504 / background continuation
```

When `xspct_db_background_slots` is greater than `0` the query keeps running after the 504 is
sent and will warm the cache for the next request once it completes.

### Error backend

The error backend immediately returns a `500` error string without doing any work.  Use it
to verify that your monitoring, alerting, and caller error-handling all behave correctly when
a backend is unavailable.

```yaml
xspct_db_queries:
  always_fails:
    db_type: error
```

Any request that reaches this query will receive a `500 Internal Server Error` response.

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

## Wildcard Domain Query

When a user address is **not found** by the regular backend queries, the wildcard domain
query fallback can return a domain-level default object instead of an empty result.

Enable the fallback on a per-query basis with `wildcard_domain_query: true`.  When set,
that query will be **re-executed** using a computed *wildcard key* (e.g. `@example.com`)
as the lookup address.  The result (if any) is re-keyed under the original user address
in the response.

When multiple queries enable wildcard fallback, xspct_db derives a wildcard key for
**each wildcard-enabled query** using that query's own `wildcard_key_pattern` and
`wildcard_key_replacement` settings.  This allows different backends to use different
key formats in the same request flow.

### Quick example

```yaml
xspct_db_queries:
  users:
    db_type: yaml
    primary_key: mail
    attr_list: ["*"]
    search_filter: [mail]
    wildcard_domain_query: true   # enable wildcard fallback for this query

xspct_db_yaml_data:
  users:
    alice@mailexample.de:
      mail: alice@mailexample.de
      uid: alice
    # Wildcard entry — returned for any unknown user at *.mailexample.de
    "@mailexample.de":
      mail: "@mailexample.de"
      uid: wildcard
      greylisting: "TRUE"
```

A request for `unknown@sub.mailexample.de` finds no direct match, so the fallback runs and
looks up `@mailexample.de`.  The wildcard object is returned under the key
`unknown@sub.mailexample.de` in the response.

### Wildcard key computation

The wildcard key is derived from the queried address by a configurable regex substitution.

| Query option | Default | Description |
|---|---|---|
| `wildcard_key_pattern` | `.*@(.+)` | Regex applied to the address |
| `wildcard_key_replacement` | `@\1` | Replacement string for `re.sub` |

**Default behaviour** — use the full domain part as the wildcard key:

| Input address | Wildcard key |
|---|---|
| `user@example.com` | `@example.com` |
| `user@sub.example.com` | `@sub.example.com` |

To strip one subdomain level instead (so that `user@sub.example.com` looks up
`@example.com`), override the pattern:

```yaml
# Strip one subdomain level
wildcard_key_pattern: '.*@[^.]+\.(.+)'
wildcard_key_replacement: '@\1'
```

**Match-only mode** — when `wildcard_key_replacement` is omitted, `re.search` is used
instead of `re.sub`.  The first capture group (or the full match when no groups are
defined) becomes the wildcard key:

```yaml
# Use the domain part directly as the wildcard key (match-only mode)
wildcard_key_pattern: '@(.+)'
```

When the pattern does not match, the wildcard fallback is skipped for that address.

### Cache behaviour

For `GET /v1/query/{user}`, wildcard cache entries are only used as a fast-path after the
full address has already produced a negative cache hit.  This prevents a cached wildcard
entry from masking a real backend object for a specific mailbox.

### Stats counters

The fallback updates two counters in the stats output:

| Counter | Description |
|---|---|
| `wildcard_domain_hits` | Wildcard key was found in the backend |
| `wildcard_domain_misses` | Wildcard key was not found (empty fallback) |

These counters are also exported on `/metrics` as:

| Prometheus metric | Description |
|---|---|
| `xspct_db_wildcard_domain_hits_total` | Wildcard domain fallback hits |
| `xspct_db_wildcard_domain_misses_total` | Wildcard domain fallback misses |

### Full example with custom pattern

```yaml
xspct_db_queries:
  users:
    db_type: ldap
    server: ldap://ldap.example.com
    bind_dn: cn=reader,dc=example,dc=com
    bind_dn_pw: secret
    base_dn: ou=users,dc=example,dc=com
    search_filter: "(mail={MAIL})"
    search_filter_replace:
      "{MAIL}": username
    primary_key: mail
    attr_list: [mail, uid, quota]
    # Wildcard fallback: query @example.com for any unknown user@example.com
    wildcard_domain_query: true
    # Use default pattern (.*@(.+)) or override for subdomain stripping:
    #wildcard_key_pattern: '.*@[^.]+\.(.+)'
    #wildcard_key_replacement: '@\1'
```

---

## Address Rewrite Rules

`xspct_db_rewrite_rules` rewrites addresses **before** the prefilter, object cache lookup,
and backend query execution.  Use this when client-visible addresses should map to a canonical
mailbox before any whitelist or backend logic runs.

Rules are evaluated in order.  The first rule that changes the address wins; later rules are
not evaluated.

| Top-level key | Type | Default | Description |
|---|---|---|---|
| `xspct_db_rewrite_rules` | `list[dict]` | `null` | Ordered rewrite rules, each with `pattern` and `replacement` |

### Rule schema

| Rule field | Required | Description |
|---|---|---|
| `pattern` | yes | Python regex compiled with `re.compile()` |
| `replacement` | yes | Replacement string passed to `re.sub()` |

### Behaviour summary

- Rewrite runs before the prefilter, so domain whitelists and pattern filters see the canonical address.
- The response is still keyed under the original address received from the client.
- Both the original and canonical address forms are registered as cache aliases.
- A rule only counts as a match when it actually changes the address string.

### Example: relay-domain canonicalisation

```yaml
xspct_db_rewrite_rules:
  - pattern: '^(.+)@relay\\.mailexample\\.de$'
    replacement: '\\1@mailexample.de'
```

`alice@relay.mailexample.de` is rewritten to `alice@mailexample.de` for prefilter, cache,
and backend lookup, but the response still returns:

```json
{
  "users": {
    "alice@relay.mailexample.de": {
      "mail": ["alice@mailexample.de"],
      "uid": ["alice"]
    }
  }
}
```

### Example: enable wildcard only after rewriting

```yaml
xspct_db_rewrite_rules:
  - pattern: '^(.+)@realm$'
    replacement: '\\1@sub.mailexample.de'

xspct_db_queries:
  users:
    db_type: yaml
    primary_key: mail
    attr_list: ["*"]
    search_filter: [mail]
    wildcard_domain_query: true
```

In that setup, `unknown@realm` rewrites to `unknown@sub.mailexample.de`, and the wildcard
fallback then derives `@mailexample.de` from the rewritten canonical address.

---

## Concurrency

xspct_db uses two `asyncio.Semaphore` instances to limit concurrent backend queries and prevent
resource exhaustion under load.  The queue system is only active when `xspct_db_request_timeout`
is greater than `0`.

| Key | Default | Description |
|-----|---------|-------------|
| `xspct_db_foreground_slots` | `30` | Maximum concurrent foreground (client-blocking) queries |
| `xspct_db_background_slots` | `5` | Maximum concurrent background queries after a timeout |

**Request lifecycle (timeout > 0):**

1. Try to acquire a foreground slot (blocks up to `xspct_db_request_timeout`).
   If no slot is free within the deadline → **503 Service Overloaded**.
2. Run the backend query wrapped in `asyncio.shield`.
3. If the query finishes in time → release the slot and return the normal response.
4. If the query exceeds the timeout → return **504 Request Timeout** to the client.
   At the same time, try to acquire a background slot (non-blocking):
   - Slot free → the query keeps running in the background (populates caches for subsequent requests).
   - No background slot → the query task is cancelled immediately.

When `xspct_db_request_timeout` is `0` the semaphores are created but never used.

## Key Translation and Value Splitting

```yaml
# Rename attribute keys in the result
xspct_db_key_translation:
  cn: displayname

# Split string values on a delimiter
xspct_db_value_split:
  aliases: ","
```

---

## Multiple Databases and Result Merging

More than one query can be defined under `xspct_db_queries`.  All queries are executed in the
order they appear in the config file.  Results from each backend are **merged** into the same
`users` dict using `dict_merge`: scalar values are promoted to lists and lists are extended, so
attributes that appear in multiple backends are combined rather than overwritten.

### Parallel LDAP + MySQL lookup (merging)

This setup queries an LDAP directory for basic user attributes and a MySQL database for
additional quota/policy attributes.  Because both backends store the user under the same primary
key (`mail`), the results are automatically merged into a single user object.

```yaml
xspct_db_queries:

  # Backend 1 – LDAP directory (user identity attributes)
  ldap_users:
    db_type: ldap
    server: ldap://ldap.example.com
    bind_dn: cn=reader,dc=example,dc=com
    bind_dn_pw: secret
    base_dn: ou=users,dc=example,dc=com
    search_filter: "(mail={MAIL})"
    search_filter_replace:
      "{MAIL}": username
    primary_key: mail
    attr_list: [mail, uid, cn, aliases]

  # Backend 2 – MySQL policy database (quota attributes)
  mysql_policy:
    db_type: mysql
    host: 127.0.0.1
    port: 3306
    user: xspct
    password: secret
    db: maildb
    query: "SELECT mail, quota, active FROM users WHERE mail = '{MAIL}'"
    query_replace:
      "{MAIL}": username
    primary_key: mail
    pool_minconn: 1
    pool_maxconn: 10
```

A lookup for `alice@example.com` queries both backends and returns a merged response:

```json
{
  "users": {
    "alice@example.com": {
      "mail": "alice@example.com",
      "uid": "alice",
      "cn": "Alice Example",
      "aliases": ["a.example@example.com"],
      "quota": "2048M",
      "active": "1"
    }
  }
}
```

---

## `use_result` — Chained Queries

`use_result` chains a second query that uses an **attribute from a previous query result**
as its search value instead of the original email address.  This is the standard pattern for
resolving domain- or group-level attributes after a per-user lookup.

Without `use_result` the second query would search for the user's email address in the second
backend, which typically doesn't contain per-user rows there.
With `use_result` it uses a field from the first result (e.g. a group DN or a domain name) as
the search key for the second backend.

### Required per-query keys

| Key | Description |
|-----|-------------|
| `use_result: true` | Enable chained-query mode for this query |
| `result_object_attr` | Attribute from the **previous** result whose value is used as the new search key |

### Example: LDAP user lookup → LDAP domain policy lookup

```yaml
xspct_db_queries:

  # Step 1 – resolve the user; result includes a `domain` attribute
  ldap_users:
    db_type: ldap
    server: ldap://ldap.example.com
    bind_dn: cn=reader,dc=example,dc=com
    bind_dn_pw: secret
    base_dn: ou=users,dc=example,dc=com
    search_filter: "(mail={MAIL})"
    search_filter_replace:
      "{MAIL}": username
    primary_key: mail
    attr_list: [mail, uid, cn, domain, aliases]

  # Step 2 – use the `domain` attribute from step 1 to query domain policies
  ldap_domains:
    db_type: ldap
    server: ldap://ldap.example.com
    bind_dn: cn=reader,dc=example,dc=com
    bind_dn_pw: secret
    base_dn: ou=domains,dc=example,dc=com
    search_filter: "(domainName={DOMAIN})"
    search_filter_replace:
      "{DOMAIN}": username
    primary_key: mail        # keep the user's mail as primary key so results merge
    attr_list: [domainName, quota_default, reject_score]
    use_result: true         # don't search by email; use a prior result attribute instead
    result_object_attr: domain  # read the `domain` field from the step-1 result
```

**How it works:**

1. `ldap_users` runs first and finds `alice@example.com`.  Her result contains
   `domain: example.com`.
2. `ldap_domains` sees `use_result: true` and looks up `example.com` in the domains tree
   rather than `alice@example.com`.
3. The domain attributes are merged into Alice's existing user object.

Resulting response:

```json
{
  "users": {
    "alice@example.com": {
      "mail": "alice@example.com",
      "uid": "alice",
      "cn": "Alice Example",
      "domain": "example.com",
      "aliases": ["a.example@example.com"],
      "domainName": "example.com",
      "quota_default": "1024M",
      "reject_score": "15"
    }
  }
}
```

If the first query does not find the user (no `domain` attribute to read), or if the user is
not found in `user_to_pkey`, the chained query is skipped silently for that address.

### Example: MySQL user lookup → MySQL domain defaults

```yaml
xspct_db_queries:

  # Step 1 – per-user row; includes a domain FK column
  mysql_users:
    db_type: mysql
    host: 127.0.0.1
    port: 3306
    user: xspct
    password: secret
    db: maildb
    query: "SELECT mail, uid, domain FROM users WHERE mail = '{MAIL}'"
    query_replace:
      "{MAIL}": username
    primary_key: mail
    pool_minconn: 1
    pool_maxconn: 20

  # Step 2 – per-domain row; keyed on the domain name from step 1
  mysql_domains:
    db_type: mysql
    host: 127.0.0.1
    port: 3306
    user: xspct
    password: secret
    db: maildb
    query: "SELECT domain, quota_default, reject_score FROM domains WHERE domain = '{DOMAIN}'"
    query_replace:
      "{DOMAIN}": username
    primary_key: mail        # keep the user's mail as primary key so results merge
    pool_minconn: 1
    pool_maxconn: 5
    use_result: true
    result_object_attr: domain
```

---

## Complete Configuration Example

A production-style configuration combining TLS, Redis cache, LDAP + domain chaining, key
translation, value splitting, and per-request timeouts:

```yaml
xspct_db_listen_address: ["0.0.0.0", "::"]
xspct_db_listen_port: "11350"
xspct_db_log_level: 20        # INFO
xspct_db_request_timeout: 5   # return 504 after 5 seconds; continue in background

xspct_db_api_key:
  - "prod-key-abc123"
  - "monitoring-key-xyz"

xspct_db_tls:
  tls_enabled: true
  tls_cert: /etc/ssl/xspct-db/fullchain.pem
  tls_key:  /etc/ssl/xspct-db/privkey.pem

xspct_db_local_cache:
  enabled: true
  expire: 30
  expire_negative: 60
  max_entries: 50000

xspct_db_redis_cache:
  enabled: true
  host: redis.internal
  port: 6379
  expire: 120
  expire_negative: 120
  max_connections: 40

xspct_db_response_cache:
  enabled: true
  expire: 15
  max_entries: 10000

xspct_db_queries:

  ldap_users:
    db_type: ldap
    server: ldap://ldap.internal
    bind_dn: cn=xspct,dc=example,dc=com
    bind_dn_pw: secret
    base_dn: ou=users,dc=example,dc=com
    search_filter: "(|(mail={MAIL})(mailAlias={MAIL}))"
    search_filter_replace:
      "{MAIL}": username
    primary_key: mail
    attr_list: [mail, uid, cn, domain, mailAlias, quota]
    pool_minconn: 2
    pool_maxconn: 20

  ldap_domains:
    db_type: ldap
    server: ldap://ldap.internal
    bind_dn: cn=xspct,dc=example,dc=com
    bind_dn_pw: secret
    base_dn: ou=domains,dc=example,dc=com
    search_filter: "(domainName={DOMAIN})"
    search_filter_replace:
      "{DOMAIN}": username
    primary_key: mail
    attr_list: [domainName, quota_default, reject_score, greylist_score]
    pool_minconn: 1
    pool_maxconn: 5
    use_result: true
    result_object_attr: domain

xspct_db_key_translation:
  mailAlias: aliases
  cn: displayname

xspct_db_value_split:
  aliases: ","

xspct_db_rspamd_alias_fields:
  - aliases
```
