# SPDX-License-Identifier: EUPL-1.2
# SPDX-FileCopyrightText: 2026 Carsten Rosenberg <c.rosenberg@heinlein-support.de>

"""Address prefilter: domain whitelist + username pattern validation.

The prefilter is applied to every query endpoint *before* any cache or backend
lookup.  Addresses that do not pass the filter are silently dropped; if all
addresses are dropped the endpoint returns an empty result immediately without
hitting any backend.

Two independent sub-filters (each has its own ``enabled`` flag):

``xspct_db_prefilter_domains``
    Keeps only addresses whose domain part is in a dynamically maintained
    ``frozenset[str]``.  The set is built from one or more *sources* and
    refreshed at runtime without restarting the service:

    * ``inline``  — static list in the YAML config
    * ``file``    — plain-text file (one domain per line, ``#`` = comment);
                    reloaded automatically when the file's mtime changes
    * ``redis``   — Redis ``SET`` key (``SMEMBERS``); reloaded on pub/sub
                    signal and/or on a configurable safety-net interval

    **Last-known-good / expiry logic:**

    * When a reload produces a valid set (≥ ``min_domains`` entries) the
      in-memory ``frozenset`` is replaced atomically.
    * When a reload produces a *defunct* set (empty or below ``min_domains``)
      the previous ``frozenset`` is kept.
    * The ``_domain_set_loaded_at`` timestamp is updated only on a *valid*
      reload.  When ``max_age > 0`` and the timestamp is older than
      ``max_age`` seconds at filter time, the frozenset is dropped and the
      filter is bypassed until the next successful reload.
    * On the very first startup, if no valid set can be loaded, the filter
      is bypassed (no set to fall back to) with a WARNING.

``xspct_db_prefilter_patterns``
    Keeps only addresses that match *at least one* of the configured regular
    expressions (compiled once at startup / reload).

Both sub-filters are gated by a master switch ``xspct_db_prefilter.enabled``.
"""

from __future__ import annotations

import asyncio
import logging
import os
import re
import time
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from aiohttp import web

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Module-level state
# ---------------------------------------------------------------------------

# Domain filter state
_domain_set: frozenset[str] = frozenset()
_domain_set_active: bool = False  # False = bypass (no valid set yet / expired)
_domain_set_loaded_at: float = 0.0  # monotonic time of last valid load
_domain_set_expired_logged: bool = False  # suppress repeated expiry log lines
_file_mtime: float = 0.0  # last seen mtime of the domain file

# Pattern filter state
_patterns: list[re.Pattern[str]] = []


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _load_file(path: str) -> frozenset[str]:
    """Read a domain file and return a frozenset of lower-cased domain strings.

    Lines starting with ``#`` (after stripping) and blank lines are ignored.
    Returns an empty frozenset on any I/O error.
    """
    try:
        with open(path) as fh:
            domains: set[str] = set()
            for line in fh:
                stripped = line.strip()
                if stripped and not stripped.startswith("#"):
                    domains.add(stripped.lower())
        return frozenset(domains)
    except Exception as exc:
        logger.error("prefilter: error reading domain file %s: %s", path, exc)
        return frozenset()


async def _load_redis(cfg: dict[str, Any]) -> frozenset[str]:
    """Fetch the domain set from a Redis SET key (SMEMBERS).

    Returns an empty frozenset when Redis is not configured, the key is not
    set, or the connection fails.
    """
    from xspct_db import cache  # local import to avoid circular dependency

    if cache.connection is None:
        logger.warning("prefilter: Redis source configured but cache.connection is None")
        return frozenset()

    redis_key = cfg["xspct_db_prefilter_domains"].get("redis_key", "")
    if not redis_key:
        return frozenset()

    try:
        members = await cache.connection.smembers(redis_key)
        return frozenset(m.lower() for m in members if m)
    except Exception as exc:
        logger.error("prefilter: error fetching Redis key %s: %s", redis_key, exc)
        return frozenset()


