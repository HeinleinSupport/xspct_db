# SPDX-License-Identifier: EUPL-1.2
# SPDX-FileCopyrightText: 2026 Carsten Rosenberg <c.rosenberg@heinlein-support.de>

"""aiohttp route handlers – OpenAPI-documented via aiohttp-pydantic."""

from __future__ import annotations

import asyncio
import json
import logging
import re
from typing import Any
from urllib.parse import unquote

from aiohttp import web
from aiohttp_pydantic import PydanticView
from aiohttp_pydantic.oas.typing import r200, r401, r500, r504
from pydantic import ValidationError

from xspct_db import cache, prefilter, stats
from xspct_db.auth import verify_api_key
from xspct_db.schemas import (
    ErrorResponse,
    QueryJsonRequest,
    QueryResponse,
    RspamdSettingsRequest,
    RspamdSettingsResponse,
)
from xspct_db.utils import add_rspamd_id, generate_session_id, timer

logger = logging.getLogger(__name__)


def _build_settings_data(
    userdata: dict[str, Any],
    cfg: dict[str, Any],
) -> dict[str, Any]:
    """Build the structured ``settings_data`` block.

    Returns ``{"users": {...}, "aliases": {...}}`` where ``aliases`` is a
    reverse map of alias-value → primary-key, built from the fields named in
    ``xspct_db_rspamd_alias_fields``.  Returns ``{}`` when no users were found.
    """
    users = userdata.get("users", {})
    if not users:
        return {}
    alias_fields: list[str] = cfg.get("xspct_db_rspamd_alias_fields", ["aliases"])
    aliases: dict[str, str] = {}
    for pkey, udata in users.items():
        for field in alias_fields:
            for val in udata.get(field, []):
                if val != pkey:
                    aliases[val] = pkey
    return {"users": users, "aliases": aliases}


_MSGPACK_MIMES = frozenset({"application/msgpack", "application/x-msgpack"})
_PARSE_FAILED = object()

try:
    import msgpack as _msgpack  # type: ignore[import-untyped]
except ImportError:
    _msgpack = None  # type: ignore[assignment]


def _parse_body(raw: bytes, content_type: str = "") -> Any:
    """Return a parsed object from *raw* bytes.

    When *content_type* indicates msgpack (``application/msgpack`` or
    ``application/x-msgpack``) the body is decoded with :mod:`msgpack`.
    Otherwise the body must be valid JSON.
    """
    if not raw:
        return None
    # Normalise: strip parameters such as "; charset=utf-8"
    ct = content_type.split(";", 1)[0].strip().lower()
    if ct in _MSGPACK_MIMES:
        try:
            if _msgpack is None:
                return _PARSE_FAILED
            return _msgpack.unpackb(raw, raw=False)
        except Exception:
            return _PARSE_FAILED
    try:
        return json.loads(raw)
    except Exception:
        return _PARSE_FAILED


def _invalid_body_response() -> web.Response:
    """Return a stable 400 response for malformed request payloads."""
    return web.Response(status=400, text="400 Bad Request")


def _detect_response_format(request: web.Request) -> str:
    """Return ``"msgpack"`` or ``"json"`` for the response body encoding.

    The ``Accept`` header takes precedence; if absent or not ``application/msgpack``
    / ``application/x-msgpack``, the request ``Content-Type`` is mirrored.
    """
    accept = request.headers.get("Accept", "")
    for part in accept.split(","):
        mime = part.split(";", 1)[0].strip().lower()
        if mime in _MSGPACK_MIMES:
            if _msgpack is not None:
                return "msgpack"
            # msgpack not installed – continue to next Accept entry
            continue
        if mime == "application/json":
            return "json"
    ct = request.headers.get("Content-Type", "").split(";", 1)[0].strip().lower()
    if ct in _MSGPACK_MIMES and _msgpack is not None:
        return "msgpack"
    return "json"


def _serialize_body(data: Any, fmt: str) -> tuple[bytes, str]:
    """Serialise *data* to bytes and return ``(body, content_type)``.

    *fmt* must be ``"json"`` or ``"msgpack"``.
    Raises :class:`aiohttp.web.HTTPNotAcceptable` (406) when *fmt* is
    ``"msgpack"`` but :mod:`msgpack` is not installed.
    """
    if fmt == "msgpack":
        try:
            if _msgpack is None:
                raise web.HTTPNotAcceptable(text="msgpack library not installed")
            return _msgpack.packb(data, use_bin_type=True), "application/msgpack"
        except web.HTTPNotAcceptable:
            raise
    return json.dumps(data).encode(), "application/json"


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _log_response(s: str, response: web.Response) -> web.Response:
    """Log response status, headers, and body at DEBUG level, then return the response unchanged."""
    if logger.isEnabledFor(logging.DEBUG):
        ct = response.content_type or ""
        body: Any = None
        try:
            if ct in _MSGPACK_MIMES and _msgpack is not None and response.body:
                body = _msgpack.unpackb(response.body, raw=False)
            else:
                body = response.text
        except Exception:
            body = f"<{len(response.body or b'')} bytes>"
        headers = dict(response.headers)
        logger.debug("%s ← %d  headers=%s  body=%s", s, response.status, headers, body)
    return response


def _log_request(s: str, request: web.Request, body: Any = None) -> None:
    """Emit a DEBUG line with method, URL, all headers (API key masked), and optional body."""
    if not logger.isEnabledFor(logging.DEBUG):
        return
    api_header = "X-Api-Key"
    headers: dict[str, str] = {}
    for name, value in request.headers.items():
        if name.lower() == api_header.lower() and len(value) > 4:
            headers[name] = value[:4] + "…"
        else:
            headers[name] = value
    params = dict(request.rel_url.query)
    try:
        matched_path = request.match_info.route.resource.canonical
    except Exception:
        matched_path = "-"
    logger.debug(
        "%s %s %s (matched: %s)  params=%s  headers=%s  body=%s",
        s,
        request.method,
        request.path,
        matched_path,
        params or "-",
        headers,
        body if body is not None else "-",
    )


