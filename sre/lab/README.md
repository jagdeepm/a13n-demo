# kind lab for the SRE agent

```
                 read-only ServiceAccount (view + nodes/metrics/SLO CRD, no secrets)
 sre_agent.py ──stdio MCP──▶ kubernetes-mcp-server (read_only=true, toolsets=[core]) ──▶ kind-sre-lab
      │                                                                                   ▲
      └── kubectl_fix (approval every call, 6 verbs, ns=shop, your admin context) ─────────┘
```

## Setup (once)

From the project root (see the main [README](../README.md) for the full walkthrough):

```bash
mise run setup    # downloads + verifies bin/kubernetes-mcp-server and bin/a13n-envd
mise run lab:up   # cluster, metrics-server, shop app, read-only identity, lab/mcp.toml
```

## Practise

```bash
mise run lab:break crashloop      # or: bad-image oom pending no-endpoints errors unready
mise run agent:k8s
# you> customers report checkout problems, investigate the shop namespace
mise run lab:reset                # back to healthy
```

| Scenario | Symptom the agent should find | Expected fix |
|---|---|---|
| `crashloop` | new checkout-api pod CrashLoopBackOff, log: invalid `--port` | `rollout_undo checkout-api` |
| `bad-image` | ImagePullBackOff, tag `6.17.0-hotfix` doesn't exist | `rollout_undo checkout-api` |
| `oom` | payments-worker OOMKilled / exit 137, 64Mi limit | `rollout_undo payments-worker` (or raise limit) |
| `pending` | frontend new pod Pending, FailedScheduling Insufficient cpu | `rollout_undo frontend` |
| `no-endpoints` | all pods healthy, loadgen `000`; Service selector `app=checkout` ≠ pod label | `set_service_selector checkout-api checkout-api` |
| `errors` | all pods healthy, loadgen ~1/3 `4xx`/`5xx` | `rollout_undo checkout-api` |
| `unready` | frontend Running 0/1 Ready, readiness probe failing | `rollout_undo frontend` |

`no-endpoints` and `errors` are the good tests: every pod looks fine, so the agent must read the customer
view (loadgen logs) and compare the Service selector with the pod labels. In the rollout scenarios the old
ReplicaSet keeps serving (loadgen stays 200), so the agent should report "stuck rollout, no customer impact yet".

Checks: `mise run test` (simulated), `mise run test:k8s` (live, scripted model), `mise run test:e2e` (real model).
Tear down: `mise run lab:down`. The read-only token lasts 24h; `mise run lab:up` again refreshes it.
