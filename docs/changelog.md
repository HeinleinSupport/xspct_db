# Changelog

## 0.7.4 (2026-05-13)

### Features
- Wildcard domain query fallback: when a user address is not found by the regular backend
  queries, a configurable wildcard key (e.g. `@example.com`) is looked up and the result is
  returned under the original address — enabled per query with `wildcard_domain_query: true`
- Wildcard key computation is driven by a `re.sub`-based pattern configurable per query:
  - `wildcard_key_pattern` (default `.*@[^.]+\.(.+)`) — regex applied to the address
  - `wildcard_key_replacement` (default `@\1`) — replacement string; omit for match-only mode
  - Default behaviour strips one subdomain level: `user@sub.example.com` → `@example.com`
- New stats counters `wildcard_domain_hits` and `wildcard_domain_misses` track fallback outcomes
- Enhanced L1 / Redis cache debug logging: cache hit/miss details included in DEBUG-level log lines

### Fixes
- Reject level computation in `_compute_rcpt_settings`: use `min()` across all translated
  recipient reject scores instead of requiring all values to be identical — ensures the most
  restrictive score is applied when recipients have different reject levels
- `reject_level` entry now included in `settings_data.profile.applied_rules` when a reject
  score is set
- Improved error handling for missing `prometheus-client`; response logging enhanced with
  status code and `Content-Type` in DEBUG output

### Documentation
- New **Wildcard Domain Query** section in `docs/guide/configuration.md` with quick example,
  key computation reference, stats counters, and full LDAP example
- README: new Wildcard Domain Query paragraph with link to configuration reference
- Installation instructions updated throughout to use GitHub source URL
- Project layout and API endpoint descriptions updated in reference docs

## 0.7.3 (2026-05-08)

### Features
- Prometheus metrics integration via `prometheus_client` (optional extra `[metrics]`):
  - New package `xspct_db/metrics/` with `setup_metrics(app)` entry point
  - HTTP middleware records `http_requests_total{method,route,status}`, `http_request_duration_seconds{method,route}`, and `http_requests_in_flight`
  - Background task measures `event_loop_lag_seconds` with WARNING log above 100 ms threshold
  - `/metrics` handler with per-app TTL cache (`xspct_db_metrics_cache_ttl`, default 5 s) and optional auth
  - Custom `_StatsCollector` bridges existing `stats.stats` counters into the Prometheus registry on every scrape (zero-overhead when not enabled)
  - `ProcessCollector` and `PlatformCollector` included by default (process CPU, memory, open FDs)
  - Enabled via `xspct_db_metrics_enabled: true`; without the extra the service starts normally with metrics disabled
- Old hand-rolled `/metrics` endpoint (`MetricsView`, `_prometheus_lines()`) removed; route is now registered by `setup_metrics()`
- Periodic `log_stats_periodically` background task removed from startup/shutdown; Prometheus scrape replaces it

### Fix
- `_detect_response_format()`: skip msgpack MIME types in the `Accept` header when the `msgpack` library is not installed instead of returning `"msgpack"` unconditionally — prevents 406 responses from clients (e.g. rspamd) that advertise msgpack support but can fall back to JSON
- `_log_response()`: now includes response headers in the DEBUG log line
- `_rs_task()`: rspamd-settings response DEBUG log now includes HTTP status code and `Content-Type`

### Configuration
- New top-level config keys:
  - `xspct_db_metrics_enabled: false` — enable the Prometheus `/metrics` endpoint
  - `xspct_db_metrics_cache_ttl: 5` — TTL in seconds for the cached `generate_latest()` output

### Project
- New optional dependency extra: `metrics = ["prometheus-client>=0.19"]`
- `all` extra extended with `prometheus-client>=0.19`
- Startup WARNING when `msgpack` library is not installed
- New test package `tests/metrics/` (middleware, loop-lag, handler, integration)

## 0.7.2 (2026-05-08)

### Features
- Dynamic Rspamd settings rules engine in `POST /v1/rspamd-settings`:
  - Data-driven rule evaluation replaces hardcoded static settings
  - Per-recipient attribute conditions (`truthy`, `falsy`, `eq`, `ne`, `present`, `absent`) with per-rule aggregation (`all` = AND, `any` = OR)
  - Built-in default rule: `disable_greylisting` — disables greylisting symbols and sets `actions.greylist: null` when all recipients have `greylisting=FALSE`
  - Override default rules entirely via `xspct_db_rspamd_rules` in config (YAML list of rule dicts)
  - Single-recipient fast path: skips list allocation and `all()`/`any()` aggregation
  - Handles LDAP string booleans (`"TRUE"` / `"FALSE"`) and single-element lists returned by all backends
  - SA → Rspamd reject-score translation via `xspct_db_reject_level_map`; `actions.reject` is only set when **all** recipients have a mapped `reject_level` and the result differs from `xspct_db_reject_level_default`
- `settings_data` extended with a `profile` section when at least one user was found:
  - `settings_data.profile.applied_rules` — list of rule names that fired for this request
- `RspamdSettingsResponse` schema extended:
  - `subject: str | None` — Rspamd subject rewrite field
  - `symbols: list[str] | dict[str, float]` — supports scored symbol map alongside plain list
  - `actions: dict[str, float | str]` — widened to allow `"null"` string value for greylist passthrough

### Configuration
- New top-level config keys (with defaults):
  - `xspct_db_reject_level_map: {"5": 13, "6": 15, "6.31": 17}`
  - `xspct_db_reject_level_default: 15`
  - `xspct_db_rspamd_rules: null` (use built-in default when null)

### Project
- Dependency SBOM (`bom.json`) generated via `cyclonedx-bom` (CycloneDX JSON) and committed alongside each release
- New hatch scripts: `hatch run sbom-deps` / `hatch run sbom`
- New dev dependencies: `reuse>=4.0`, `cyclonedx-bom>=5.0`
- `/generate-sbom` VS Code Copilot slash command added

### Debug
- Removed noisy per-merge `DEBUG merge_userdata` log line from `backends/base.py`
- Added `DEBUG` log of the computed Rspamd settings response before serialisation in `_rs_task()`


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