def _build_domain_set_sync(cfg: dict[str, Any]) -> frozenset[str]:
    """Build a domain frozenset from inline + file sources (synchronous).

    Used at startup before the event loop is fully running for async calls.
    Redis source is skipped here; it is loaded in ``start()`` via the async
    path.
    """
    dcfg = cfg.get("xspct_db_prefilter_domains", {})
    domains: set[str] = set()

    # Inline list
    for d in dcfg.get("inline", []):
        if isinstance(d, str) and d.strip():
            domains.add(d.strip().lower())

    # File
    path = dcfg.get("file", "")
    if path:
        domains.update(_load_file(path))

    return frozenset(domains)


async def _build_domain_set_full(cfg: dict[str, Any]) -> frozenset[str]:
    """Build a domain frozenset from all sources (async — includes Redis)."""
    domains: set[str] = set(_build_domain_set_sync(cfg))
    redis_members = await _load_redis(cfg)
    domains.update(redis_members)
    return frozenset(domains)


def _compile_patterns(cfg: dict[str, Any]) -> list[re.Pattern[str]]:
    """Compile the configured username patterns into a list of ``re.Pattern``."""
    compiled: list[re.Pattern[str]] = []
    for raw in cfg.get("xspct_db_prefilter_patterns", {}).get("patterns", []):
        try:
            compiled.append(re.compile(raw))
        except re.error as exc:
            logger.error("prefilter: invalid pattern %r: %s", raw, exc)
    return compiled


def _validate_and_apply(new_set: frozenset[str], cfg: dict[str, Any]) -> None:
    """Validate *new_set* and update module-level state.

    * Valid (≥ ``min_domains``) → replace ``_domain_set``, update timestamp,
      set ``_domain_set_active = True``.
    * Defunct (< ``min_domains`` or empty) → keep previous set; log ERROR.
    * First-load defunct → bypass (``_domain_set_active`` stays False).
    """
    global _domain_set, _domain_set_active, _domain_set_loaded_at, _domain_set_expired_logged

    dcfg = cfg.get("xspct_db_prefilter_domains", {})
    min_domains = int(dcfg.get("min_domains", 0))

    if len(new_set) >= max(min_domains, 1 if min_domains > 0 else 0) or (min_domains == 0 and len(new_set) >= 0):
        # Accept any set when min_domains == 0; require >= min_domains otherwise.
        if min_domains > 0 and len(new_set) < min_domains:
            _log_defunct(new_set, min_domains)
            return
    else:
        _log_defunct(new_set, min_domains)
        return

    _domain_set = new_set
    _domain_set_active = True
    _domain_set_loaded_at = time.monotonic()
    _domain_set_expired_logged = False
    logger.info("prefilter: domain set updated with %d entries", len(new_set))
    from xspct_db import stats as _stats

    _stats.stats["prefilter_domain_count"] = len(new_set)


def _log_defunct(new_set: frozenset[str], min_domains: int) -> None:
    global _domain_set_active
    if _domain_set_active:
        logger.error(
            "prefilter: reload produced %d entries (< min_domains=%d); keeping previous %d domains",
            len(new_set),
            min_domains,
            len(_domain_set),
        )
    else:
        logger.error(
            "prefilter: first load produced %d entries (< min_domains=%d); bypassing filter",
            len(new_set),
            min_domains,
        )


def _check_expiry(cfg: dict[str, Any]) -> bool:
    """Return True when the domain set is currently active (not expired).

    When ``max_age > 0`` and the set has not been refreshed within ``max_age``
    seconds, the set is dropped and the filter is bypassed until the next
    successful reload.  The expiry log is emitted only once per expiry event.
    """
    global _domain_set, _domain_set_active, _domain_set_expired_logged

    if not _domain_set_active:
        return False

    max_age = float(cfg.get("xspct_db_prefilter_domains", {}).get("max_age", 0))
    if max_age <= 0:
        return True

    age = time.monotonic() - _domain_set_loaded_at
    if age <= max_age:
        return True

    # Expired — drop set and switch to bypass.
    if not _domain_set_expired_logged:
        logger.error(
            "prefilter: domain set expired (age=%.0fs > max_age=%.0fs); bypassing until next valid reload",
            age,
            max_age,
        )
        _domain_set_expired_logged = True
    _domain_set = frozenset()
    _domain_set_active = False
    return False


# ---------------------------------------------------------------------------
# Public filter API (called from routes.py)
# ---------------------------------------------------------------------------


