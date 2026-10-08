"""Tests for the metrics collector — the number-crunching core.

These exercise the collector against the *documented hook payload shapes* the
agent actually emits (``usage`` is the ``CanonicalUsage`` dict from
``agent/usage_pricing.py``; ``context_length`` comes off the compressor), so a
drift in either is caught here rather than in a user's Discord server.
"""

from __future__ import annotations

import time

import pytest
from discord_presence.metrics import MetricsCollector


def usage(input_tokens=0, output_tokens=0, cache_read=0, cache_write=0):
    """A usage dict shaped like the ``post_api_request`` payload."""
    prompt = input_tokens + cache_read + cache_write
    return {
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "cache_read_tokens": cache_read,
        "cache_write_tokens": cache_write,
        "reasoning_tokens": 0,
        "request_count": 1,
        "prompt_tokens": prompt,
        "total_tokens": prompt + output_tokens,
    }


def post(collector, *, model="m", duration=1.0, context_length=1000, **usage_kw):
    collector.on_post_api_request(
        session_id="s1", model=model, provider="p", platform="discord",
        api_duration=duration, usage=usage(**usage_kw), context_length=context_length,
    )


class TestEmptyState:
    def test_snapshot_is_safe_with_no_data(self):
        snap = MetricsCollector().snapshot()
        assert snap.tps == 0.0
        assert snap.cache_hit_pct is None      # no data, not a scary 0%
        assert snap.context_pct is None
        assert snap.api_calls == 0

    def test_idle_seconds_zero_before_any_activity(self):
        assert MetricsCollector().snapshot().idle_seconds() == 0.0


class TestThroughput:
    def test_windowed_tps_is_sum_over_sum_not_mean_of_ratios(self):
        c = MetricsCollector()
        # 100 tokens in 1s (100 t/s) then 10 tokens in 1s (10 t/s).
        post(c, output_tokens=100, duration=1.0)
        post(c, output_tokens=10, duration=1.0)
        snap = c.snapshot()
        # True throughput = 110/2 = 55. A mean of ratios would say 55 too here,
        # so use asymmetric durations to tell the two apart.
        assert snap.tps == pytest.approx(55.0)

    def test_asymmetric_durations_prove_sum_over_sum(self):
        c = MetricsCollector()
        post(c, output_tokens=100, duration=0.1)   # 1000 t/s
        post(c, output_tokens=10, duration=10.0)   # 1 t/s
        snap = c.snapshot()
        assert snap.tps == pytest.approx(110 / 10.1)   # sum/sum
        assert snap.tps != pytest.approx((1000 + 1) / 2)  # not mean of ratios

    def test_instant_tps_is_the_last_request(self):
        c = MetricsCollector()
        post(c, output_tokens=100, duration=1.0)
        post(c, output_tokens=20, duration=2.0)
        assert c.snapshot().instant_tps == pytest.approx(10.0)

    def test_zero_duration_does_not_divide_by_zero(self):
        c = MetricsCollector()
        post(c, output_tokens=50, duration=0.0)
        assert c.snapshot().tps == 0.0
        assert c.snapshot().instant_tps == 0.0

    def test_absurd_and_negative_durations_are_dropped(self):
        c = MetricsCollector()
        post(c, output_tokens=50, duration=-0.8)
        post(c, output_tokens=50, duration=float("nan"))
        post(c, output_tokens=100, duration=2.0)
        snap = c.snapshot()
        assert snap.tps == pytest.approx(50.0)

    def test_window_is_bounded(self):
        c = MetricsCollector(window=3)
        for _ in range(10):
            post(c, output_tokens=10, duration=1.0)
        assert c.snapshot().requests == 3
        # Lifetime counters are NOT bounded by the window.
        assert c.snapshot().api_calls == 10


class TestCacheHitRate:
    def test_hit_ratio_matches_the_status_bar_formula(self):
        c = MetricsCollector()
        post(c, input_tokens=100, cache_read=800, cache_write=100)
        snap = c.snapshot()
        # prompt = 100 + 800 + 100 = 1000; read 800 → 80%
        assert snap.cache_hit_pct == pytest.approx(80.0)

    def test_zero_reads_report_none_not_zero_percent(self):
        c = MetricsCollector()
        post(c, input_tokens=1000)
        assert c.snapshot().cache_hit_pct is None

    def test_clamped_to_100(self):
        c = MetricsCollector()
        post(c, input_tokens=0, cache_read=500)
        assert c.snapshot().cache_hit_pct == pytest.approx(100.0)

    def test_accumulates_across_the_window(self):
        c = MetricsCollector()
        post(c, input_tokens=500, cache_read=0)
        post(c, input_tokens=0, cache_read=1000)
        # window prompt 1500, reads 1000 → 66.7%
        assert c.snapshot().cache_hit_pct == pytest.approx(1000 / 1500 * 100)


class TestContextUsage:
    def test_percent_from_the_last_request(self):
        c = MetricsCollector()
        post(c, input_tokens=1000, context_length=2000)
        post(c, input_tokens=500, context_length=2000)
        snap = c.snapshot()
        assert snap.context_used == 500
        assert snap.context_length == 2000
        assert snap.context_pct == pytest.approx(25.0)

    def test_context_length_learned_from_post_when_pre_never_fired(self):
        c = MetricsCollector()
        post(c, input_tokens=500, context_length=4000)
        assert c.snapshot().context_length == 4000

    def test_percent_clamped_when_prompt_exceeds_window(self):
        # Reasoning models can replay thinking and exceed the durable window.
        c = MetricsCollector()
        post(c, input_tokens=3000, context_length=2000)
        assert c.snapshot().context_pct == 100.0

    def test_no_context_length_means_no_percent(self):
        c = MetricsCollector()
        post(c, input_tokens=500, context_length=0)
        assert c.snapshot().context_pct is None


