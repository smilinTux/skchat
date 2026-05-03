#!/usr/bin/env bash
# Set up Tailscale-issued HTTPS for skchat-webui + LiveKit signalling.
# Idempotent — re-running just refreshes the serve config.
#
# Result:
#   webui:    https://<MagicDNS-name>/
#   livekit:  wss://<MagicDNS-name>:8443/
#
# Tailscale handles cert acquisition + renewal automatically.
# This script needs sudo only the first time (to set --operator=$USER).
set -euo pipefail

DNS_NAME="$(tailscale status --json | python3 -c '
import sys, json
d = json.load(sys.stdin)["Self"]["DNSName"].rstrip(".")
print(d)
')"
TS_IP="$(tailscale ip -4 | head -1)"

if [[ -z "$DNS_NAME" || -z "$TS_IP" ]]; then
  echo "tailscale not up — run 'tailscale up' first" >&2
  exit 1
fi

# One-time: let cbrd21 manage tailscale serve without sudo on every call.
if [[ "$(tailscale debug prefs 2>/dev/null | grep -o '"OperatorUser":"[^"]*"' | cut -d'"' -f4)" != "$USER" ]]; then
  echo "→ first run: setting tailscale operator (needs sudo once)"
  sudo tailscale set --operator="$USER"
fi

# Webui on 443. If something else is already mounted at /, leave it alone.
if ! tailscale serve status --json | python3 -c '
import sys, json
d = json.load(sys.stdin) or {}
web = (d.get("Web") or {})
host = next(iter(web.values()), {}).get("Handlers", {}) if web else {}
sys.exit(0 if "/" in host else 1)
' 2>/dev/null; then
  echo "→ mounting webui at https://$DNS_NAME/"
  tailscale serve --bg --https=443 http://localhost:8765
fi

# LiveKit on 8443 — proxies to the tailnet IP, not localhost (livekit binds tailnet only).
echo "→ mounting livekit at wss://$DNS_NAME:8443/"
tailscale serve --https=8443 off >/dev/null 2>&1 || true
tailscale serve --bg --https=8443 "http://${TS_IP}:7880"

# Wire skchat-webui to use WSS.
DROP_DIR="$HOME/.config/systemd/user/skchat-webui.service.d"
mkdir -p "$DROP_DIR"

# Preserve existing API key/secret if a livekit.conf already exists.
if [[ -f "$DROP_DIR/livekit.conf" ]]; then
  API_KEY=$(grep -E '^Environment=SKCHAT_LIVEKIT_API_KEY=' "$DROP_DIR/livekit.conf" | cut -d= -f3-)
  API_SECRET=$(grep -E '^Environment=SKCHAT_LIVEKIT_API_SECRET=' "$DROP_DIR/livekit.conf" | cut -d= -f3-)
fi
API_KEY="${API_KEY:-skchat-lumina}"
API_SECRET="${API_SECRET:-$(openssl rand -hex 32)}"

cat > "$DROP_DIR/livekit.conf" <<EOF
[Service]
Environment=SKCHAT_LIVEKIT_URL=wss://$DNS_NAME:8443
Environment=SKCHAT_LIVEKIT_API_KEY=$API_KEY
Environment=SKCHAT_LIVEKIT_API_SECRET=$API_SECRET
Environment=SKCHAT_LIVEKIT_DEFAULT_ROOM=lumina-and-chef
EOF

systemctl --user daemon-reload
systemctl --user restart skchat-webui.service

echo
echo "✓ Tailscale TLS active"
echo "  webui:    https://$DNS_NAME/"
echo "  livekit:  https://$DNS_NAME/livekit"
echo "  WSS:      wss://$DNS_NAME:8443/"
echo
echo "From any tailnet peer (laptop, phone via Tailscale app), open:"
echo "  https://$DNS_NAME/livekit"
echo
echo "First page load may stall briefly while Tailscale fetches the cert."