# ---------------------------------------------------------------------------
# Concurrency – foreground / background query queues
#
# Every query endpoint runs through _run_with_queues() when
# xspct_db_request_timeout > 0.  Two asyncio.Semaphore instances are stored
# on the aiohttp Application object at startup:
#
#   app["fg_sem"]   — limits concurrent *foreground* (client-blocking) queries
#                     (capacity = xspct_db_foreground_slots, default 30)
#   app["bg_sem"]   — limits concurrent *background* tasks that continue after
#                     a timeout has been returned to the client
#                     (capacity = xspct_db_background_slots, default 5)
#   app["bg_tasks"] — set[asyncio.Task] tracking live background tasks for
#                     clean shutdown
#
# Request lifecycle with queues enabled:
#   1. Acquire fg_sem (blocks up to *timeout*).  If full → 503.
#   2. Create task for the backend coroutine.
#   3. asyncio.wait_for(asyncio.shield(task), timeout).
#   4a. Success → release fg_sem, return (result, False).
#   4b. Timeout → try to acquire bg_sem (non-blocking):
#       • slot free  → release fg_sem, hand task to _finalize_background(),
#                      return (None, True)  →  caller returns 504.
#       • no slot    → cancel task, release fg_sem, stats.background_rejected++,
#                      return (None, True)  →  caller returns 504.
# ---------------------------------------------------------------------------


class _ServiceOverloaded(Exception):
    """Raised when no foreground semaphore slot is available within the deadline."""


async def _finalize_background(
    s: str,
    task: asyncio.Task,
    bg_sem: asyncio.Semaphore,
    bg_tasks: set[asyncio.Task],
) -> None:
    """Await a timed-out query task in background; always release *bg_sem*."""
    try:
        await task
        stats.stats["background_completed"] += 1
    except asyncio.CancelledError:
        logger.debug("%s background task cancelled", s)
    except Exception as exc:
        stats.stats["background_errors"] += 1
        logger.exception("%s background task raised: %s", s, exc)
    finally:
        bg_sem.release()
        bg_tasks.discard(asyncio.current_task())


async def _run_with_queues(
    app: web.Application,
    s: str,
    coro: Any,
    timeout: float,
) -> tuple[Any, bool]:
    """Execute *coro* with foreground/background semaphore management.

    Returns ``(result, False)`` when *coro* finishes within *timeout*, or
    ``(None, True)`` when the foreground deadline expires.  In the latter case
    the task is promoted to a background slot (or cancelled if no slot is free).

    When *timeout* is ``<= 0`` the coroutine runs directly without semaphores.

    Raises :exc:`_ServiceOverloaded` if no foreground slot can be acquired
    within the deadline (caller should return 503).
    """
    if timeout <= 0:
        return await coro, False

    fg_sem: asyncio.Semaphore = app["fg_sem"]
    bg_sem: asyncio.Semaphore = app["bg_sem"]
    bg_tasks: set[asyncio.Task] = app["bg_tasks"]

    # Acquire a foreground slot (blocking up to *timeout*).
    try:
        await asyncio.wait_for(fg_sem.acquire(), timeout=timeout)
    except asyncio.TimeoutError:
        stats.stats["foreground_overloaded"] += 1
        coro.close()
        raise _ServiceOverloaded

    task = asyncio.create_task(coro)
    try:
        result = await asyncio.wait_for(asyncio.shield(task), timeout=timeout)
        return result, False
    except asyncio.TimeoutError:
        stats.stats["requests_timeout"] += 1
        # Try to promote to background (non-blocking).
        try:
            await asyncio.wait_for(bg_sem.acquire(), timeout=0)
        except asyncio.TimeoutError:
            task.cancel()
            stats.stats["background_rejected"] += 1
            return None, True
        # Hand task to background finalizer.
        bg_task = asyncio.create_task(_finalize_background(s, task, bg_sem, bg_tasks))
        bg_tasks.add(bg_task)
        return None, True
    finally:
        fg_sem.release()


# ---------------------------------------------------------------------------
# Rspamd settings computation
# ---------------------------------------------------------------------------

# Default rules that translate user-data attributes into Rspamd settings.
# Each rule has:
#   name        – human-readable identifier (used in log messages)
#   condition   – which user attribute to inspect and how:
#                   attribute  str   user-data key
#                   operator   str   truthy | falsy | eq | ne | present | absent
#                   value      Any   comparison value for eq / ne
#                   default    Any   assumed value when the attribute is absent
#   aggregation str  "all"  → rule fires only when ALL rcpts match the condition
#                    "any"  → rule fires when ANY rcpt matches
#   apply       dict  keys that are merged into the final settings response:
#                   actions         dict[str, float|str]
#                   symbols_disabled list[str]
#                   symbols_enabled  list[str]
#                   groups_disabled  list[str]
#                   groups_enabled   list[str]
#                   symbols         dict[str, float]   (name→score, future)
#                   subject         str                (future)
#
# Override with xspct_db_rspamd_rules in config to replace these entirely.
_DEFAULT_RSPAMD_RULES: list[dict[str, Any]] = [
    {
        "name": "disable_greylisting",
        "condition": {"attribute": "greylisting", "operator": "falsy", "default": True},
        "aggregation": "all",
        "apply": {
            "symbols_disabled": ["GREYLIST_CHECK", "GREYLIST_SAVE", "GREYLIST"],
            "symbols": {"SETTINGS_GREYLIST_DISABLED": 0.0},
            "actions": {"greylist": "null"},
        },
    },
]


