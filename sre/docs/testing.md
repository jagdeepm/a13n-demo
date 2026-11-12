# Testing

Three levels, cheapest first. Run them in this order when you change something.

| Test | Model | Infra | Cost | Proves |
|---|---|---|---|---|
| `mise run test` | scripted | simulated fleet | free, ~10s | wiring: tools, permissions, approvals, skills, subagent, sandbox, state |
| `mise run test:k8s` | scripted | kind cluster | free, ~30s | MCP connection, read-only tools, subagent's narrowed MCP, a real `kubectl rollout undo` |
| `mise run test:e2e` | **real LLM** | kind cluster | free-$0.50 per scenario | the model actually diagnoses and fixes incidents |

## Scripted model (levels 1 and 2)

`build_agent()` accepts a model object instead of a model name. The tests pass Pydantic AI's `FunctionModel`:
a Python function that plays the model.

```python
SCRIPT = [
    ("view", {"file_path": "/workspace/.agents/skills/k8s-triage/SKILL.md"}),
    ("resources_list", {"apiVersion": "sre.demo.io/v1", "kind": "ServiceLevelObjective", "namespace": "shop"}),
    ("pods_list_in_namespace", {"namespace": "shop"}),
    ("delegate", {"subagent": "log-analyst", "prompt": "Analyze the loadgen pod logs in shop"}),
    ("kubectl_fix", {"action": "rollout_undo", "name": "checkout-api"}),
]

async def respond(messages, info):
    returns = tool_returns(messages)            # how many tools have answered so far
    if len(returns) < len(SCRIPT):
        name, args = SCRIPT[len(returns)]       # next scripted step
        assert name in {t.name for t in info.function_tools}   # Harness really offers this tool
        yield {0: DeltaToolCall(name=name, json_args=json.dumps(args))}
    else:
        yield "Rolled back checkout-api."
```

Everything except the model's decisions is real: Harness runs the tools, the MCP server queries the cluster,
the sandbox enforces its policy, approvals suspend and resume. The same function also plays the subagent:
when `delegate` is not among its tools, it knows it's the child, and asserts it was given only
`pods_list_in_namespace` and `pods_log`.

What it can't prove: whether a real model makes good decisions. That's level 3.

## Real-model E2E (level 3)

`test_e2e_llm.py`, per scenario:

1. `lab.sh reset`, `lab.sh break <scenario>`, wait 45s for symptoms.
2. Run one agent turn with the real model and the prompt
   *"Customers report checkout problems. Investigate the shop namespace and fix the root cause."*
3. Auto-approve its tool calls (lab only; the count is recorded).
4. **Grade by cluster state**, with admin `kubectl`, never the agent's own claims:
   all four rollouts complete, every pod Running and Ready, the Service selects `app=checkout-api`,
   and the last 10 loadgen requests are all HTTP 200.
5. `lab.sh reset`.

```bash
mise run test:e2e                                   # no-endpoints crashloop oom
mise run test:e2e -- bad-image pending errors unready
mise run test:e2e -- --model anthropic/claude-sonnet-5.5 crashloop
```

### Results (Nov 2026)

`nvidia/nemotron-3-ultra-550b-a55b:free`: **7/7**, each with one approved fix.

| Scenario | Time | Fix | Model calls |
|---|---|---|---|
| crashloop | 34s | rollout_undo checkout-api | 12 |
| no-endpoints | 32s | set_service_selector checkout-api | 12 |
| oom | 48s | rollout_undo payments-worker | 11 |
| bad-image | 42s | rollout_undo checkout-api | 11 |
| pending | 86s | rollout_undo frontend | 21 |
| errors | 78s | rollout_undo checkout-api | 19 |
| unready | 98s | rollout_undo frontend | 18 |

`anthropic/claude-sonnet-5.5`: `no-endpoints` and `oom` passed in 7-8 calls each (~200k input tokens,
~$0.45/scenario); `crashloop` was diagnosed correctly but ran out of OpenRouter credit before the fix.

### What E2E runs taught us

- Free Nemotron sent `app=checkout-api` as a selector value six times, costing six approvals.
  Fix: `kubectl_fix` now accepts that form and its refusals say exactly what's expected.
  Result: 7 approvals → 1, 67s → 32s.
- Kubernetes objects are verbose: 170k-560k input tokens per scenario. Most is ReplicaSet lists and
  Deployment YAML. Cheaper next step: steer the runbook toward label selectors, or trim tool output.

## Adding a scenario

1. Add a `case` line under `break)` in `lab/lab.sh` that injects the failure.
2. Add a row to the table in `lab/README.md` (symptom + expected fix).
3. If the fix needs a new action, add it to `kubectl_fix` in `k8s.py` (keep it narrow).
4. Make sure `cluster_healthy()` in `test_e2e_llm.py` detects the failure; extend it if not.
5. `mise run test:e2e -- <new-scenario>`.
