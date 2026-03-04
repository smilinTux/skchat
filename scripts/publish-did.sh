#!/usr/bin/env bash
# publish-did.sh — Push the Tier 3 public DID document to skworld.io
#
# Uploads ~/.skcapstone/did/public.json to Cloudflare KV under the key
# "<AGENT_SLUG>" in the DID_DOCUMENTS namespace, making the document available at:
#   https://ws.weblink.skworld.io/agents/<slug>/.well-known/did.json
#
# DID string: did:web:ws.weblink.skworld.io:agents:<slug>
#
# Prerequisites:
#   - wrangler CLI installed and authenticated (wrangler login)
#   - CF_ACCOUNT_ID set in environment or wrangler.toml
#   - DID_KV_NAMESPACE_ID set (the KV namespace binding for DID_DOCUMENTS)
#
# Environment variables:
#   AGENT_SLUG          Agent slug for the KV key (default: from profile.json name)
#   DID_PUBLIC_FILE     Path to the Tier 3 DID file (default: ~/.skcapstone/did/public.json)
#   DID_KV_NAMESPACE_ID Cloudflare KV namespace ID for DID_DOCUMENTS
#   WRANGLER_CONFIG     Path to wrangler.toml (default: weblink-signaling/wrangler.toml)
#   GIT_FALLBACK        Set to "true" to git-commit instead of uploading to KV
#   SKWORLD_REPO        Path to the skworld.io repo (for GIT_FALLBACK mode)
#
# Usage:
#   cd /path/to/smilintux-org
#   bash skchat/scripts/publish-did.sh
#
#   # Or with custom slug:
#   AGENT_SLUG=lumina bash skchat/scripts/publish-did.sh

set -euo pipefail

SKCAPSTONE_HOME="${SKCAPSTONE_HOME:-$HOME/.skcapstone}"
DID_PUBLIC_FILE="${DID_PUBLIC_FILE:-$SKCAPSTONE_HOME/did/public.json}"
WRANGLER_CONFIG="${WRANGLER_CONFIG:-$(dirname "$0")/../../weblink-signaling/wrangler.toml}"
GIT_FALLBACK="${GIT_FALLBACK:-false}"
SKWORLD_REPO="${SKWORLD_REPO:-}"

# Opt-out: read from policy.json, override with DID_PUBLISH_PUBLIC env var.
# Default: true (public publishing). Set DID_PUBLISH_PUBLIC=false or policy.json to opt out.
_POLICY_FILE="$SKCAPSTONE_HOME/did/policy.json"
if [[ "${DID_PUBLISH_PUBLIC:-}" == "false" ]]; then
    PUBLISH_PUBLIC="false"
elif [[ "${DID_PUBLISH_PUBLIC:-}" == "true" ]]; then
    PUBLISH_PUBLIC="true"