def _eval_attr_condition(value: Any, operator: str, cmp_value: Any = None) -> bool:
    """Evaluate *operator* against a single attribute *value*.

    Handles LDAP-style strings (``"TRUE"`` / ``"FALSE"``), Python bools,
    single-element lists (as returned by LDAP / YAML backends), and
    ``None`` (absent).  Callers should substitute the rule ``default`` before
    calling when the attribute is missing from the user dict.
    """
    # Unwrap single-element lists produced by the LDAP/YAML backends.
    scalar: Any = value
    if isinstance(value, list):
        if len(value) == 0:
            scalar = None  # empty list → treat as absent
        else:
            scalar = value[0]

    # Normalise LDAP strings and ints to Python bool for truthy/falsy checks.
    normalised: Any = scalar
    if isinstance(scalar, str):
        upper = scalar.upper()
        if upper == "TRUE":
            normalised = True
        elif upper == "FALSE":
            normalised = False

    if operator == "truthy":
        return bool(normalised)
    if operator == "falsy":
        return not bool(normalised)
    if operator == "present":
        return scalar is not None
    if operator == "absent":
        return scalar is None
    if operator == "eq":
        return normalised == cmp_value
    if operator == "ne":
        return normalised != cmp_value
    return False


def _compute_rcpt_settings(
    userdata: dict[str, Any],
    rcpts: list[str],
    cfg: dict[str, Any],
) -> dict[str, Any]:
    """Compute Rspamd actions / symbol lists from recipient user objects.

    Returns a dict with the keys that should be merged into
    :class:`~xspct_db.schemas.RspamdSettingsResponse`:
    ``actions``, ``symbols_disabled``, ``symbols_enabled``,
    ``groups_disabled``, ``groups_enabled``, ``symbols``, ``subject``.

    ``symbols_enabled`` and ``groups_enabled`` are ``None`` when empty so
    callers can use ``exclude_none=True`` cleanly.
    """
    users: dict[str, Any] = userdata.get("users", {})
    rcpt_set = set(rcpts)
    rcpt_users: list[dict[str, Any]] = [v for k, v in users.items() if k in rcpt_set and isinstance(v, dict)]

    # Accumulated result
    actions: dict[str, Any] = {}
    symbols_disabled: list[str] = []
    symbols_enabled: list[str] = []
    groups_disabled: list[str] = []
    groups_enabled: list[str] = []
    symbols_scored: dict[str, float] = {}
    subject: str | None = None
    fired_rules: list[str] = []

    # --- Rules engine ---
    rules: list[dict[str, Any]] = cfg.get("xspct_db_rspamd_rules") or _DEFAULT_RSPAMD_RULES
    for rule in rules:
        cond = rule.get("condition", {})
        attr = cond.get("attribute", "")
        operator = cond.get("operator", "truthy")
        cmp_value = cond.get("value")
        default = cond.get("default")
        aggregation = rule.get("aggregation", "all")
        apply = rule.get("apply", {})

        if not rcpt_users:
            # No known recipients — skip rules; fail-safe handled below.
            continue

        if len(rcpt_users) == 1:
            # Fast path: skip list allocation and all()/any() for the common single-rcpt case.
            raw = rcpt_users[0].get(attr)
            fires = _eval_attr_condition(raw if raw is not None else default, operator, cmp_value)
        else:
            per_rcpt_results = [
                _eval_attr_condition((udata.get(attr) if udata.get(attr) is not None else default), operator, cmp_value)
                for udata in rcpt_users
            ]
            fires = all(per_rcpt_results) if aggregation == "all" else any(per_rcpt_results)
        if not fires:
            continue

        fired_rules.append(rule.get("name", ""))

        # Merge apply block into accumulator.
        for sym in apply.get("symbols_disabled", []):
            if sym not in symbols_disabled:
                symbols_disabled.append(sym)
        for sym in apply.get("symbols_enabled", []):
            if sym not in symbols_enabled:
                symbols_enabled.append(sym)
        for grp in apply.get("groups_disabled", []):
            if grp not in groups_disabled:
                groups_disabled.append(grp)
        for grp in apply.get("groups_enabled", []):
            if grp not in groups_enabled:
                groups_enabled.append(grp)
        for k, v in apply.get("actions", {}).items():
            actions[k] = v
        for sym, score in apply.get("symbols", {}).items():
            symbols_scored[sym] = score
        if apply.get("subject") and subject is None:
            subject = apply["subject"]

    # --- Reject level ---
    # actions["reject"] is set only when ALL rcpts have a reject_level present in the
    # translation map AND the computed minimum differs from the configured default.
    # If any rcpt has no mapped level (missing or unmapped), the action is omitted so
    # Rspamd keeps its own default threshold.
    reject_level_map: dict[str, int] = cfg.get("xspct_db_reject_level_map", {"5": 13, "6": 15, "6.31": 17})
    reject_level_default: int = int(cfg.get("xspct_db_reject_level_default", 15))

    translated_values: list[int] = []
    all_mapped = bool(rcpt_users)  # False when there are no rcpts
    for udata in rcpt_users:
        raw_level = udata.get("reject_level")
        if isinstance(raw_level, list):
            raw_level = raw_level[0] if raw_level else None
        if raw_level is not None and str(raw_level) != "":
            translated = reject_level_map.get(str(raw_level))
            if translated is not None:
                translated_values.append(translated)
                continue
        # Rcpt has no level or unmapped value — abort: leave reject unset.
        all_mapped = False
        break

    if all_mapped and translated_values:
        computed = min(translated_values)
        if computed != reject_level_default:
            actions["reject"] = computed
            fired_rules.append(f"reject({computed})")

    return {
        "actions": actions,
        "symbols_disabled": symbols_disabled,
        "symbols_enabled": symbols_enabled or None,
        "groups_disabled": groups_disabled,
        "groups_enabled": groups_enabled or None,
        "symbols": symbols_scored if symbols_scored else [],
        "subject": subject,
        "fired_rules": fired_rules,
    }


