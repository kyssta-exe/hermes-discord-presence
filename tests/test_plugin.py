"""Registration-surface tests.

The plugin is loaded through the *real* Hermes plugin machinery against a
temporary ``HERMES_HOME`` wherever possible, so what is asserted here is
behaviour (a hook fires, a tool answers, a platform factory is queued) rather
than the shape of the source.

Falls back to a stub context when Hermes is not importable, so the suite still
runs on a bare checkout.
"""

from __future__ import annotations

import json

import discord_presence
import pytest
import yaml
from discord_presence import presence, pulse_tool, schemas
from discord_presence.config import PresenceConfig


def _manifest():
    """The on-disk plugin.yaml — the admission gate reads this file."""
    from pathlib import Path

    return yaml.safe_load(
        (Path(discord_presence.__file__).parent / "plugin.yaml").read_text()
    )


try:  # the real thing, when Hermes is on the path
    from hermes_cli.plugins import VALID_HOOKS

    HERMES_AVAILABLE = True
except Exception:  # pragma: no cover - bare checkout
    VALID_HOOKS = set()
    HERMES_AVAILABLE = False


class RecordingCtx:
    """Minimal stand-in for ``PluginContext`` recording every registration."""

    plugin_id = "discord-presence"
    profile_name = "default"
    plugin_config = {}

    def __init__(self, settings=None):
        self._settings = settings or {}
        self.tools = []
        self.hooks = []
        self.commands = []
        self.cli_commands = []
        self.skills = []
        self.platform_handlers = []

    def get_config(self, key, default=None):
        return self._settings.get(key, default)

    def set_config(self, key, value):
        self._settings[key] = value

    def register_tool(self, name, **kwargs):
        self.tools.append((name, kwargs))

    def register_hook(self, name, callback):
        self.hooks.append((name, callback))

    def register_command(self, name, handler, **kwargs):
        self.commands.append((name, handler, kwargs))

    def register_cli_command(self, name, **kwargs):
        self.cli_commands.append((name, kwargs))

    def register_skill(self, name, path):
        self.skills.append((name, path))

    def register_platform_handler(self, platform, factory):
        self.platform_handlers.append((platform, factory))


@pytest.fixture(autouse=True)
def _clean_process_state():
    """Each test gets a fresh collector and no leftover publishers."""
    discord_presence.get_collector().reset()
    presence.stop_all()
    yield
    presence.stop_all()
    discord_presence.get_collector().reset()


def _pulse_handler():
    """The registered ``/pulse`` handler."""
    return dict((n, h) for n, h, _ in RecordingCtxAndRegister().commands)["pulse"]


class TestRegistration:
    def test_registers_every_declared_surface(self):
        ctx = RecordingCtx()
        discord_presence.register(ctx)

        assert [n for n, _ in ctx.hooks] == [
            "pre_api_request", "post_api_request", "api_request_error",
            "pre_tool_call", "post_tool_call", "pre_llm_call", "post_llm_call",
        ]
        assert [n for n, _ in ctx.tools] == ["agent_pulse"]
        assert [n for n, _, _ in ctx.commands] == ["pulse"]
        assert [n for n, _ in ctx.cli_commands] == ["discord-presence"]
        assert [p for p, _ in ctx.platform_handlers] == ["discord"]
        assert [n for n, _ in ctx.skills] == ["discord-presence"]

    @pytest.mark.skipif(not HERMES_AVAILABLE, reason="Hermes not importable")
    def test_every_hook_name_is_a_valid_hermes_hook(self):
        ctx = RecordingCtx()
        discord_presence.register(ctx)
        for name, _ in ctx.hooks:
            assert name in VALID_HOOKS, name

    def test_declared_manifest_lists_match_registrations(self):
        """``provides_tools`` / ``provides_hooks`` must match what register() does —
        the catalog admission check enforces exactly this."""
        manifest = _manifest()
        ctx = RecordingCtx()
        discord_presence.register(ctx)

        assert sorted(manifest["provides_tools"]) == sorted(n for n, _ in ctx.tools)
        assert sorted(manifest["provides_hooks"]) == sorted(n for n, _ in ctx.hooks)

    def test_config_schema_keys_are_all_read_by_the_plugin(self):
        manifest = _manifest()
        from discord_presence.config import CONFIG_SCHEMA

        assert set(manifest["config_schema"]) == set(CONFIG_SCHEMA)

    def test_disabled_mode_registers_read_surfaces_but_no_hooks(self):
        ctx = RecordingCtx({"enabled": False})
        discord_presence.register(ctx)
        assert ctx.hooks == []
        assert ctx.platform_handlers == []
        # The read surfaces stay available so /pulse still answers.
        assert [n for n, _, _ in ctx.commands] == ["pulse"]
        assert [n for n, _ in ctx.tools] == ["agent_pulse"]

    def test_registration_is_repeatable(self):
        """A plugin reload calls register() again in the same process."""
        discord_presence.register(RecordingCtx())
        ctx = RecordingCtx()
        discord_presence.register(ctx)
        assert len(ctx.hooks) == 7

    def test_manifest_declares_no_capabilities(self):
        """Observer-only plugin: no tool overrides, no privileged surfaces."""
        manifest = _manifest()
        assert "capabilities" not in manifest
        assert "requires_env" not in manifest


