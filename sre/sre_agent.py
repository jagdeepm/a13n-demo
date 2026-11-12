"""On-call SRE agent built on a13n Harness.

Features exercised (grep for the tag to find each):
  [tools]        typed Python functions as tools, grouped in Capabilities
  [permissions]  ToolPermissionsCapability: remediation + real shell commands need human approval
  [hitl]         suspended runs -> approval / ask_user_question -> resume with DeferredToolResume
  [skills]       runbooks as .agents/skills/*/SKILL.md, discovered by SkillsCapability
  [env]          Local Envd Environment: file tools + shell in a restricted macOS Seatbelt sandbox
                 (only ./workspace is readable/writable, network denied)
  [subagent]     log-analyst child agent with its own instructions and narrower tools
  [stream]       live tool-call / text events
  [state]        HarnessState saved to disk -> continue the Thread after restart
  [usage]        UsageLimits budget + per-turn token/cost report
  [mcp]          --k8s: kubernetes-mcp-server (read-only ServiceAccount) as an MCP toolset, shared with the subagent

Run:  uv run sre_agent.py                              (new session; key from env or ~/.openrouter.env)
      uv run sre_agent.py --resume                         (continue saved thread)
      SRE_EGRESS=inherit uv run sre_agent.py               (let sandboxed commands use the network)
      uv run sre_agent.py --k8s                            (real kind cluster via MCP; see lab/lab.sh)
Needs bin/a13n-envd 0.8.0 (or A13N_ENVD_EXECUTABLE / a13n-envd on PATH).
"""

import argparse
import asyncio
import contextlib
import dataclasses
import json
import os
from dataclasses import dataclass
from pathlib import Path

from a13n_harness import (
    AgentDefinition,
    AgentSpec,
    DeferredToolResume,
    EnvironmentMount,
    HarnessBuilder,
    HarnessEvent,
    HarnessRunResultEvent,
    HarnessState,
    RunBindings,
    SubagentDefinition,
)
from a13n_harness.capabilities import SkillsCapability, SubagentCapability, UserInteractionCapability
from a13n_harness.environment import DynamicEnvironmentCapability, DynamicEnvironmentConfiguration
from a13n_harness.environment.execution import EnvironmentConnector
from a13n_harness.providers.environment.local_envd.configuration import LocalEnvdLaunchConfiguration
from a13n_harness.providers.environment.local_envd.provider import LOCAL_ENVD
from a13n_harness.providers.environment.local_envd.runtime import (
    LocalEnvdProviderRuntime,
    TemporaryLocalEnvdRuntimeAllocator,
    resolve_a13n_envd_executable,
)
from a13n_harness.tools import ToolPermissions, ToolPermissionsCapability
from pydantic_ai.capabilities import Capability
from pydantic_ai.messages import (
    FunctionToolCallEvent,
    FunctionToolResultEvent,
    PartDeltaEvent,
    PartStartEvent,
    TextPart,
    TextPartDelta,
)
from pydantic_ai.models import Model
from pydantic_ai.usage import UsageLimits

import infra
import k8s

HERE = Path(__file__).resolve().parent
WORKSPACE = HERE / "workspace"
ENVD_BIN = HERE / "bin" / "a13n-envd"  # must match a13n-envd-client version (0.8.0)
# [env] Seatbelt policy: workspace is the ONLY granted path; plus a minimal read-only system view
SANDBOX = {"mode": "restricted", "grants": [{"path": str(WORKSPACE), "access": "read_write"}]}
EGRESS = os.environ.get("SRE_EGRESS", "deny")  # "inherit" lets commands reach the network; macOS has no allowlist mode
STATE_FILE = HERE / "state.json"
DIM, BOLD, YELLOW, RESET = "\033[2m", "\033[1m", "\033[33m", "\033[0m"

INSTRUCTIONS = """You are the on-call SRE for a small e-commerce platform.
Always follow the incident-triage skill (read its SKILL.md first) when investigating problems.
Diagnostic tools are safe; remediation tools and shell commands require human approval, so justify
each one in a single sentence before calling it. Be terse: bullet points, numbers, no fluff.
Your workspace is /workspace (runbooks in /workspace/.agents/skills). Shell commands run in a macOS sandbox: only the workspace is\naccessible and outbound network is blocked, so don't try to reach real infrastructure from the shell."""


