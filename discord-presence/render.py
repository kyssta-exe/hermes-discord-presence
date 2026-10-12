"""Pure rendering: snapshot + config → the strings Discord shows.

Kept free of any ``discord`` import so it is unit-testable and so ``register()``
works on a host that has the Discord platform but no gateway running.

Layout contract (mirrors the Hermes status bar's segment vocabulary, so a user
reading both sees the same glyphs):

* ``⚡`` tokens/sec        — throughput, windowed
* ``◎`` cache hit rate     — higher is better
* ``▰▰▰▱▱`` context bar + percent — lower is better
* ``⧉`` running tool       — what it is doing right now
* ``☤`` model
* ``◷`` latency
* ``Σ`` lifetime tokens

Two Discord surfaces are available and they are independent:

* the **custom status** — one 128-character line, rendered by
  :func:`render_status` under ``mode`` (``rotate`` / ``compact`` / ``full``);
* a **named activity** ("Watching …") — a second line Discord shows at the same
  time as the custom status. ``activity_type: both`` uses it for the segments
  the custom status could not carry (see :func:`dual_status`), so every metric
  is on screen at once instead of competing for one line.
"""

from __future__ import annotations

from .metrics import Snapshot

GLYPH_TPS = "⚡"
GLYPH_CACHE = "◎"
GLYPH_TOOL = "⧉"
GLYPH_MODEL = "☤"
GLYPH_LATENCY = "◷"
GLYPH_TOTALS = "Σ"

# Discord caps activity name/state at 128 chars; leave headroom for the emoji
# and for the "Watching " prefix Discord draws in front of an activity name.
_MAX_STATUS = 110
_MAX_ACTIVITY = 110


def _compact_tokens(count: int) -> str:
    """``1234`` → ``1.2K``, ``1234567`` → ``1.2M``."""
    value = float(max(0, count))
    for limit, suffix in ((1_000_000_000, "B"), (1_000_000, "M"), (1_000, "K")):
        if value >= limit:
            return f"{value / limit:.1f}{suffix}"
    return str(int(value))


def build_context_bar(pct: float | None, width: int, fill: str, empty: str) -> str:
    """``[████░░░░░░]``-style bar, clamped to 0-100%."""
    safe = 0.0 if pct is None else max(0.0, min(100.0, pct))
    filled = int(round((safe / 100.0) * max(1, width)))
    filled = max(0, min(width, filled))
    return f"{fill * filled}{empty * max(0, width - filled)}"


def _fmt_pct(value: float | None, precision: int = 1) -> str:
    return "--" if value is None else f"{value:.{precision}f}%"


def _join(parts: list[tuple[str, str]], separator: str) -> str:
    return separator.join(text for _, text in parts)


def _fits(parts: list[tuple[str, str]], separator: str, limit: int) -> list[tuple[str, str]]:
    """Longest leading run of *parts* whose joined text fits in *limit* chars."""
    kept: list[tuple[str, str]] = []
    for item in parts:
        if kept and len(_join(kept + [item], separator)) > limit:
            break
        kept.append(item)
    if kept and len(_join(kept, separator)) > limit:
        return []      # a single segment longer than the cap: caller decides
    return kept