class TestHookWiring:
    def test_hooks_feed_the_collector_end_to_end(self):
        discord_presence.register(RecordingCtx())
        collector = discord_presence.get_collector()

        for name, callback in [(n, c) for n, c in _hooks()]:
            if name == "post_api_request":
                callback(
                    session_id="s", model="m", provider="p", platform="discord",
                    api_duration=2.0,
                    usage={"input_tokens": 100, "output_tokens": 200,
                           "cache_read_tokens": 700, "prompt_tokens": 800,
                           "total_tokens": 1000},
                    context_length=2000,
                )

        snap = collector.snapshot()
        assert snap.api_calls == 1
        assert snap.tps == pytest.approx(100.0)
        assert snap.cache_hit_pct == pytest.approx(87.5)
        assert snap.context_pct == pytest.approx(40.0)

    def test_tool_hook_tracks_the_running_tool(self):
        discord_presence.register(RecordingCtx())
        hooks = dict(_hooks())
        hooks["pre_tool_call"](tool_name="terminal", args={"command": "ls"}, task_id="t")
        assert discord_presence.get_collector().snapshot().current_tool == "terminal"
        hooks["post_tool_call"](
            tool_name="terminal", args={}, result="", task_id="t", duration_ms=1
        )
        assert discord_presence.get_collector().snapshot().current_tool == ""


def _hooks():
    ctx = RecordingCtx()
    discord_presence.register(ctx)
    return ctx.hooks


class TestTool:
    def test_returns_json_on_success(self):
        payload = json.loads(pulse_tool.agent_pulse({}))
        assert payload["ok"] is True
        assert payload["detail"] == "summary"
        for key in ("tokens_per_second", "cache_hit_pct", "context_pct", "state", "model"):
            assert key in payload

    def test_summary_omits_raw_counters(self):
        payload = json.loads(pulse_tool.agent_pulse({"detail": "summary"}))
        assert "api_calls" not in payload

    def test_full_adds_raw_counters_and_a_report(self):
        payload = json.loads(pulse_tool.agent_pulse({"detail": "full"}))
        assert "api_calls" in payload
        assert "report" in payload
        assert "presence_config" in payload

    def test_unknown_detail_falls_back_to_summary(self):
        assert json.loads(pulse_tool.agent_pulse({"detail": "wat"}))["detail"] == "summary"

    def test_accepts_injected_context_kwargs(self):
        """Hermes forwards task_id/session_id/... only for named params; **kwargs
        opts into the full, additively-growing payload."""
        payload = json.loads(
            pulse_tool.agent_pulse({}, task_id="t", session_id="s", user_task="x", brand_new="y")
        )
        assert payload["ok"] is True

    def test_never_raises_on_a_broken_collector(self, monkeypatch):
        def boom():
            raise RuntimeError("collector exploded")

        monkeypatch.setattr(discord_presence, "get_collector", boom)
        payload = json.loads(pulse_tool.agent_pulse({}))
        assert payload["ok"] is False
        assert "RuntimeError" in payload["error"]

    def test_reflects_live_metrics(self):
        discord_presence.register(RecordingCtx())
        dict(_hooks())["post_api_request"](
            session_id="s", model="acme/fast", api_duration=1.0,
            usage={"input_tokens": 0, "output_tokens": 50, "cache_read_tokens": 500,
                   "prompt_tokens": 500, "total_tokens": 550},
            context_length=1000,
        )
        payload = json.loads(pulse_tool.agent_pulse({}))
        assert payload["tokens_per_second"] == 50.0
        assert payload["cache_hit_pct"] == 100.0
        assert payload["context_pct"] == 50.0
        assert payload["model"] == "acme/fast"

    def test_schema_shape(self):
        schema = schemas.AGENT_PULSE
        assert schema["name"] == "agent_pulse"
        assert schema["parameters"]["type"] == "object"
        assert "detail" in schema["parameters"]["properties"]
        assert len(schema["description"]) > 80   # the model needs a real description


