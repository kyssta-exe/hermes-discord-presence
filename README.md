# hermes-discord-presence

Publishes live Hermes agent telemetry as your **Discord bot's presence** — the
same numbers the desktop app shows in its bottom status bar, visible to everyone
in the server without anyone having to ask.

```
📊 ⚡ 96 tok/s          ← throughput, windowed
📊 ◎ 79% cache         ← prompt-cache hit rate
📊 ▰▰▱▱▱▱▱▱▱▱ · 2% ctx · 4.2K/200.0K
```

Out of the box the status carries the three headline metrics. The running tool
and the active model are one switch each — useful, but they cost status length,
so they stay off until you ask:

```
📊 ⚡ 96 tok/s · ◎ 79% cache · ▰▰▱▱▱▱▱▱▱▱ · 2% ctx · ⧉ terminal · ☤ gpt-5.6
```

## Install

```bash
hermes plugins install kyssta-exe/hermes-discord-presence
hermes plugins enable discord-presence
```

Or drop the directory in by hand:

```bash
git clone https://github.com/kyssta-exe/hermes-discord-presence
cp -r hermes-discord-presence/discord-presence ~/.hermes/plugins/
hermes plugins enable discord-presence
```

Requires the Discord platform to be connected (`DISCORD_BOT_TOKEN` set and the
`discord-platform` plugin enabled) for the presence to publish. Without it the
plugin still loads — `/pulse`, the tool and the CLI all keep working, they just
have nowhere to show a status.

## What you get

| Surface | What it does |
|---|---|
| **Discord presence** | Rotating or compact custom status on the gateway's own bot client, plus a status dot that tracks the work |
| **`/pulse`** | Formatted telemetry report in any gateway session (Discord, Telegram, CLI) |
| **`agent_pulse` tool** | The model can report its own performance when you ask "how fast are you going?" |
| **`hermes discord-presence`** | `status` / `json` / `config` / `reset` from a shell |

### The metrics

| Glyph | Metric | Definition | Default |
|---|---|---|---|
| ⚡ | Tokens/sec | `sum(output tokens) / sum(request durations)` over a 12-request rolling window — true throughput, not a mean of per-request ratios | **on** |
| ◎ | Cache hit rate | `cache_read / prompt`, where `prompt = input + cache_read + cache_write` | **on** |
| ▰▱ | Context usage | last request's `prompt_tokens` ÷ the model's context length, with a block bar | **on** |
| ⧉ | Running tool | the tool executing right now | off |
| ☤ | Model | active route, last path segment only | off |
| ◷ | Latency | mean request duration | off |
| Σ | Lifetime tokens | session total | off |

Discord caps a custom status at 128 characters, so every extra segment competes
with the ones you actually asked for. The three defaults fit comfortably; turn
the rest on one at a time, and watch the length if you use `compact` mode.

All of it comes from documented plugin hooks — `post_api_request`,
`pre_tool_call`, `pre_llm_call`. No core internals, no monkey-patching, no
private agent attributes.

### The status dot

| State | When |
|---|---|
| 🟢 `online` | a turn or tool is live |
| 🟡 `idle` | no activity for `idle_after_seconds` (default 180s) |
| 🔴 `dnd` | context occupancy reaches `context_warn_percent` (default 80%) |

## Configuration

Every key lives at `plugins.entries.discord-presence.settings` in
`config.yaml`, or in the Desktop under **Settings → Plugins → Discord Presence**
(the manifest's `config_schema` renders the form automatically).

```yaml
plugins:
  entries:
    discord-presence:
      settings:
        mode: compact              # everything on one line instead of rotating
        activity_type: watching    # "Watching ⚡ 96 tok/s" instead of a custom status
        update_interval_seconds: 30
        show_model: false
        show_context_bar: true
        context_warn_percent: 90
        status_emoji: "🚀"
```

| Key | Type | Default | Meaning |
|---|---|---|---|
| `enabled` | bool | `true` | Publish the presence at all |
| `mode` | str | `rotate` | `rotate` = one segment per update; `compact` = all joined |
| `activity_type` | str | `custom` | `custom`, `playing`, `watching`, `listening`, `competing` |
| `update_interval_seconds` | int | `20` | Seconds between updates (10–600) |
| `status_emoji` | str | `📊` | Leading emoji |
| `idle_after_seconds` | int | `180` | Quiet period before the dot goes idle |
| `context_warn_percent` | int | `80` | Occupancy that switches the dot to dnd |
| `show_tokens_per_second` | bool | `true` | |
| `show_cache_hit` | bool | `true` | |
| `show_context` | bool | `true` | |
| `show_context_bar` | bool | `true` | |
| `show_tool` | bool | `false` | Running tool (off — costs status length) |
| `show_model` | bool | `false` | Active model (off — costs status length) |
| `show_latency` | bool | `false` | |
| `show_totals` | bool | `false` | |
| `bar_width` | int | `10` | Context bar cells (3–20) |
| `separator` | str | `· ` | Between segments in compact mode |

## Reading the numbers

- **Cache hit near 100%** — the prompt prefix is being reused; input cost is at
  its floor. A drop means the prefix changed: a model switch, context
  compression, a toolset change, or a mid-conversation system-prompt edit.
- **"no cached reads"** — the window contains zero cache reads. That is *no
  data*, not 0%. Some providers never report cache fields; for those the
  segment stays hidden rather than showing a misleading `0%`.
- **Tokens/sec is windowed** — read it as a trend over several updates, not
  per-tick. One long request reads low; a burst of short ones reads high.
- **Context % includes replayed reasoning** on reasoning models, so the last
  request's prompt can exceed the durable transcript.
- **Compression count is an estimate**, inferred from a >50% prompt-token drop
  between consecutive requests. It is labelled as an estimate everywhere it
  appears.

## Rate limits and safety

Discord allows roughly five presence updates per 20 seconds per session. The
default 20s interval sits well inside that, and the publisher **skips the REST
call entirely when neither the text nor the status dot changed** — an idle bot
sends almost nothing.

Every hook is observer-only: nothing rewrites a prompt, blocks a tool, or
overrides a built-in. The manifest declares **no capabilities**. A presence
failure is logged and swallowed; it cannot take the gateway down.

## Development

```bash
python -m pytest tests/ -q          # 98 unit tests, no Hermes needed

# The 14 end-to-end tests load the plugin through the real PluginManager.
# They need a HERMES_HOME with the plugin enabled:
export HERMES_HOME=/tmp/hx
mkdir -p $HERMES_HOME/plugins
cp -r discord-presence $HERMES_HOME/plugins/
hermes config set plugins.enabled '["discord-presence"]'
python -m pytest tests/ -q          # 112 total

hermes plugins doctor discord-presence      # discovery + register() smoke test
hermes plugins validate discord-presence    # the catalog admission gate
```

## Layout

```
plugin.yaml          manifest v2 + config_schema (18 keys)
pyproject.toml       PM dependency declaration (no runtime deps)
__init__.py          register(ctx): hooks, platform handler, tool, CLI, skill
metrics.py           thread-safe rolling window over hook payloads
config.py            settings resolution + the schema mirror
render.py            pure snapshot → strings (no discord import)
presence.py          the Discord publisher (discord imported lazily)
pulse_tool.py        agent_pulse tool handler
schemas.py           tool schema
skills/discord-presence/SKILL.md
```

`render.py` and `metrics.py` deliberately have no `discord` import, so the
number-crunching is unit-testable and `register()` works on a host where
discord.py was never installed.

## License

MIT
