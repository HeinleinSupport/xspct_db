# Changelog

## 0.7.1 (2026-05-07)

### Features
- Address prefilter: reject addresses before any cache or backend lookup
  - Domain whitelist (`xspct_db_prefilter_domains`): frozenset built from inline list, file, and/or Redis SET; background watchers reload on file mtime change and Redis pub/sub signal
  - Pattern filter (`xspct_db_prefilter_patterns`): per-address regex matching (Python `re.search`)
  - Master switch `xspct_db_prefilter.enabled`; each sub-filter has its own `enabled` flag
  - Last-known-good state machine: failed reloads keep the previous valid set; `min_domains` guard; `max_age` TTL with bypass on expiry
  - Filtered requests return `200 {"users": {}}` without hitting any backend

### Stats / Metrics
- New stats counters: `prefilter_domain_count` (gauge), `prefilter_domain_hits`, `prefilter_domain_misses`, `prefilter_pattern_hits`, `prefilter_pattern_misses`
- Periodic log emits `STATS prefilter_domains` and `STATS prefilter_patterns` lines with hit rate
- All five metrics exposed on `/metrics` as Prometheus counters/gauge

### Renamed
- `settings_extra_data` → `settings_data` in `RspamdSettingsResponse` schema and all related code

## 0.7.0 (2026-05-06)

### Security
- Clamp client-controlled per-request timeout header to a configurable maximum (`xspct_db_request_timeout_header_max`, default 120 s); values ≤ 0 are rejected with a warning
- Added configurable limit for batch user lookups (`xspct_db_query_json_max_users`, default 500); exceeding it returns `400 Bad Request`
- Log a `WARNING` at startup when API key verification is disabled (`xspct_db_api_key_verify_fail: false`)
- Explicit configurable request body size limit (`xspct_db_client_max_size`, default 1 MiB)

### Performance
- Stats background task reference stored on the app object; properly cancelled on shutdown
- `msgpack` imported once at module level instead of on every request
- `POST /v1/query-json` response cache key uses a sorted tuple instead of `frozenset`
- Periodic proactive Redis health-check PING resets the circuit-breaker on recovery without waiting for a real query
- Prometheus `/metrics` output cached with a 5 s TTL to avoid rebuilding the payload on every scrape

## 0.6.0 (2026-04-27)

- Updated copyright year to 2026
- Added Redis cache tests using `fakeredis`
- Added LDAP backend tests using mocked `bonsai`
- Created SPDX `LICENSES/` folder with canonical EUPL-1.2 text and `LICENSE` symlink
- Added Sphinx documentation

## Earlier releases

See the repository history for changes prior to 0.6.0.
