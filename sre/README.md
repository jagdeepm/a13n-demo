# SRE on-call agent (a13n Harness demo)

A learning project: an AI on-call SRE that investigates incidents, asks a human before changing anything,
and fixes them. Built on [a13n Harness](https://github.com/converge-ai-labs/agent-foundation) (agent runtime),
[OpenRouter](https://openrouter.ai) (models), [kubernetes-mcp-server](https://github.com/containers/kubernetes-mcp-server)
(cluster access) and [kind](https://kind.sigs.k8s.io) (local Kubernetes).

It runs in two modes:

| Mode | Command | Infra | Needs |
|---|---|---|---|
| **Simulated** | `mise run agent` | In-process fake fleet with one planted incident ([`infra.py`](infra.py)) | API key |
| **Kubernetes** | `mise run agent:k8s` | Real local kind cluster, 7 injectable failures ([`lab/`](lab/README.md)) | API key, Docker |

```text
you> customers report checkout problems, investigate the shop namespace
→ view(/workspace/.agents/skills/k8s-triage/SKILL.md)          reads its runbook
→ resources_list(ServiceLevelObjective)                         reads SLOs (a CRD) via MCP
→ pods_list_in_namespace(shop) → events_list → pods_log(loadgen) gathers evidence
→ delegate(log-analyst, ...)                                    subagent with log-only access
APPROVAL NEEDED kubectl_fix {"action": "rollout_undo", "name": "checkout-api"}
  approve? [y/N] y
→ pods_list_in_namespace(shop) → pods_log(loadgen)              verifies the fix
```

## Prerequisites

- macOS (Apple Silicon or Intel): the shell sandbox uses macOS Seatbelt
- [mise](https://mise.jdx.dev/getting-started.html): `brew install mise` and [activate it in your shell](https://mise.jdx.dev/getting-started.html#activate-mise)
- [Docker Desktop](https://www.docker.com/products/docker-desktop/) running (Kubernetes mode only)
- An [OpenRouter API key](https://openrouter.ai/keys). Free models work; see [Choosing a model](#choosing-a-model)
- GitHub CLI login (used to download two release binaries): `gh auth login`

## Setup, step by step

```bash
# 1. Go to the project and let mise use its config
cd ~/dev/a13n-demo/sre
mise trust

# 2. Install the pinned tools: Python 3.13, uv, kind, kubectl, gh
mise install

# 3. Install Python dependencies and download the two native binaries (checksum-verified):
#    bin/a13n-envd (sandbox daemon) and bin/kubernetes-mcp-server
mise run setup

# 4. Save your OpenRouter key where the agent looks for it (file readable only by you)
echo 'OPENROUTER_API_KEY=sk-or-...' > ~/.openrouter.env && chmod 600 ~/.openrouter.env

# 5. Check everything is in place
mise run doctor

# 6. Verify the wiring without spending anything (scripted model, no key needed)
mise run test
```

### Simulated mode

```bash
mise run agent
# you> checkout is slow and customers see errors, investigate
```

The planted incident: `checkout-api v2.4.1` shrank its DB connection pool from 50 to 5. A good run reads
the runbook, finds the deploy, confirms "pool exhausted" in the logs and asks to roll back.

### Kubernetes mode

```bash
mise run lab:up                   # ~2 min: kind cluster, metrics-server, shop app, read-only identity
mise run test:k8s                 # optional: live wiring check, scripted model, no key needed
mise run lab:break no-endpoints   # inject a failure (list: mise run lab:break)
mise run agent:k8s
# you> customers report checkout problems, investigate the shop namespace
mise run lab:reset                # back to healthy
```

Scenarios: `crashloop`, `bad-image`, `oom`, `pending`, `no-endpoints`, `errors`, `unready`.
Expected diagnosis and fix for each: [lab/README.md](lab/README.md).

### Real-model end-to-end test

```bash
mise run test:e2e                                  # 3 default scenarios
mise run test:e2e -- crashloop errors unready      # pick scenarios
mise run test:e2e -- --model anthropic/claude-sonnet-5.5 oom
```

Each scenario: inject the failure, let the model investigate, auto-approve its fix (lab only, logged),
then grade by **cluster state**: rollouts complete, pods Ready, Service selector correct, loadgen 10/10 HTTP 200.

### Clean up

```bash
mise run lab:down       # delete the kind cluster and generated files
```

## Choosing a model

Default: `nvidia/nemotron-3-ultra-550b-a55b:free` (set in [`mise.toml`](mise.toml) as `OPENROUTER_MODEL`).
Override per run with `--model`, e.g. `mise run agent:k8s -- --model anthropic/claude-sonnet-5.5`.

Measured on this lab (Nov 2026):

| Model | Scenarios passed | Cost / scenario | Notes |
|---|---|---|---|
| `nvidia/nemotron-3-ultra-550b-a55b:free` | 7/7 | $0 | 11-21 model calls; free tier is rate-limited |
| `anthropic/claude-sonnet-5.5` | 2/2 graded (`crashloop` ran out of credit) | ~$0.45 | 7-8 model calls, ~200k input tokens |

The model must support tool calling. List free ones: <https://openrouter.ai/models?variant=free>.

## mise tasks

| Task | What it does |
|---|---|
| `setup` | `uv sync` + download/verify `a13n-envd` and `kubernetes-mcp-server` into `bin/` |
| `doctor` | Check tools, binaries, Docker, API key, cluster |
| `agent` / `agent:k8s` | Interactive agent (simulated / kind) |
| `lab:up` `lab:break <s>` `lab:reset` `lab:status` `lab:down` | Manage the kind lab |
| `test` | Offline: scripted model + simulated fleet + sandbox escape checks |
| `test:k8s` | Live MCP wiring against kind, scripted model |
| `test:e2e` | Real model, graded by cluster state |

Extra arguments go after `--`: `mise run agent:k8s -- --resume`.

## Project layout

```text
sre/
├── sre_agent.py          agent: builder, sandbox, streaming, approvals, CLI
├── infra.py              simulated fleet + its tools (simulated mode)
├── k8s.py                MCP client + kubectl_fix (Kubernetes mode)
├── workspace/            the ONLY directory the agent's shell can touch
│   └── .agents/skills/   runbooks: incident-triage, k8s-triage, postmortem
├── lab/                  kind lab: app.yaml, rbac.yaml, lab.sh, README.md
├── scripts/              fetch-binaries.sh
├── test_offline.py       scripted model, no key, no cluster
├── test_k8s.py           scripted model against kind
├── test_e2e_llm.py       real model against kind
├── docs/                 how it works (below)
└── mise.toml             tools, env, tasks
```

## Documentation

1. [Architecture](docs/architecture.md): components, the two modes, how one turn flows
2. [Harness features](docs/harness-features.md): every a13n Harness feature used, where it is in the code
3. [Safety model](docs/safety.md): approvals, sandbox, read-only identity, what is and isn't protected
4. [Kubernetes MCP server](docs/mcp.md): setup, tools exposed, how the agent is kept from changing the cluster
5. [Testing](docs/testing.md): the three test levels, how the scripted model works, E2E grading
6. [Troubleshooting](docs/troubleshooting.md): problems hit while building this, and fixes
7. [Production](docs/production.md): questions to answer before a real cluster (auth, private networks, approvals, ...)
8. [Lab scenarios](lab/README.md): the seven failures and their expected fixes
