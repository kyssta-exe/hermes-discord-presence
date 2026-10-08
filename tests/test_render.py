"""Tests for rendering and config resolution — no discord import required."""

from __future__ import annotations

from discord_presence.config import CONFIG_SCHEMA, PresenceConfig, describe
from discord_presence.metrics import MetricsCollector
from discord_presence.render import (
    build_context_bar,
    compact_status,
    presence_status,
    render_report,
    render_status,
    rotate_status,
    segments,
)


def collector_with(**post_kw):
    """One collector carrying a single completed request.

    ``prompt_tokens`` is derived from the same inputs the provider would report
    (``input + cache_read + cache_write``), so passing ``input_tokens=950``
    really produces a 950-token prompt rather than a stale 100.
    """
    c = MetricsCollector()
    input_tokens = post_kw.pop("input_tokens", 100)
    output_tokens = post_kw.pop("output_tokens", 100)
    cache_read = post_kw.pop("cache_read", 0)
    prompt_tokens = input_tokens + cache_read
    c.on_post_api_request(
        session_id="s1", model="openai/gpt-5.6", provider="openai", platform="discord",
        api_duration=post_kw.pop("duration", 1.0),
        usage={
            "input_tokens": input_tokens,
            "output_tokens": output_tokens,
            "cache_read_tokens": cache_read,
            "cache_write_tokens": 0,
            "prompt_tokens": prompt_tokens,
            "total_tokens": prompt_tokens + output_tokens,
        },
        context_length=post_kw.pop("context_length", 1000),
    )
    return c


class TestContextBar:
    def test_full_and_empty(self):
        cfg = PresenceConfig(bar_width=10, fill="#", empty="-")
        assert build_context_bar(100, cfg.bar_width, cfg.fill, cfg.empty) == "##########"
        assert build_context_bar(0, cfg.bar_width, cfg.fill, cfg.empty) == "----------"

    def test_half(self):
        assert build_context_bar(50, 10, "#", "-") == "#####-----"

    def test_clamped_out_of_range(self):
        assert build_context_bar(250, 4, "#", "-") == "####"
        assert build_context_bar(-20, 4, "#", "-") == "----"

    def test_none_renders_empty(self):
        assert build_context_bar(None, 4, "#", "-") == "----"

    def test_width_respected(self):
        bar = build_context_bar(37, 7, "#", "-")
        assert len(bar) == 7


class TestSegments:
    def test_all_enabled_by_default(self):
        snap = collector_with(cache_read=400, output_tokens=100).snapshot()
        segs = dict(segments(snap, PresenceConfig()))
        assert "tps" in segs
        assert "cache" in segs
        assert "context" in segs
        # Off by default: they cost status length, so the headline three own it.
        assert "model" not in segs
        assert "tool" not in segs
        assert "latency" not in segs   # opt-in
        assert "totals" not in segs    # opt-in

    def test_opt_in_segments_appear_when_switched_on(self):
        snap = collector_with(cache_read=400).snapshot()
        cfg = PresenceConfig(show_model=True, show_latency=True, show_totals=True)
        segs = dict(segments(snap, cfg))
        assert "model" in segs
        assert "latency" in segs
        assert "totals" in segs

    def test_default_presence_is_the_three_headline_metrics(self):
        """Out of the box the status shows exactly throughput, cache and context."""
        snap = collector_with(cache_read=400).snapshot()
        text = compact_status(snap, PresenceConfig(mode="compact"))
        assert "tok/s" in text
        assert "cache" in text
        assert "ctx" in text
        assert "gpt-5.6" not in text

    def test_toggles_remove_segments(self):
        snap = collector_with().snapshot()
        cfg = PresenceConfig(
            show_tokens_per_second=False, show_model=False, show_cache_hit=False,
            show_context=False,
        )
        assert segments(snap, cfg) == []

    def test_context_toggle_alone_survives(self):
        snap = collector_with().snapshot()
        cfg = PresenceConfig(
            show_tokens_per_second=False, show_model=False, show_cache_hit=False,
        )
        assert [key for key, _ in segments(snap, cfg)] == ["context"]

    def test_tool_segment_appears_only_while_running(self):
        # Opt-in, so the config has to ask for it explicitly.
        cfg = PresenceConfig(show_tool=True)
        c = collector_with()
        assert "tool" not in dict(segments(c.snapshot(), cfg))
        c.on_pre_tool_call(tool_name="terminal", args={})
        assert "⧉ terminal" in dict(segments(c.snapshot(), cfg))["tool"]

    def test_no_data_hides_the_segment(self):
        """An idle bot must not render '⚡ -- tok/s' on repeat."""
        segs = segments(MetricsCollector().snapshot(), PresenceConfig())
        assert segs == []

    def test_model_is_shortened_to_the_last_path_segment(self):
        snap = collector_with().snapshot()
        seg = dict(segments(snap, PresenceConfig(show_model=True)))["model"]
        assert "gpt-5.6" in seg
        assert "openai/" not in seg

    def test_long_model_name_truncated(self):
        c = MetricsCollector()
        c.on_post_api_request(model="vendor/" + "x" * 40, api_duration=1.0,
                              usage={"output_tokens": 1, "prompt_tokens": 1})
        model_seg = dict(segments(c.snapshot(), PresenceConfig(show_model=True)))["model"]
        assert model_seg.endswith("...")
        assert len(model_seg) <= 30


