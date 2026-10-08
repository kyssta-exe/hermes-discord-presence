"""discord-presence — publish live Hermes agent telemetry as a Discord bot status.

Shows the same numbers as the desktop app's bottom status bar — tokens per
second, prompt-cache hit rate, context-window occupancy, the tool currently
running — on the Discord bot's presence, so anyone in the server can see how
hard the agent is working without asking it.

Surfaces this plugin adds:

* **Discord presence** — a rotating or compact custom status, published on the
  gateway's own bot client (``register_platform_handler("discord", ...)``).
* **``/pulse`` slash command** — a formatted report in any gateway session.
* **``agent_pulse`` tool** — the model can report its own telemetry on request.
* **``hermes discord-presence`` CLI** — status and configuration from a shell.

Everything is observer-only: the hooks record numbers, nothing rewrites a
prompt, blocks a tool, or overrides a built-in.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from . import presence, pulse_tool, schemas
from .config import CONFIG_SCHEMA, PresenceConfig, describe
from .metrics import DEFAULT_WINDOW, MetricsCollector

logger = logging.getLogger(__name__)

# ── process-wide singletons ──────────────────────────────────────────────────
# The hooks fire from the agent thread and the publisher from the bot's event
# loop, so the collector owns its own lock and these are plain module handles.
# ``lazy_singleton`` from plugins.plugin_utils guards the build; the values
# themselves are intentionally process-scoped (one gateway process = one bot).

_collector: MetricsCollector | None = None
_config: PresenceConfig | None = None
_ctx: Any = None


def get_collector() -> MetricsCollector:
    """The collector every hook writes to and every reader reads from."""
    global _collector
    if _collector is None:
        _collector = MetricsCollector(window=DEFAULT_WINDOW)
    return _collector


def get_config() -> PresenceConfig:
    """The resolved config (defaults until ``register()`` runs)."""
    global _config
    if _config is None:
        _config = PresenceConfig()
    return _config


def _set_config(config: PresenceConfig) -> None:
    global _config
    _config = config


# ── hook callbacks ───────────────────────────────────────────────────────────
# Thin adapters so the collector's signatures stay independent of the payloads.


def _on_pre_api_request(**kwargs: Any) -> None:
    get_collector().on_pre_api_request(**kwargs)


def _on_post_api_request(**kwargs: Any) -> None:
    get_collector().on_post_api_request(**kwargs)


def _on_api_request_error(**kwargs: Any) -> None:
    get_collector().on_api_request_error(**kwargs)


def _on_pre_tool_call(**kwargs: Any) -> None:
    get_collector().on_pre_tool_call(**kwargs)


def _on_post_tool_call(**kwargs: Any) -> None:
    get_collector().on_post_tool_call(**kwargs)


def _on_pre_llm_call(**kwargs: Any) -> None:
    get_collector().on_pre_llm_call(**kwargs)


def _on_post_llm_call(**kwargs: Any) -> None:
    get_collector().on_post_llm_call(**kwargs)


# ── slash command ────────────────────────────────────────────────────────────


def _handle_pulse(raw_args: str) -> str:
    """/pulse [summary|full] — the same numbers, formatted for chat."""
    from . import render

    collector = get_collector()
    config = get_config()
    snapshot = collector.snapshot()
    detail = (raw_args or "").strip().lower()
    if detail in {"config", "settings"}:
        return describe(config)
    if detail in {"full", "-v", "verbose"}:
        return render.render_report(snapshot, config) + "\n\n" + describe(config)
    return render.render_report(snapshot, config)


# ── CLI command ──────────────────────────────────────────────────────────────


def _cli_command(args: Any) -> None:
    """``hermes discord-presence [status|config|reset]``."""
    import json

    sub = str(getattr(args, "discord_presence_command", "") or "status").strip().lower()
    config = get_config()
    collector = get_collector()

    if sub in {"config", "settings"}:
        print(describe(config))
        for key, spec in CONFIG_SCHEMA.items():
            value = getattr(config, key, spec.get("default"))
            print(f"  {key:<26} {value!r}")
        return

    if sub == "reset":
        collector.reset()
        print("discord-presence: counters reset")
        return

    snapshot = collector.snapshot()
    print(describe(config))
    print(f"  publishers live        {presence.active_publishers()}")
    print(f"  state                  "
          f"{'thinking' if snapshot.inflight else snapshot.current_tool or 'idle'}")
    print(f"  model                  {snapshot.model or '—'}")
    print(f"  tokens/sec (windowed)  {snapshot.tps:.1f} over {snapshot.requests} samples")
    print(f"  tokens/sec (last)      {snapshot.instant_tps:.1f}")
    cache = "—" if snapshot.cache_hit_pct is None else f"{snapshot.cache_hit_pct:.1f}%"
    print(f"  cache hit              {cache}")
    ctx_pct = "—" if snapshot.context_pct is None else f"{snapshot.context_pct:.1f}%"
    print(
        f"  context                {snapshot.context_used:,} / "
        f"{snapshot.context_length:,} ({ctx_pct})"
    )
    print(f"  mean latency           {snapshot.latency:.3f}s")
    print(f"  api calls / errors     {snapshot.api_calls} / {snapshot.errors}")
    print(f"  compressions (est.)    {snapshot.compressions}")
    print(f"  lifetime tokens        {snapshot.total_tokens:,}")
    if sub == "json":
        print(json.dumps(snapshot.as_dict(), indent=2))


def _setup_argparse(subparser: Any) -> None:
    subs = subparser.add_subparsers(dest="discord_presence_command")
    subs.add_parser("status", help="Show live telemetry and presence state")
    subs.add_parser("json", help="Same, as raw JSON")
    subs.add_parser("config", help="Show the resolved settings")
    subs.add_parser("reset", help="Zero the counters")


# ── registration ─────────────────────────────────────────────────────────────


def register(ctx: Any) -> None:
    """Wire hooks, the Discord platform handler, the slash command, the tool and
    the CLI subcommand."""
    global _ctx
    _ctx = ctx

    config = PresenceConfig.from_ctx(ctx)
    _set_config(config)
    collector = get_collector()

    if not config.enabled:
        logger.info("discord-presence: disabled via settings — hooks stay inert")
        # Still register the read surfaces so /pulse and the tool answer, but no
        # presence is published and the write hooks are not subscribed.
        ctx.register_command(
            "pulse", _handle_pulse, description="Show live agent performance telemetry",
        )
        ctx.register_cli_command(
            name="discord-presence",
            help="Show live agent telemetry (discord-presence is disabled)",
            setup_fn=_setup_argparse,
            handler_fn=_cli_command,
        )
        ctx.register_tool(
            name="agent_pulse",
            toolset="discord_presence",
            schema=schemas.AGENT_PULSE,
            handler=pulse_tool.agent_pulse,
        )
        return

    # Observers. Every one of these returns nothing; none can steer the agent.
    ctx.register_hook("pre_api_request", _on_pre_api_request)
    ctx.register_hook("post_api_request", _on_post_api_request)
    ctx.register_hook("api_request_error", _on_api_request_error)
    ctx.register_hook("pre_tool_call", _on_pre_tool_call)
    ctx.register_hook("post_tool_call", _on_post_tool_call)
    ctx.register_hook("pre_llm_call", _on_pre_llm_call)
    ctx.register_hook("post_llm_call", _on_post_llm_call)

    # The presence itself: handed the live discord.py Bot at connect time.
    def _wire_discord(bot: Any, adapter: Any) -> None:
        presence.wire(bot, adapter, collector, config)

    ctx.register_platform_handler("discord", _wire_discord)

    ctx.register_command(
        "pulse", _handle_pulse, description="Show live agent performance telemetry",
    )
    ctx.register_tool(
        name="agent_pulse",
        toolset="discord_presence",
        schema=schemas.AGENT_PULSE,
        handler=pulse_tool.agent_pulse,
    )
    ctx.register_cli_command(
        name="discord-presence",
        help="Show live agent telemetry (tokens/sec, cache hit, context usage)",
        setup_fn=_setup_argparse,
        handler_fn=_cli_command,
    )

    # Bundled skill: how to read the numbers and tune the presence.
    skills_dir = Path(__file__).parent / "skills"
    for child in sorted(skills_dir.iterdir()) if skills_dir.is_dir() else []:
        skill_md = child / "SKILL.md"
        if child.is_dir() and skill_md.is_file():
            ctx.register_skill(child.name, skill_md)

    logger.info(
        "discord-presence: armed (%s mode, %s activity, every %ss)",
        config.mode, config.activity_type, config.update_interval_seconds,
    )