def segments(snapshot: Snapshot, config, lean: bool = False) -> list[tuple[str, str]]:
    """``[(key, text), ...]`` for every enabled segment that has data.

    A segment with nothing to say is omitted rather than shown as ``--``: an
    idle bot showing ``⚡ -- t/s`` on repeat reads as broken, not as "no data".

    *lean* drops the words that the glyphs already imply (``⚡ 96/s`` instead of
    ``⚡ 96 tok/s``), so ``mode: full`` — every metric at once — still fits
    inside Discord's character cap.
    """
    sep = config.separator
    out: list[tuple[str, str]] = []

    if config.show_tokens_per_second and snapshot.tps > 0:
        rate = f"{snapshot.tps:.0f}/s" if lean else f"{snapshot.tps:.0f} tok/s"
        out.append(("tps", f"{GLYPH_TPS} {rate}"))
    if config.show_cache_hit and snapshot.cache_hit_pct is not None:
        pct = _fmt_pct(snapshot.cache_hit_pct, 0)
        out.append(("cache", f"{GLYPH_CACHE} {pct}" if lean else f"{GLYPH_CACHE} {pct} cache"))
    if config.show_context and snapshot.context_pct is not None:
        used = _compact_tokens(snapshot.context_used)
        total = _compact_tokens(snapshot.context_length)
        parts: list[str] = []
        if config.show_context_bar:
            parts.append(
                build_context_bar(snapshot.context_pct, config.bar_width, config.fill, config.empty)
            )
        if lean:
            parts.append(f"{snapshot.context_pct:.0f}%")
        else:
            parts.append(f"{snapshot.context_pct:.0f}% ctx")
            if total != "0":
                parts.append(f"{used}/{total}")
        out.append(("context", sep.join(parts)))
    if config.show_tool and snapshot.current_tool:
        out.append(("tool", f"{GLYPH_TOOL} {snapshot.current_tool}"))
    if config.show_latency and snapshot.latency > 0:
        out.append(("latency", f"{GLYPH_LATENCY} {snapshot.latency:.1f}s"))
    if config.show_model and snapshot.model:
        model = snapshot.model.split("/")[-1]
        if len(model) > 22:
            model = f"{model[:19]}..."
        out.append(("model", f"{GLYPH_MODEL} {model}"))
    if config.show_totals and snapshot.total_tokens > 0:
        totals = _compact_tokens(snapshot.total_tokens)
        suffix = "" if lean else " tok"
        out.append(("totals", f"{GLYPH_TOTALS} {totals}{suffix}"))
    return out


def _is_lean(config) -> bool:
    """``mode: full`` = every metric on, short labels."""
    return getattr(config, "mode", "") == "full"


def idle_status(config, now: float, snapshot: Snapshot | None = None) -> str:
    """What to show when there is nothing to report."""
    if snapshot is not None and snapshot.inflight:
        return f"{config.status_emoji} thinking…"
    return f"{config.status_emoji} idle · waiting for you"


def rotate_status(snapshot: Snapshot, config, turn: int) -> str:
    """One segment per update, cycling — keeps the status short and readable."""
    parts = segments(snapshot, config)
    if not parts:
        return idle_status(config, 0.0, snapshot)
    # "thinking…" outranks a metric only while a request is actually in flight.
    if snapshot.inflight and not snapshot.current_tool:
        return f"{config.status_emoji} thinking…"
    return f"{config.status_emoji} {parts[turn % len(parts)][1]}"


def compact_status(snapshot: Snapshot, config) -> str:
    """Every enabled segment joined, trimmed to Discord's length cap."""
    lean = _is_lean(config)
    parts = segments(snapshot, config, lean=lean)
    if not parts:
        return idle_status(config, 0.0, snapshot)
    kept = _fits(parts, config.separator, _MAX_STATUS)
    text = _join(kept, config.separator) or _join(parts, config.separator)[:_MAX_STATUS]
    return f"{config.status_emoji} {text}"


def render_status(snapshot: Snapshot, config, turn: int = 0) -> str:
    """The single string that becomes the Discord presence."""
    if config.mode in {"compact", "full"}:
        return compact_status(snapshot, config)
    return rotate_status(snapshot, config, turn)


# ── the second line: activity_type "both" ────────────────────────────────────
# Discord renders a custom status *and* one named activity ("Watching …") at the
# same time. The custom status is capped at 128 characters, so anything that
# does not fit there — or would make it unreadable — can ride the activity line
# instead. That is what makes every metric visible at once.


