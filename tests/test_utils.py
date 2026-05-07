# SPDX-License-Identifier: EUPL-1.2
# SPDX-FileCopyrightText: 2026 Carsten Rosenberg <c.rosenberg@heinlein-support.de>

"""Tests for xspct_db.utils."""

from __future__ import annotations

import time

from xspct_db.utils import (
    add_rspamd_id,
    dict_merge,
    generate_session_id,
    timer,
)


def test_generate_session_id_length():
    sid = generate_session_id()
    assert len(sid) == 6


def test_generate_session_id_unique():
    ids = {generate_session_id() for _ in range(100)}
    assert len(ids) > 1


def test_add_rspamd_id_both():
    result = add_rspamd_id("abc123", "xyz789")
    assert result == "<abc123-xyz789>"


def test_add_rspamd_id_session_only():
    result = add_rspamd_id("abc123", None)
    assert result == "<abc123>"


def test_add_rspamd_id_neither():
    result = add_rspamd_id("", None)
    # Falls back to a generated session id
    assert result.startswith("<") and result.endswith(">")


def test_timer_start_returns_zero():
    assert timer("start") == 0


def test_timer_elapsed_is_string_representable():
    timer("start")
    time.sleep(0.01)
    elapsed_str = str(timer())
    assert float(elapsed_str) >= 0.0


# ---------------------------------------------------------------------------
# dict_merge
# ---------------------------------------------------------------------------


def test_dict_merge_disjoint_dicts():
    result = dict_merge({"a": 1}, {"b": 2})
    assert result == {"a": 1, "b": 2}


def test_dict_merge_shared_equal_key():
    result = dict_merge({"a": 1}, {"a": 1})
    assert result == {"a": 1}


def test_dict_merge_shared_different_scalar():
    result = dict_merge({"a": 1}, {"a": 2})
    assert result == {"a": [1, 2]}


def test_dict_merge_list_values():
    result = dict_merge({"a": [1, 2]}, {"a": [3]})
    assert result == {"a": [1, 2, 3]}


def test_dict_merge_nested():
    d1 = {"x": {"inner": 1}}
    d2 = {"x": {"inner": 2}}
    result = dict_merge(d1, d2)
    assert result == {"x": {"inner": [1, 2]}}