def filter_user(s: str, user: str, app: "web.Application") -> bool:
    """Return ``True`` when *user* passes all enabled prefilters.

    Called from ``QueryView.get()`` for single-user lookups.
    """
    cfg: dict[str, Any] = app["config"]
    if not cfg.get("xspct_db_prefilter", {}).get("enabled", False):
        return True

    from xspct_db import stats as _stats

    pcfg = cfg.get("xspct_db_prefilter_patterns", {})
    if pcfg.get("enabled", False) and _patterns:
        if not any(p.search(user) for p in _patterns):
            logger.debug("%s prefilter: %r rejected by pattern filter", s, user)
            _stats.stats["prefilter_pattern_misses"] += 1
            return False
        _stats.stats["prefilter_pattern_hits"] += 1

    dcfg = cfg.get("xspct_db_prefilter_domains", {})
    if dcfg.get("enabled", False) and _check_expiry(cfg):
        domain = user.split("@", 1)[-1].lower()
        if domain not in _domain_set:
            logger.debug("%s prefilter: %r rejected (domain %r not in set)", s, user, domain)
            _stats.stats["prefilter_domain_misses"] += 1
            return False
        _stats.stats["prefilter_domain_hits"] += 1

    return True


def filter_addresses(s: str, addresses: list[str], app: "web.Application") -> list[str]:
    """Return the subset of *addresses* that pass all enabled prefilters.

    Called from ``QueryJsonView.post()`` and ``RspamdSettingsView.post()``.
    """
    cfg: dict[str, Any] = app["config"]
    if not cfg.get("xspct_db_prefilter", {}).get("enabled", False):
        return addresses

    result = list(addresses)

    from xspct_db import stats as _stats

    pcfg = cfg.get("xspct_db_prefilter_patterns", {})
    if pcfg.get("enabled", False) and _patterns:
        before = len(result)
        result = [a for a in result if any(p.search(a) for p in _patterns)]
        removed = before - len(result)
        if removed:
            logger.debug(
                "%s prefilter: pattern filter removed %d of %d addresses",
                s,
                removed,
                before,
            )
        _stats.stats["prefilter_pattern_hits"] += len(result)
        _stats.stats["prefilter_pattern_misses"] += removed

    dcfg = cfg.get("xspct_db_prefilter_domains", {})
    if dcfg.get("enabled", False) and _check_expiry(cfg):
        before = len(result)
        result = [a for a in result if a.split("@", 1)[-1].lower() in _domain_set]
        removed = before - len(result)
        if removed:
            logger.debug(
                "%s prefilter: domain filter removed %d of %d addresses",
                s,
                removed,
                before,
            )
        _stats.stats["prefilter_domain_hits"] += len(result)
        _stats.stats["prefilter_domain_misses"] += removed

    return result


# ---------------------------------------------------------------------------
# Background tasks
# ---------------------------------------------------------------------------


async def _file_watcher(cfg: dict[str, Any]) -> None:
    """Background task: reload the domain file when its mtime changes."""
    global _file_mtime

    dcfg = cfg.get("xspct_db_prefilter_domains", {})
    path = dcfg.get("file", "")
    interval = float(dcfg.get("file_reload_interval", 60))
    if not path or interval <= 0:
        return

    while True:
        await asyncio.sleep(interval)
        try:
            mtime = os.stat(path).st_mtime
        except OSError:
            logger.warning("prefilter: domain file not found: %s", path)
            continue

        if mtime == _file_mtime:
            continue

        _file_mtime = mtime
        logger.info("prefilter: domain file changed, reloading from %s", path)
        new_set = await _build_domain_set_full(cfg)
        _validate_and_apply(new_set, cfg)