def _rspamd_cache_key(rspamd_req: Any, cfg: dict[str, Any]) -> tuple:
    """Build a cache key tuple for a Rspamd-settings request.

    Fields included in the key are configured via
    ``xspct_db_response_cache.rspamd_key_fields``.
    """
    field_map = {
        "from": rspamd_req.from_addr,
        "rcpts": frozenset(rspamd_req.rcpts),
        "mta-name": rspamd_req.mta_name,
        "settings-name": rspamd_req.settings_name,
        "settings-id": rspamd_req.settings_id,
    }
    key_fields: list[str] = cfg["xspct_db_response_cache"].get(
        "rspamd_key_fields", ["from", "rcpts", "mta-name", "settings-name", "settings-id"]
    )
    values = tuple(field_map.get(f) for f in key_fields)
    return ("rspamd-settings",) + values


# ---------------------------------------------------------------------------
# Health endpoints
# ---------------------------------------------------------------------------


class HealthView(PydanticView):
    async def get(self) -> r200[dict]:
        """Service liveness check."""
        return web.Response(text="Hello, world", headers={"Connection": "Keep-Alive"})


class PingView(PydanticView):
    async def get(self) -> r200[dict]:
        """Ping → Pong."""
        return web.Response(text="Pong")


# ---------------------------------------------------------------------------
# Query endpoints
# ---------------------------------------------------------------------------


# Default wildcard key pattern: strips one subdomain level.
# ``user@sub.example.com`` → ``@example.com``
_DEFAULT_WILDCARD_PATTERN = r".*@[^.]+\.(.+)"
_DEFAULT_WILDCARD_REPLACEMENT = r"@\1"


def _compute_wildcard_key(
    address: str,
    wildcard_queries: dict[str, dict[str, Any]],
) -> str | None:
    """Return the wildcard lookup key for *address*.

    Two modes depending on what is configured on the first wildcard-enabled
    query that defines ``wildcard_key_pattern``:

    **Match mode** (only ``wildcard_key_pattern`` set):
        The regex is applied with ``re.search``.  The first capture group is
        returned when one is present; the full match is returned otherwise.
        Returns ``None`` when the pattern does not match.

    **Substitution mode** (both ``wildcard_key_pattern`` and
    ``wildcard_key_replacement`` set):
        ``re.sub(pattern, replacement, address)`` is used to transform the
        entire address into the wildcard key.  Returns ``None`` when the
        result equals the original address (i.e. no substitution took place).

    When neither option is configured in the query, the built-in defaults
    :data:`_DEFAULT_WILDCARD_PATTERN` / :data:`_DEFAULT_WILDCARD_REPLACEMENT`
    are used, which strip one subdomain level::

        user@sub.example.com  →  @example.com

    Returns ``None`` when no wildcard key can be derived for *address*.
    """
    # Find the first pattern defined across all wildcard-enabled queries.
    pattern: str | None = None
    replacement: str | None = None
    for qv in wildcard_queries.values():
        p = qv.get("wildcard_key_pattern")
        if p:
            pattern = p
            replacement = qv.get("wildcard_key_replacement")
            break

    # Fall back to built-in defaults.
    if pattern is None:
        pattern = _DEFAULT_WILDCARD_PATTERN
        replacement = _DEFAULT_WILDCARD_REPLACEMENT

    try:
        if replacement is not None:
            result = re.sub(pattern, replacement, address)
            return result if result != address else None
        m = re.search(pattern, address)
    except re.error:
        logger.warning("wildcard_key_pattern %r is not a valid regex", pattern)
        return None

    if replacement is not None:
        # Already handled above; unreachable but keeps type checker happy.
        return None  # pragma: no cover
    if m is None:
        return None
    return m.group(1) if m.lastindex else m.group(0)


