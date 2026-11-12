#!/usr/bin/env bash
# Download the two native binaries mise's registry doesn't provide, verify checksums, put them in bin/.
#   a13n-envd              sandbox daemon; version MUST equal the installed a13n-envd-client (Python)
#   kubernetes-mcp-server  Kubernetes MCP server (containers/kubernetes-mcp-server)
# Skips a binary that already exists at the right version. Needs: gh (authenticated), shasum, tar.
set -euo pipefail
cd "$(dirname "$0")/.."
MCP_VERSION=v0.1.2

case "$(uname -s)-$(uname -m)" in
  Darwin-arm64)  ENVD_TARGET=aarch64-apple-darwin; MCP_ASSET=kubernetes-mcp-server-darwin-arm64 ;;
  Darwin-x86_64) ENVD_TARGET=x86_64-apple-darwin;  MCP_ASSET=kubernetes-mcp-server-darwin-amd64 ;;
  *) echo "This demo's sandbox is the macOS Seatbelt backend; unsupported platform $(uname -sm)"; exit 1 ;;
esac
mkdir -p bin
tmp=$(mktemp -d); trap 'rm -rf "$tmp"' EXIT

# --- a13n-envd: match the Python client version that uv installed ---
ENVD_VERSION=$(uv run python -c 'from importlib.metadata import version; print(version("a13n-envd-client"))')
if [[ "$(bin/a13n-envd --version 2>/dev/null)" == "a13n-envd $ENVD_VERSION" ]]; then
  echo "a13n-envd $ENVD_VERSION already installed"
else
  echo "Downloading a13n-envd $ENVD_VERSION ($ENVD_TARGET)"
  archive="a13n-envd-$ENVD_VERSION-$ENVD_TARGET.tar.gz"
  gh release download "release/a13n-envd-v$ENVD_VERSION" -R converge-ai-labs/agent-foundation \
    -p "$archive" -p SHA256SUMS -D "$tmp"
  (cd "$tmp" && grep " $archive\$\| \./$archive\$" SHA256SUMS | shasum -a 256 -c -)
  tar -xzf "$tmp/$archive" -C "$tmp"
  find "$tmp" -name a13n-envd -type f -exec cp {} bin/a13n-envd \;
  chmod +x bin/a13n-envd
  bin/a13n-envd --version
fi

# --- kubernetes-mcp-server: verify against GitHub's recorded asset digest ---
if [[ "$(bin/kubernetes-mcp-server --version 2>/dev/null)" == "$MCP_VERSION" ]]; then
  echo "kubernetes-mcp-server $MCP_VERSION already installed"
else
  echo "Downloading kubernetes-mcp-server $MCP_VERSION ($MCP_ASSET)"
  gh release download "$MCP_VERSION" -R containers/kubernetes-mcp-server -p "$MCP_ASSET" -O "$tmp/$MCP_ASSET"
  want=$(gh api "repos/containers/kubernetes-mcp-server/releases/tags/$MCP_VERSION" \
    --jq ".assets[]|select(.name==\"$MCP_ASSET\")|.digest")
  got="sha256:$(shasum -a 256 "$tmp/$MCP_ASSET" | cut -d' ' -f1)"
  [[ "$want" == "$got" ]] || { echo "CHECKSUM MISMATCH: want $want got $got"; exit 1; }
  echo "checksum OK ($got)"
  install -m 755 "$tmp/$MCP_ASSET" bin/kubernetes-mcp-server
  bin/kubernetes-mcp-server --version
fi