async def _redis_watcher(cfg: dict[str, Any]) -> None:
    """Background task: reload domains from Redis on pub/sub signal or interval."""
    from xspct_db import cache  # local import

    dcfg = cfg.get("xspct_db_prefilter_domains", {})
    redis_key = dcfg.get("redis_key", "")
    redis_channel = dcfg.get("redis_channel", "")
    reload_interval = float(dcfg.get("redis_reload_interval", 300))

    if not redis_key:
        return

    if cache.connection is None:
        logger.warning("prefilter: Redis watcher: cache.connection not available, skipping")
        return

    pubsub = None
    if redis_channel:
        try:
            pubsub = cache.connection.pubsub()
            await pubsub.subscribe(redis_channel)
            logger.info("prefilter: subscribed to Redis channel %r", redis_channel)
        except Exception as exc:
            logger.error("prefilter: failed to subscribe to %r: %s", redis_channel, exc)
            pubsub = None

    last_reload = time.monotonic()

    while True:
        # Check pub/sub message (non-blocking, 1 s window)
        if pubsub is not None:
            try:
                message = await pubsub.get_message(ignore_subscribe_messages=True, timeout=1.0)
                if message and message.get("type") == "message":
                    logger.info("prefilter: pub/sub reload signal on channel %r", redis_channel)
                    new_set = await _load_redis(cfg)
                    _validate_and_apply(new_set, cfg)
                    last_reload = time.monotonic()
                    continue
            except Exception as exc:
                logger.warning("prefilter: pub/sub get_message error: %s", exc)
        else:
            await asyncio.sleep(1)

        # Safety-net periodic reload
        if reload_interval > 0 and time.monotonic() - last_reload >= reload_interval:
            logger.debug("prefilter: safety-net Redis reload")
            new_set = await _load_redis(cfg)
            _validate_and_apply(new_set, cfg)
            last_reload = time.monotonic()


# ---------------------------------------------------------------------------
# Lifecycle: start / stop
# ---------------------------------------------------------------------------


async def start(app: "web.Application", cfg: dict[str, Any]) -> None:
    """Initialise the prefilter; start background watcher tasks.

    Called from ``server._on_startup()``.
    """
    global _patterns, _file_mtime

    if not cfg.get("xspct_db_prefilter", {}).get("enabled", False):
        logger.debug("prefilter: disabled")
        return

    # Compile patterns
    pcfg = cfg.get("xspct_db_prefilter_patterns", {})
    if pcfg.get("enabled", False):
        _patterns = _compile_patterns(cfg)
        logger.info("prefilter: compiled %d username patterns", len(_patterns))

    # Domain filter
    dcfg = cfg.get("xspct_db_prefilter_domains", {})
    if dcfg.get("enabled", False):
        # Warn if max_age < smallest reload interval
        max_age = float(dcfg.get("max_age", 0))
        if max_age > 0:
            min_interval = (
                min(
                    v
                    for v in [
                        float(dcfg.get("file_reload_interval", 0)),
                        float(dcfg.get("redis_reload_interval", 0)),
                    ]
                    if v > 0
                )
                if any(float(dcfg.get(k, 0)) > 0 for k in ("file_reload_interval", "redis_reload_interval"))
                else 0
            )
            if min_interval > 0 and max_age < min_interval * 2:
                logger.warning(
                    "prefilter: max_age=%.0f is less than 2x the smallest reload interval (%.0f); "
                    "the domain set may expire before the next reload",
                    max_age,
                    min_interval,
                )

        # Record initial file mtime so the watcher doesn't reload immediately
        path = dcfg.get("file", "")
        if path:
            try:
                _file_mtime = os.stat(path).st_mtime
            except OSError:
                _file_mtime = 0.0

        # Initial load (async — includes Redis)
        new_set = await _build_domain_set_full(cfg)
        _validate_and_apply(new_set, cfg)

    # Start background tasks
    tasks: list[asyncio.Task] = []
    if dcfg.get("enabled", False):
        tasks.append(asyncio.create_task(_file_watcher(cfg)))
        tasks.append(asyncio.create_task(_redis_watcher(cfg)))
    app["_prefilter_tasks"] = tasks

    logger.info(
        "prefilter: started (domains_enabled=%s domain_count=%d patterns_enabled=%s pattern_count=%d)",
        dcfg.get("enabled", False),
        len(_domain_set),
        pcfg.get("enabled", False),
        len(_patterns),
    )


async def stop(app: "web.Application") -> None:
    """Cancel and await all prefilter background tasks.

    Called from ``server._on_shutdown()``.
    """
    tasks: list[asyncio.Task] = app.get("_prefilter_tasks", [])
    for task in tasks:
        task.cancel()
    if tasks:
        await asyncio.gather(*tasks, return_exceptions=True)
    logger.debug("prefilter: stopped")
