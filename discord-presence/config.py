"""Settings resolution for the Discord presence plugin.

Reads ``plugins.entries.discord-presence.settings.*`` through ``ctx.get_config``
(so the Desktop Settings → Plugins form, ``hermes config set`` and this module
all agree on one source of truth) and falls back to the defaults below. Every
value is coerced defensively: a typo in config.yaml must never stop the
presence from publishing.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

PLUGIN_ID = "discord-presence"

MODES = ("rotate", "compact")
ACTIVITY_TYPES = ("custom", "playing", "watching", "listening", "competing")

_DEFAULT_MODE = "rotate"
_DEFAULT_ACTIVITY = "custom"
_DEFAULT_INTERVAL = 20
_DEFAULT_EMOJI = "📊"


def _bool(value: Any, default: bool) -> bool:
    """Truthy coercion that also accepts the YAML spellings of false."""
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    if isinstance(value, str):
        lowered = value.strip().lower()
        if lowered in {"1", "true", "yes", "on"}:
            return True
        if lowered in {"0", "false", "no", "off", ""}:
            return False
    return default


def _int(value: Any, default: int, low: int, high: int) -> int:
    try:
        number = int(value)
    except (TypeError, ValueError):
        return default
    return max(low, min(high, number))


def _choice(value: Any, allowed: tuple, default: str) -> str:
    if isinstance(value, str) and value.strip().lower() in allowed:
        return value.strip().lower()
    return default


@dataclass(frozen=True)
class PresenceConfig:
    """Everything the publisher needs, already validated."""

    enabled: bool = True
    mode: str = _DEFAULT_MODE                 # rotate | compact
    activity_type: str = _DEFAULT_ACTIVITY    # custom | playing | watching | ...
    update_interval_seconds: int = _DEFAULT_INTERVAL
    status_emoji: str = _DEFAULT_EMOJI
    idle_after_seconds: int = 180
    context_warn_percent: int = 80

    show_tokens_per_second: bool = True
    show_cache_hit: bool = True
    show_context: bool = True
    show_context_bar: bool = True
    show_tool: bool = True
    show_model: bool = True
    show_latency: bool = False
    show_totals: bool = False

    # ── rendering knobs ──────────────────────────────────────────────────────
    bar_width: int = 10
    fill: str = "▰"
    empty: str = "▱"
    separator: str = " · "

    @classmethod
    def from_ctx(cls, ctx: Any) -> PresenceConfig:
        """Build from a live ``PluginContext`` (defaults for anything unset)."""
        get = getattr(ctx, "get_config", None)
        if get is None:
            return cls()

        def cfg(key: str, default: Any) -> Any:
            try:
                value = get(key, default)
            except Exception:
                return default
            return default if value is None else value

        interval = _int(
            cfg("update_interval_seconds", _DEFAULT_INTERVAL), _DEFAULT_INTERVAL, 10, 600
        )
        idle_after = _int(cfg("idle_after_seconds", 180), 180, 10, 86_400)
        warn = _int(cfg("context_warn_percent", 80), 80, 10, 100)
        bar_width = _int(cfg("bar_width", 10), 10, 3, 20)

        return cls(
            enabled=_bool(cfg("enabled", True), True),
            mode=_choice(cfg("mode", _DEFAULT_MODE), MODES, _DEFAULT_MODE),
            activity_type=_choice(
                cfg("activity_type", _DEFAULT_ACTIVITY), ACTIVITY_TYPES, _DEFAULT_ACTIVITY
            ),
            update_interval_seconds=interval,
            status_emoji=str(cfg("status_emoji", _DEFAULT_EMOJI) or _DEFAULT_EMOJI)[:8],
            idle_after_seconds=idle_after,
            context_warn_percent=warn,
            show_tokens_per_second=_bool(cfg("show_tokens_per_second", True), True),
            show_cache_hit=_bool(cfg("show_cache_hit", True), True),
            show_context=_bool(cfg("show_context", True), True),
            show_context_bar=_bool(cfg("show_context_bar", True), True),
            show_tool=_bool(cfg("show_tool", True), True),
            show_model=_bool(cfg("show_model", True), True),
            show_latency=_bool(cfg("show_latency", False), False),
            show_totals=_bool(cfg("show_totals", False), False),
            bar_width=bar_width,
            fill=str(cfg("bar_fill", "▰") or "▰")[:2],
            empty=str(cfg("bar_empty", "▱") or "▱")[:2],
            separator=str(cfg("separator", " · ") or " · ")[:8],
        )


# ── manifest mirror ──────────────────────────────────────────────────────────
# The plugin.yaml config_schema is what the Desktop renders; this mapping is the
# single place that documents the same keys for readers of the source. Keep the
# two in sync — plugin.yaml is authoritative for the UI.

CONFIG_SCHEMA: dict = {
    "enabled": {"type": "bool", "default": True,
                "description": "Publish agent telemetry as the Discord bot presence"},
    "mode": {"type": "str", "choices": list(MODES), "default": _DEFAULT_MODE,
             "description": "rotate = one metric per update; compact = all metrics joined"},
    "activity_type": {"type": "str", "choices": list(ACTIVITY_TYPES), "default": _DEFAULT_ACTIVITY,
                      "description": "Discord activity kind (custom status vs Playing/Watching)"},
    "update_interval_seconds": {"type": "int", "default": _DEFAULT_INTERVAL,
                                "description": "Seconds between presence updates (10-600)"},
    "status_emoji": {"type": "str", "default": _DEFAULT_EMOJI,
                     "description": "Leading emoji for the custom status"},
    "idle_after_seconds": {"type": "int", "default": 180,
                           "description": "Show the idle dot after this long without activity"},
    "context_warn_percent": {"type": "int", "default": 80,
                             "description": "Context occupancy that switches the presence to dnd"},
    "show_tokens_per_second": {"type": "bool", "default": True, "description": "Show tokens/sec"},
    "show_cache_hit": {
        "type": "bool", "default": True, "description": "Show prompt-cache hit rate",
    },
    "show_context": {"type": "bool", "default": True, "description": "Show context usage"},
    "show_context_bar": {"type": "bool", "default": True, "description": "Draw the context bar"},
    "show_tool": {"type": "bool", "default": True, "description": "Show the running tool"},
    "show_model": {"type": "bool", "default": True, "description": "Show the active model"},
    "show_latency": {"type": "bool", "default": False, "description": "Show mean request latency"},
    "show_totals": {"type": "bool", "default": False, "description": "Show lifetime token totals"},
    "bar_width": {"type": "int", "default": 10, "description": "Context bar cells (3-20)"},
    "separator": {"type": "str", "default": " · ", "description": "Segment separator"},
}


def describe(config: PresenceConfig | None = None) -> str:
    """One-line human summary, used by ``/pulse`` and the CLI command."""
    cfg = config or PresenceConfig()
    return (
        f"discord-presence: {'on' if cfg.enabled else 'off'} · {cfg.mode} · "
        f"{cfg.activity_type} · every {cfg.update_interval_seconds}s"
    )
