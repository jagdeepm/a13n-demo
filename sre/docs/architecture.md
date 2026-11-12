# Architecture

## Components

```text
                ┌──────────────────────── your Mac ─────────────────────────────────────────┐
                │                                                                           │
  you ◀──────▶  │  sre_agent.py  (host process: a13n Harness agent loop)                    │
  terminal      │    │                                                                      │
                │    ├─ model calls ──────────────────────────────▶ OpenRouter ──▶ LLM      │
                │    │                                                                      │
                │    ├─ Python tools (in-process)                                           │
                │    │    simulated mode: infra.py  fleet_status, tail_logs, rollback ...   │
                │    │    k8s mode:       k8s.kubectl_fix ──(admin kubeconfig)──┐           │
                │    │                                                         │           │
                │    ├─ MCP (stdio) ──▶ bin/kubernetes-mcp-server ─(read-only)─┤           │
                │    │                   lab/mcp.toml, lab/readonly.kubeconfig  ▼           │
                │    │                                               kind cluster sre-lab   │
                │    │                                               (Docker container)     │
                │    │                                                                      │
                │    └─ Environment (EIP) ─▶ bin/a13n-envd ─▶ Seatbelt sandbox              │
                │                              file tools + shell, rw = ./workspace only,   │
                │                              network denied                               │
                └───────────────────────────────────────────────────────────────────────────┘
```

| Piece | Runs where | Purpose |
|---|---|---|
| `sre_agent.py` | host Python process | Builds the agent, runs the conversation loop, prints events, asks you for approvals |
| a13n Harness | library inside that process | The agent runtime: tool calls, permissions, skills, subagents, state, usage |
| OpenRouter | internet | Routes model requests to the selected LLM |
| `infra.py` | in-process | Simulated fleet, simulated mode only |
| `kubernetes-mcp-server` | child process (stdio) | Exposes Kubernetes reads as MCP tools; k8s mode only |
| `k8s.kubectl_fix` | in-process, runs `kubectl` | The only write path to the cluster |
| `a13n-envd` | child process | Sandbox daemon; the agent's file tools and shell run inside its Seatbelt session |
| kind `sre-lab` | Docker container | Local Kubernetes with the `shop` app |

## The two modes

`build_agent(model, mcp_client=None)` in `sre_agent.py` decides:

| | Simulated (`mcp_client=None`) | Kubernetes (`mcp_client` given) |
|---|---|---|
| Diagnostics | `fleet_status`, `recent_deploys`, `tail_logs` (Python) | MCP tools: `pods_*`, `resources_*`, `events_list`, ... |
| Remediation | `rollback`, `scale`, `restart` (Python, gated) | `kubectl_fix` (Python, gated) |
| Runbook | `incident-triage` skill | `k8s-triage` skill |
| Subagent tools | `tail_logs` | MCP `pods_list_in_namespace` + `pods_log` only |
| Saved thread | `state.json` | `state-k8s.json` |

Everything else (sandbox, approvals, streaming, skills, usage) is identical.

## One turn, step by step

What happens when you type a message (`turn()` in `sre_agent.py`):

1. **Start a Run.** `agent.stream(prompt, environment=..., bindings=..., previous_state=...)`.
   - `environment`: a fresh sandbox Session on the shared `a13n-envd` daemon, mounted at `/workspace`.
   - `bindings`: in k8s mode, a fresh MCP projection of the one shared MCP connection.
   - `previous_state`: the conversation so far, so the model remembers earlier turns.
2. **Model loop.** Harness sends instructions, tool schemas and history to the model. The model replies with
   tool calls; Harness runs them and sends results back, until the model answers in text.
3. **Streaming.** Every tool call and result is printed as it happens (`→ tool(args)` / `← result`).
4. **Suspension.** If the model calls a tool whose permission is `ask` (any remediation, any shell command),
   Harness does not run it. The Run ends with `status="suspended"` and a list of pending approvals.
5. **Human decision.** `collect_human_input()` shows each pending call and asks `approve? [y/N]`.
   If the model used `ask_user_question`, you pick an option instead.
6. **Resume.** A *new* Run starts with `previous_state` + `DeferredToolResume(answers)`. Approved calls run,
   denied ones return a denial to the model. Steps 2-6 repeat until the model finishes.
7. **Report.** Token usage (accumulated across the resumed Runs) is printed; state is saved to disk.

```text
Run 1: model ─▶ tools ─▶ model ─▶ kubectl_fix?  ── suspended ──┐
                                                              you: y
Run 2: (resume) kubectl_fix runs ─▶ model ─▶ verify tools ─▶ answer ── completed, state saved
```

## Lifetimes

| Resource | Created | Closed |
|---|---|---|
| MCP connection (`k8s.mcp_client()`) | once per session, in `main()` | on exit |
| Sandbox daemon (`sandbox_runtime()`) | once per session, started on first file/shell use | on exit |
| Sandbox Session, MCP projection | once per Run | when the Run ends |
| Conversation (`HarnessState`) | first turn | saved after each completed turn; `--resume` loads it |

The connection and the daemon are expensive, so the session shares them. Each Run gets its own lightweight view
of them, which is how Harness keeps Runs independent.

## Kubernetes lab

See [lab/README.md](../lab/README.md). In short: namespace `shop` with `checkout-api` (2 replicas), `payments-worker`,
`frontend`, and `loadgen`. `loadgen` logs one line per request (`200 0.001s`, `000` = connection failed), which is
the "customer view" the agent reads. A `ServiceLevelObjective` CRD holds each service's SLO, owner and
remediation notes, so the agent has a custom resource to discover.
