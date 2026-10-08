"""End-to-end proof against the REAL Hermes plugin machinery.

The other test modules stub a ``PluginContext``; this one loads the plugin
through ``PluginManager`` and the live hook registry exactly as the gateway
does, then fires the payload shapes the agent actually emits and asserts on what
a Discord user would see.

Skipped automatically when Hermes is not importable (a bare checkout with no
``hermes-agent`` on the path), so the suite still runs standalone.

Run it against an isolated home:

    HERMES_HOME=/tmp/hx hermes-agent/venv/bin/python -m pytest tests/test_e2e.py -q
"""

from __future__ import annotations

import importlib
import json
import sys
from pathlib import Path

import pytest

HERMES_SRC_CANDIDATES = (
    Path("/usr/local/lib/hermes-agent"),
    Path.home() / ".hermes" / "hermes-agent",
)

try:
    for _candidate in HERMES_SRC_CANDIDATES:
        if (_candidate / "hermes_cli" / "plugins.py").is_file():
            if str(_candidate) not in sys.path:
                sys.path.insert(0, str(_candidate))
            break
    from hermes_cli.lifecycle import has_hook  # noqa: F401
    from hermes_cli.plugins import discover_plugins, get_plugin_manager  # noqa: F401

    HERMES_AVAILABLE = True
except Exception:  # pragma: no cover - bare checkout
    HERMES_AVAILABLE = False

pytestmark = pytest.mark.skipif(
    not HERMES_AVAILABLE, reason="Hermes source not importable — run inside a Hermes checkout"
)


# ── fixtures ─────────────────────────────────────────────────────────────────

PLUGIN_DIR = Path(__file__).resolve().parent.parent / "discord-presence"


@pytest.fixture(scope="module")
def loaded_plugin():
    """Discover plugins for real and hand back the loaded plugin's module."""
    discover_plugins()
    manager = get_plugin_manager()
    entries = [
        p for p in manager.list_plugins() if "discord-presence" in str(p.get("key", ""))
    ]
    if not entries:
        pytest.skip(
            "discord-presence not enabled in this HERMES_HOME — set "
            "plugins.enabled: [discord-presence] and re-run"
        )

    # Hermes imports a directory plugin under a namespaced module name
    # (``hermes_plugins.discord_presence``), so find the live object rather
    # than guessing the spelling.
    for name, module in list(sys.modules.items()):
        if "discord_presence" in name and hasattr(module, "get_collector"):
            return module
    pytest.skip("plugin module not found in sys.modules after discovery")


@pytest.fixture(autouse=True)
def _fresh_counters(loaded_plugin):
    loaded_plugin.get_collector().reset()
    yield
    loaded_plugin.get_collector().reset()


# The payload shape ``agent/turn_response_intake.py`` actually emits.
USAGE = {
    "input_tokens": 900,
    "output_tokens": 240,
    "cache_read_tokens": 3300,
    "cache_write_tokens": 0,
    "reasoning_tokens": 0,
    "request_count": 1,
    "prompt_tokens": 4200,
    "total_tokens": 4440,
}


def _fire_one_turn(loaded_plugin, *, tool="terminal", duration=2.5):
    """Fire one realistic turn. ``tool=None`` means no tool is running, which
    also clears any tool left set by a previous call in the same test."""
    collector = loaded_plugin.get_collector()
    collector.on_pre_api_request(
        task_id="t1", turn_id="turn-1", api_request_id="turn-1:api:1", session_id="sess-e2e",
        platform="discord", model="openai/gpt-5.6", provider="openai",
        api_mode="chat_completions", api_call_count=1, message_count=12,
        tool_count=88, approx_input_tokens=4200, max_tokens=4096, started_at=0.0,
    )
    collector.on_post_api_request(
        task_id="t1", turn_id="turn-1", api_request_id="turn-1:api:1", session_id="sess-e2e",
        platform="discord", model="openai/gpt-5.6", provider="openai",
        api_call_count=1, api_duration=duration, started_at=0.0, ended_at=duration,
        finish_reason="tool_calls", usage=USAGE, context_length=200_000,
        assistant_content_chars=880, assistant_tool_call_count=2,
    )
    if tool:
        collector.on_pre_tool_call(tool_name=tool, args={"command": "ls"}, task_id="t1")
    else:
        collector.on_post_tool_call(tool_name="", args={}, result="", task_id="t1", duration_ms=0)
    return collector


# ── the plugin is genuinely loaded ───────────────────────────────────────────


class TestLoadedForReal:
    def test_hooks_are_in_the_live_registry(self, loaded_plugin):
        for hook in ("pre_api_request", "post_api_request", "api_request_error",
                     "pre_tool_call", "post_tool_call", "pre_llm_call", "post_llm_call"):
            assert has_hook(hook), f"{hook} was not registered by the loaded plugin"

    def test_manifest_matches_what_register_did(self, loaded_plugin):
        """The catalog admission check, asserted directly."""
        import yaml

        manifest = yaml.safe_load((PLUGIN_DIR / "plugin.yaml").read_text())
        assert manifest["provides_tools"] == ["agent_pulse"]
        assert len(manifest["provides_hooks"]) == 7

    def test_discord_platform_handler_is_queued(self, loaded_plugin):
        factories = get_plugin_manager().get_platform_handler_factories("discord")
        assert factories, "no discord platform handler factory queued for connect time"

    def test_skill_is_registered(self, loaded_plugin):
        manager = get_plugin_manager()
        lister = getattr(manager, "list_skills", None) or getattr(manager, "get_skills", None)
        if lister is None:
            pytest.skip("this Hermes exposes no skill listing on PluginManager")
        skills = lister()
        names = [str(s.get("name", s) if isinstance(s, dict) else s) for s in skills]
        assert any("discord" in n for n in names), f"skill missing from {names}"


