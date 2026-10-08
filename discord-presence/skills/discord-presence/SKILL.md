---
name: discord-presence
description: "Read live Hermes agent telemetry — tokens/sec, prompt-cache hit rate, context-window occupancy, running tool, latency — as a Discord bot presence or on demand via /pulse and the agent_pulse tool."
version: 1.0.0
license: MIT
metadata:
  hermes:
    tags: [discord, gateway, observability, metrics, presence, telemetry]
    homepage: https://github.com/kyssta-exe/hermes-discord-presence
---

# Discord Presence — live agent telemetry

This plugin turns the same numbers the Hermes desktop app shows in its bottom
status bar into a Discord bot presence, plus two on-demand read surfaces.

## What is published

| Glyph | Metric | How it is computed |
|---|---|---|
| ⚡ | Tokens/sec | `sum(output tokens) / sum(request durations)` over a 12-request rolling window — true throughput, not a mean of per-request ratios |
| ◎ | Cache hit rate | `cache_read / prompt`, where `prompt = input + cache_read + cache_write` |
| ▰▰▱▱ | Context usage | last request's `prompt_tokens` / the model's context length, with a block bar |
| ⧉ | Running tool | the tool currently executing, from `pre_tool_call` — **off by default** |
| ☤ | Model | the active route, last path segment only — **off by default** |
| ◷ | Latency | mean request duration over the window (opt-in) |
| Σ | Lifetime tokens | session total (opt-in) |

The first three metrics ship enabled; the rest are one switch each. Discord caps
a custom status at 128 characters, so every extra segment competes with the ones
the user actually asked for — when suggesting one, check the rendered length in
`compact` mode first.

Presence **status dot** follows the same ladder as the status bar:

* `online` while a turn or tool is live;
* `idle` after `idle_after_seconds` of quiet (default 180s);
* `dnd` once context occupancy reaches `context_warn_percent` (default 80%).

## Reading it on demand

* `/pulse` — a formatted report in any gateway session (Telegram, Discord, CLI).
  `/pulse full` adds the raw config; `/pulse config` prints just the settings.
* `agent_pulse` tool — the model can call it when a user asks "how fast are you
  going?" or "is the cache working?". Pass `detail: "full"` for raw counters.
* `hermes discord-presence status` (or `json`) from a shell.

## Interpreting the numbers

* **Cache hit near 100%** — the prompt prefix is being reused; input cost is at
  its floor. A drop usually means the prefix changed: a model switch, context
  compression, a toolset change, or a mid-conversation system-prompt edit.
* **Cache hit shown as "no cached reads"** — the window contains zero cache
  reads, which is *no data*, not 0%. Some providers never report cache fields;
  for those the segment stays hidden rather than showing a misleading 0%.
* **Tokens/sec is windowed** — a single long request can read low, and a burst
  of short ones high. Read it as a trend over several updates, not per-tick.
* **Context % includes replayed reasoning** on reasoning models, so the last
  request's prompt can exceed the durable transcript. Compression is the only
  sanctioned cache break; the plugin *estimates* compressions from a >50%
  prompt-token drop between consecutive requests and labels it an estimate.

## Tuning

Settings live at `plugins.entries.discord-presence.settings` in `config.yaml`,
or in the Desktop under Settings → Plugins → Discord Presence. Common knobs:

```yaml
plugins:
  entries:
    discord-presence:
      settings:
        mode: compact              # everything on one line instead of rotating
        activity_type: watching    # "Watching ⚡ 84 tok/s" instead of a custom status
        update_interval_seconds: 30
        show_model: false
        context_warn_percent: 90
```

`rotate` (the default) shows one segment per update so the status stays short
and legible; `compact` joins every enabled segment and trims from the right to
fit Discord's 128-character limit.

## Rate limits and safety

Discord allows roughly five presence updates per 20 seconds per session. The
default 20s interval is well inside that, and the publisher **skips the REST
call entirely when neither the text nor the status dot changed** — an idle bot
sends almost nothing.

Every hook in this plugin is observer-only: nothing rewrites a prompt, blocks a
tool, or overrides a built-in. A presence failure is logged and swallowed; it
can never take the gateway down.
