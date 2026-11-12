# a13n Harness features used

Each feature is tagged in the code; `grep -n "\[mcp\]" *.py` finds it. Docs links are to the Harness repo.

## [tools] Python functions as tools

```python
def tail_logs(service: Service, lines: int = 5) -> list[str]:
    """Return the last `lines` log lines for a service."""

Capability(id="diagnostics", tools=[infra.fleet_status, infra.recent_deploys, infra.tail_logs])
```

The signature becomes the JSON schema the model sees (`Service` is a `Literal`, so the model can only pass
known names). The docstring is the description. A `Capability` groups tools; its `id` becomes part of each
tool's stable ID: `tool/diagnostics/tail_logs`. Docs: `docs/a13n-harness/tools-and-dependencies.md`.

## [permissions] Tool permissions

```python
ToolPermissionsCapability(ToolPermissions(rules={
    "tool/remediation/*": "ask",          # every fix needs a human
    "environment.shell_exec": "ask",      # every shell command needs a human
}))
```

Modes: `allow`, `ask`, `deny`, `review` (a separate model rates risk). Everything not matched is allowed, so the
read-only diagnostics and MCP tools run freely. Docs: `managed-tools.md#select-tool-permissions`.

## [hitl] Human in the loop

Two kinds of pause, both returned as `result.status == "suspended"`:

- **Approvals** (`result.deferred.approvals`): from `ask` permissions.
- **Questions** (`result.deferred.calls`): the model called `ask_user_question` (`UserInteractionCapability`).

`collect_human_input()` turns them into answers; `deferred.build_results(approvals=..., calls=...)` ties each
answer to its exact tool-call ID, and the next Run receives them via `DeferredToolResume`. Nothing waits in
memory: you could save the suspended state and approve hours later. Docs: `human-in-the-loop.md`.

## [skills] Runbooks as Skills

`workspace/.agents/skills/<name>/SKILL.md` with `name`/`description` frontmatter. `SkillsCapability()` lists
them in the model's instructions; the model reads the full procedure with the `view` file tool when it needs it.
Edit a runbook and the agent's behaviour changes without code changes. Docs: `skills.md`.

## [env] Environment: files and shell in a sandbox

```python
LocalEnvdProviderRuntime(executable=..., configuration=LocalEnvdLaunchConfiguration(
    sandbox={"mode": "restricted", "grants": [{"path": WORKSPACE, "access": "read_write"}]},
    egress={"mode": "deny"}, full_control=True))
EnvironmentMount(source, provider_root=str(WORKSPACE), working_directory=str(WORKSPACE))
```

`DynamicEnvironmentCapability` gives the model `view`, `ls`, `grep`, `write`, `edit`, `shell_exec`, ...
`EnvironmentMount(provider_root=...)` maps the model's `/workspace` path onto the real directory.
`expected_boundary` makes a Session fail instead of silently running with weaker isolation.
See [safety.md](safety.md). Docs: `environments.md`, `docs/a13n-envd/isolation.md`.

## [subagent] Delegation

```python
SubagentDefinition(name="log-analyst", description="Summarize errors in one service's logs.",
                   agent=log_analyst, run_bindings_factory=child_bindings)
```

`SubagentCapability()` gives the parent `delegate(subagent, prompt)`. The child has its own instructions,
budget (`request_limit=6`) and tools. Children do **not** inherit the parent's MCP tools; `run_bindings_factory`
hands the log-analyst a narrower view of the same MCP connection (`allowed_tools=["pods_list_in_namespace",
"pods_log"]`), so it can read logs but can't remediate. Docs: `delegation-and-codeact.md`.

## [mcp] MCP tools

```python
client = Client(StdioTransport("bin/kubernetes-mcp-server", ["--config", "lab/mcp.toml"]))  # once
MCP(id="k8s", local=MCPToolset(client, id="k8s", cache_tools=False))                       # per Run
RunBindings.embedded(capabilities=(that_mcp,))
```

The host owns one connection for the session; each Run gets a fresh projection through `RunBindings`.
MCP tool IDs are `mcp/k8s/<tool>`, so the same permission rules can target them. Docs: `mcp.md`.
Setup and guardrails: [mcp.md](mcp.md).

## [stream] Streaming events

`agent.stream(...)` yields `HarnessEvent`s wrapping Pydantic AI events (`FunctionToolCallEvent`,
`FunctionToolResultEvent`, `PartStartEvent`/`PartDeltaEvent` for text) and ends with one
`HarnessRunResultEvent` carrying the result. Subagent events are forwarded too.

## [state] Saved conversations

`result.state.model_dump_json()` after each completed turn; `HarnessState.model_validate_json()` +
`previous_state=` on start. `--resume` continues the same thread after a restart. The state holds messages and
IDs, not live connections; those are rebuilt each session. Docs: `state-and-resume.md`.

## [usage] Budgets and cost

- `AgentSpec(usage_limits=UsageLimits(request_limit=40))`: hard cap on model calls per Run.
- `model_settings={"max_tokens": 4096}`: caps each reply (and what OpenRouter reserves credit for).
- `resume_usage=True` on resumed Runs so the printed totals cover the whole turn, not just the last Run.
- `result.usage`: requests, input/output tokens, cost when the model's price is known.

## Testing with a scripted model

`build_agent` accepts a native model object as well as a model string, so tests pass a Pydantic AI
`FunctionModel` that scripts the tool calls. See [testing.md](testing.md).
