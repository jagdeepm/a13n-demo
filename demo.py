# /// script
# requires-python = ">=3.13"
# dependencies = ["a13n-harness==0.33.0"]
# ///
"""Tiny a13n Harness demo: chat REPL over OpenRouter with one Python tool and Thread memory.

Run:  OPENROUTER_API_KEY=sk-or-... uv run demo.py
"""

import asyncio
import os
from datetime import datetime

from a13n_harness import AgentSpec, HarnessBuilder
from pydantic_ai.capabilities import Capability

MODEL = "openrouter:" + os.environ.get("OPENROUTER_MODEL", "openai/gpt-4o-mini")


def now(tz_offset_hours: int = 0) -> str:
    """Return the current UTC time shifted by tz_offset_hours, as ISO 8601."""
    from datetime import UTC, timedelta

    return (datetime.now(UTC) + timedelta(hours=tz_offset_hours)).isoformat(timespec="seconds")


async def main() -> None:
    if not os.environ.get("OPENROUTER_API_KEY"):
        raise SystemExit("Set OPENROUTER_API_KEY first.")
    agent = HarnessBuilder().build(
        AgentSpec(model=MODEL, instructions="Be concise. Use the `now` tool for any time question."),
        output_type=str,
        capabilities=(Capability(id="clock", tools=[now]),),
    )
    state = None  # ponytail: in-memory only; see docs/a13n-harness/state-and-resume.md to persist
    print(f"Model: {MODEL}. Ctrl-D to quit.")
    while True:
        try:
            prompt = input("\nyou> ").strip()
        except EOFError:
            return
        if not prompt:
            continue
        result = await agent.run(prompt, previous_state=state)
        print("agent>", result.output_or_raise())
        state = result.state  # carries the conversation into the next turn


if __name__ == "__main__":
    asyncio.run(main())