def build_agent(model: str | Model, mcp_client=None):
    """[tools] [permissions] [skills] [env] [subagent] [hitl] [usage] [mcp]. `model` may be a native Model for tests.

    mcp_client=None -> simulated fleet (infra.py). An entered k8s.mcp_client() -> real kind cluster via MCP.
    """
    spec_model, native = (model, None) if isinstance(model, str) else (None, model)
    log_rules = "Return: dominant error, count, probable cause, one-line evidence quote."

    if mcp_client is None:
        instructions = INSTRUCTIONS
        tools = (
            Capability(id="diagnostics", tools=[infra.fleet_status, infra.recent_deploys, infra.tail_logs]),
            Capability(id="remediation", tools=[infra.rollback, infra.scale, infra.restart]),
        )
        child_tools, child_bindings = (Capability(id="logs", tools=[infra.tail_logs]),), None
        child_instructions = f"You analyze service logs. {log_rules}"
    else:  # [mcp] diagnostics come from the MCP server per Run (see stream_turn); only the fix tool is static
        instructions = k8s.INSTRUCTIONS
        tools = (Capability(id="remediation", tools=[k8s.kubectl_fix]),)
        child_tools = ()
        # [subagent] children don't inherit MCP; hand the log-analyst a narrower projection of the same client
        child_bindings = lambda b: dataclasses.replace(  # noqa: E731
            b, capabilities=(*b.capabilities, k8s.mcp(mcp_client, ["pods_list_in_namespace", "pods_log"]))
        )
        child_instructions = (f"You analyze Kubernetes pod logs in namespace shop with pods_log "
                              f"(set previous=true for crashed containers). {log_rules}")

    log_analyst = AgentDefinition(  # [subagent] only sees logs, can't remediate
        agent=AgentSpec(model=spec_model, instructions=child_instructions, usage_limits=UsageLimits(request_limit=6),
                        model_settings={"max_tokens": 2048}),
        model=native,
        output_type=str,
        definition_id="log-analyst-v1",
        capabilities=child_tools,
    )

    return HarnessBuilder().build(
        AgentDefinition(
            agent=AgentSpec(
                model=spec_model, instructions=instructions, usage_limits=UsageLimits(request_limit=40),
                model_settings={"max_tokens": 4096},  # [usage] caps each reply; also what OpenRouter reserves credit for
            ),
            model=native,
            output_type=str,
            definition_id="sre-oncall-v1",
            capabilities=(
                *tools,
                SkillsCapability(),  # [skills] discovers /workspace/.agents/skills/*
                DynamicEnvironmentCapability(DynamicEnvironmentConfiguration()),  # [env] view/write/shell_exec...
                SubagentCapability(),  # [subagent] adds delegate / resume_subagent tools
                UserInteractionCapability(),  # [hitl] adds ask_user_question
                ToolPermissionsCapability(  # [permissions] stable IDs: tool/<capability-id>/<fn>
                    ToolPermissions(rules={"tool/remediation/*": "ask", "environment.shell_exec": "ask"})
                ),
            ),
            subagents=(
                SubagentDefinition(
                    name="log-analyst",
                    description="Summarize errors in one service's logs.",
                    agent=log_analyst,
                    run_bindings_factory=child_bindings,
                ),
            ),
        )
    )


@dataclass(frozen=True)
class Workspace:
    """[env] Host-owned source: Harness opens it lazily on first file/shell use."""

    connector: EnvironmentConnector
    provider_key = property(lambda s: s.connector.provider_key)
    environment_id = property(lambda s: s.connector.environment_id)
    descriptor = property(lambda s: s.connector.descriptor)
    state = property(lambda s: s.connector.state)

    async def ensure_ready(self) -> EnvironmentConnector:
        return self.connector


def sandbox_runtime() -> LocalEnvdProviderRuntime:
    """[env] One Envd daemon for the whole session; each Run opens its own sandboxed Session on it."""
    return LocalEnvdProviderRuntime(
        executable=resolve_a13n_envd_executable(ENVD_BIN if ENVD_BIN.exists() else None),
        allocate_private_runtime=TemporaryLocalEnvdRuntimeAllocator(),
        configuration=LocalEnvdLaunchConfiguration.model_validate({
            "sandbox": SANDBOX,  # launch ceiling: sessions can narrow it, never widen it
            "egress": {"mode": EGRESS},
            "full_control": True,  # any command allowed, but only inside the Seatbelt boundary
            "default_working_directory": str(WORKSPACE),
        }),
    )


def workspace(runtime: LocalEnvdProviderRuntime) -> Workspace:
    return Workspace(
        LOCAL_ENVD.execution_connector(
            {
                "session": {"working_directory": str(WORKSPACE), "sandbox": SANDBOX, "egress_mode": EGRESS},
                # refuse to run if the daemon can't actually provide this boundary (no silent weaker fallback)
                "expected_boundary": {"sandbox": SANDBOX, "egress": EGRESS},
            },
            environment_id="sre-sandbox",
            runtime=runtime,
        )
    )


async def stream_turn(agent, ask, runtime, mcp_client=None, **run_kwargs):
    """[stream] print tool activity + answer text live, return the terminal result."""
    env = EnvironmentMount(workspace(runtime), provider_root=str(WORKSPACE), working_directory=str(WORKSPACE))  # model sees /workspace
    caps = (k8s.mcp(mcp_client),) if mcp_client else ()  # [mcp] fresh projection per Run, shared connection
    async with agent.stream(*ask, environment=env, bindings=RunBindings.embedded(capabilities=caps), **run_kwargs) as stream:
        async for item in stream:
            if isinstance(item, HarnessRunResultEvent):
                return item.result
            ev = item.event if isinstance(item, HarnessEvent) else None
            if isinstance(ev, FunctionToolCallEvent):
                args = ev.part.args if isinstance(ev.part.args, str) else json.dumps(ev.part.args)
                print(f"\n{DIM}→ {ev.part.tool_name}({args[:160]}){RESET}", flush=True)
            elif isinstance(ev, FunctionToolResultEvent):
                print(f"{DIM}  ← {str(ev.part.content)[:200]}{RESET}", flush=True)
            elif isinstance(ev, PartStartEvent) and isinstance(ev.part, TextPart):
                print(ev.part.content, end="", flush=True)
            elif isinstance(ev, PartDeltaEvent) and isinstance(ev.delta, TextPartDelta):
                print(ev.delta.content_delta, end="", flush=True)


