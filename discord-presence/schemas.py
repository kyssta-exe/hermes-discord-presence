"""Tool schemas — what the LLM sees."""

AGENT_PULSE = {
    "name": "agent_pulse",
    "description": (
        "Report the live performance telemetry of this Hermes session: tokens per "
        "second, prompt-cache hit rate, context-window occupancy, mean request "
        "latency, the tool currently running, and lifetime token totals. Use it "
        "when the user asks how fast the agent is going, whether the prompt cache "
        "is working, how full the context window is, or what the agent is doing "
        "right now. Read-only; costs nothing."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "detail": {
                "type": "string",
                "enum": ["summary", "full"],
                "description": (
                    "'summary' (default) returns the headline numbers; 'full' adds "
                    "the per-sample breakdown and raw counters."
                ),
            }
        },
        "required": [],
    },
}
