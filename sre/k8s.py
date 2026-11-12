"""Real-cluster mode: read via kubernetes-mcp-server (read-only identity), fix via one narrow, approval-gated tool.

Two identities on purpose:
  read  -> MCP server, lab/readonly.kubeconfig (ServiceAccount bound to `view`), read_only=true in lab/mcp.toml
           (or a remote MCP server over HTTP + OAuth, acting as the agent's Keycloak identity: docs/remote-mcp.md)
  write -> kubectl_fix(), your admin kind context, fixed namespace, fixed verb list, human approval every call
"""

import json
import os
import re
import subprocess
import time
from pathlib import Path
from typing import Literal

import httpx2
from fastmcp import Client
from fastmcp.client.transports import StdioTransport
from pydantic_ai.capabilities import MCP
from pydantic_ai.mcp import MCPToolset

HERE = Path(__file__).resolve().parent
MCP_BIN = HERE / "bin" / "kubernetes-mcp-server"
MCP_CONFIG = HERE / "lab" / "mcp.toml"
CONTEXT, NAMESPACE = "kind-sre-lab", "shop"  # ponytail: pinned to the lab; parametrize for real clusters

INSTRUCTIONS = """You are the on-call SRE for the `shop` namespace on a Kubernetes cluster.
Always follow the k8s-triage skill (read /workspace/.agents/skills/k8s-triage/SKILL.md first).
Kubernetes tools (pods_*, resources_*, events_list, ...) are read-only and safe; use them freely.
The only way to change the cluster is kubectl_fix, which needs human approval: justify it in one sentence first.
Be terse: bullet points, evidence (pod names, reasons, counts), no fluff.
The shell runs in a macOS sandbox with no cluster access; don't use it for Kubernetes."""


class ClientCredentials(httpx2.Auth):
    """OAuth client_credentials against a fixed token URL; re-fetches the token 60s before it expires.

    ponytail: the token fetch is a blocking call inside the async flow (~once per token lifetime). fastmcp's own
    ClientCredentialsOAuthProvider discovers the token URL via the MCP server instead; use it when that URL is
    reachable from the agent (here the advertised issuer is the kind node IP, which macOS can't route to).
    """

    def __init__(self, token_url: str, client_id: str, client_secret: str, ca: str | None = None):
        self.token_url, self.basic, self.verify = token_url, (client_id, client_secret), ca or True
        self.token, self.expires = "", 0.0

    def auth_flow(self, request):
        if time.time() > self.expires - 60:
            r = httpx2.post(self.token_url, data={"grant_type": "client_credentials"}, auth=self.basic, verify=self.verify)
            r.raise_for_status()
            body = r.json()
            self.token, self.expires = body["access_token"], time.time() + body["expires_in"]
        request.headers["Authorization"] = f"Bearer {self.token}"
        yield request


def mcp_client() -> Client:
    """Host-owned connection to the MCP server; enter once, reuse across Runs.

    SRE_MCP_URL set (lab/remote.env) -> remote server over HTTP with an OAuth token (docs/remote-mcp.md)
    otherwise                        -> local stdio child process with lab/mcp.toml
    """
    if url := os.environ.get("SRE_MCP_URL"):
        e = os.environ
        return Client(url, auth=ClientCredentials(e["SRE_OIDC_TOKEN_URL"], e["SRE_OIDC_CLIENT_ID"],
                                                  e["SRE_OIDC_CLIENT_SECRET"], e.get("SRE_OIDC_CA")))
    if not MCP_BIN.exists() or not MCP_CONFIG.exists():
        raise SystemExit(f"Missing {MCP_BIN.name} or {MCP_CONFIG}. See README: download the server, run lab/lab.sh up.")
    return Client(StdioTransport(str(MCP_BIN), ["--config", str(MCP_CONFIG)]))


def mcp(client: Client, allowed_tools: list[str] | None = None) -> MCP:
    """Fresh per-Run projection of the shared client. Tool IDs become mcp/k8s/<tool>."""
    return MCP(id="k8s", local=MCPToolset(client, id="k8s", cache_tools=False), allowed_tools=allowed_tools)


_NAME = re.compile(r"^[a-z0-9]([a-z0-9.-]{0,61}[a-z0-9])?$")
_VALUE = re.compile(r"^[A-Za-z0-9.-]{1,32}$")
Action = Literal["rollout_undo", "rollout_restart", "scale", "set_memory_limit", "set_cpu_request", "set_service_selector"]


def kubectl_fix(action: Action, name: str, value: str = "") -> str:
    """Apply one remediation in the shop namespace. Production-impacting; requires approval.

    action: rollout_undo | rollout_restart (deployment `name`);
            scale (value=replicas); set_memory_limit (value e.g. 256Mi); set_cpu_request (value e.g. 50m);
            set_service_selector (Service `name`, value = the app label VALUE only, e.g. `checkout-api`, not `app=...`).
    """
    if action == "set_service_selector":  # models often send app=x / "app: x" / {"app":"x"}; accept the obvious intent
        value = re.sub(r'^[{\s"]*app["\s]*[:=]\s*"?|["}\s]*$', "", value)
    if not _NAME.match(name):
        return f"refused: name {name!r} must be a Kubernetes resource name like checkout-api"
    if value and not _VALUE.match(value):
        return f"refused: value {value!r} must be a bare value like checkout-api, 256Mi, 50m or 3 (no '=', ':', quotes)"
    if action in ("scale", "set_memory_limit", "set_cpu_request", "set_service_selector") and not value:
        return f"refused: {action} needs a value"
    args = {
        "rollout_undo": ["rollout", "undo", f"deploy/{name}"],
        "rollout_restart": ["rollout", "restart", f"deploy/{name}"],
        "scale": ["scale", f"deploy/{name}", f"--replicas={value}"],
        "set_memory_limit": ["set", "resources", f"deploy/{name}", f"--limits=memory={value}"],
        "set_cpu_request": ["set", "resources", f"deploy/{name}", f"--requests=cpu={value}"],
        "set_service_selector": ["patch", "svc", name, "-p", json.dumps({"spec": {"selector": {"app": value}}})],
    }[action]
    cmd = ["kubectl", "--context", CONTEXT, "-n", NAMESPACE, *args]  # argv list, no shell: no injection
    r = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
    return f"$ {' '.join(cmd)}\n{r.stdout}{r.stderr}".strip()