# ── the numbers are right ────────────────────────────────────────────────────


class TestLiveNumbers:
    def test_snapshot_matches_the_payload_arithmetic(self, loaded_plugin):
        snap = _fire_one_turn(loaded_plugin).snapshot()
        assert snap.tps == pytest.approx(96.0)               # 240 / 2.5
        assert snap.cache_hit_pct == pytest.approx(3300 / 4200 * 100)
        assert snap.context_pct == pytest.approx(4200 / 200_000 * 100)
        assert snap.latency == pytest.approx(2.5)
        assert snap.current_tool == "terminal"
        assert snap.model == "openai/gpt-5.6"
        assert snap.platform == "discord"
        assert snap.busy is True

    def test_cold_session_reports_none_not_zero(self, loaded_plugin):
        snap = loaded_plugin.get_collector().snapshot()
        assert snap.cache_hit_pct is None
        assert snap.context_pct is None
        assert snap.tps == 0.0


# ── what a Discord member literally reads ────────────────────────────────────


class TestRenderedPresence:
    def test_rotate_mode_cycles_every_segment(self, loaded_plugin):
        _fire_one_turn(loaded_plugin)
        snap = loaded_plugin.get_collector().snapshot()
        render = importlib.import_module(f"{loaded_plugin.__name__}.render")

        # The default presence carries the three headline metrics; the tool and
        # model segments are opt-in, so switch them on to see all five rotate.
        config_mod = importlib.import_module(f"{loaded_plugin.__name__}.config")
        full = config_mod.PresenceConfig(
            mode="rotate", show_tool=True, show_model=True, show_latency=True,
        )
        lines = [render.render_status(snap, full, turn) for turn in range(6)]
        assert any("tok/s" in ln for ln in lines)
        assert any("cache" in ln for ln in lines)
        assert any("ctx" in ln for ln in lines)
        assert any("terminal" in ln for ln in lines)
        assert any("gpt-5.6" in ln for ln in lines)
        assert len(set(lines)) > 1, "rotate mode did not actually rotate"

    def test_every_rendered_line_fits_discords_limit(self, loaded_plugin):
        _fire_one_turn(loaded_plugin)
        snap = loaded_plugin.get_collector().snapshot()
        render = importlib.import_module(f"{loaded_plugin.__name__}.render")
        config_mod = importlib.import_module(f"{loaded_plugin.__name__}.config")

        compact = config_mod.PresenceConfig(mode="compact", show_latency=True, show_totals=True)
        # Discord caps activity name/state at 128 characters.
        assert len(render.render_status(snap, compact)) <= 128

    def test_status_dot_tracks_the_work(self, loaded_plugin):
        import time

        render = importlib.import_module(f"{loaded_plugin.__name__}.render")
        config = loaded_plugin.get_config()

        busy = _fire_one_turn(loaded_plugin).snapshot()
        assert render.presence_status(busy, config, time.time()) == "online"

        # No tool running, and nothing for a long time: the dot must relax.
        quiet = _fire_one_turn(loaded_plugin, tool=None, duration=1.0).snapshot()
        assert render.presence_status(quiet, config, time.time()) == "online"
        assert render.presence_status(quiet, config, time.time() + 10_000) == "idle"

    def test_context_pressure_overrides_everything(self, loaded_plugin):
        """A near-full context window shows dnd even while work is in flight."""
        import time

        render = importlib.import_module(f"{loaded_plugin.__name__}.render")
        config = loaded_plugin.get_config()
        collector = _fire_one_turn(loaded_plugin)
        collector.on_post_api_request(
            session_id="s", model="m", provider="p", platform="discord", api_duration=1.0,
            usage={"input_tokens": 195_000, "output_tokens": 10, "prompt_tokens": 195_000,
                   "total_tokens": 195_010},
            context_length=200_000,
        )
        snap = collector.snapshot()
        assert snap.context_pct == pytest.approx(97.5)
        assert render.presence_status(snap, config, time.time()) == "dnd"


# ── the on-demand surfaces ───────────────────────────────────────────────────


class TestOnDemandSurfaces:
    def test_agent_pulse_tool_reports_live_numbers(self, loaded_plugin):
        _fire_one_turn(loaded_plugin)
        pulse_tool = importlib.import_module(f"{loaded_plugin.__name__}.pulse_tool")

        payload = json.loads(pulse_tool.agent_pulse({}, task_id="t1", session_id="sess-e2e"))
        assert payload["ok"] is True
        assert payload["tokens_per_second"] == pytest.approx(96.0)
        assert payload["cache_hit_pct"] == pytest.approx(3300 / 4200 * 100, abs=0.01)
        assert payload["current_tool"] == "terminal"
        assert payload["state"] == "running terminal"

    def test_agent_pulse_full_adds_raw_counters(self, loaded_plugin):
        _fire_one_turn(loaded_plugin)
        pulse_tool = importlib.import_module(f"{loaded_plugin.__name__}.pulse_tool")

        payload = json.loads(pulse_tool.agent_pulse({"detail": "full"}))
        assert payload["api_calls"] == 1
        assert "report" in payload
        assert "96.0 tok/s" in payload["report"]

    def test_pulse_slash_command_renders_the_report(self, loaded_plugin):
        _fire_one_turn(loaded_plugin)
        text = loaded_plugin._handle_pulse("")
        assert "Hermes agent pulse" in text
        assert "96.0 tok/s" in text
        assert "terminal" in text

    def test_pulse_config_prints_the_resolved_settings(self, loaded_plugin):
        assert "discord-presence" in loaded_plugin._handle_pulse("config")
