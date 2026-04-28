# HTTP API Reference

All query endpoints require authentication via the configured API key header
(default: `X-Api-Key`).

## Health Endpoints

### `GET /`

Returns `200 Hello, world`. Used for liveness probes.

### `GET /ping`

Returns `200 Pong`. Lightweight health check.

---

## Metrics

### `GET /metrics`

Returns Prometheus-compatible text metrics.
Authentication is optional and controlled by `xspct_db_metrics_auth`.

**Metrics exposed:**

| Metric | Type | Description |
|--------|------|-------------|
| `xspct_db_requests_total` | counter | All incoming requests |
| `xspct_db_requests_known_total` | counter | Requests where the user was found |
| `xspct_db_requests_unknown_total` | counter | Requests where the user was not found |
| `xspct_db_local_cache_hits_total` | counter | L1 in-process cache hits |
| `xspct_db_local_cache_misses_total` | counter | L1 in-process cache misses |
| `xspct_db_response_cache_hits_total` | counter | Response cache hits (query-json / rspamd-settings) |
| `xspct_db_response_cache_misses_total` | counter | Response cache misses |
| `xspct_db_redis_hits_total` | counter | Positive Redis cache hits |
| `xspct_db_redis_misses_total` | counter | Redis cache misses |
| `xspct_db_redis_negative_hits_total` | counter | Negative cache hits |
| `xspct_db_query_requests_total{query="…"}` | counter | Queries executed per backend |
| `xspct_db_query_duration_seconds_total{query="…"}` | counter | Accumulated query time per backend |

---

## Query Endpoints

The canonical path prefix is `/v1/`; legacy paths (`/query/v1/{user}`,
`/query-json/v1`, `/rspamd-settings/v1`) are accepted for backwards compatibility.

### `GET /v1/query/{user}`

Look up a single user across all configured query backends.
The L1 in-process cache is consulted first, then Redis (L2) when enabled, then the backend.

**Authentication:** `X-Api-Key` header (or the configured header name)

**Path parameter:** `user` – the email address or username to look up (URL-encoded)

**Response (user found):**

```json
{
  "users": {
    "alice@mailexample.de": {
      "mail": "alice@mailexample.de",
      "uid": "alice",
      "aliases": ["a.smith@mailexample.de"]
    }
  }
}
```

**Response (user not found):**

```json
{"users": {}}
```

Returns `401 Unauthorized` on bad/missing API key.
Returns `500` on backend errors.
Returns `504` when a per-request timeout is exceeded.

---

### `POST /v1/query-json`

Batch lookup for multiple users in a single request.
The response cache (`xspct_db_response_cache`) is consulted first when enabled; on a miss the
result is stored for subsequent identical requests.
Redis (L2) is **not** consulted or populated for batch requests.

**Authentication:** `X-Api-Key` header

**Request body:**

```json
{
  "users": [
    "alice@mailexample.de",
    "bob@mailexample.de"
  ]
}
```

**Response:**

```json
{
  "users": {
    "alice@mailexample.de": {
      "mail": "alice@mailexample.de",
      "uid": "alice"
    },
    "bob@mailexample.de": {}
  }
}
```

Users not found in any backend are returned with an empty dict.
Returns `401 Unauthorized` on bad/missing API key.
Returns `500` on backend errors.

---

### `POST /v1/rspamd-settings`

Returns an Rspamd settings blob for use with the Rspamd HTTP settings module.
The response cache (`xspct_db_response_cache`) is consulted first when enabled; on a miss the
result is stored for subsequent identical requests.  The cache key is built from the fields
listed in `xspct_db_response_cache.rspamd_key_fields`.

**Authentication:** `X-Api-Key` header

**Request body** (all fields optional):

```json
{
  "uid": "<rspamd-session-uid>",
  "from": "sender@mailexample.de",
  "rcpts": ["recipient@mailexample.de"],
  "mta-name": "postfix",
  "mta-host": "mail.example.com",
  "ip": "203.0.113.1",
  "settings-name": "inbound",
  "settings-id": "abc123"
}
```

**Response** (`application/json`):

```json
{
  "actions": {
    "reject": 15,
    "greylist": 8,
    "add header": 13
  },
  "flags": ["skip_process", "no_stat"],
  "groups_disabled": ["antivirus", "external_services"],
  "groups_enabled": null,
  "symbols": ["INCOMING_API_TEST", "INCOMING"],
  "symbols_disabled": [],
  "symbols_enabled": null,
  "settings_extra_data": {
    "users": {
      "sender@mailexample.de": {"mail": "sender@mailexample.de", "uid": "sender"}
    },
    "aliases": {"alias@mailexample.de": "sender@mailexample.de"}
  },
  "settings_error": []
}
```

`settings_extra_data` contains all users found for the envelope addresses (sender + recipients)
mapped by primary key, plus a reverse alias map.  It is an empty object when no users are found.
`settings_error` contains any error messages produced during settings evaluation.
