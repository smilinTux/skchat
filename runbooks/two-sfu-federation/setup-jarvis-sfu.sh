#!/usr/bin/env bash
# Shape B — stand up the second sovereign LiveKit SFU (jarvis @ .41) for true
# two-SFU peer federation. Idempotent; run ON .41 (or via ssh). See README.md.
#
# Prereqs: tailscale up (tagged node), the skchat-jarvis LiveKit key/secret.
set -euo pipefail

TNET_IP="${TNET_IP:-100.86.156.5}"          # .41 tailnet IP
PEER_TNET="${PEER_TNET:-100.108.59.57}"     # .158 (lumina) tailnet IP — the federating peer
LK_BIN="$HOME/.local/bin/livekit-server"
LK_YAML="$HOME/.config/livekit/livekit.yaml"
JARVIS_SECRET="${JARVIS_SECRET:?set JARVIS_SECRET to the skchat-jarvis LiveKit secret}"
LUMINA_SECRET="${LUMINA_SECRET:-beda9639c3f2ed5904698daa75aa53aba5a3689c8a8f73e56ec32e1e79c109ac}"

echo "== 1. livekit.yaml (bound to tailnet IP, no STUN/TURN) =="
mkdir -p "$(dirname "$LK_YAML")"
cat > "$LK_YAML" <<EOF
port: 7880
bind_addresses:
  - $TNET_IP
rtc:
  tcp_port: 7881
  port_range_start: 50000
  port_range_end: 50200
  use_external_ip: false
keys:
  skchat-jarvis: $JARVIS_SECRET
  skchat-lumina: $LUMINA_SECRET
log_level: info
EOF

echo "== 2. systemd user unit (tailnet-wait) =="
cat > "$HOME/.config/systemd/user/livekit-server.service" <<EOF
[Unit]
Description=LiveKit SFU (tailnet, jarvis)
After=network-online.target tailscaled.service
Wants=network-online.target

[Service]
Type=simple
ExecStartPre=/bin/sh -c 'for i in \$(seq 1 60); do ip -4 addr show tailscale0 2>/dev/null | grep -q "100\\." && exit 0; sleep 1; done; echo "tailscale0 100.x never appeared" >&2; exit 1'
ExecStart=$LK_BIN --config $LK_YAML
Restart=on-failure
RestartSec=5

[Install]
WantedBy=default.target
EOF
systemctl --user daemon-reload
systemctl --user enable --now livekit-server.service
echo "   livekit-server: $(systemctl --user is-active livekit-server.service)"

echo "== 3. tailscale serve — signaling ws (PIA-permitted, terminates at tailscaled) =="
# The jarvis webui advertises wss://<host>.ts.net/livekit-ws as the public SFU url.
tailscale serve --bg --set-path=/livekit-ws "http://${TNET_IP}:7880"

cat <<NOTE

== 4. REQUIRED — unblock the .41 inbound MEDIA path (PIA killswitch) ==
The tailnet ACL is allow-all and is NOT the blocker. The blocker is .41's **PIA VPN
killswitch**, which drops inbound tailnet traffic to the SFU media ports (livekit answers
fine on .41's own bind; only remote .158->.41:7880 is EHOSTUNREACH). Plain INPUT ACCEPTs do
NOT fix it (the interference is in PIA's own mangle/MARK chains — do not hand-edit blind).
Pick ONE (see README.md "Fix options"):
  1. PIA app: allow the tailscale interface / split-tunnel excluding tailscale0 + 100.64.0.0/10.
  2. coturn TURN relay on .158 (PIA-agnostic; media relays via the reachable host, zero .41 inbound).
  3. Shape A (works today): both join the .158 SFU (runbooks/cross-instance-call-test/).
Verify after: from .158, 'curl -m6 http://$TNET_IP:7880/' connects (not 000).
NOTE
echo "done."
