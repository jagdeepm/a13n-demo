"""Live check against the kind lab, no API key: scripted model drives MCP + subagent + approved fix.

Needs: lab/lab.sh up, bin/kubernetes-mcp-server.   Run: uv run test_k8s.py
Injects `bad-image`, investigates through MCP, approves rollout_undo, verifies recovery, resets the lab.
"""

import asyncio
import json
import re
import subprocess

from pydantic_ai.messages import ModelRequest, ToolReturnPart
from pydantic_ai.models.function import DeltaToolCall, FunctionModel

import k8s
from sre_agent import build_agent, sandbox_runtime, turn

LAB = str(k8s.HERE / "lab" / "lab.sh")
SCRIPT = [
    ("view", {"file_path": "/workspace/.agents/skills/k8s-triage/SKILL.md"}),
    ("resources_list", {"apiVersion": "sre.demo.io/v1", "kind": "ServiceLevelObjective", "namespace": "shop"}),
    ("pods_list_in_namespace", {"namespace": "shop"}),
    ("delegate", {"subagent": "log-analyst", "prompt": "Analyze the loadgen pod logs in shop"}),
    ("kubectl_fix", {"action": "rollout_undo", "name": "checkout-api"}),
]
seen: dict[str, str] = {}


def tool_returns(messages):
    return [p for m in messages if isinstance(m, ModelRequest) for p in m.parts if isinstance(p, ToolReturnPart)]


async def respond(messages, info):
    returns = tool_returns(messages)
    names = {t.name for t in info.function_tools}
    if "delegate" not in names:  # log-analyst child: must see ONLY its two MCP tools
        assert names >= {"pods_list_in_namespace", "pods_log"} and "pods_get" not in names, names
        if not returns:
            yield {0: DeltaToolCall(name="pods_list_in_namespace", json_args='{"namespace":"shop"}', tool_call_id="c1")}
        elif len(returns) == 1:
            pod = re.search(r"loadgen-[a-z0-9]+-[a-z0-9]+", str(returns[0].content)).group(0)
            yield {0: DeltaToolCall(name="pods_log", json_args=json.dumps({"name": pod, "namespace": "shop", "tail": 5}),
                                    tool_call_id="c2")}
        else:
            yield "loadgen: " + str(returns[-1].content)[-200:]
        return
    seen.update({r.tool_name: str(r.content) for r in returns})
    if len(returns) < len(SCRIPT):
        name, args = SCRIPT[len(returns)]
        assert name in names, f"{name} not offered; have {sorted(names)}"
        yield {0: DeltaToolCall(name=name, json_args=json.dumps(args), tool_call_id=f"p{len(returns)}")}
    else:
        yield "Rolled back checkout-api."


def sh(*cmd):
    return subprocess.run(cmd, capture_output=True, text=True, check=True).stdout


async def main() -> None:
    sh(LAB, "reset")
    sh(LAB, "break", "bad-image")
    for _ in range(30):  # wait for the symptom to appear
        if "ImagePullBackOff" in sh("kubectl", "--context", k8s.CONTEXT, "-n", "shop", "get", "pods") or "ErrImagePull" in sh(
            "kubectl", "--context", k8s.CONTEXT, "-n", "shop", "get", "pods"):
            break
        await asyncio.sleep(2)
    try:
        answers = iter(["y"])
        async with k8s.mcp_client() as client, sandbox_runtime() as runtime:
            # guardrail: server must expose only read-only tools, and the identity must not see Secrets
            tools = await client.list_tools()
            assert tools and all(t.annotations.read_only_hint for t in tools), [t.name for t in tools]
            assert not {"pods_delete", "pods_exec", "resources_create_or_update"} & {t.name for t in tools}
            r = await client.call_tool("resources_list", {"apiVersion": "v1", "kind": "Secret", "namespace": "shop"}, raise_on_error=False)
            assert r.is_error, "Secrets must be denied"
            agent =build_agent(FunctionModel(stream_function=respond), client)
            await turn(agent, "checkout problems", None, runtime, prompt=lambda _: next(answers), mcp_client=client)

        assert "checkout-team" in seen["resources_list"], seen["resources_list"][:300]  # CRD read via MCP
        assert re.search(r"ImagePullBackOff|ErrImagePull", seen["pods_list_in_namespace"]), seen["pods_list_in_namespace"][:400]
        assert "checkout-api GET /api/info" in seen["delegate"], seen["delegate"][:300]  # child read logs via MCP
        assert "rolled back" in seen["kubectl_fix"], seen["kubectl_fix"]
        sh("kubectl", "--context", k8s.CONTEXT, "-n", "shop", "rollout", "status", "deploy/checkout-api", "--timeout=120s")
        image = sh("kubectl", "--context", k8s.CONTEXT, "-n", "shop", "get", "deploy/checkout-api",
                   "-o", "jsonpath={.spec.template.spec.containers[0].image}")
        assert image.endswith("podinfo:6.17.0"), image
        print("\nK8S LIVE CHECK PASSED")
    finally:
        sh(LAB, "reset")


asyncio.run(main())