elif [[ -f "$_POLICY_FILE" ]]; then
    PUBLISH_PUBLIC=$(python3 -c "
import json, pathlib
d = json.loads(pathlib.Path('$_POLICY_FILE').read_text())
print('true' if d.get('publish_public', True) else 'false')
" 2>/dev/null || echo "true")
else
    PUBLISH_PUBLIC="true"
fi

if [[ "$PUBLISH_PUBLIC" != "true" ]]; then
    echo "INFO: DID public publishing is DISABLED (opt-out active)."
    echo "      To publish, set DID_PUBLISH_PUBLIC=true or run: did_policy(publish_public=true)"
    exit 0
fi

echo "=== Publish Sovereign DID (Tier 3 Public) ==="

# ---------------------------------------------------------------------------
# Step 1: Resolve public DID file
# ---------------------------------------------------------------------------
if [[ ! -f "$DID_PUBLIC_FILE" ]]; then
    echo "ERROR: Public DID file not found: $DID_PUBLIC_FILE"
    echo "       Run 'did_publish' MCP tool or: bash did-setup.sh"
    exit 1
fi

echo "[1/4] DID file: $DID_PUBLIC_FILE"

# ---------------------------------------------------------------------------
# Step 2: Resolve agent slug
# ---------------------------------------------------------------------------
if [[ -z "${AGENT_SLUG:-}" ]]; then
    # Derive from profile.json entity name
    AGENT_SLUG=$(python3 -c "
import json, re, pathlib
p = pathlib.Path('$HOME/.capauth/identity/profile.json')
if p.exists():
    d = json.loads(p.read_text())
    name = d.get('entity', {}).get('name', 'agent')
    print(re.sub(r'[^a-z0-9-]', '-', name.lower()).strip('-'))
else:
    print('agent')
" 2>/dev/null || echo "agent")
fi

echo "[2/4] Agent slug: $AGENT_SLUG"

# Update did:web ID in the public document to reflect actual CF Worker URL
python3 - <<PYEOF
import json
from pathlib import Path

pub_path = Path("$DID_PUBLIC_FILE")
doc = json.loads(pub_path.read_text())

# Update the DID id to the CF Worker URL
correct_id = f"did:web:ws.weblink.skworld.io:agents:$AGENT_SLUG"
if doc.get("id") != correct_id:
    doc["id"] = correct_id
    # Update VM controller
    for vm in doc.get("verificationMethod", []):
        vm["id"] = f"{correct_id}#key-1"
        vm["controller"] = correct_id
    doc["authentication"] = [f"{correct_id}#key-1"]
    doc["assertionMethod"] = [f"{correct_id}#key-1"]
    pub_path.write_text(json.dumps(doc, indent=2))
    print(f"  Updated DID id to: {correct_id}")
else:
    print(f"  DID id already correct: {correct_id}")
PYEOF

# ---------------------------------------------------------------------------
# Step 3: Publish to Cloudflare KV or git fallback
# ---------------------------------------------------------------------------
if [[ "$GIT_FALLBACK" == "true" ]]; then
    echo "[3/4] Git fallback mode — committing to repo."

    if [[ -z "$SKWORLD_REPO" ]]; then
        echo "ERROR: SKWORLD_REPO must be set for git fallback mode."
        exit 1
    fi

    TARGET_DIR="$SKWORLD_REPO/agents/$AGENT_SLUG/.well-known"
    mkdir -p "$TARGET_DIR"
    cp "$DID_PUBLIC_FILE" "$TARGET_DIR/did.json"

    cd "$SKWORLD_REPO"
    git add "agents/$AGENT_SLUG/.well-known/did.json"
    git commit -m "chore: publish DID for $AGENT_SLUG" || echo "  Nothing to commit."
    echo "  Committed to $SKWORLD_REPO"
else
    echo "[3/4] Publishing to Cloudflare KV..."

    if ! command -v wrangler &>/dev/null; then
        echo "ERROR: wrangler CLI not found."
        echo "       Install: npm install -g wrangler"
        echo "       Or set GIT_FALLBACK=true to use git instead."
        exit 1
    fi

    if [[ -z "${DID_KV_NAMESPACE_ID:-}" ]]; then
        echo "ERROR: DID_KV_NAMESPACE_ID is not set."
        echo "       Create a KV namespace in Cloudflare and set DID_KV_NAMESPACE_ID."
        echo "       Or set GIT_FALLBACK=true to use git instead."
        exit 1
    fi

    WRANGLER_ARGS=""
    if [[ -f "$WRANGLER_CONFIG" ]]; then
        WRANGLER_ARGS="--config $WRANGLER_CONFIG"
    fi

    # Upload DID document as JSON string value
    wrangler kv:key put \
        --namespace-id="$DID_KV_NAMESPACE_ID" \
        $WRANGLER_ARGS \
        "$AGENT_SLUG" \
        "$(cat "$DID_PUBLIC_FILE")"

    echo "  Uploaded to KV namespace $DID_KV_NAMESPACE_ID under key '$AGENT_SLUG'"
fi

# ---------------------------------------------------------------------------
# Step 4: Verify (if CF Worker is reachable)
# ---------------------------------------------------------------------------
echo "[4/4] Verifying..."
CF_URL="https://ws.weblink.skworld.io/agents/${AGENT_SLUG}/.well-known/did.json"

if curl -sf --max-time 10 "$CF_URL" >/dev/null 2>&1; then
    echo "  OK: $CF_URL is reachable"
    echo ""
    echo "  DID string: did:web:ws.weblink.skworld.io:agents:$AGENT_SLUG"
else
    echo "  INFO: CF Worker not yet reachable or DID route not deployed."
    echo "        Deploy first: cd weblink-signaling && wrangler deploy"
fi

echo ""
echo "=== Publish complete ==="
echo ""
echo "Public DID: did:web:ws.weblink.skworld.io:agents:$AGENT_SLUG"
echo "Resolve:    curl $CF_URL"