class QueryView(PydanticView):
    async def get(self, user: str, /) -> r200[QueryResponse] | r401[ErrorResponse] | r500[ErrorResponse] | r504[ErrorResponse]:
        """
        Look up a single user across all configured backends.

        The ``user`` path segment is URL-decoded before lookup.
        The L1 in-process cache is consulted first, then Redis (L2) when enabled,
        then the backend.

        **Example curl**::

            curl -s -H "X-Api-Key: your-key" \
                 http://localhost:11350/v1/query/alice@mailexample.de | python3 -m json.tool

        **Example request**::

            GET /v1/query/alice@mailexample.de
            X-Api-Key: your-key

        **Example response (user found)**::

            {
                "users": {
                    "alice@mailexample.de": {
                        "mail": "alice@mailexample.de",
                        "uid": "alice",
                        "aliases": ["a.smith@mailexample.de"]
                    }
                }
            }

        **Example response (user not found)**::

            {"users": {}}

        Returns ``504`` if the backend query exceeds the configured timeout.
        """
        timer("start")
        stats.stats["requests_total"] += 1

        cfg: dict[str, Any] = self.request.app["config"]
        s_id = generate_session_id()
        s = add_rspamd_id(s_id, self.request.headers.get(cfg["xspct_db_rspamd_header"]))

        _log_request(s, self.request)

        if not verify_api_key(s, self.request.headers.get(cfg["xspct_db_api_header"]), cfg):
            return _log_response(s, web.Response(status=401, text="401 Unauthorized"))

        fmt = _detect_response_format(self.request)
        user = unquote(user)

        if not prefilter.filter_user(s, user, self.request.app):
            stats.stats["requests_unknown"] += 1
            _body, _ctype = _serialize_body({"users": {}}, fmt)
            return _log_response(
                s,
                web.Response(
                    body=_body,
                    content_type=_ctype,
                    headers={"Connection": "Keep-Alive"},
                ),
            )

        userdata: dict[str, Any] = {"users": {}}
        user_to_pkey: dict[str, Any] = {}
        use_redis = cfg["xspct_db_redis_cache"]["enabled"] and cache.connection is not None
        use_local = cfg["xspct_db_local_cache"].get("enabled", True)

        # --- L1 / L2 cache lookup ---
        cache_object = None
        if use_local or use_redis:
            cache_object, cache_source = await cache.get_object_with_source(s, user, cfg)
            if isinstance(cache_object, dict):
                if cache_source == "local":
                    stats.stats["local_cache_hits"] += 1
                else:
                    stats.stats["redis_hits"] += 1
            elif isinstance(cache_object, bool) and not cache_object:
                if cache_source == "local":
                    stats.stats["local_cache_hits"] += 1
                else:
                    stats.stats["redis_negative_hits"] += 1
            else:
                if use_redis:
                    stats.stats["redis_misses"] += 1
                stats.stats["local_cache_misses"] += 1

        if isinstance(cache_object, dict):
            stats.stats["requests_known"] += 1
            userdata["users"][user] = cache_object
            _body, _ctype = _serialize_body(userdata, fmt)
            return _log_response(
                s,
                web.Response(
                    body=_body,
                    content_type=_ctype,
                    headers={"Connection": "Keep-Alive"},
                ),
            )

        # Determine whether any query is configured with wildcard_domain_query.
        wildcard_queries = {qk: qv for qk, qv in cfg.get("xspct_db_queries", {}).items() if qv.get("wildcard_domain_query")}
        domain_key = _compute_wildcard_key(user, wildcard_queries) if wildcard_queries else None
        wildcard_enabled = domain_key is not None

        # Negative cache hit: return empty unless wildcard fallback is possible.
        if isinstance(cache_object, bool) and not cache_object:
            if not wildcard_enabled:
                stats.stats["requests_unknown"] += 1
                _body, _ctype = _serialize_body(userdata, fmt)
                return _log_response(
                    s,
                    web.Response(
                        body=_body,
                        content_type=_ctype,
                        headers={"Connection": "Keep-Alive"},
                    ),
                )
            # Negative cache for the full address is already set; skip the user
            # backend and go straight to the domain wildcard lookup.
            skip_user_backend = True
        else:
            skip_user_backend = False

        # --- Backend query ---
        user_parts = user.split("@", 1)
        domain = user_parts[-1]
        # domain_key is already computed above via _compute_wildcard_key; assert non-None
        # here is safe because wildcard_enabled guards all code paths that use domain_key.

        # Fast-path: check L1/L2 cache for the domain wildcard key before acquiring
        # a semaphore slot.
        if wildcard_enabled and (use_local or use_redis):
            dom_cache_object, _ = await cache.get_object_with_source(s, domain_key, cfg)
            if isinstance(dom_cache_object, dict):
                stats.stats["wildcard_domain_hits"] += 1
                stats.stats["requests_known"] += 1
                userdata["users"][user] = dom_cache_object
                _body, _ctype = _serialize_body(userdata, fmt)
                return _log_response(
                    s,
                    web.Response(
                        body=_body,
                        content_type=_ctype,
                        headers={"Connection": "Keep-Alive"},
                    ),
                )
            if isinstance(dom_cache_object, bool) and not dom_cache_object:
                stats.stats["wildcard_domain_misses"] += 1
                stats.stats["requests_unknown"] += 1
                _body, _ctype = _serialize_body(userdata, fmt)
                return _log_response(
                    s,
                    web.Response(
                        body=_body,
                        content_type=_ctype,
                        headers={"Connection": "Keep-Alive"},
                    ),
                )

        users = [
            {
                "username": user,
                "address": user,
                "userpart": user_parts[0],
                "domain": domain,
            }
        ]
        domain_users = [
            {
                "username": domain_key,
                "address": domain_key,
                "userpart": "",
                "domain": domain,
            }
        ]

        request_timeout = float(cfg.get("xspct_db_request_timeout", 0))
        timeout_header = cfg.get("xspct_db_request_timeout_header", "")
        if timeout_header:
            header_val = self.request.headers.get(timeout_header)
            if header_val is not None:
                try:
                    header_timeout = float(header_val)
                    if header_timeout > 0:
                        max_timeout = float(cfg.get("xspct_db_request_timeout_header_max", 120))
                        request_timeout = min(header_timeout, max_timeout)
                    else:
                        logger.warning("%s - ignoring invalid timeout header value: %s", s, header_val)
                except (ValueError, TypeError):
                    pass

        from xspct_db.backends import run_queries

        async def _query_task() -> tuple[dict[str, Any], dict[str, Any], str | bool, bool]:
            """Run the user query, then a domain wildcard query when needed.

            Returns ``(userdata, user_to_pkey, query_error, used_wildcard)`` where
            *used_wildcard* indicates whether the result came from the domain
            fallback rather than a direct user match.
            """
            _ud: dict[str, Any] = {"users": {}}
            _u2p: dict[str, Any] = {}
            _err: str | bool = False

            if not skip_user_backend:
                _ud, _u2p, _err = await run_queries(s, user, use_redis, users, _ud, _u2p, cfg)
                if isinstance(_err, str):
                    return _ud, _u2p, _err, False
                if user in _u2p:
                    return _ud, _u2p, _err, False

            if not wildcard_enabled:
                return _ud, _u2p, _err, False

            # Domain wildcard pass — only queries with wildcard_domain_query: true.
            _dom_ud: dict[str, Any] = {"users": {}}
            _dom_u2p: dict[str, Any] = {}
            _dom_ud, _dom_u2p, _dom_err = await run_queries(
                s, domain_key, use_redis, domain_users, _dom_ud, _dom_u2p, cfg, wildcard=True
            )
            if isinstance(_dom_err, str):
                return _ud, _u2p, _dom_err, False
            if domain_key in _dom_u2p:
                _ud["users"][user] = _dom_ud["users"][domain_key]
                _u2p[user] = user
                return _ud, _u2p, False, True

            return _ud, _u2p, _err, False

        try:
            result, timed_out = await _run_with_queues(
                self.request.app,
                s,
                _query_task(),
                request_timeout,
            )
        except _ServiceOverloaded:
            return _log_response(s, web.Response(status=503, text="503 Service Overloaded"))

        if timed_out:
            return _log_response(s, web.Response(status=504, text="504 Request Timeout"))

        userdata, user_to_pkey, query_error, used_wildcard = result

        if isinstance(query_error, str):
            return _log_response(s, web.Response(status=500, text=query_error))

        if user in user_to_pkey:
            stats.stats["requests_known"] += 1
            if used_wildcard:
                stats.stats["wildcard_domain_hits"] += 1
        else:
            stats.stats["requests_unknown"] += 1
            if wildcard_enabled:
                stats.stats["wildcard_domain_misses"] += 1

        _body, _ctype = _serialize_body(userdata, fmt)
        return _log_response(
            s,
            web.Response(
                body=_body,
                content_type=_ctype,
                headers={"Connection": "Keep-Alive"},
            ),
        )


