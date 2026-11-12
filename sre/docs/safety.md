# Safety model

The agent is assumed to be capable of mistakes (and its inputs, such as logs, could contain hostile text).
Protection comes from layers that don't depend on the model behaving well.

## Layers

| Layer | What it stops | Where |
|---|---|---|
| **Approvals** | Any remediation or shell command without a human `y` | `ToolPermissionsCapability` in `sre_agent.py` |
| **Narrow fix tool** | Arbitrary kubectl: only 6 actions, fixed namespace and context, validated names/values, no shell | `k8s.kubectl_fix` |
| **Read-only cluster identity** | The MCP server writing, deleting, exec-ing, or reading Secrets | `lab/rbac.yaml`, `lab/mcp.toml` |
| **Shell sandbox** | Shell commands reading/writing outside `workspace/` or using the network | `a13n-envd` restricted Seatbelt session |
| **Budgets** | Runaway loops and cost | `UsageLimits`, `max_tokens` |

## Two cluster identities

| | Reads (MCP server) | Writes (`kubectl_fix`) |
|---|---|---|
| Credentials | ServiceAccount token in `lab/readonly.kubeconfig` (24h) | your admin `kind-sre-lab` context |
| RBAC | built-in `view` (no Secrets) + nodes, `metrics.k8s.io`, SLO CRD, CRD list | cluster admin |
| Server-side | `read_only = true` (only `readOnlyHint` tools registered), `toolsets = ["core"]`, Secrets denied | n/a |
| Gate | none needed | human approval on every call |

Details, the full tool list and live verification: [mcp.md](mcp.md).

Verified: `kubectl --kubeconfig lab/readonly.kubeconfig auth can-i delete pods -n shop` → `no`;
listing Secrets → `Forbidden`. Deliberately not granted: `nodes/proxy` (the kubelet API, which can reach exec).

The write path uses your admin context, so the approval prompt **is** the safeguard there. For a real cluster,
give `kubectl_fix` its own ServiceAccount limited to `patch` on Deployments/Services in one namespace.

## Shell sandbox

Every file tool and `shell_exec` runs inside an `a13n-envd` Session with:

- **Seatbelt restricted mode** (`sandbox-exec`): read-write grant on `workspace/` only, a minimal read-only system
  view, private HOME/TMP.
- **Network `deny`** (`SRE_EGRESS=deny`).
- `expected_boundary` set, so if the daemon can't provide this boundary the Session fails instead of
  falling back to something weaker.

`test_offline.py` proves it each run, from inside the sandbox:

```text
READ_OUTSIDE_BLOCKED    head ../sre_agent.py      (works unsandboxed)
WRITE_OUTSIDE_BLOCKED   touch ../escape.txt
NET_BLOCKED             nc -z 1.1.1.1 443         (works unsandboxed, and with SRE_EGRESS=inherit)
WRITE_WORKSPACE_OK      echo hi > note.txt
```

### What the sandbox does not cover

- **Host-side tools.** Python tools (`kubectl_fix`, `infra.*`) and the MCP server run in the host process with
  your user's rights. The sandbox only wraps the model's file and shell tools.
- **macOS limits.** No process namespaces and no privilege-gain blocking (`privilege_gain_blocked=false`);
  no destination allowlist (network is all-or-nothing). The Linux backend (bubblewrap + controlled egress with
  brokered secrets) is stronger.
- **HTTPS clients** fail even with `SRE_EGRESS=inherit`: the system view hides `/etc/ssl/openssl.cnf`.

## Approvals in tests

`test_e2e_llm.py` auto-approves fixes so it can run unattended against the **local lab**. Every approval is
counted in the result. Never point it at a real cluster.

## Secrets

- The OpenRouter key is read from the environment or `~/.openrouter.env` (mode 600) and never printed or logged.
- `lab/readonly.kubeconfig` holds a cluster token; it's created mode 600 and git-ignored.
- Free OpenRouter models may log prompts. Prompts here contain lab cluster state only; don't use free models
  with real infrastructure data.
