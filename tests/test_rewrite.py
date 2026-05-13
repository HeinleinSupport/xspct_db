# SPDX-License-Identifier: EUPL-1.2
# SPDX-FileCopyrightText: 2026 Carsten Rosenberg <c.rosenberg@heinlein-support.de>

"""Unit tests for xspct_db.rewrite."""

from __future__ import annotations

from xspct_db.rewrite import apply_rewrite_rules, compile_rules

# ---------------------------------------------------------------------------
# compile_rules
# ---------------------------------------------------------------------------


def test_compile_rules_empty_list():
    assert compile_rules([]) == []


def test_compile_rules_none():
    assert compile_rules(None) == []


def test_compile_rules_valid():
    rules = compile_rules([{"pattern": r"^(.+)@old\.example\.com$", "replacement": r"\1@new.example.com"}])
    assert len(rules) == 1
    pattern, replacement = rules[0]
    assert pattern.pattern == r"^(.+)@old\.example\.com$"
    assert replacement == r"\1@new.example.com"


def test_compile_rules_skips_missing_pattern(caplog):
    rules = compile_rules([{"replacement": r"\1@new.example.com"}])
    assert rules == []
    assert "missing 'pattern'" in caplog.text


def test_compile_rules_skips_missing_replacement(caplog):
    rules = compile_rules([{"pattern": r"^(.+)@old\.example\.com$"}])
    assert rules == []
    assert "missing 'replacement'" in caplog.text


def test_compile_rules_skips_invalid_regex(caplog):
    rules = compile_rules([{"pattern": r"[invalid", "replacement": "x"}])
    assert rules == []
    assert "invalid regex" in caplog.text


def test_compile_rules_skips_non_dict_entry(caplog):
    rules = compile_rules(["broken"])  # type: ignore[list-item]
    assert rules == []
    assert "expected dict" in caplog.text


def test_compile_rules_multiple():
    raw = [
        {"pattern": r"^(.+)@a\.example\.com$", "replacement": r"\1@canonical.example.com"},
        {"pattern": r"^(.+)@b\.example\.com$", "replacement": r"\1@canonical.example.com"},
    ]
    rules = compile_rules(raw)
    assert len(rules) == 2


# ---------------------------------------------------------------------------
# apply_rewrite_rules
# ---------------------------------------------------------------------------


def test_apply_no_rules_returns_original():
    assert apply_rewrite_rules("user@example.com", []) == "user@example.com"


def test_apply_matching_rule_rewrites():
    rules = compile_rules([{"pattern": r"^(.+)@relay\.example\.com$", "replacement": r"\1@example.com"}])
    result = apply_rewrite_rules("alice@relay.example.com", rules)
    assert result == "alice@example.com"


def test_apply_non_matching_rule_returns_original():
    rules = compile_rules([{"pattern": r"^(.+)@relay\.example\.com$", "replacement": r"\1@example.com"}])
    result = apply_rewrite_rules("alice@other.example.com", rules)
    assert result == "alice@other.example.com"


def test_apply_first_match_wins():
    rules = compile_rules([
        {"pattern": r"^(.+)@relay\.example\.com$", "replacement": r"\1@first.example.com"},
        {"pattern": r"^(.+)@relay\.example\.com$", "replacement": r"\1@second.example.com"},
    ])
    result = apply_rewrite_rules("alice@relay.example.com", rules)
    assert result == "alice@first.example.com"


def test_apply_non_changing_rule_does_not_count_as_match():
    """A rule that produces the identical string is not treated as a match."""
    rules = compile_rules([
        # This pattern matches but replacement produces the same string.
        {"pattern": r"^(.+)@example\.com$", "replacement": r"\1@example.com"},
        {"pattern": r"^(.+)@example\.com$", "replacement": r"\1@canonical.example.com"},
    ])
    result = apply_rewrite_rules("alice@example.com", rules)
    assert result == "alice@canonical.example.com"


def test_apply_domain_substitution():
    rules = compile_rules([{"pattern": r"@old\.domain\.example\.com$", "replacement": "@example.org"}])
    result = apply_rewrite_rules("user@old.domain.example.com", rules)
    assert result == "user@example.org"


def test_apply_sasl_realm_stripping():
    """Strip a SASL realm suffix: user@realm -> user@canonical.example.com."""
    rules = compile_rules([{"pattern": r"^(.+)@realm$", "replacement": r"\1@canonical.example.com"}])
    assert apply_rewrite_rules("user@realm", rules) == "user@canonical.example.com"
    # Other addresses untouched.
    assert apply_rewrite_rules("user@other.realm.com", rules) == "user@other.realm.com"
