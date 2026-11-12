"""Auth checks for the remote MCP server (HTTP + OAuth). No API key, no model.

Needs: lab/lab.sh remote-up, lab/lab.sh remote-forward running.   Run: mise run test:remote
"""

import asyncio
import os
import subprocess

import httpx2

import k8s

URL = os.environ["SRE_MCP_URL"]
e = os.environ


def can_i(who: str, *args: str) -> str:
    cmd = ["kubectl", "--context", k8s.CONTEXT, "auth", "can-i", *args, "--as", who]
    return subprocess.run(cmd, capture_output=True, text=True).stdout.strip()


def status(auth) -> int:
    """HTTP status of an MCP initialize request (fastmcp hides the 401 behind a generic error)."""
    init = {"jsonrpc": "2.0", "id": 1, "method": "initialize",
            "params": {"protocolVersion": "2025-06-18", "capabilities": {}, "clientInfo": {"name": "test", "version": "0"}}}
    return httpx2.post(URL, json=init, auth=auth, headers={"Accept": "application/json, text/event-stream"}).status_code


async def main() -> None:
    other = k8s.ClientCredentials(e["SRE_OIDC_TOKEN_URL"], "other-app", e["SRE_TEST_OTHER_CLIENT_SECRET"], e.get("SRE_OIDC_CA"))
    agent = k8s.ClientCredentials(e["SRE_OIDC_TOKEN_URL"], e["SRE_OIDC_CLIENT_ID"], e["SRE_OIDC_CLIENT_SECRET"], e.get("SRE_OIDC_CA"))
    assert status(None) == 401, "no token must be rejected"
    assert status(other) == 401, "a token without the kubernetes-mcp-server audience must be rejected"
    assert status(agent) == 200, "the agent's token must be accepted"
    print("ok  no token / wrong-audience token -> 401, agent token -> 200")

    async with k8s.mcp_client() as c:  # sre-agent client credentials from remote.env
        tools = await c.list_tools()
        assert tools and all(t.annotations.read_only_hint for t in tools), [t.name for t in tools]
        r = await c.call_tool("pods_list_in_namespace", {"namespace": "shop"})
        assert "checkout-api" in str(r.content)
        r = await c.call_tool("resources_list", {"apiVersion": "v1", "kind": "Secret", "namespace": "shop"}, raise_on_error=False)
        assert r.is_error, "Secrets must be denied"
    print(f"ok  agent token -> {len(tools)} read-only tools, pods listed, Secrets denied")

    # Passthrough is real: the server's own identity can't read anything, the agent's OIDC user can only read.
    assert can_i("system:serviceaccount:sre-system:kubernetes-mcp-server", "list", "pods", "-n", "shop") == "no"
    assert can_i("oidc:service-account-sre-agent", "list", "pods", "-n", "shop") == "yes"
    assert can_i("oidc:service-account-sre-agent", "delete", "pods", "-n", "shop") == "no"
    print("ok  server SA has no RBAC; oidc:service-account-sre-agent is read-only")
    print("\nREMOTE MCP AUTH CHECK PASSED")


asyncio.run(main())
