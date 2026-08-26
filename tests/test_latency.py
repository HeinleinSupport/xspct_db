# SPDX-License-Identifier: EUPL-1.2
# SPDX-FileCopyrightText: 2026 Carsten Rosenberg <c.rosenberg@heinlein-support.de>

"""Tests for the per-span latency tracker and anomaly detection."""

from __future__ import annotations

import logging
from typing import Any

import pytest

from xspct_db import latency


@pytest.fixture(autouse=True)
def reset_latency():
    """Restore default tuning and drop all samples around each test."""
    latency.configure({})
    latency.set_observer(None)
    yield
    latency.set_observer(None)
    latency.configure({})


def _cfg(**overrides: Any) -> dict[str, Any]:
    return {"xspct_db_latency": {"enabled": True, **overrides}}


# ---------------------------------------------------------------------------
# configure() / enabled()
# ---------------------------------------------------------------------------


def test_enabled_by_default():
    latency.configure({})
    assert latency.enabled() is True


def test_can_be_disabled():
    latency.configure(_cfg(enabled=False))
    assert latency.enabled() is False
    latency.record("redis.get_alias", 1.0)
    assert latency.snapshot() == {}


def test_configure_resets_samples():
    latency.record("redis.get_alias", 0.1)
    assert latency.snapshot()["redis.get_alias"]["count"] == 1
    latency.configure({})
    assert latency.snapshot() == {}


# ---------------------------------------------------------------------------
# record() / snapshot()
# ---------------------------------------------------------------------------


def test_record_accumulates_count_and_max():
    for value in (0.01, 0.05, 0.02):
        latency.record("ldap.search", value)
    snap = latency.snapshot()["ldap.search"]
    assert snap["count"] == 3
    assert snap["max"] == pytest.approx(0.05)
    assert snap["avg"] == pytest.approx((0.01 + 0.05 + 0.02) / 3)


def test_spans_are_tracked_independently():
    latency.record("redis.get_alias", 0.001)
    latency.record("mysql.query", 0.5)
    snap = latency.snapshot()
    assert snap["redis.get_alias"]["count"] == 1
    assert snap["mysql.query"]["count"] == 1


def test_window_is_bounded():
    latency.configure(_cfg(window=16, min_samples=1, baseline_interval=0))
    for i in range(100):
        latency.record("ldap.search", 0.001 * i)
    assert latency.snapshot()["ldap.search"]["window"] == 16
    # The lifetime counter keeps counting past the window size.
    assert latency.snapshot()["ldap.search"]["count"] == 100


def test_median_and_percentiles():
    latency.configure(_cfg(min_samples=1, baseline_interval=0))
    for i in range(1, 101):
        latency.record("ldap.search", i / 1000)
    snap = latency.snapshot()["ldap.search"]
    assert snap["median"] == pytest.approx(0.051)
    assert snap["p90"] == pytest.approx(0.091)
    assert snap["p99"] == pytest.approx(0.1)


def test_observer_receives_every_sample():
    seen: list[tuple[str, float]] = []
    latency.set_observer(lambda name, value: seen.append((name, value)))
    latency.record("redis.get_user", 0.004)
    latency.record("redis.get_user", 0.006)
    assert seen == [("redis.get_user", 0.004), ("redis.get_user", 0.006)]


# ---------------------------------------------------------------------------
# span() context manager
# ---------------------------------------------------------------------------


def test_span_sync_records_a_sample():
    with latency.span("prefilter"):
        pass
    assert latency.snapshot()["prefilter"]["count"] == 1


async def test_span_async_records_a_sample():
    async with latency.span("redis.get_alias"):
        pass
    assert latency.snapshot()["redis.get_alias"]["count"] == 1


def test_span_records_even_when_the_body_raises():
    with pytest.raises(ValueError), latency.span("ldap.search"):
        raise ValueError("boom")
    assert latency.snapshot()["ldap.search"]["count"] == 1


# ---------------------------------------------------------------------------
# Anomaly detection
# ---------------------------------------------------------------------------


def test_no_anomaly_before_min_samples():
    latency.configure(_cfg(min_samples=50, anomaly_floor=0.0, baseline_interval=0))
    latency.record("ldap.search", 10.0)
    assert latency.snapshot()["ldap.search"]["anomalies"] == 0


