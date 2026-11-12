# Kubernetes MCP server

How the agent reads the cluster through MCP, which features are used, and why it can't change anything that way.

## Setup

```text
sre_agent.py ──stdio (JSON-RPC)──▶ bin/kubernetes-mcp-server --config lab/mcp.toml
                                        │  kubeconfig: lab/readonly.kubeconfig
                                        ▼  (ServiceAccount sre-system/sre-agent, token 24h)
                                   kind cluster sre-lab  ──RBAC: view + extra reads──▶ allow / Forbidden
```

| Step | Who | What |
|---|---|---|
| 1 | `mise run setup` → `scripts/fetch-binaries.sh` | Downloads [containers/kubernetes-mcp-server](https://github.com/containers/kubernetes-mcp-server) `v0.1.2` (a single Go binary) and checks its SHA256 |
| 2 | `mise run lab:up` → `lab/rbac.yaml` | Creates the `sre-agent` ServiceAccount and binds it to read-only roles |
| 3 | `lab:up` | Mints a 24h token (`kubectl create token`), writes `lab/readonly.kubeconfig` (mode 600) |
| 4 | `lab:up` | Writes `lab/mcp.toml` (below) |
| 5 | `sre_agent.py --k8s` | `k8s.mcp_client()` starts the binary as a child process and talks MCP over stdin/stdout |

No port is opened: the server is a stdio child of the agent and exits with it.

### `lab/mcp.toml` (generated)

```toml
kubeconfig = ".../lab/readonly.kubeconfig"   # the read-only identity, not your admin context
read_only = true              # register only tools annotated readOnlyHint=true
toolsets = ["core"]           # pods, generic resources (incl. CRDs), events, nodes, top; no helm etc.
denied_resources = [{group = "", version = "v1", kind = "Secret"}]   # refused even if RBAC allowed it
log_file = ".../lab/mcp.log"  # every call the server handles
```

v0.1.2 is configured by TOML only (`--config`); the older CLI flags (`--read-only`, ...) are gone.

## Tools the agent gets

With the config above, the server registers 14 tools, all `readOnlyHint=true, destructiveHint=false`:

| Tool | Used for |
|---|---|
| `pods_list`, `pods_list_in_namespace`, `pods_get` | Pod status, restarts, reasons (CrashLoopBackOff, OOMKilled, ...) |
| `pods_log` | Container logs (incl. `previous`); the loadgen "customer view" |
| `pods_top`, `nodes_top` | CPU/memory from metrics-server |
| `resources_list`, `resources_get` | Any kind by apiVersion/kind: Deployments, ReplicaSets, Services, Endpoints, the `ServiceLevelObjective` CRD |
| `api_resources_list` | Discover what kinds exist (finds the CRD) |
| `events_list` | Scheduling failures, image pull errors, probe failures |
| `namespaces_list`, `projects_list` | Discovery (`projects_list` is for OpenShift; empty here) |
| `nodes_log`, `nodes_stats_summary` | Registered, but need `nodes/proxy`, which isn't granted, so they return Forbidden |

Not registered because they aren't read-only: `pods_delete`, `pods_exec`, `pods_run`, `resources_create_or_update`,
`resources_delete`, `resources_scale`, ... If the model calls one anyway, the server answers `unknown tool`.

## How Harness uses it

```python
client = Client(StdioTransport("bin/kubernetes-mcp-server", ["--config", "lab/mcp.toml"]))  # k8s.mcp_client()

async with client:                                   # one connection per session (main())
    ...
    MCP(id="k8s", local=MCPToolset(client, id="k8s", cache_tools=False),          # k8s.mcp()
        allowed_tools=None)                          # parent: all 14
    RunBindings.embedded(capabilities=(that_mcp,))   # fresh projection per Run (stream_turn)
```

| Harness feature | Where | Effect |
|---|---|---|
| `MCP` capability over a host-owned client | `k8s.mcp()` | The expensive process + handshake happen once; each Run gets a cheap view |
| Per-Run `RunBindings` | `stream_turn()` | MCP tools are bound to a Run, not baked into the agent definition |
| Stable tool IDs `mcp/k8s/<tool>` | automatic | Permission rules can target MCP tools like any other |
| `allowed_tools` | subagent `run_bindings_factory` | The `log-analyst` child sees only `pods_list_in_namespace` and `pods_log` |
| Streaming events | `stream_turn()` | Every MCP call and result is printed as `→ pods_log(...)` / `← ...` |

## How we stop the agent changing the cluster

Each layer below would block a write on its own. None of them relies on the model following instructions.

| # | Layer | Enforced by | Stops |
|---|---|---|---|
| 1 | **Tool not offered** | `read_only = true` in the MCP server | Write/exec tools are never registered. The model can't call what doesn't exist |
| 2 | **Identity can't write** | Kubernetes RBAC on the `sre-agent` ServiceAccount (`view` + get/list on nodes, metrics, SLO CRD, CRDs) | Even a buggy or modified server gets `Forbidden` from the API server on any create/update/patch/delete/exec |
| 3 | **Secrets hidden twice** | `view` excludes Secrets; `denied_resources` in `mcp.toml` | Reading credentials out of the cluster |
| 4 | **No kubelet proxy** | `nodes/proxy` deliberately not granted | The kubelet API route to exec/logs on nodes |
| 5 | **Short-lived credential** | 24h token, kubeconfig mode 600, git-ignored | A leaked kubeconfig staying useful |
| 6 | **Subagent narrowed** | `allowed_tools` | The log-analyst can only list pods and read logs |
| 7 | **Single, gated write path** | `kubectl_fix`: 6 fixed actions, namespace `shop` pinned, validated args, argv (no shell), permission `ask` | Every real change is a named action a human approves with `y` |

Layer 2 is the one that matters most: the API server enforces it, whatever process holds the token.
Layers 1 and 3 keep the model from wasting turns on tools that would fail anyway.

### Verified (Nov 2026, kind `sre-lab`)

```text
$ kubectl --kubeconfig lab/readonly.kubeconfig auth can-i <verb> <resource> -n shop
delete pods            no          get secrets            no
create pods            no          get nodes --subresource=proxy   no
patch deployments      no          create pods --subresource=exec  no
get pods/log           yes         list servicelevelobjectives     yes

MCP: list_tools → 14 tools, all readOnlyHint=true
     pods_delete / pods_exec / resources_create_or_update → unknown tool
     resources_list kind=Secret → "resource not allowed"
     nodes_log / nodes_stats_summary → forbidden
```

`mise run test:k8s` asserts the first three MCP checks on every run, so a config change that loosens them fails
the test.

Note: `kubectl auth can-i get nodes/proxy` answers `yes` because kubectl reads it as "the node named proxy".
Use `--subresource=proxy`.

## What this does not cover

- **The write path uses your admin context.** `kubectl_fix` runs with `kind-sre-lab` (cluster-admin). Its safety
  is the narrow action list plus the approval prompt. For a real cluster, give it its own ServiceAccount with
  only `patch` on `deployments`, `deployments/scale`, `services` in one namespace.
- **Reads can still leak data.** Anything the identity can read (ConfigMaps, env vars in pod specs, logs) goes to
  the model provider. Free OpenRouter models may log prompts, so use this with lab data only.
- **Prompt injection via logs.** A log line could tell the model to "roll back everything". The model can only
  *propose* a fix; you see the exact call before approving. Read it.
- **Load.** Read-only calls can still be heavy (`pods_list` across a big cluster). `UsageLimits(request_limit=40)`
  caps the number of calls, not their size.
- **Auto-approval in `test_e2e_llm.py`** bypasses layer 7 for unattended runs. Lab only.

## Pointing it at a real cluster

1. Create a dedicated read-only ServiceAccount (copy `lab/rbac.yaml`; consider namespace-scoped `RoleBinding`s
   instead of cluster-wide).
2. Generate a kubeconfig with a short-lived token for it; keep `read_only = true` and `denied_resources`.
3. Add more `denied_resources` for anything sensitive (e.g. ConfigMaps holding credentials).
4. Replace `kubectl_fix`'s admin context with a write ServiceAccount limited to the patch verbs it needs.
5. Use a paid model with a no-logging policy.
6. Never run `test_e2e_llm.py` against it.