def _balanced_split(
    parts: list[tuple[str, str]], separator: str, limit: int
) -> tuple[list[tuple[str, str]], list[tuple[str, str]]]:
    """Two individually-capped groups of *parts*, balanced by rendered width.

    Two short lines read better than one crammed one, so the split point is
    around half the joined length — but the second group is never left empty
    when there is more than one segment to show.
    """
    if len(parts) < 2:
        return parts, []
    total = len(_join(parts, separator))
    target = max(len(parts[0][1]), (total + 1) // 2)
    head: list[tuple[str, str]] = []
    for item in parts:
        if head and len(_join(head + [item], separator)) > target:
            break
        head.append(item)
    if len(head) >= len(parts):        # keep at least one segment for line two
        head = head[:-1]
    return _fits(head, separator, limit), _fits(parts[len(head):], separator, limit)


def dual_segments(
    snapshot: Snapshot, config, turn: int = 0
) -> tuple[list[tuple[str, str]], list[tuple[str, str]]]:
    """``(custom-status segments, activity-line segments)`` for dual mode."""
    lean = _is_lean(config)
    parts = segments(snapshot, config, lean=lean)
    if not parts:
        return [], []

    if config.mode in {"compact", "full"}:
        return _balanced_split(parts, config.separator, _MAX_STATUS)

    # rotate: line one shows the tick's headline metric (or "thinking…"), the
    # activity line carries the rest, so all of them are visible at any moment.
    if snapshot.inflight and not snapshot.current_tool:
        return [], _fits(parts, config.separator, _MAX_ACTIVITY)
    headline = parts[turn % len(parts)]
    tail = [item for item in parts if item[0] != headline[0]]
    return [headline], _fits(tail, config.separator, _MAX_ACTIVITY)


def dual_status(snapshot: Snapshot, config, turn: int = 0) -> tuple[str, str | None]:
    """``(custom status, activity line)`` — the second is ``None`` if unused."""
    head, tail = dual_segments(snapshot, config, turn)
    activity = _join(tail, config.separator)[:_MAX_ACTIVITY] or None
    text = _join(head, config.separator)
    if not text:
        return idle_status(config, 0.0, snapshot), activity
    return f"{config.status_emoji} {text}", activity


def render_statuses(
    snapshot: Snapshot, config, turn: int = 0
) -> tuple[str, str | None]:
    """Both Discord lines for one update.

    The first element is always the custom-status text. The second is the named
    activity's text, and is only populated for ``activity_type: both``; for the
    single-kind settings the publisher turns the first string into the activity
    instead (``Watching ⚡ 96 tok/s``).
    """
    if config.activity_type == "both":
        return dual_status(snapshot, config, turn)
    return render_status(snapshot, config, turn), None


def presence_status(snapshot: Snapshot, config, now: float):
    """Discord ``Status`` enum name for the current state.

    ``online`` while working, ``idle`` after a quiet spell, ``dnd`` when the
    context is close to its limit — the same three states the status bar colour
    ladder uses.
    """
    if snapshot.context_pct is not None and snapshot.context_pct >= config.context_warn_percent:
        return "dnd"
    if snapshot.busy_at(now):
        return "online"
    if snapshot.idle_seconds(now) >= config.idle_after_seconds:
        return "idle"
    return "online"


def render_report(snapshot: Snapshot, config) -> str:
    """Multi-line plain-text report for the ``/pulse`` slash command."""
    lines: list[str] = ["**Hermes agent pulse**", ""]

    state = (
        "thinking…" if snapshot.inflight
        else f"running `{snapshot.current_tool}`" if snapshot.current_tool
        else "in a turn" if snapshot.busy
        else "idle"
    )
    lines.append(f"State: **{state}**")
    if snapshot.model:
        route = f"{snapshot.provider}/{snapshot.model}" if snapshot.provider else snapshot.model
        lines.append(f"Model: `{route}`")
    lines.append("")

    if snapshot.tps > 0:
        lines.append(
            f"{GLYPH_TPS} **{snapshot.tps:.1f} tok/s** windowed "
            f"({snapshot.instant_tps:.1f} tok/s last request, {snapshot.requests} samples)"
        )
    if snapshot.cache_hit_pct is not None:
        lines.append(f"{GLYPH_CACHE} Cache hit: **{_fmt_pct(snapshot.cache_hit_pct)}**")
    else:
        lines.append(f"{GLYPH_CACHE} Cache hit: no cached reads in this window")
    if snapshot.context_pct is not None:
        bar = build_context_bar(
            snapshot.context_pct, config.bar_width * 2, config.fill, config.empty
        )
        lines.append(
            f"Context: `{bar}` **{snapshot.context_pct:.1f}%** "
            f"({snapshot.context_used:,} / {snapshot.context_length:,} tokens)"
        )
    if snapshot.latency > 0:
        lines.append(f"{GLYPH_LATENCY} Mean latency: {snapshot.latency:.2f}s")
    lines.append("")
    lines.append(
        f"Session: {snapshot.api_calls} API calls · {snapshot.errors} errors · "
        f"{snapshot.compressions} compressions · "
        f"{GLYPH_TOTALS} {snapshot.total_tokens:,} tokens"
    )
    if snapshot.platform:
        lines.append(f"Surface: `{snapshot.platform}`")
    return "\n".join(lines)