class TestSlashCommand:
    def test_pulse_renders_a_report(self):
        discord_presence.register(RecordingCtx())
        handler = _pulse_handler()
        text = handler("")
        assert "Hermes agent pulse" in text
        assert "Session:" in text

    def test_pulse_report_includes_live_numbers(self):
        """Feed a real request through the hook, then check /pulse shows it."""
        discord_presence.register(RecordingCtx())
        for name, callback in _hooks():
            if name == "post_api_request":
                callback(
                    session_id="s", model="m", provider="p", platform="discord",
                    api_duration=1.0,
                    usage={"input_tokens": 100, "output_tokens": 120,
                           "cache_read_tokens": 300, "prompt_tokens": 400,
                           "total_tokens": 520},
                    context_length=1000,
                )
        text = _pulse_handler()("")
        assert "120.0 tok/s" in text
        assert "75.0%" in text          # 300/400 cache hit
        assert "40.0%" in text          # 400/1000 context

    def test_pulse_config_prints_settings(self):
        assert "discord-presence" in _pulse_handler()("config")

    def test_pulse_full_includes_the_config_summary(self):
        assert "every" in _pulse_handler()("full")


def RecordingCtxAndRegister():
    ctx = RecordingCtx()
    discord_presence.register(ctx)
    return ctx

class TestPresencePublisher:
    def test_no_publisher_without_a_discord_client(self):
        assert presence.active_publishers() == 0

    def test_wire_starts_one_publisher_per_client(self, monkeypatch):
        discord_presence.register(RecordingCtx())
        monkeypatch.setattr(presence, "_PUBLISHERS", {})

        class FakeTask:
            def done(self):
                return False

            def cancel(self):
                pass

        class FakeBot:
            def __init__(self):
                self.loop = None

        created = []

        class FakeLoop:
            def create_task(self, coro):
                created.append(coro)
                coro.close()          # avoid "never awaited" warnings
                return FakeTask()

        bot = FakeBot()
        bot.loop = FakeLoop()

        discord_presence.get_config()
        presence.wire(bot, type("A", (), {"name": "Discord"})(), discord_presence.get_collector(),
                      discord_presence.get_config())
        assert len(created) == 1
        assert presence.active_publishers() == 1

    def test_wire_is_idempotent_on_the_same_client(self, monkeypatch):
        monkeypatch.setattr(presence, "_PUBLISHERS", {})

        class FakeTask:
            def done(self):
                return False

            def cancel(self):
                pass

        class FakeLoop:
            def __init__(self):
                self.count = 0

            def create_task(self, coro):
                self.count += 1
                coro.close()
                return FakeTask()

        class FakeBot:
            def __init__(self):
                self.loop = FakeLoop()

        bot = FakeBot()
        cfg = discord_presence.get_config()
        collector = discord_presence.get_collector()
        adapter = type("A", (), {"name": "Discord"})()
        presence.wire(bot, adapter, collector, cfg)
        presence.wire(bot, adapter, collector, cfg)
        assert bot.loop.count == 1

    def test_stop_all_clears_publishers(self, monkeypatch):
        monkeypatch.setattr(presence, "_PUBLISHERS", {})

        class FakeTask:
            def done(self):
                return False

            def cancel(self):
                pass

        class FakeBot:
            loop = None

        class FakeLoop:
            def create_task(self, coro):
                coro.close()
                return FakeTask()

        bot = FakeBot()
        bot.loop = FakeLoop()
        presence.wire(bot, None, discord_presence.get_collector(), discord_presence.get_config())
        presence.stop_all()
        assert presence.active_publishers() == 0


