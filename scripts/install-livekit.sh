#!/usr/bin/env bash
# Install + configure livekit-server on this box, bound to the tailscale IP.
# Idempotent — safe to re-run.
set -euo pipefail

LIVEKIT_VERSION="${LIVEKIT_VERSION:-1.9.1}"
INSTALL_DIR="${INSTALL_DIR:-$HOME/.local/bin}"
CONFIG_DIR="${CONFIG_DIR:-$HOME/.config/livekit}"
UNIT_DIR="$HOME/.config/systemd/user"

TS_IP="$(tailscale ip -4 2>/dev/null | head -1 || true)"
if [[ -z "$TS_IP" ]]; then
  echo "tailscale ip -4 returned nothing — is tailscaled up?" >&2
  exit 1
fi

API_KEY="${LIVEKIT_API_KEY:-skchat-lumina}"
API_SECRET="${LIVEKIT_API_SECRET:-$(openssl rand -hex 32)}"

mkdir -p "$INSTALL_DIR" "$CONFIG_DIR" "$UNIT_DIR"

if [[ ! -x "$INSTALL_DIR/livekit-server" ]]; then
  echo "→ downloading livekit-server $LIVEKIT_VERSION"
  arch=$(uname -m)
  case "$arch" in
    x86_64)  pkg="linux_amd64" ;;
    aarch64) pkg="linux_arm64" ;;
    *) echo "unsupported arch: $arch" >&2; exit 1 ;;
  esac
  url="https://github.com/livekit/livekit/releases/download/v${LIVEKIT_VERSION}/livekit_${LIVEKIT_VERSION}_${pkg}.tar.gz"
  tmp=$(mktemp -d)
  curl -fsSL "$url" -o "$tmp/lk.tar.gz"
  tar -xzf "$tmp/lk.tar.gz" -C "$tmp"
  install -m 0755 "$tmp/livekit-server" "$INSTALL_DIR/livekit-server"
  rm -rf "$tmp"
fi

cat > "$CONFIG_DIR/livekit.yaml" <<YAML
port: 7880
bind_addresses:
  - $TS_IP
rtc:
  tcp_port: 7881
  port_range_start: 50000
  port_range_end: 50200
  use_external_ip: false
  # Tailnet-only — no STUN/TURN needed
keys:
  $API_KEY: $API_SECRET
log_level: info
YAML

cat > "$UNIT_DIR/livekit-server.service" <<UNIT
[Unit]
Description=LiveKit SFU (tailnet)
After=network-online.target tailscaled.service
Wants=network-online.target

[Service]
Type=simple
ExecStart=$INSTALL_DIR/livekit-server --config $CONFIG_DIR/livekit.yaml
Restart=on-failure
RestartSec=5
StandardOutput=journal
StandardError=journal

[Install]
WantedBy=default.target
UNIT

# Drop-in for skchat-webui so its token endpoint can mint JWTs against the new server.
mkdir -p "$UNIT_DIR/skchat-webui.service.d"
cat > "$UNIT_DIR/skchat-webui.service.d/livekit.conf" <<DROP
[Service]
Environment=SKCHAT_LIVEKIT_URL=ws://$TS_IP:7880
Environment=SKCHAT_LIVEKIT_API_KEY=$API_KEY
Environment=SKCHAT_LIVEKIT_API_SECRET=$API_SECRET
Environment=SKCHAT_LIVEKIT_DEFAULT_ROOM=lumina-and-chef
DROP

systemctl --user daemon-reload
systemctl --user enable --now livekit-server.service
systemctl --user restart skchat-webui.service

echo
echo "✓ livekit-server installed at $INSTALL_DIR/livekit-server"
echo "✓ config: $CONFIG_DIR/livekit.yaml (bound to $TS_IP:7880)"
echo "✓ skchat-webui restarted with LiveKit env"
echo
echo "Test from this machine:"
echo "  curl http://localhost:8765/livekit/config"
echo
echo "Browser join (from any tailnet peer):"
echo "  http://$TS_IP:8765/livekit"
echo
echo "API secret (save somewhere):"
echo "  $API_SECRET"
