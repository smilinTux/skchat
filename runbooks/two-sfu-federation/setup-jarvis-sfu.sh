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

echo "== 4. PIA killswitch allow-rules for the SFU media ports on the tailnet iface =="
# PIA's killswitch + tailscale's ts-input already ACCEPT tailscale0 inbound, but make
# the SFU rtc ports explicit + survive PIA reloads. (ts-input ACCEPTs -i tailscale0 already.)
for rule in \
  "-i tailscale0 -p tcp --dport 7880 -j ACCEPT" \
  "-i tailscale0 -p tcp --dport 7881 -j ACCEPT" \
  "-i tailscale0 -p udp --dport 50000:50200 -j ACCEPT"; do
  sudo iptables -C INPUT $rule 2>/dev/null || sudo iptables -I INPUT 1 $rule
done
echo "   iptables INPUT allow-rules ensured for 7880/7881/50000-50200 on tailscale0"

cat <<NOTE

== 5. REQUIRED — tailscale ACL grant (Chef / tailnet admin) ==
.41 is a 'tagged-devices' node; the tailnet ACL gates DIRECT peer port access
(funnel + disco/ping are NOT gated, which is why signaling works but media did not).
Add to the tailnet policy at https://login.tailscale.com/admin/acls :

  {"action":"accept",
   "src":["$PEER_TNET"],
   "dst":["$TNET_IP:7880,7881,50000-50200"]}

(and the symmetric grant for any other peer that must join jarvis-hosted confs).
Until this lands, lumina@.158 can reach jarvis SIGNALING but not jarvis SFU MEDIA,
so Shape-B media falls back per README (coturn relay / Shape-A shared SFU).
NOTE
echo "done."
