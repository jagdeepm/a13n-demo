# Troubleshooting

Problems met while building this demo, with their fixes. Start with `mise run doctor`.

## Setup

**`mise run setup` fails downloading binaries**
It uses `gh`. Run `gh auth login`. The script verifies checksums and stops on a mismatch; don't bypass it.

**`a13n-envd` version mismatch**
The daemon must exactly match the Python `a13n-envd-client` that `uv sync` installed. Delete `bin/a13n-envd`
and rerun `mise run setup`; the script reads the client version and fetches the matching daemon.

**macOS blocks a binary ("unidentified developer")**
`xattr -d com.apple.quarantine bin/kubernetes-mcp-server` (or `bin/a13n-envd`).

**Wrong Python in `.venv`**
`mise.toml` sets `UV_PYTHON=3.13`. If `.venv` was created earlier with another version: `rm -rf .venv && uv sync`.

## Kind lab

**`lab:up` fails with `no matches for kind "ServiceLevelObjective"`**
A CRD must be Established before its objects can be created. `lab.sh up` waits and re-applies; if you apply
`app.yaml` by hand, run it twice.

**MCP calls fail with `Unauthorized` after a day**
The read-only token in `lab/readonly.kubeconfig` lasts 24h. `mise run lab:up` refreshes it (safe to re-run).

**`pods_top` says metrics not available**
metrics-server needs about a minute after `lab:up`.

**Shell loops in zsh don't split `$K`**
`K="kubectl --context x"; $K get pods` fails in zsh (`command not found: kubectl --context x`): zsh doesn't
word-split variables. `lab.sh` runs under bash for this reason; use `bash -c '...'` for ad-hoc loops.

## Sandbox

**`environment_denied` on shell commands**
The shell profile Harness requests is named `default`. Already configured in `sandbox_runtime()`.

**`path_outside_mounts` / `path_not_found` for `/workspace/...`**
The model sees `/workspace`; `EnvironmentMount(provider_root=WORKSPACE, working_directory=WORKSPACE)` maps it
onto the real directory. Without `working_directory`, relative shell paths resolve elsewhere.

**`curl` fails inside the sandbox even with `SRE_EGRESS=inherit`**
Not the network: the restricted system view hides `/etc/ssl/openssl.cnf`, so macOS curl fails at startup.
Test egress with `nc -z -G 3 1.1.1.1 443` instead.

## Models / OpenRouter

**`ModelQuotaExceededError`, status 402**
Your OpenRouter balance can't cover the request's worst case (input + max output). Check:
`curl -s https://openrouter.ai/api/v1/credits -H "Authorization: Bearer $OPENROUTER_API_KEY"`.
Top up, use a `:free` model, or lower `max_tokens` in `build_agent`.

**`ModelAuthenticationError`, status 401**
Key missing or wrong. `mise run doctor` checks it's present; regenerate at <https://openrouter.ai/keys>.

**Free model is slow or returns 429**
Free models are rate-limited and sometimes overloaded. Retry, or switch: `--model google/gemma-4-31b-it:free`.

**`No endpoints found matching your data policy`**
Free providers may log prompts; allow that in OpenRouter privacy settings, or use a paid model.

**Agent loops or repeats refused calls**
Make tool errors say exactly what's expected (see `kubectl_fix`'s refusal messages). Models correct themselves
from specific errors; they guess from vague ones.