class TestRenderStatus:
    def test_idle_text_when_nothing_to_report(self):
        text = render_status(MetricsCollector().snapshot(), PresenceConfig())
        assert "idle" in text

    def test_rotate_cycles_one_segment_per_turn(self):
        snap = collector_with(cache_read=400).snapshot()
        cfg = PresenceConfig()
        seen = {rotate_status(snap, cfg, turn) for turn in range(4)}
        assert len(seen) > 1   # it really rotates

    def test_rotate_wraps_around(self):
        snap = collector_with(cache_read=400).snapshot()
        cfg = PresenceConfig()
        parts = segments(snap, cfg)
        for turn in range(len(parts) * 3):
            assert rotate_status(snap, cfg, turn) == rotate_status(snap, cfg, turn + len(parts))

    def test_thinking_outranks_metrics_while_inflight(self):
        c = collector_with()
        c.on_pre_api_request(session_id="s1", model="m")
        text = render_status(c.snapshot(), PresenceConfig(), turn=0)
        assert "thinking" in text

    def test_compact_joins_every_enabled_segment(self):
        snap = collector_with(cache_read=400).snapshot()
        cfg = PresenceConfig(mode="compact")
        text = compact_status(snap, cfg)
        assert "tok/s" in text and "cache" in text and "ctx" in text

    def test_compact_respects_discords_length_cap(self):
        c = MetricsCollector()
        c.on_post_api_request(
            model="a-very-long-model-name-that-goes-on/" + "y" * 60,
            api_duration=1.0,
            usage={"input_tokens": 1, "output_tokens": 1, "prompt_tokens": 2},
            context_length=10_000_000,
        )
        text = compact_status(c.snapshot(), PresenceConfig(mode="compact", separator=" · "))
        # Discord caps activity name/state at 128; we target 110 + emoji headroom.
        assert len(text) <= 128

    def test_emoji_leads_the_status(self):
        snap = collector_with().snapshot()
        cfg = PresenceConfig(status_emoji="🔥")
        assert render_status(snap, cfg).startswith("🔥")

    def test_custom_separator_is_used(self):
        snap = collector_with(cache_read=400).snapshot()
        cfg = PresenceConfig(mode="compact", separator=" | ")
        assert " | " in compact_status(snap, cfg)


class TestPresenceStatus:
    def test_online_while_working(self):
        c = collector_with()
        c.on_pre_tool_call(tool_name="terminal", args={})
        assert presence_status(c.snapshot(), PresenceConfig(), time_now()) == "online"

    def test_dnd_when_context_is_near_the_limit(self):
        snap = collector_with(input_tokens=950, context_length=1000).snapshot()
        assert presence_status(snap, PresenceConfig(context_warn_percent=80), time_now()) == "dnd"

    def test_dnd_threshold_is_configurable(self):
        # 75% occupancy: below the default 80 warn line, above a raised 70 one.
        snap = collector_with(input_tokens=750, context_length=1000).snapshot()
        assert _status(snap, context_warn_percent=80) == "online"
        assert _status(snap, context_warn_percent=70) == "dnd"

    def test_idle_after_the_quiet_period(self):
        import time

        c = collector_with()
        far_future = time.time() + 10_000
        quiet = c.snapshot()
        assert presence_status(quiet, PresenceConfig(idle_after_seconds=180), far_future) == "idle"

    def test_online_before_the_idle_threshold(self):
        c = collector_with()
        assert _status(c.snapshot(), idle_after_seconds=180) == "online"


