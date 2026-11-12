# a13n-demo

Learning demos for [a13n Agent Foundation](https://github.com/converge-ai-labs/agent-foundation) (Harness `0.33.0`).

| Path | What |
|---|---|
| [`demo.py`](demo.py) | Smallest possible agent: one OpenRouter model, one tool. `uv run demo.py` |
| [`sre/`](sre/README.md) | SRE agent: runbooks as skills, approvals, sandboxed shell, subagent, Kubernetes via a read-only MCP server, a kind lab with 7 failure scenarios, and tests |

Start with [sre/README.md](sre/README.md).

CI ([.github/workflows/ci.yml](.github/workflows/ci.yml)) runs `mise run check`: gitleaks over the full history,
a forbidden-files guard (kubeconfigs, `.env`, saved state, binaries), shellcheck, kubeconform, ruff, and `uv lock --check`.
