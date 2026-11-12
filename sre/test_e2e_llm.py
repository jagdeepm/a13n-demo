"""Real-model E2E on the kind lab: inject a failure, let the LLM investigate + fix, grade by cluster state.

Run: uv run test_e2e_llm.py [--model <openrouter-id>] [scenario ...]   (default: $OPENROUTER_MODEL or free Nemotron)
Key: OPENROUTER_API_KEY from env, else ~/.openrouter.env. Approvals are auto-granted (lab only) and logged.
Grading never trusts the agent's words: pods Ready, rollouts complete, Service selects pods, loadgen all 200.
"""

import argparse
import asyncio
import os
import subprocess
import time

import k8s
from sre_agent import build_agent, load_key, sandbox_runtime, turn

LAB = str(k8s.HERE / "lab" / "lab.sh")
KUBECTL = ["kubectl", "--context", k8s.CONTEXT, "-n", "shop"]
PROMPT = "Customers report checkout problems. Investigate the shop namespace and fix the root cause."


def sh(*cmd, check=True) -> str:
    return subprocess.run(cmd, capture_output=True, text=True, check=check).stdout


def cluster_healthy() -> tuple[bool, str]:
    """Ground truth, checked with admin kubectl (not the agent's tools)."""
    for d in ("checkout-api", "payments-worker", "frontend", "loadgen"):
        r = subprocess.run([*KUBECTL, "rollout", "status", f"deploy/{d}", "--timeout=90s"], capture_output=True, text=True)
        if r.returncode:
            return False, f"rollout of {d} not complete"
    bad = [ln for ln in sh(*KUBECTL, "get", "pods", "--no-headers").splitlines()
           if (cols := ln.split())[2] != "Running" or cols[1].split("/")[0] != cols[1].split("/")[1]]
    if bad:
        return False, f"unhealthy pods: {bad}"
    if sh(*KUBECTL, "get", "svc", "checkout-api", "-o", "jsonpath={.spec.selector.app}") != "checkout-api":
        return False, "Service selector still wrong"
    time.sleep(12)  # let loadgen produce fresh lines after the fix
    codes = [ln.split()[5] for ln in sh(*KUBECTL, "logs", "deploy/loadgen", "--tail=10").splitlines()]
    if any(c != "200" for c in codes):
        return False, f"loadgen still failing: {codes}"
    return True, "all rollouts complete, pods Ready, selector OK, loadgen 10/10 200"


async def run_scenario(name: str, model: str) -> dict:
    sh(LAB, "reset")
    sh(LAB, "break", name)
    print(f"\n{'=' * 70}\nSCENARIO {name}: injected, waiting 45s for symptoms...", flush=True)
    await asyncio.sleep(45)

    approvals: list[str] = []

    def auto_approve(question: str) -> str:  # stands in for the human; every decision is recorded
        approvals.append(question.strip())
        return "y" if "approve" in question else "1"

    started = time.monotonic()
    async with k8s.mcp_client() as client, sandbox_runtime() as runtime:
        agent = build_agent(f"openrouter:{model}", client)
        try:
            state = await turn(agent, PROMPT, None, runtime, prompt=auto_approve, mcp_client=client)
            error = None
        except Exception as e:  # budget exhausted, model error... still grade the cluster
            state, error = None, f"{type(e).__name__}: {e}"
    elapsed = time.monotonic() - started

    ok, why = cluster_healthy()
    result = {"scenario": name, "passed": ok, "detail": why, "seconds": round(elapsed), "approvals": len(approvals),
              "error": error, "thread": state.thread_id if state else None}
    print(f"\nRESULT {name}: {'PASS' if ok else 'FAIL'} — {why} ({result['seconds']}s, {len(approvals)} approvals)"
          + (f"\n  run error: {error}" if error else ""), flush=True)
    sh(LAB, "reset")
    return result


async def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--model", default=os.environ.get("OPENROUTER_MODEL", "nvidia/nemotron-3-ultra-550b-a55b:free"))
    p.add_argument("scenarios", nargs="*", default=["no-endpoints", "crashloop", "oom"])
    args = p.parse_args()
    load_key()
    results = [await run_scenario(s, args.model) for s in args.scenarios]
    print(f"\n{'=' * 70}\nSUMMARY ({args.model})")
    for r in results:
        print(f"  {'PASS' if r['passed'] else 'FAIL'}  {r['scenario']:13} {r['seconds']:>4}s  {r['detail']}")
    print(f"  {sum(r['passed'] for r in results)}/{len(results)} passed")


asyncio.run(main())