def collect_human_input(deferred, prompt=input):
    """[hitl] turn pending approvals/questions into correlated answers."""
    approvals, calls = {}, {}
    for req in deferred.approvals:
        c = req.target_call
        print(f"\n{YELLOW}{BOLD}APPROVAL NEEDED{RESET} {c.tool_name} {json.dumps(c.args_as_dict())}")
        approvals[c.tool_call_id] = prompt("  approve? [y/N] ").strip().lower() == "y"
    for req in deferred.calls:
        c = req.target_call
        answers = {}
        for q in c.args_as_dict()["questions"]:
            labels = [o["label"] for o in q["options"]]
            print(f"\n{YELLOW}{BOLD}QUESTION{RESET} {q['question']}")
            for i, o in enumerate(q["options"], 1):
                print(f"  {i}. {o['label']}: {o['description']}")
            pick = prompt("  choose # ").strip()
            answers[q["question"]] = labels[int(pick) - 1] if pick.isdigit() and 0 < int(pick) <= len(labels) else labels[0]
        calls[c.tool_call_id] = {"answers": answers}
    return deferred.build_results(approvals=approvals, calls=calls)


async def turn(agent, text, state, runtime, prompt=input, mcp_client=None):
    """One user turn = possibly several Runs (each approval/question suspends and resumes)."""
    result = await stream_turn(agent, (text,), runtime, mcp_client, previous_state=state)
    while result.status == "suspended":
        answers = collect_human_input(result.deferred, prompt)
        result = await stream_turn(
            agent, (), runtime, mcp_client, previous_state=result.state, resume_usage=True, deferred_resume=DeferredToolResume(result.deferred, answers)
        )
    result.raise_for_status()
    u = result.usage  # [usage]
    print(f"\n{DIM}[{u.requests} model calls, {u.input_tokens} in / {u.output_tokens} out tokens"
          f"{f', ${u.cost_usd:.4f}' if getattr(u, 'cost_usd', None) is not None else ''}]{RESET}")
    return result.state


def load_key() -> None:
    """OPENROUTER_API_KEY from the environment, else from ~/.openrouter.env (KEY=value). Never printed."""
    if os.environ.get("OPENROUTER_API_KEY"):
        return
    for line in Path("~/.openrouter.env").expanduser().read_text().splitlines():
        k, _, v = line.partition("=")
        if k.strip() == "OPENROUTER_API_KEY":
            os.environ["OPENROUTER_API_KEY"] = v.strip().strip("'\"")  # never printed
            return
    raise SystemExit("OPENROUTER_API_KEY not set and not found in ~/.openrouter.env")


async def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--resume", action="store_true", help="continue the saved thread")
    p.add_argument("--k8s", action="store_true", help="real kind cluster via kubernetes-mcp-server (run lab/lab.sh up)")
    p.add_argument("--model", default=os.environ.get("OPENROUTER_MODEL", "nvidia/nemotron-3-ultra-550b-a55b:free"))
    args = p.parse_args()
    load_key()

    state_file = STATE_FILE.with_name("state-k8s.json") if args.k8s else STATE_FILE  # different toolsets, separate threads
    state = HarnessState.model_validate_json(state_file.read_text()) if args.resume and state_file.exists() else None
    async with contextlib.AsyncExitStack() as stack:
        mcp_client = await stack.enter_async_context(k8s.mcp_client()) if args.k8s else None  # [mcp] one connection
        runtime = await stack.enter_async_context(sandbox_runtime())  # [env] daemon starts lazily on first use
        agent = build_agent(f"openrouter:{args.model}", mcp_client)
        mode = f"kind cluster {k8s.CONTEXT} via MCP (read-only)" if args.k8s else "simulated fleet"
        print(f"{BOLD}SRE on-call agent{RESET} ({args.model}, {mode}){' — resumed thread' if state else ''}. Ctrl-D to quit.")
        print(f"{DIM}Shell sandbox: Seatbelt, rw={WORKSPACE}, egress={EGRESS}{RESET}")
        print(f"{DIM}Try: '{'customers report checkout problems, investigate the shop namespace' if args.k8s else 'checkout is slow and customers see errors, investigate'}'{RESET}")
        while True:
            try:
                text = input(f"\n{BOLD}you>{RESET} ").strip()
            except EOFError:
                return
            if text:
                state = await turn(agent, text, state, runtime, mcp_client=mcp_client)
                state_file.write_text(state.model_dump_json())  # [state] completed turns only


if __name__ == "__main__":
    asyncio.run(main())
