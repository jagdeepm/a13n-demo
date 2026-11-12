"""Offline end-to-end check: scripted model drives skills, tools, subagent, approval, sandboxed shell. No API key.

Run: uv run test_offline.py
"""

import asyncio
import json

from pydantic_ai.messages import ModelRequest, ToolReturnPart
from pydantic_ai.models.function import DeltaToolCall, FunctionModel

import infra
from sre_agent import WORKSPACE, build_agent, sandbox_runtime, turn

SCRIPT = [  # parent's plan, one tool call per step
    ("view", {"file_path": "/workspace/.agents/skills/incident-triage/SKILL.md"}),
    ("fleet_status", {}),
    ("recent_deploys", {"service": "checkout-api"}),
    ("delegate", {"subagent": "log-analyst", "prompt": "Analyze checkout-api logs"}),
    ("rollback", {"service": "checkout-api", "to_version": "v2.4.0"}),
    ("shell_exec", {"command": 'echo shell-ok; head -1 ../sre_agent.py >/dev/null 2>&1 && echo READ_OUTSIDE_OK || echo READ_OUTSIDE_BLOCKED; touch ../escape.txt 2>/dev/null && echo WRITE_OUTSIDE_OK || echo WRITE_OUTSIDE_BLOCKED; nc -z -G 3 1.1.1.1 443 2>/dev/null && echo NET_OK || echo NET_BLOCKED; echo hi > note.txt && echo WRITE_WORKSPACE_OK'}),
    ("fleet_status", {}),
]
seen: list[tuple[str, str]] = []


async def respond(messages, info):
    returns = [p for m in messages if isinstance(m, ModelRequest) for p in m.parts if isinstance(p, ToolReturnPart)]
    names = {t.name for t in info.function_tools}
    if "delegate" not in names:  # we're the log-analyst child
        if not returns:
            yield {0: DeltaToolCall(name="tail_logs", json_args='{"service":"checkout-api"}', tool_call_id="c1")}
        else:
            yield "dominant error: connection pool exhausted (size=5)"
        return
    seen[:] = [(r.tool_name, str(r.content)) for r in returns]
    if len(returns) < len(SCRIPT):
        name, args = SCRIPT[len(returns)]
        assert name in names, f"{name} not offered; have {sorted(names)}"
        yield {0: DeltaToolCall(name=name, json_args=json.dumps(args), tool_call_id=f"p{len(returns)}")}
    else:
        yield "Resolved: rolled back checkout-api to v2.4.0."


async def main() -> None:
    answers = iter(["y", "y"])  # approve rollback, approve shell
    agent = build_agent(FunctionModel(stream_function=respond))
    async with sandbox_runtime() as runtime:
        state = await turn(agent, "checkout is slow", None, runtime, prompt=lambda _: next(answers))

    out = dict(seen)
    assert "Incident triage" in out["view"], out["view"]
    assert "pool exhausted" in out["delegate"], out["delegate"]
    assert "rolled back to v2.4.0" in out["rollback"], out["rollback"]
    sh = out["shell_exec"]
    print("\nSANDBOX SHELL OUTPUT:", sh)
    assert "shell-ok" in sh, sh
    for expect in ("READ_OUTSIDE_BLOCKED", "WRITE_OUTSIDE_BLOCKED", "NET_BLOCKED", "WRITE_WORKSPACE_OK"):  # the sandbox
        assert expect in sh, f"{expect} missing: {sh}"
    assert not (WORKSPACE.parent / "escape.txt").exists()
    (WORKSPACE / "note.txt").unlink()
    assert infra.FLEET["checkout-api"]["healthy"], "rollback didn't fix the fleet"
    assert state is not None and state.thread_id
    print("\nOFFLINE CHECK PASSED")


asyncio.run(main())