def time_now():
    import time

    return time.time()


def _status(snapshot, **config_kw):
    """``presence_status`` against wall-clock now, with the config inline."""
    return presence_status(snapshot, PresenceConfig(**config_kw), time_now())


class TestReport:
    def test_report_has_the_headline_numbers(self):
        snap = collector_with(cache_read=400, context_length=1000).snapshot()
        text = render_report(snap, PresenceConfig())
        assert "tok/s" in text
        assert "Cache hit" in text
        assert "Context" in text
        assert "API calls" in text

    def test_report_handles_a_cold_session(self):
        text = render_report(MetricsCollector().snapshot(), PresenceConfig())
        assert "idle" in text
        assert "no cached reads" in text

    def test_report_shows_the_running_tool(self):
        c = collector_with()
        c.on_pre_tool_call(tool_name="read_file", args={})
        assert "read_file" in render_report(c.snapshot(), PresenceConfig())

    def test_report_shows_the_provider_route(self):
        snap = collector_with().snapshot()
        assert "openai/gpt-5.6" in render_report(snap, PresenceConfig())


class TestConfig:
    def test_defaults(self):
        cfg = PresenceConfig()
        assert cfg.enabled is True
        assert cfg.mode == "rotate"
        assert cfg.activity_type == "custom"
        assert cfg.update_interval_seconds == 20

    def test_from_ctx_reads_settings_and_coerces(self):
        class Ctx:
            def __init__(self, values):
                self._values = values

            def get_config(self, key, default=None):
                return self._values.get(key, default)

        cfg = PresenceConfig.from_ctx(Ctx({
            "enabled": "false",
            "mode": "COMPACT",
            "update_interval_seconds": "45",
            "show_model": 0,
            "bar_width": 999,
        }))
        assert cfg.enabled is False
        assert cfg.mode == "compact"
        assert cfg.update_interval_seconds == 45
        assert cfg.show_model is False
        assert cfg.bar_width == 20      # clamped to the max

    def test_interval_is_clamped_both_ways(self):
        class Ctx:
            def get_config(self, key, default=None):
                return {"update_interval_seconds": 1}.get(key, default)

        assert PresenceConfig.from_ctx(Ctx()).update_interval_seconds == 10

    def test_unknown_choice_falls_back_to_the_default(self):
        class Ctx:
            def get_config(self, key, default=None):
                return {"mode": "wat", "activity_type": "nope"}.get(key, default)

        cfg = PresenceConfig.from_ctx(Ctx())
        assert cfg.mode == "rotate"
        assert cfg.activity_type == "custom"

    def test_garbage_types_do_not_raise(self):
        class Ctx:
            def get_config(self, key, default=None):
                return {"update_interval_seconds": object(), "bar_width": [], "enabled": {}}.get(
                    key, default
                )

        cfg = PresenceConfig.from_ctx(Ctx())
        assert cfg.update_interval_seconds == 20
        assert cfg.bar_width == 10
        assert cfg.enabled is True

    def test_ctx_without_get_config_yields_defaults(self):
        assert PresenceConfig.from_ctx(object()) == PresenceConfig()

    def test_get_config_raising_does_not_break_registration(self):
        class Ctx:
            def get_config(self, key, default=None):
                raise RuntimeError("config unreadable")

        assert PresenceConfig.from_ctx(Ctx()) == PresenceConfig()

    def test_schema_matches_the_dataclass_fields(self):
        """Every config_schema key must be a real PresenceConfig field."""
        for key in CONFIG_SCHEMA:
            assert hasattr(PresenceConfig(), key), key

    def test_describe_is_a_one_liner(self):
        assert "\n" not in describe(PresenceConfig())
        assert "discord-presence" in describe()
