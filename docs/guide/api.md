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
| `xspct_db_redis_hits_total` | counter | Positive Redis cache hits |
| `xspct_db_redis_misses_total` | counter | Redis cache misses |
| `xspct_db_redis_negative_hits_total` | counter | Negative cache hits |
| `xspct_db_query_requests_total{query="…"}` | counter | Queries executed per backend |
| `xspct_db_query_duration_seconds_total{query="…"}` | counter | Accumulated query time per backend |

---

## Query Endpoints

### `GET /query/v1/{user}`

Look up a single user across all configured query backends.

**Authentication:** `X-Api-Key` header (or the configured header name)

**Path parameter:** `user` – the email address or username to look up (URL-encoded)

**Response:**

```json
{
  "users": {
    "alice@example.com": {
      "mail": ["alice@example.com"],
      "uid": ["alice"]
    }
  }
}
```

Returns an empty `users` object (`{}`) when the user is not found.
Returns `401 Unauthorized` on bad/missing API key.
Returns `500` on backend errors.
Returns `504` when a per-request timeout is exceeded.

---

### `POST /query-json/v1`

Batch lookup for multiple users in a single request.

**Authentication:** `X-Api-Key` header

**Request body:**

```json
{
  "users": [
    {"username": "alice@example.com"},
    {"username": "bob@example.com"}
  ]
}
```

**Response:** same shape as `/query/v1/{user}` but containing all matched users.

---

### `POST /rspamd-settings/v1`

Returns an Rspamd settings blob for the queried user.
Intended for use with the Rspamd `settings_redis` or HTTP settings module.

**Authentication:** `X-Api-Key` header

**Request body:** same JSON format as `/query-json/v1`

**Response:** plain-text Rspamd settings in UCL format with
`Content-Type: text/plain; version=0.0.4; charset=utf-8`.
