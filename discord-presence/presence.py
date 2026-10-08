"""The Discord presence publisher.

Wired through ``ctx.register_platform_handler("discord", factory)``: the
factory receives the live ``discord.ext.commands.Bot`` at connect time and
starts one publisher task on that bot's event loop. Nothing here touches core
files, and the ``discord`` import lives inside the factory so ``register()``
still succeeds on a host where discord.py is not installed.

Rate-limit posture: Discord allows roughly five presence updates per 20 seconds
per session. The default 20s interval is comfortably inside that, and the
publisher skips the REST call entirely when the rendered string and status are
unchanged — so an idle bot sends almost nothing.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from . import render
from .config import PresenceConfig
from .metrics import MetricsCollector, Snapshot

logger = logging.getLogger(__name__)

# One publisher per native client. Keyed by id() of the bot object: the Discord
# adapter rebuilds ``commands.Bot`` on reconnect, and a rebuilt client must get a
# fresh publisher while the old one winds down.
_PUBLISHERS: dict[int, Any] = {}

# discord.py maps these names onto ActivityType; unknown values fall back to custom.
_ACTIVITY_TYPES = {
    "playing": "playing",
    "streaming": "streaming",
    "listening": "listening",
    "watching": "watching",
    "competing": "competing",
    "custom": "custom",
}


def _activity(discord: Any, config: PresenceConfig, text: str):
    """Build the activity object for *text* under the configured kind."""
    kind = config.activity_type
    if kind == "custom":
        # Custom status: the text rides in ``state``, with the emoji beside it.
        return discord.CustomActivity(name=text, emoji=config.status_emoji or None)
    activity_type = getattr(discord.ActivityType, _ACTIVITY_TYPES.get(kind, "playing"))
    return discord.Activity(type=activity_type, name=text)


def _status(discord: Any, name: str):
    return getattr(discord.Status, name, discord.Status.online)


class PresencePublisher:
    """Periodically pushes the metrics snapshot onto the bot's presence."""

    def __init__(
        self,
        bot: Any,
        collector: MetricsCollector,
        config: PresenceConfig,
        *,
        label: str = "discord",
    ) -> None:
        self._bot = bot
        self._collector = collector
        self._config = config
        self._label = label
        self._turn = 0
        self._last_text: str | None = None
        self._last_status: str | None = None
        self._task: asyncio.Task | None = None
        self._stopped = asyncio.Event()

    # ── lifecycle ────────────────────────────────────────────────────────────

    def start(self) -> asyncio.Task | None:
        """Start the loop on the bot's own event loop. Idempotent."""
        if self._task is not None and not self._task.done():
            return self._task
        loop = getattr(self._bot, "loop", None) or asyncio.get_event_loop()
        self._task = loop.create_task(self._run())
        return self._task

    def stop(self) -> None:
        self._stopped.set()
        task = self._task
        if task is not None and not task.done():
            task.cancel()

    async def _run(self) -> None:
        # Wait for READY: change_presence before the session is established is
        # silently dropped by the gateway.
        try:
            ready = getattr(self._bot, "wait_until_ready", None)
            if ready is not None:
                await ready()
        except Exception as exc:  # closed client / cancelled
            logger.debug("[%s] presence publisher never became ready: %s", self._label, exc)
            return

        interval = max(10, self._config.update_interval_seconds)
        while not self._stopped.is_set():
            try:
                await self._publish_once()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                # A presence update must never take the gateway down.
                logger.debug("[%s] presence update failed: %s", self._label, exc)
            try:
                await asyncio.wait_for(self._stopped.wait(), timeout=interval)
            except TimeoutError:
                continue
            except asyncio.CancelledError:
                raise

    # ── one tick ─────────────────────────────────────────────────────────────

    async def _publish_once(self) -> None:
        import time

        snapshot: Snapshot = self._collector.snapshot()
        text = render.render_status(snapshot, self._config, self._turn)
        status_name = render.presence_status(snapshot, self._config, time.time())
        self._turn += 1

        if text == self._last_text and status_name == self._last_status:
            return  # unchanged — skip the REST call entirely

        is_closed = getattr(self._bot, "is_closed", None)
        if callable(is_closed) and self._bot.is_closed():
            self._stopped.set()
            return

        import discord  # local: only reached when a gateway is live

        await self._bot.change_presence(
            activity=_activity(discord, self._config, text),
            status=_status(discord, status_name),
        )
        self._last_text = text
        self._last_status = status_name


def wire(bot: Any, adapter: Any, collector: MetricsCollector, config: PresenceConfig) -> None:
    """Platform-handler factory body: attach one publisher to *bot*.

    Called by the Discord adapter at connect time (and again on plugin reload,
    which the adapter dedupes per ``(plugin, factory qualname)``). Idempotent per
    native client, so a reconnect that reuses the same bot object does not spawn
    a second loop.
    """
    key = id(bot)
    existing = _PUBLISHERS.get(key)
    if existing is not None:
        task = existing._task
        if task is not None and not task.done():
            return  # already publishing on this exact client
        existing.stop()

    label = getattr(adapter, "name", "discord") or "discord"
    publisher = PresencePublisher(bot, collector, config, label=label)
    _PUBLISHERS[key] = publisher
    publisher.start()
    logger.info(
        "[%s] discord-presence: publishing telemetry every %ss",
        label, config.update_interval_seconds,
    )


def active_publishers() -> int:
    """Count of live publishers — used by the CLI status command and tests."""
    return sum(
        1
        for p in _PUBLISHERS.values()
        if p._task is not None and not p._task.done()
    )


def stop_all() -> None:
    """Tear every publisher down (tests, plugin unload)."""
    for publisher in list(_PUBLISHERS.values()):
        try:
            publisher.stop()
        except Exception:
            pass
    _PUBLISHERS.clear()