def test_outlier_over_median_is_flagged(caplog):
    latency.configure(_cfg(min_samples=10, anomaly_factor=4.0, anomaly_floor=0.05, baseline_interval=0))
    for _ in range(50):
        latency.record("ldap.search", 0.01)
    with caplog.at_level(logging.WARNING, logger="xspct_db.latency"):
        latency.record("ldap.search", 2.0)
    snap = latency.snapshot()["ldap.search"]
    assert snap["anomalies"] == 1
    assert "LATENCY anomaly span=ldap.search" in caplog.text


def test_sample_below_the_floor_is_not_an_anomaly():
    # 200x the median, but still far below anomaly_floor — not worth alerting on.
    latency.configure(_cfg(min_samples=10, anomaly_factor=4.0, anomaly_floor=0.05, baseline_interval=0))
    for _ in range(50):
        latency.record("redis.get_alias", 0.0001)
    latency.record("redis.get_alias", 0.02)
    assert latency.snapshot()["redis.get_alias"]["anomalies"] == 0


def test_anomaly_logging_is_rate_limited(caplog):
    latency.configure(_cfg(min_samples=10, anomaly_factor=4.0, anomaly_floor=0.05, baseline_interval=0, log_interval=3600))
    for _ in range(50):
        latency.record("mysql.query", 0.01)
    with caplog.at_level(logging.WARNING, logger="xspct_db.latency"):
        for _ in range(5):
            latency.record("mysql.query", 2.0)
    assert latency.snapshot()["mysql.query"]["anomalies"] == 5
    assert caplog.text.count("LATENCY anomaly") == 1


# ---------------------------------------------------------------------------
# Per-request span collection
# ---------------------------------------------------------------------------


def test_request_breakdown_is_collected():
    latency.start_request()
    latency.record("redis.get_alias", 0.001)
    latency.record("redis.get_alias", 0.002)
    latency.record("ldap.search", 0.05)
    collected = latency.spans()
    assert collected["redis.get_alias"] == [pytest.approx(0.003), 2]
    assert collected["ldap.search"] == [pytest.approx(0.05), 1]


def test_spans_are_not_collected_outside_a_request():
    latency.record("ldap.search", 0.05)
    assert latency.spans() is None


def test_finish_request_records_the_total_span():
    t0 = latency.start_request()
    latency.finish_request("query", t0)
    assert latency.snapshot()["request.query"]["count"] == 1
    # The per-request accumulator is released once the request is done.
    assert latency.spans() is None


def test_slow_request_logs_the_breakdown(caplog):
    latency.configure(_cfg(min_samples=10, baseline_interval=0, slow_request_factor=4.0, slow_request_floor=0.0))
    # Establish a fast baseline for request.query.
    for _ in range(50):
        latency.record("request.query", 0.001)

    t0 = latency.start_request()
    latency.set_tag("<abc123>")
    latency.record("ldap.search", 0.4)
    with caplog.at_level(logging.WARNING, logger="xspct_db.latency"):
        # Backdate the start so the request looks slow without a real sleep.
        latency.finish_request("query", t0 - 0.5)

    assert "SLOW query" in caplog.text
    assert "<abc123>" in caplog.text
    assert "ldap.search=" in caplog.text


def test_fast_request_logs_nothing(caplog):
    latency.configure(_cfg(min_samples=10, baseline_interval=0, slow_request_floor=0.0))
    for _ in range(50):
        latency.record("request.query", 0.001)
    t0 = latency.start_request()
    with caplog.at_level(logging.WARNING, logger="xspct_db.latency"):
        latency.finish_request("query", t0)
    assert "SLOW" not in caplog.text


# ---------------------------------------------------------------------------
# Observer lifecycle
# ---------------------------------------------------------------------------


def test_set_observer_none_detaches():
    seen: list[str] = []
    latency.set_observer(lambda name, value: seen.append(name))
    latency.record("ldap.search", 0.01)
    latency.set_observer(None)
    latency.record("ldap.search", 0.01)
    assert seen == ["ldap.search"]
