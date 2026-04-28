# SPDX-License-Identifier: EUPL-1.2
# SPDX-FileCopyrightText: 2026 Carsten Rosenberg <c.rosenberg@heinlein-support.de>

"""Tests for xspct_db.stats: counters, pool sampling, and log output."""

from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from xspct_db import stats as stats_mod
from xspct_db.stats import (
    log_stats,
    reset,
    sample_pool_connections,
    stats,
    update_query_stats,
)

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture(autouse=True)
def clean_stats():
    """Reset global stats before every test."""
    reset()
    yield
    reset()


@pytest.fixture
def base_cfg() -> dict[str, Any]:
    return {
        "xspct_db_redis_cache": {"max_connections": 40},
        "xspct_db_types_enabled": {},
    }


# ---------------------------------------------------------------------------
# update_query_stats
# ---------------------------------------------------------------------------

class TestUpdateQueryStats:
    def test_first_entry_is_recorded(self):
        update_query_stats("q1", 0.01)
        assert stats["queries"]["q1"]["count"] == 1
        assert stats["queries"]["q1"]["time_total"] == pytest.approx(0.01)
        assert stats["queries"]["q1"]["time_min"] == pytest.approx(0.01)
        assert stats["queries"]["q1"]["time_max"] == pytest.approx(0.01)

    def test_multiple_entries_accumulate(self):
        update_query_stats("q1", 0.01)
        update_query_stats("q1", 0.03)
        update_query_stats("q1", 0.02)
        qs = stats["queries"]["q1"]
        assert qs["count"] == 3
        assert qs["time_total"] == pytest.approx(0.06)
        assert qs["time_min"] == pytest.approx(0.01)
        assert qs["time_max"] == pytest.approx(0.03)

    def test_separate_keys_are_independent(self):
        update_query_stats("q1", 0.01)
        update_query_stats("q2", 0.05)
        assert stats["queries"]["q1"]["count"] == 1
        assert stats["queries"]["q2"]["count"] == 1


# ---------------------------------------------------------------------------
# reset
# ---------------------------------------------------------------------------

class TestReset:
    def test_counters_zeroed(self):
        stats["requests_total"] = 5
        stats["redis_hits"] = 3
        update_query_stats("q1", 0.01)
        reset()
        assert stats["requests_total"] == 0
        assert stats["redis_hits"] == 0
        assert stats["queries"] == {}
        assert stats["pool_connections"] == {}


# ---------------------------------------------------------------------------
# sample_pool_connections
# ---------------------------------------------------------------------------

class TestSamplePoolConnections:
    def test_no_redis_no_backends(self, base_cfg):
        with patch("xspct_db.cache.connection", None):
            sample_pool_connections(base_cfg)
        assert stats["pool_connections"] == {}

    def test_redis_pool_sampled_via_created_connections(self, base_cfg):
        mock_pool = MagicMock()
        mock_pool._created_connections = 3
        mock_redis = MagicMock()
        mock_redis.connection_pool = mock_pool
        with patch("xspct_db.cache.connection", mock_redis):
            sample_pool_connections(base_cfg)
        pc = stats["pool_connections"]["redis"]
        assert pc["min"] == 3
        assert pc["max"] == 3
        assert pc["count"] == 1
        assert pc["limit"] == 40

    def test_redis_pool_sampled_via_fallback(self, base_cfg):
        mock_pool = MagicMock(spec=[])  # no _created_connections attribute
        in_use = MagicMock()
        in_use.__len__ = MagicMock(return_value=2)
        available = MagicMock()
        available.__len__ = MagicMock(return_value=1)
        mock_pool._in_use_connections = in_use
        mock_pool._available_connections = available
        mock_redis = MagicMock()
        mock_redis.connection_pool = mock_pool
        with patch("xspct_db.cache.connection", mock_redis):
            sample_pool_connections(base_cfg)
        assert stats["pool_connections"]["redis"]["max"] == 3

    def test_multiple_samples_build_min_max_avg(self, base_cfg):
        for count in (1, 5, 3):
            mock_pool = MagicMock()
            mock_pool._created_connections = count
            mock_redis = MagicMock()
            mock_redis.connection_pool = mock_pool
            with patch("xspct_db.cache.connection", mock_redis):
                sample_pool_connections(base_cfg)
        pc = stats["pool_connections"]["redis"]
        assert pc["min"] == 1
        assert pc["max"] == 5
        assert pc["count"] == 3
        assert pc["sum"] == pytest.approx(9.0)

    def test_mysql_pools_sampled(self, base_cfg):
        cfg = dict(base_cfg)
        cfg["xspct_db_types_enabled"] = {"mysql": True}
        mock_pool = MagicMock()
        mock_pool.size = 2
        mock_pool.maxsize = 20
        mock_mysql = MagicMock()
        mock_mysql._pools = {"mysql1": mock_pool}
        with patch("xspct_db.cache.connection", None), \
             patch.dict("sys.modules", {"xspct_db.backends.mysql_backend": mock_mysql}):
            sample_pool_connections(cfg)
        assert "mysql1" in stats["pool_connections"]
        assert stats["pool_connections"]["mysql1"]["max"] == 2