class TestActivityObjects:
    """The two-line publish path, against the real discord.py activity objects."""

    def test_custom_kind_builds_a_custom_status(self):
        discord = pytest.importorskip("discord")
        cfg = PresenceConfig(activity_type="custom")
        status = presence._activity(discord, cfg, "📊 ⚡ 96 tok/s")
        assert isinstance(status, discord.CustomActivity)

    def test_named_kind_carries_its_type_and_text(self):
        discord = pytest.importorskip("discord")
        act = presence._activity(discord, PresenceConfig(activity_type="watching"), "⚡ 96 tok/s")
        assert act.type is discord.ActivityType.watching
        assert act.name == "⚡ 96 tok/s"

    def test_both_publishes_a_custom_status_and_a_named_activity(self):
        discord = pytest.importorskip("discord")
        cfg = PresenceConfig(activity_type="both", activity_kind="listening")
        acts = presence._activities(discord, cfg, "📊 ⚡ 96 tok/s", "◎ 79% cache")
        assert len(acts) == 2
        assert isinstance(acts[0], discord.CustomActivity)
        assert acts[1].type is discord.ActivityType.listening
        assert acts[1].name == "◎ 79% cache"

    def test_both_with_an_empty_second_line_publishes_one_activity(self):
        discord = pytest.importorskip("discord")
        acts = presence._activities(discord, PresenceConfig(activity_type="both"), "📊 idle", None)
        assert len(acts) == 1
        assert isinstance(acts[0], discord.CustomActivity)

    def test_streaming_uses_the_configured_url(self):
        discord = pytest.importorskip("discord")
        cfg = PresenceConfig(activity_type="streaming", activity_url="https://example.com/live")
        act = presence._activity(discord, cfg, "⚡ 96 tok/s")
        assert act.type is discord.ActivityType.streaming
        assert act.url == "https://example.com/live"

    def test_streaming_without_a_url_degrades_to_playing(self):
        """Discord rejects a streaming activity with no link — never send one."""
        discord = pytest.importorskip("discord")
        act = presence._activity(discord, PresenceConfig(activity_type="streaming"), "x")
        assert act.type is discord.ActivityType.playing

    def test_dual_line_kinds_may_stream_too(self):
        discord = pytest.importorskip("discord")
        cfg = PresenceConfig(
            activity_type="both", activity_kind="streaming",
            activity_url="https://example.com/live",
        )
        acts = presence._activities(discord, cfg, "📊 ⚡ 96 tok/s", "◎ 79% cache")
        assert acts[1].type is discord.ActivityType.streaming


