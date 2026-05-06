# SPDX-License-Identifier: EUPL-1.2
# SPDX-FileCopyrightText: 2026 Carsten Rosenberg <c.rosenberg@heinlein-support.de>

"""Tests for xspct_db.backends.base helpers."""

from __future__ import annotations

from typing import Any

from xspct_db.backends.base import (
    maybe_list,
    merge_userdata,
    split_values_into_list,
    translate_entries,
)


def _cfg(**overrides: Any) -> dict[str, Any]:
    base = {
        "xspct_db_key_translation": {},
        "xspct_db_value_split": {},
    }
    base.update(overrides)
    return base


# ---------------------------------------------------------------------------
# split_values_into_list
# ---------------------------------------------------------------------------

def test_split_string_to_list():
    result = split_values_into_list("s", "a,b,c", key="field",
                                    query_config={"value_split": {"field": ","}})
    assert result == ["a", "b", "c"]


def test_split_list_items():
    result = split_values_into_list("s", ["a,b", "c"], key="field",
                                    query_config={"value_split": {"field": ","}})
    assert result == ["a", "b", "c"]


def test_no_split_rule_scalar():
    assert split_values_into_list("s", "hello") == ["hello"]


def test_no_split_rule_int():
    assert split_values_into_list("s", 42) == ["42"]


def test_no_split_rule_list_passthrough():
    assert split_values_into_list("s", ["x", "y"]) == ["x", "y"]


# ---------------------------------------------------------------------------
# maybe_list
# ---------------------------------------------------------------------------

def test_maybe_list_creates_empty_for_missing_key():
    entries = {}
    result = maybe_list("s", entries, "key", {}, _cfg())
    assert result["key"] == []


def test_maybe_list_converts_string():
    entries = {"key": "value"}
    result = maybe_list("s", entries, "key", {}, _cfg())
    assert result["key"] == ["value"]


# ---------------------------------------------------------------------------
# translate_entries
# ---------------------------------------------------------------------------

def test_translate_entries_basic():
    data = {"mail": "user@mailexample.de", "uid": "user"}
    query_config = {"primary_key": "mail", "attr_list": ["*"]}
    pk, entries = translate_entries("s", query_config, data, _cfg())
    assert pk == "user@mailexample.de"
    assert entries["uid"] == ["user"]


def test_translate_entries_key_translation():
    data = {"mail": "user@mailexample.de", "sn": "Smith"}
    query_config = {
        "primary_key": "mail",
        "attr_list": ["*"],
        "key_translation": {"sn": "surname"},
    }
    pk, entries = translate_entries("s", query_config, data, _cfg())
    assert "surname" in entries
    assert "sn" not in entries


def test_translate_entries_force_primary_key():
    data = {"mail": "user@mailexample.de"}
    query_config = {"primary_key": "mail", "attr_list": ["*"]}
    pk, _ = translate_entries("s", query_config, data, _cfg(), force_primary_key="forced")
    assert pk == "forced"


def test_translate_entries_attr_filter():
    data = {"mail": "user@mailexample.de", "secret": "hidden"}
    query_config = {"primary_key": "mail", "attr_list": ["mail"]}
    _, entries = translate_entries("s", query_config, data, _cfg())
    assert "secret" not in entries


# ---------------------------------------------------------------------------
# merge_userdata
# ---------------------------------------------------------------------------

def test_merge_userdata_creates_new_entry():
    userdata: dict[str, Any] = {"users": {}}
    result = merge_userdata("s", "alice", {"uid": ["alice"]}, userdata)
    assert "alice" in result["users"]


def test_merge_userdata_merges_existing():
    userdata: dict[str, Any] = {"users": {"alice": {"groups": ["admin"]}}}
    result = merge_userdata("s", "alice", {"groups": ["staff"]}, userdata)
    assert "admin" in result["users"]["alice"]["groups"]
    assert "staff" in result["users"]["alice"]["groups"]


def test_merge_userdata_merges_nested_in_place():
    userdata: dict[str, Any] = {
        "users": {"alice": {"attrs": {"groups": ["admin"], "quota": ["1G"]}}}
    }
    target_before = userdata["users"]["alice"]

    result = merge_userdata(
        "s",
        "alice",
        {"attrs": {"groups": ["staff"], "quota": ["2G"], "shell": ["/bin/zsh"]}},
        userdata,
    )

    assert result["users"]["alice"] is target_before
    assert result["users"]["alice"]["attrs"]["groups"] == ["admin", "staff"]
    assert result["users"]["alice"]["attrs"]["quota"] == ["1G", "2G"]
    assert result["users"]["alice"]["attrs"]["shell"] == ["/bin/zsh"]