class QueryJsonView(PydanticView):
    async def post(self) -> r200[QueryResponse] | r401[ErrorResponse] | r500[ErrorResponse]:
        """
        Batch user lookup.

        Accepts a list of users and queries all configured backends for each.
        The response cache (``xspct_db_response_cache``) is consulted first when enabled;
        on a miss the result is stored for subsequent identical requests.
        Redis (L2) is **not** consulted or populated on batch requests.

        **Example curl**::

            curl -s -X POST http://localhost:11350/v1/query-json \
                 -H "X-Api-Key: your-key" \
                 -H "Content-Type: application/json" \
                 -d '{"users": ["alice@mailexample.de", "bob@mailexample.de"]}' | python3 -m json.tool

        **Example request**::

            POST /v1/query-json
            Content-Type: application/json
            X-Api-Key: your-key

            {
                "users": [
                    "alice@mailexample.de",
                    "bob@mailexample.de"
                ]
            }

        **Example response**::

            {
                "users": {
                    "alice@mailexample.de": {
                        "mail": "alice@mailexample.de",
                        "uid": "alice",
                        "aliases": ["a.smith@mailexample.de"]
                    },
                    "bob@mailexample.de": {}
                }
            }

        Users not found in any backend are returned with an empty dict.
        """
        timer("start")
        cfg: dict[str, Any] = self.request.app["config"]
        s_id = generate_session_id()
        s = add_rspamd_id(s_id, self.request.headers.get(cfg["xspct_db_rspamd_header"]))

        raw_body = await self.request.read()
        parsed_body: Any = _parse_body(raw_body, self.request.headers.get("Content-Type", ""))
        if raw_body and parsed_body is _PARSE_FAILED:
            return _log_response(s, _invalid_body_response())

        try:
            query_req = QueryJsonRequest.model_validate(parsed_body if isinstance(parsed_body, dict) else {})
        except ValidationError:
            return _log_response(s, _invalid_body_response())

        _log_request(s, self.request, body=parsed_body)

        if not verify_api_key(s, self.request.headers.get(cfg["xspct_db_api_header"]), cfg):
            return _log_response(s, web.Response(status=401, text="401 Unauthorized"))

        max_users = int(cfg.get("xspct_db_query_json_max_users", 500))
        if len(query_req.users) > max_users:
            logger.warning("%s - query-json user count %d exceeds limit %d", s, len(query_req.users), max_users)
            return _log_response(s, web.Response(status=400, text="400 Bad Request: too many users"))

        # Apply prefilter
        filtered_users = prefilter.filter_addresses(s, list(query_req.users), self.request.app)
        if not filtered_users:
            logger.debug("%s prefilter: all users filtered out, returning empty result", s)
            _body, _ctype = _serialize_body({"users": {}}, _detect_response_format(self.request))
            return _log_response(
                s,
                web.Response(
                    body=_body,
                    content_type=_ctype,
                    headers={"Connection": "Keep-Alive"},
                ),
            )

        fmt = _detect_response_format(self.request)

        # --- Response cache lookup ---
        response_cache_key = ("query-json", tuple(sorted(filtered_users)), fmt)
        cached = cache.get_response(response_cache_key, cfg, s)
        if cached is not None:
            stats.stats["response_cache_hits"] += 1
            cached_body, cached_ctype = cached
            return _log_response(
                s,
                web.Response(
                    body=cached_body,
                    content_type=cached_ctype,
                    headers={"Connection": "Keep-Alive"},
                ),
            )
        stats.stats["response_cache_misses"] += 1

        users = [
            {
                "username": u,
                "address": u,
                "userpart": u.split("@", 1)[0],
                "domain": u.split("@", 1)[-1],
            }
            for u in filtered_users
        ]

        async def _qj_task() -> tuple[bytes, str, str | bool]:
            """Run backend queries and cache the response body."""
            from xspct_db.backends import run_queries

            userdata: dict[str, Any] = {"users": {}}
            user_to_pkey: dict[str, Any] = {}
            userdata, user_to_pkey, query_error = await run_queries(s, "", False, users, userdata, user_to_pkey, cfg)
            if isinstance(query_error, str):
                return b"", "application/json", query_error

            # Wildcard domain fallback for users not found by any direct query.
            wildcard_queries = {qk: qv for qk, qv in cfg.get("xspct_db_queries", {}).items() if qv.get("wildcard_domain_query")}
            if wildcard_queries:
                missing = [u for u in filtered_users if u not in user_to_pkey]
                if missing:
                    # Map each missing address to its wildcard key; skip those that
                    # produce no key (e.g. pattern does not match).
                    addr_to_wk = {u: _compute_wildcard_key(u, wildcard_queries) for u in missing}
                    addr_to_wk = {u: wk for u, wk in addr_to_wk.items() if wk is not None}
                    unique_domain_keys = list(dict.fromkeys(addr_to_wk.values()))
                    dom_users = [
                        {
                            "username": dk,
                            "address": dk,
                            "userpart": "",
                            "domain": dk[1:] if dk.startswith("@") else dk,
                        }
                        for dk in unique_domain_keys
                    ]
                    dom_ud, dom_u2p, dom_err = await run_queries(s, "", False, dom_users, {"users": {}}, {}, cfg, wildcard=True)
                    if not isinstance(dom_err, str):
                        for u, dk in addr_to_wk.items():
                            if dk in dom_u2p:
                                userdata["users"][u] = dom_ud["users"][dk]
                                user_to_pkey[u] = u

            body, ctype = _serialize_body(userdata, fmt)
            cache.set_response(response_cache_key, (body, ctype), cfg)
            return body, ctype, False

        request_timeout = float(cfg.get("xspct_db_request_timeout", 0))
        try:
            result, timed_out = await _run_with_queues(
                self.request.app,
                s,
                _qj_task(),
                request_timeout,
            )
        except _ServiceOverloaded:
            return _log_response(s, web.Response(status=503, text="503 Service Overloaded"))

        if timed_out:
            return _log_response(s, web.Response(status=504, text="504 Request Timeout"))

        response_body, response_ctype, query_error = result

        if isinstance(query_error, str):
            return _log_response(s, web.Response(status=500, text=query_error))

        return _log_response(
            s,
            web.Response(
                body=response_body,
                content_type=response_ctype,
                headers={"Connection": "Keep-Alive"},
            ),
        )