class TestPublisherTick:
    """One polling tick of the real publisher against a fake client."""

    @staticmethod
    def _bot(calls, raw=None):
        """A fake client with the two routes the publisher may use.

        ``raw`` collects the op-3 gateway payloads (the dual-line route),
        ``calls`` collects ``change_presence`` calls (single line).
        """

        class FakeSocket:
            async def send(self, data):
                if raw is not None:
                    raw.append(data)

        class FakeBot:
            loop = None

            def __init__(self):
                self.ws = FakeSocket() if raw is not None else None

            async def change_presence(self, **kwargs):
                calls.append(kwargs)

            def is_closed(self):
                return False

        return FakeBot()

    @staticmethod
    def _collector():
        """A collector holding one realistic completed request."""
        collector = discord_presence.get_collector()
        collector.on_post_api_request(
            session_id="s", model="openai/gpt-5.6", provider="openai", platform="discord",
            api_duration=2.5,
            usage={"input_tokens": 900, "output_tokens": 240, "cache_read_tokens": 3300,
                   "prompt_tokens": 4200, "total_tokens": 4440},
            context_length=200_000,
        )
        collector.on_pre_tool_call(tool_name="terminal", args={})
        return collector

    def test_dual_mode_sends_two_activities_in_one_gateway_payload(self):
        """discord.py cannot express two activities, so the op-3 payload is raw."""
        import asyncio

        pytest.importorskip("discord")
        calls, raw = [], []
        cfg = PresenceConfig(mode="full", activity_type="both")
        publisher = presence.PresencePublisher(self._bot(calls, raw), self._collector(), cfg)
        asyncio.run(publisher._publish_once())

        assert calls == [], "the dual line must not be sent as one activity"
        assert len(raw) == 1
        payload = json.loads(raw[0])
        assert payload["op"] == 3
        assert payload["d"]["status"] == "online"
        activities = payload["d"]["activities"]
        assert len(activities) == 2
        assert activities[0]["type"] == 4                       # custom status
        assert activities[0]["state"].startswith("📊")
        assert activities[1]["type"] == 3                       # "Watching …"
        assert len(str(activities[0]["state"])) <= 128
        assert len(activities[1]["name"]) <= 128
        combined = f"{activities[0]['state']} {activities[1]['name']}"
        for needle in ("/s", "%", "terminal", "gpt-5.6"):
            assert needle in combined

    def test_dual_mode_degrades_to_one_line_without_a_socket(self):
        import asyncio

        pytest.importorskip("discord")
        import discord

        calls = []
        cfg = PresenceConfig(mode="full", activity_type="both")
        publisher = presence.PresencePublisher(self._bot(calls), self._collector(), cfg)
        asyncio.run(publisher._publish_once())
        assert len(calls) == 1
        assert isinstance(calls[0]["activity"], discord.CustomActivity)

    def test_single_kind_still_uses_change_presence(self):
        import asyncio

        pytest.importorskip("discord")
        calls, raw = [], []
        cfg = PresenceConfig(activity_type="watching")
        publisher = presence.PresencePublisher(self._bot(calls, raw), self._collector(), cfg)
        asyncio.run(publisher._publish_once())
        assert raw == []
        assert len(calls) == 1
        assert getattr(calls[0]["status"], "name", calls[0]["status"]) == "online"
        assert "tok/s" in calls[0]["activity"].name

    def test_unchanged_renders_skip_the_rest_call(self):
        import asyncio

        pytest.importorskip("discord")
        calls = []
        collector = discord_presence.get_collector()
        collector.on_post_api_request(
            session_id="s", model="openai/gpt-5.6", api_duration=1.0,
            usage={"input_tokens": 100, "output_tokens": 100, "prompt_tokens": 200,
                   "total_tokens": 300},
            context_length=1000,
        )
        # One metric only: the dual renderer has nothing for a second line, so
        # both ticks render identically and the second must not hit Discord.
        cfg = PresenceConfig(
            mode="compact", activity_type="both", show_tokens_per_second=False,
            show_context=False,
        )
        publisher = presence.PresencePublisher(self._bot(calls), collector, cfg)
        asyncio.run(publisher._publish_once())
        asyncio.run(publisher._publish_once())
        assert len(calls) == 1

    def test_change_in_the_second_line_triggers_an_update(self):
        import asyncio

        pytest.importorskip("discord")
        raw = []
        cfg = PresenceConfig(mode="rotate", activity_type="both")
        publisher = presence.PresencePublisher(self._bot([], raw), self._collector(), cfg)
        asyncio.run(publisher._publish_once())
        asyncio.run(publisher._publish_once())
        # The rotation moves a metric between the two lines every tick, so the
        # second line alone is enough to justify the update.
        assert len(raw) == 2
        first = json.loads(raw[0])["d"]["activities"]
        second = json.loads(raw[1])["d"]["activities"]
        assert [a["state"] for a in first if "state" in a] != [
            a["state"] for a in second if "state" in a
        ] or [a["name"] for a in first] != [a["name"] for a in second]

    def test_closed_client_ends_the_loop_instead_of_raising(self):
        import asyncio

        pytest.importorskip("discord")
        calls = []

        class ClosedBot:
            loop = None

            async def change_presence(self, **kwargs):
                calls.append(kwargs)

            def is_closed(self):
                return True

        publisher = presence.PresencePublisher(ClosedBot(), self._collector(), PresenceConfig())
        asyncio.run(publisher._publish_once())
        assert calls == []
        assert publisher._stopped.is_set()