# ---------------------------------------------------------------------------
# log_stats
# ---------------------------------------------------------------------------

class TestLogStats:
    def test_request_counters_logged(self, base_cfg, caplog):
        stats["requests_total"] = 7
        stats["requests_known"] = 5
        stats["requests_unknown"] = 2
        with patch("xspct_db.cache.connection", None), \
             caplog.at_level("INFO", logger="xspct_db.stats"):
            log_stats(base_cfg)
        assert "requests_total=7" in caplog.text
        assert "requests_known=5" in caplog.text
        assert "requests_unknown=2" in caplog.text

    def test_redis_hit_rate_logged(self, base_cfg, caplog):
        stats["redis_hits"] = 3
        stats["redis_misses"] = 1
        stats["redis_negative_hits"] = 0
        mock_redis = MagicMock()
        mock_redis.connection_pool._created_connections = 1
        with patch("xspct_db.cache.connection", mock_redis), \
             caplog.at_level("INFO", logger="xspct_db.stats"):
            log_stats(base_cfg)
        assert "redis_hits=3" in caplog.text
        assert "redis_hit_rate=75.0%" in caplog.text

    def test_redis_hit_rate_zero_when_no_lookups(self, base_cfg, caplog):
        mock_redis = MagicMock()
        mock_redis.connection_pool._created_connections = 0
        with patch("xspct_db.cache.connection", mock_redis), \
             caplog.at_level("INFO", logger="xspct_db.stats"):
            log_stats(base_cfg)
        assert "redis_hit_rate=0.0%" in caplog.text

    def test_pool_connection_stats_logged_and_cleared(self, base_cfg, caplog):
        stats["pool_connections"]["redis"] = {
            "min": 1, "max": 5, "sum": 9.0, "count": 3, "limit": 40
        }
        with patch("xspct_db.cache.connection", None), \
             caplog.at_level("INFO", logger="xspct_db.stats"):
            log_stats(base_cfg)
        assert "pool[redis] conns min=1 avg=3.0 max=5" in caplog.text
        assert stats["pool_connections"] == {}

    def test_limit_reached_hint_shown(self, base_cfg, caplog):
        stats["pool_connections"]["mysql1"] = {
            "min": 20, "max": 20, "sum": 20.0, "count": 1, "limit": 20
        }
        with patch("xspct_db.cache.connection", None), \
             caplog.at_level("INFO", logger="xspct_db.stats"):
            log_stats(base_cfg)
        assert "LIMIT_REACHED" in caplog.text

    def test_query_timing_logged(self, base_cfg, caplog):
        update_query_stats("mysql1", 0.001)
        update_query_stats("mysql1", 0.003)
        with patch("xspct_db.cache.connection", None), \
             caplog.at_level("INFO", logger="xspct_db.stats"):
            log_stats(base_cfg)
        assert "query[mysql1]" in caplog.text
        assert "count=2" in caplog.text
        assert "avg=0.00200s" in caplog.text

    def test_no_redis_no_connection_line(self, base_cfg, caplog):
        with patch("xspct_db.cache.connection", None), \
             caplog.at_level("INFO", logger="xspct_db.stats"):
            log_stats(base_cfg)
        assert "redis_connections" not in caplog.text


# ---------------------------------------------------------------------------
# log_stats_periodically
# ---------------------------------------------------------------------------

class TestLogStatsPeriodically:
    async def test_calls_log_stats_after_interval(self, base_cfg, caplog):
        cfg = dict(base_cfg)
        cfg["xspct_db_stats_interval"] = 0.05
        cfg["xspct_db_stats_sample_interval"] = 0.02
        stats["requests_total"] = 1

        import asyncio
        with patch("xspct_db.cache.connection", None), \
             caplog.at_level("INFO", logger="xspct_db.stats"):
            task = asyncio.create_task(stats_mod.log_stats_periodically(cfg))
            await asyncio.sleep(0.12)
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass

        assert "requests_total=1" in caplog.text
