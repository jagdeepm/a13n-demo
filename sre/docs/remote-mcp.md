# Remote MCP server over HTTP with OAuth (lab)

The production pattern from [production.md §2.1](production.md#21-remote-mcp-server-option-2), running in kind:
the MCP server runs **inside the cluster**, the agent reaches it over HTTP, and every call carries an OAuth token
from Keycloak. Nothing in the cluster trusts the agent's laptop; it trusts the token.

```text
                         ┌─────────────────────────── kind cluster sre-lab ───────────────────────────┐
agent (sre_agent.py)     │                                                                             │
  │ 1. client_credentials│  Keycloak (ns keycloak, HTTPS, NodePort 30443)   realm sre                  │
  ├──── :18080 ──────────┼─▶  client sre-agent  → token aud=kubernetes-mcp-server                       │
  │                      │                                                                             │
  │ 2. MCP + Bearer      │  kubernetes-mcp-server (ns sre-system, SA with NO RBAC)                     │
  └──── :18081 ──────────┼─▶  require_oauth: verify iss/aud/signature via Keycloak JWKS → else 401     │
                         │      │ 3. same token (cluster_auth_mode=passthrough)                        │
                         │      ▼                                                                      │
                         │  kube-apiserver: AuthenticationConfiguration trusts Keycloak                │
                         │      username = oidc:service-account-sre-agent → RBAC view + extra reads    │
                         └─────────────────────────────────────────────────────────────────────────────┘
:18080 / :18081 are `kubectl port-forward`s standing in for the API gateway / load balancer.
```

## Run it

Needs the normal lab (`mise run lab:up`).

```bash
mise run lab:remote-up        # ~2 min first time: certs, API server OIDC config, Keycloak, MCP server
mise run lab:remote-forward   # terminal 2: keep running
mise run test:remote          # auth checks (no API key)
mise run agent:remote         # the agent, through the remote server
mise run lab:remote-down      # remove Keycloak + MCP server
```

Any existing test runs against the remote server if `lab/remote.env` is loaded, e.g.
`set -a; . lab/remote.env; set +a; uv run test_k8s.py`.

## What `lab:remote-up` does

| Step | What | Why |
|---|---|---|
| 1 | Creates a lab CA and a Keycloak certificate in `lab/remote-pki/` (git-ignored) | The API server only accepts `https` issuers. Python 3.13 also needs the Authority/Subject Key Identifier extensions |
| 2 | Writes `/etc/kubernetes/pki/oidc/auth-config.yaml` on the kind node and adds `--authentication-config` to the API server (one restart; later edits reload live) | The API server must accept Keycloak tokens, see [Why the API server must trust Keycloak](#why-the-api-server-must-trust-keycloak) |
| 3 | Random client secrets in Secret `keycloak/clients`; applies [`lab/remote-mcp.yaml`](../lab/remote-mcp.yaml) with the issuer URL filled in | Keycloak realm `sre` with clients `sre-agent` and `other-app`; MCP server; RBAC for the OIDC user |
| 4 | Writes `lab/remote.env` (mode 600, git-ignored) | Agent side: MCP URL, token URL, client id/secret, CA path |

The API server config:

```yaml
apiVersion: apiserver.config.k8s.io/v1
kind: AuthenticationConfiguration
jwt:
- issuer:
    url: https://<node-ip>:30443/realms/sre
    audiences: [kubernetes-mcp-server]
    certificateAuthority: <lab CA>
  claimMappings:
    username: {claim: preferred_username, prefix: "oidc:"}
```

The issuer is the kind node IP + NodePort because it must be the same URL for three parties: the API server
(host network, can't resolve `*.svc`), the MCP pod, and the `iss` claim in tokens. Keycloak's `--hostname` pins
`iss`, so tokens fetched through the port-forward still carry the node-IP issuer.

## Why the API server must trust Keycloak

The first attempt let the MCP server read the cluster with its own ServiceAccount and use OAuth only to decide
who may call it. The server refuses to start that way:

```text
Error: cluster_auth_mode "kubeconfig" is not compatible with require_oauth=true: all authenticated users would
share a single cluster identity, breaking per-user audit trails; use passthrough or token exchange to preserve
user identity on the cluster
```

So with OAuth on, the caller's identity always reaches Kubernetes:

| `cluster_auth_mode` | Token sent to the API server | Needs |
|---|---|---|
| `passthrough` **(used here)** | The caller's token, unchanged | API server trusts the IdP for audience `kubernetes-mcp-server` |
| `passthrough` + `[token_exchange]` | A new token from the IdP (RFC 8693, Keycloak, Entra OBO) with the API server's audience | IdP supports token exchange; the MCP server has its own client credentials |

Token exchange is the tighter version: with plain passthrough, the agent's token is also valid **directly** on
the API server (same read-only RBAC, so no extra rights, but the MCP layer can be bypassed). Use exchange when the
API server is reachable by the agent.

## The agent side

`k8s.mcp_client()` switches on `SRE_MCP_URL`:

```python
Client(url, auth=ClientCredentials(token_url, client_id, client_secret, ca))   # remote, HTTP
Client(StdioTransport("bin/kubernetes-mcp-server", ["--config", "lab/mcp.toml"]))  # local (default)
```

`ClientCredentials` is a 20-line `httpx2.Auth`: OAuth `client_credentials` grant against a fixed token URL,
token cached and refetched 60s before expiry (Keycloak access tokens last 15 min here). fastmcp ships a
`ClientCredentialsOAuthProvider`, but it discovers the token endpoint from the MCP server's OAuth metadata, which
points at the node IP, and macOS can't route to Docker container IPs. With a real, reachable IdP, use fastmcp's.

Everything else (Harness `MCP` capability, per-Run bindings, subagent `allowed_tools`, approvals) is unchanged.
The write path (`kubectl_fix`) still runs on the agent host with the admin context; see
[production.md §5](production.md#5-the-write-path).

## Verified (Nov 2026, kind v1.36.1, kubernetes-mcp-server v0.1.2, Keycloak 26.4.0)

| Check | Result |
|---|---|
| MCP call with no token / garbage token | 401 |
| Token from `other-app` (same realm, no `kubernetes-mcp-server` audience) | 401 |
| Token from `sre-agent` | 200; 14 tools, all `readOnlyHint=true` |
| API server identity of that token | `oidc:service-account-sre-agent` |
| That identity: list pods / delete pods / list Secrets | yes / Forbidden / Forbidden |
| MCP server's own ServiceAccount: list pods | no (so every successful read used the caller's token) |
| `resources_list kind=Secret` via MCP | error (`denied_resources`) |
| `test_k8s.py` through the remote server (MCP, subagent, approved fix) | PASSED |
| `test_e2e_llm.py no-endpoints`, free Nemotron model, remote server | PASS: 10 model calls, 35s, 1 approval |

`mise run test:remote` asserts the first six rows on every run.

## Lab shortcuts (not for production)

- Keycloak `start-dev`: in-memory database, realm re-imported on every restart. Use your company IdP.
- The MCP server serves plain HTTP inside the cluster; TLS would terminate at the gateway (or set
  `tls_cert`/`tls_key`). The port-forwards stand in for that gateway.
- Lab CA valid 30 days, private key deleted after signing; `lab:remote-up` regenerates certs if the node IP
  changes.
- `lab:remote-down` leaves the API server's OIDC config in place (harmless; `lab:down` deletes the cluster).
- `remote.env` holds the client secret in a file. In production it comes from a secret store, or use
  `private_key_jwt` / workload identity federation instead of a shared secret.
