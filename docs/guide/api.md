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
| `xspct_db_foreground_overloaded_total` | counter | Requests rejected because all foreground slots were busy |
| `xspct_db_requests_timeout_total` | counter | Requests that exceeded the configured timeout |
| `xspct_db_background_completed_total` | counter | Background tasks that finished successfully |
| `xspct_db_background_rejected_total` | counter | Background tasks cancelled because no background slot was free |
| `xspct_db_background_errors_total` | counter | Background tasks that raised an unhandled exception |
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

**Body encoding:** The response format is controlled by content negotiation (see
[Body encoding](#body-encoding) below).  Send `Accept: application/msgpack` to receive a
msgpack-encoded response.

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
Returns `503 Service Overloaded` when all foreground query slots are busy.
Returns `504 Request Timeout` when the per-request timeout is exceeded.

---

### `POST /v1/query-json`

Batch lookup for multiple users in a single request.
The response cache (`xspct_db_response_cache`) is consulted first when enabled; on a miss the
result is stored for subsequent identical requests.
Redis (L2) is **not** consulted or populated for batch requests.

**Authentication:** `X-Api-Key` header

**Body encoding:** Request and response bodies can be JSON or msgpack (see
[Body encoding](#body-encoding) below).
JSON and msgpack responses for the same user set are cached under separate keys.

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
Returns `400 Bad Request` when the user list exceeds `xspct_db_query_json_max_users` (default 500).
Returns `401 Unauthorized` on bad/missing API key.
Returns `500` on backend errors.
Returns `503 Service Overloaded` when all foreground query slots are busy.
Returns `504 Request Timeout` when the per-request timeout is exceeded.

---

### `POST /v1/rspamd-settings`

Returns an Rspamd settings blob for use with the Rspamd HTTP settings module.
The response cache (`xspct_db_response_cache`) is consulted first when enabled; on a miss the
result is stored for subsequent identical requests.  The cache key is built from the fields
listed in `xspct_db_response_cache.rspamd_key_fields`.

**Authentication:** `X-Api-Key` header

**Body encoding:** Request and response bodies can be JSON or msgpack (see
[Body encoding](#body-encoding) below).

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
  "flags": [],
  "groups_disabled": [],
  "symbols_disabled": ["DKIM_SIGNED"],
  "symbols": ["SETTINGS_API_TEST_RESPONSE"],
  "settings_data": {
    "users": {
      "sender@mailexample.de": {
        "mail": "sender@mailexample.de",
        "uid": "sender",
        "aliases": ["s.smith@mailexample.de"]
      }
    },
    "aliases": {"s.smith@mailexample.de": "sender@mailexample.de"}
  },
  "settings_error": []
}
```

Fields with `null` values (`groups_enabled`, `symbols_enabled`) are omitted from the response (`exclude_none=True`).

`settings_data` contains all users found for the envelope addresses (sender + recipients)
mapped by primary key, plus a reverse alias map.  It is an empty object when no users are found.
`settings_error` contains any error messages produced during settings evaluation.

Returns `401 Unauthorized` on bad/missing API key.
Returns `503 Service Overloaded` when all foreground query slots are busy.
Returns `504 Request Timeout` when the per-request timeout is exceeded.

---

## Body encoding

All query endpoints support both **JSON** (default) and **msgpack** (`pip install
"xspct_db[msgpack]"`).  Content negotiation follows this precedence:

1. **`Accept` header** — if it contains `application/msgpack` or `application/x-msgpack` the
   response is msgpack-encoded.  `application/json` forces JSON.
2. **`Content-Type` mirroring** — if the `Accept` header gives no clear preference, the
   response format mirrors the request `Content-Type`.
3. **Default** — JSON.

Supported MIME types for both request and response: `application/msgpack`,
`application/x-msgpack`.

If `msgpack` is not installed and a client requests msgpack encoding, the server returns
`406 Not Acceptable`.

**Example — send and receive msgpack:**

```bash
python3 -c "
import msgpack, sys
sys.stdout.buffer.write(msgpack.packb({'users': ['alice@mailexample.de']}))
" | curl -s -X POST http://localhost:11350/v1/query-json \
     -H 'X-Api-Key: your-key' \
     -H 'Content-Type: application/msgpack' \
     --data-binary @- | python3 -c "import msgpack,sys; print(msgpack.unpackb(sys.stdin.buffer.read()))"
```

**Example — JSON request, msgpack response:**

```bash
curl -s -X POST http://localhost:11350/v1/query-json \
     -H 'X-Api-Key: your-key' \
     -H 'Content-Type: application/json' \
     -H 'Accept: application/msgpack' \
     -d '{"users": ["alice@mailexample.de"]}' | python3 -c "import msgpack,sys; print(msgpack.unpackb(sys.stdin.buffer.read()))"
```
