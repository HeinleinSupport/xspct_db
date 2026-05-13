# SPDX-License-Identifier: EUPL-1.2
# SPDX-FileCopyrightText: 2026 Carsten Rosenberg <c.rosenberg@heinlein-support.de>

"""Address rewrite rules engine.

Rules are defined as a list of ``{pattern, replacement}`` dicts under the
configuration key ``xspct_db_rewrite_rules``.  Patterns are compiled once at
startup via :func:`compile_rules` and stored on the ``aiohttp`` application
object so that :func:`apply_rewrite_rules` incurs no repeated compilation
overhead.

Rule evaluation follows a **first-match-wins** strategy: rules are tried in
order and the first rule that actually changes the address wins; subsequent
rules are not evaluated.  When no rule matches the original address is returned
unchanged.

The rewrite step runs **before** the prefilter so the domain whitelist and
pattern filter see the canonical (rewritten) address.  Both the original and
the rewritten address are registered as cache aliases so either form produces a
cache hit on subsequent requests.

Example configuration::

    xspct_db_rewrite_rules:
      # Replace the domain of any sub.example.com address with example.org
      - pattern:     '(.+)@sub\\.example\\.com$'
        replacement: '\\1@example.org'
      # Strip a SASL realm suffix: user@realm -> user@canonical.example.com
      - pattern:     '(.+)@realm$'
        replacement: '\\1@canonical.example.com'
"""

from __future__ import annotations

import logging
import re
from re import Pattern
from typing import Any

logger = logging.getLogger(__name__)

# Type alias for the compiled rule list stored on the app object.
CompiledRules = list[tuple[Pattern[str], str]]


def compile_rules(raw: list[dict[str, Any]] | None) -> CompiledRules:
    """Compile a list of raw rule dicts into (pattern, replacement) pairs.

    Invalid or missing entries are logged and skipped so a single bad rule
    does not prevent the service from starting.  Passing None or an empty
    list returns an empty list.
    """
    compiled: CompiledRules = []
    for i, rule in enumerate(raw or []):
        if not isinstance(rule, dict):
            logger.warning("rewrite rule #%d: expected dict, got %s -- skipping", i, type(rule).__name__)
            continue
        pattern_str = rule.get("pattern")
        replacement = rule.get("replacement")
        if not pattern_str:
            logger.warning("rewrite rule #%d: missing 'pattern' -- skipping", i)
            continue
        if replacement is None:
            logger.warning("rewrite rule #%d (%r): missing 'replacement' -- skipping", i, pattern_str)
            continue
        try:
            compiled.append((re.compile(pattern_str), str(replacement)))
        except re.error as exc:
            logger.warning("rewrite rule #%d (%r): invalid regex (%s) -- skipping", i, pattern_str, exc)
    if compiled:
        logger.info("rewrite: %d rule(s) loaded", len(compiled))
    return compiled


def apply_rewrite_rules(address: str, rules: CompiledRules) -> str:
    """Apply compiled rewrite rules to address and return the result.

    Rules are evaluated in order.  The first rule that produces a string
    different from address wins; subsequent rules are not applied.  Returns
    address unchanged when no rule matches or rules is empty.
    """
    for pattern, replacement in rules:
        try:
            rewritten = pattern.sub(replacement, address)
        except re.error as exc:
            logger.warning("rewrite: error applying rule %r to %r: %s", pattern.pattern, address, exc)
            continue
        if rewritten != address:
            logger.debug("rewrite: %r -> %r (rule %r)", address, rewritten, pattern.pattern)
            return rewritten
    return address