class TestLatency:
    def test_mean_over_the_window(self):
        c = MetricsCollector()
        post(c, output_tokens=1, duration=1.0)
        post(c, output_tokens=1, duration=3.0)
        assert c.snapshot().latency == pytest.approx(2.0)


class TestLifecycle:
    def test_tool_tracking(self):
        c = MetricsCollector()
        c.on_pre_tool_call(tool_name="terminal", args={}, task_id="t")
        assert c.snapshot().current_tool == "terminal"
        c.on_post_tool_call(tool_name="terminal", args={}, result="", task_id="t", duration_ms=5)
        assert c.snapshot().current_tool == ""

    def test_turn_boundaries(self):
        c = MetricsCollector()
        assert c.snapshot().turn_live is False
        c.on_pre_llm_call(session_id="s1", model="m", platform="discord", user_message="hi")
        assert c.snapshot().turn_live is True
        c.on_post_llm_call(session_id="s1", model="m", platform="discord",
                           user_message="hi", assistant_response="ok")
        snap = c.snapshot()
        assert snap.turn_live is False
        assert snap.current_tool == ""
        assert snap.inflight is False

    def test_inflight_marker_set_and_cleared(self):
        c = MetricsCollector()
        c.on_pre_api_request(session_id="s1", model="m", context_length=1000)
        assert c.snapshot().inflight is True
        post(c, output_tokens=10, duration=1.0, context_length=1000)
        assert c.snapshot().inflight is False

    def test_error_clears_inflight_and_counts(self):
        c = MetricsCollector()
        c.on_pre_api_request(session_id="s1", model="m")
        c.on_api_request_error(session_id="s1", status_code=429, retryable=True)
        snap = c.snapshot()
        assert snap.inflight is False
        assert snap.errors == 1
        assert snap.api_calls == 0

    def test_lifetime_totals_survive_window_eviction(self):
        c = MetricsCollector(window=2)
        for _ in range(5):
            post(c, input_tokens=100, output_tokens=50)
        snap = c.snapshot()
        assert snap.api_calls == 5
        assert snap.total_tokens == 5 * 150
        assert snap.requests == 2

    def test_reset_clears_everything(self):
        c = MetricsCollector()
        post(c, output_tokens=10, duration=1.0)
        c.on_api_request_error(status_code=500)
        c.reset()
        snap = c.snapshot()
        assert (snap.api_calls, snap.errors, snap.total_tokens, snap.requests) == (0, 0, 0, 0)

    def test_compression_estimated_from_a_prompt_collapse(self):
        c = MetricsCollector()
        post(c, input_tokens=5000, context_length=100_000)
        post(c, input_tokens=1000, context_length=100_000)   # 80% drop → compression
        assert c.snapshot().compressions == 1

    def test_small_prompts_are_not_counted_as_compressions(self):
        c = MetricsCollector()
        post(c, input_tokens=1200, context_length=100_000)
        post(c, input_tokens=10, context_length=100_000)
        # Below the 1000-token floor on the *previous* request? No — 1200 > 1000,
        # so this IS a collapse. Use a genuinely small one instead:
        c.reset()
        post(c, input_tokens=900, context_length=100_000)
        post(c, input_tokens=10, context_length=100_000)
        assert c.snapshot().compressions == 0

    def test_growing_context_is_not_a_compression(self):
        c = MetricsCollector()
        post(c, input_tokens=1000, context_length=100_000)
        post(c, input_tokens=2000, context_length=100_000)
        assert c.snapshot().compressions == 0

    def test_idle_seconds_tracks_last_activity(self):
        c = MetricsCollector()
        post(c, output_tokens=1, duration=1.0)
        snap = c.snapshot()
        assert snap.idle_seconds() >= 0.0
        assert snap.idle_seconds(time.time() + 30) == pytest.approx(30.0, abs=0.5)


class TestRobustness:
    @pytest.mark.parametrize("bad", [None, "x", {}, [], float("inf")])
    def test_garbage_usage_never_raises(self, bad):
        c = MetricsCollector()
        c.on_post_api_request(session_id="s", model="m", api_duration=bad, usage=bad,
                              context_length=bad)
        assert isinstance(c.snapshot().as_dict(), dict)

    def test_missing_fields_entirely(self):
        c = MetricsCollector()
        c.on_post_api_request()
        c.on_pre_api_request()
        c.on_api_request_error()
        assert c.snapshot().api_calls == 1

    def test_snapshot_is_json_serialisable(self):
        import json

        c = MetricsCollector()
        post(c, input_tokens=100, cache_read=400, output_tokens=20, duration=1.0)
        json.dumps(c.snapshot().as_dict())

    def test_hooks_accept_extra_unknown_kwargs(self):
        """Forward compatibility: Hermes adds payload fields over time."""
        c = MetricsCollector()
        c.on_post_api_request(session_id="s", brand_new_field={"a": 1}, another=object())
        assert c.snapshot().api_calls == 1