class RspamdSettingsView(PydanticView):
    async def post(self) -> r200[RspamdSettingsResponse] | r401[ErrorResponse]:
        """
        Rspamd settings endpoint.

        Returns an Rspamd settings blob for use with the Rspamd HTTP settings module.
        The response ``Content-Type`` is ``application/json``.

        **Example curl**::

            curl -s -X POST http://localhost:11350/v1/rspamd-settings \
                 -H "X-Api-Key: your-key" \
                 -H "Content-Type: application/json" \
                 -d '{"from": "alice@mailexample.de", "rcpts": ["bob@mailexample.de"]}' | python3 -m json.tool

        **Example request**::

            POST /v1/rspamd-settings
            X-Api-Key: your-key

        **Example response**::

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
                        "alice@mailexample.de": {
                            "mail": "alice@mailexample.de",
                            "uid": "alice",
                            "aliases": ["a.smith@mailexample.de"]
                        }
                    },
                    "aliases": {
                        "a.smith@mailexample.de": "alice@mailexample.de"
                    }
                },
                "settings_error": []
            }

        """
        timer("start")
        cfg: dict[str, Any] = self.request.app["config"]
        s_id = generate_session_id()

        # Read and parse body first so uid is available for the session tag.
        raw_body = await self.request.read()
        parsed_body: Any = _parse_body(raw_body, self.request.headers.get("Content-Type", ""))
        if raw_body and parsed_body is _PARSE_FAILED:
            return _log_response(f"<{s_id}>", _invalid_body_response())

        # Construct the model explicitly from the parsed dict to avoid any
        # alias-resolution issues caused by aiohttp_pydantic's model introspection.
        try:
            if isinstance(parsed_body, dict):
                rspamd_req = RspamdSettingsRequest(
                    uid=parsed_body.get("uid", ""),
                    from_addr=parsed_body.get("from", ""),
                    rcpts=parsed_body.get("rcpts", []),
                    mta_name=parsed_body.get("mta-name"),
                    mta_host=parsed_body.get("mta-host"),
                    ip=parsed_body.get("ip"),
                    settings_name=parsed_body.get("settings-name"),
                    settings_id=parsed_body.get("settings-id"),
                )
            else:
                rspamd_req = RspamdSettingsRequest()
        except ValidationError:
            return _log_response(f"<{s_id}>", _invalid_body_response())

        # Prefer uid from body, fall back to X-Rspamd-ID header
        rspamd_id = rspamd_req.uid or self.request.headers.get(cfg["xspct_db_rspamd_header"])
        s = add_rspamd_id(s_id, rspamd_id)

        _log_request(s, self.request, body=parsed_body)

        if not verify_api_key(s, self.request.headers.get(cfg["xspct_db_api_header"]), cfg):
            return _log_response(s, web.Response(status=401, text="401 Unauthorized"))

        fmt = _detect_response_format(self.request)

        # --- Response cache lookup ---
        response_cache_key = _rspamd_cache_key(rspamd_req, cfg) + (fmt,)
        cached = cache.get_response(response_cache_key, cfg, s)
        if cached is not None:
            stats.stats["response_cache_hits"] += 1
            cached_body, cached_ctype = cached
            return _log_response(
                s,
                web.Response(
                    body=cached_body,
                    content_type=cached_ctype,
                    headers={"Connection": "Keep-Alive"},
                ),
            )
        stats.stats["response_cache_misses"] += 1

        # Look up all addresses from envelope sender + recipients
        addresses = list(dict.fromkeys(addr for addr in ([rspamd_req.from_addr] + rspamd_req.rcpts) if addr))
        addresses = prefilter.filter_addresses(s, addresses, self.request.app)

        async def _rs_task() -> tuple[bytes, str]:
            """Run backend queries and build the Rspamd settings response."""
            userdata: dict[str, Any] = {"users": {}}
            user_to_pkey: dict[str, Any] = {}
            if addresses:
                from xspct_db.backends import run_queries

                users = [
                    {
                        "username": addr,
                        "address": addr,
                        "userpart": addr.split("@", 1)[0],
                        "domain": addr.split("@", 1)[-1],
                    }
                    for addr in addresses
                ]
                userdata, user_to_pkey, _ = await run_queries(s, "", False, users, userdata, user_to_pkey, cfg)

                # Wildcard domain fallback for addresses not found by any direct query.
                wildcard_queries = {
                    qk: qv for qk, qv in cfg.get("xspct_db_queries", {}).items() if qv.get("wildcard_domain_query")
                }
                if wildcard_queries:
                    missing = [addr for addr in addresses if addr not in user_to_pkey]
                    if missing:
                        addr_to_wk = {addr: _compute_wildcard_key(addr, wildcard_queries) for addr in missing}
                        addr_to_wk = {addr: wk for addr, wk in addr_to_wk.items() if wk is not None}
                        unique_domain_keys = list(dict.fromkeys(addr_to_wk.values()))
                        dom_users = [
                            {
                                "username": dk,
                                "address": dk,
                                "userpart": "",
                                "domain": dk[1:] if dk.startswith("@") else dk,
                            }
                            for dk in unique_domain_keys
                        ]
                        from xspct_db.backends import run_queries as _run_queries_wc

                        dom_ud, dom_u2p, dom_err = await _run_queries_wc(
                            s, "", False, dom_users, {"users": {}}, {}, cfg, wildcard=True
                        )
                        if not isinstance(dom_err, str):
                            for addr, dk in addr_to_wk.items():
                                if dk in dom_u2p:
                                    userdata["users"][addr] = dom_ud["users"][dk]
                                    user_to_pkey[addr] = addr

            # Resolve rcpt query addresses to the primary keys used in userdata["users"].
            # This handles cases where the backend primary_key differs from the query address
            # (e.g. LDAP primary_key: uid, or a normalised mail attribute value).
            rcpt_primary_keys = [user_to_pkey.get(r, r) for r in rspamd_req.rcpts]
            computed = _compute_rcpt_settings(userdata, rcpt_primary_keys, cfg)
            fired_rules: list[str] = computed.pop("fired_rules", [])
            sd = _build_settings_data(userdata, cfg)
            if sd:
                sd["profile"] = {"applied_rules": fired_rules}
            elif fired_rules:
                sd = {"profile": {"applied_rules": fired_rules}}
            reply = RspamdSettingsResponse(
                **computed,
                settings_data=sd,
                settings_error=[],
            )
            reply_dict = reply.model_dump(exclude_none=True)
            body, ctype = _serialize_body(reply_dict, fmt)
            cache.set_response(response_cache_key, (body, ctype), cfg)
            return body, ctype

        request_timeout = float(cfg.get("xspct_db_request_timeout", 0))
        try:
            result, timed_out = await _run_with_queues(
                self.request.app,
                s,
                _rs_task(),
                request_timeout,
            )
        except _ServiceOverloaded:
            return _log_response(s, web.Response(status=503, text="503 Service Overloaded"))

        if timed_out:
            return _log_response(s, web.Response(status=504, text="504 Request Timeout"))

        result_body, result_ctype = result
        return _log_response(
            s,
            web.Response(
                body=result_body,
                content_type=result_ctype,
                headers={"Connection": "Keep-Alive"},
            ),
        )


# ---------------------------------------------------------------------------
# Route registration
# ---------------------------------------------------------------------------


def setup_routes(app: web.Application) -> None:
    """Register all route views on *app*."""
    _routes: list[tuple[str, type]] = [
        ("/", HealthView),
        ("/ping", PingView),
        ("/ping/", PingView),
        ("/v1/query/{user}", QueryView),
        ("/v1/query/{user}/", QueryView),
        ("/query/v1/{user}", QueryView),
        ("/query/v1/{user}/", QueryView),
        ("/v1/query-json", QueryJsonView),
        ("/v1/query-json/", QueryJsonView),
        ("/query-json/v1", QueryJsonView),
        ("/query-json/v1/", QueryJsonView),
        ("/v1/rspamd-settings", RspamdSettingsView),
        ("/v1/rspamd-settings/", RspamdSettingsView),
        ("/rspamd-settings/v1", RspamdSettingsView),
        ("/rspamd-settings/v1/", RspamdSettingsView),
    ]
    for path, view in _routes:
        logger.debug("registering route: %s → %s", path, view.__name__)
        app.router.add_view(path, view)
