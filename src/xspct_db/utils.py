# SPDX-License-Identifier: EUPL-1.2
# SPDX-FileCopyrightText: 2026 Carsten Rosenberg <c.rosenberg@heinlein-support.de>

"""Session helpers, timing utilities, and recursive dict merge."""

from __future__ import annotations

import contextvars
import secrets
import timeit
from typing import Any

# Per-async-task request start time (safe for concurrent requests).
_time_start_var: contextvars.ContextVar[float] = contextvars.ContextVar(
    "time_start", default=0.0
)


class _LazyTimer:
    """Deferred elapsed-time string for use as a ``%s`` log argument.

    ``__str__`` is only evaluated when the log record is actually emitted,
    avoiding needless computation when the log level would suppress it.
    """

    __slots__ = ()

    def __str__(self) -> str:
        return str(round(timeit.default_timer() - _time_start_var.get(), 5))

    __repr__ = __str__


_LAZY_TIMER = _LazyTimer()


def timer(action: str = "") -> Any:
    """Start or read an async-safe per-request timer.

    Pass ``"start"`` to begin timing; omit the argument (or pass anything
    else) to obtain a lazy object whose ``str()`` returns elapsed seconds.
    """
    if action == "start":
        _time_start_var.set(timeit.default_timer())
        return 0
    return _LAZY_TIMER


def generate_session_id() -> str:
    """Return a 6-character cryptographically secure hex session identifier."""
    return secrets.token_hex(3)


def add_rspamd_id(session_id: str, rspamd_id: str | None) -> str:
    """Combine *session_id* and optional *rspamd_id* into a log-tag string."""
    if session_id and rspamd_id:
        return f"<{session_id[:6]}-{rspamd_id[:6]}>"
    if session_id:
        return f"<{session_id[:6]}>"
    return f"<{generate_session_id()}>"


def dict_merge(d1: Any, d2: Any) -> Any:
    """Recursively merge two values.

    - Two dicts are merged key-by-key; shared keys are merged recursively.
    - Non-dict values are collected into a flat list.
    """
    if isinstance(d1, dict) and isinstance(d2, dict):
        return {
            **d1,
            **d2,
            **{
                k: d1[k] if d1[k] == d2[k] else dict_merge(d1[k], d2[k])
                for k in set(d1) & set(d2)
            },
        }
    return [
        *(d1 if isinstance(d1, list) else [d1]),
        *(d2 if isinstance(d2, list) else [d2]),
    ]
