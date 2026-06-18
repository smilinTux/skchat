# Tailscale Funnel Ingress — Verification Runbook

## Sovereign coturn deployment (coord 0f70eeda)

coturn runs as a Docker container with host networking:
```bash
# Status
docker ps --filter name=skchat-coturn

# Logs
docker logs skchat-coturn

# Restart via systemd
systemctl --user restart skchat-coturn.service
```

### Funnel-vs-open-UDP exposure decision

**Tailscale Funnel is HTTP(S)-only** — it cannot proxy UDP TURN media traffic.
For public (off-tailnet) guests to reach our coturn, the host UDP ports must be
open (3478 + relay range 49152-65535). Currently:

| Exposure | Status | Who can reach coturn |
|----------|--------|---------------------|
| Tailnet (`100.x.x.x:3478`) | ✅ Always | All tailnet peers |
| LAN (`192.168.0.158:3478`) | ✅ Always | LAN peers |
| Public internet (UDP) | ❌ Closed | Off-tailnet guests use free TURN fallback |

**Current setup:** SKCHAT_TURN_SECRET is set → the ICE ladder prefers sovereign
coturn. Tailnet/LAN users relay through our coturn. Public guests behind
symmetric NAT will fail to reach coturn via UDP and **fall back to the free
public TURN** (Open Relay Project) because `SKCHAT_PUBLIC_TURN_ENABLED` defaults
to true when the ICE relay fails.

**To enable full sovereign TURN for public guests:**
1. Open UDP 3478 + UDP 49152-65535 in the host firewall
2. Add the host's public IP to `SKCHAT_TURN_URLS`
3. Optionally set `SKCHAT_PUBLIC_TURN_ENABLED=false` to remove the free fallback
4. If behind a NAT router: forward ports 3478/UDP and 49152-65535/UDP

### ICE ladder verification

```python
from skchat.connectivity import ice_config

# Tailnet → direct (no STUN/TURN)
cfg = ice_config("lumina@skworld.io", "chef@skworld.io", {"on_tailnet": True})

# Cross-NAT → sovereign coturn + Google STUN, NO free public TURN
cfg = ice_config("lumina@skworld.io", "public@guest", {"on_tailnet": False})
# cfg["ice_servers"] contains:
#   [0] Google STUN URLs
#   [1] {urls: ["turn:noroc2027.tail204f0c.ts.net:3478", ...], username, credential}
```

## Public paths (exposed via Funnel on `:10000`)
| Path | Type | Gated by |
|------|------|----------|
| `/join/{room}` | GET | invite JWT |
| `/guest/join` | POST | invite JWT |
| `/livekit/*` | GET/POST | token gate / invite JWT |

## Admin paths (tailnet-only, blocked on Funnel → 404)
| Path | Type | Auth |
|------|------|------|
| `/guest/invite` | POST | operator token or tailnet |
| `/guest/revoke/{jti}` | DELETE | operator token or tailnet |
| `/pair/*` | GET/POST | tailnet |
| `/spaces/*` | GET/POST | tailnet |

## Setup commands (idempotent)
```bash
tailscale funnel --bg --https=10000 --set-path=/join     http://localhost:8765/join
tailscale funnel --bg --https=10000 --set-path=/guest/join http://localhost:8765/guest/join
tailscale funnel --bg --https=10000 --set-path=/livekit    http://localhost:8765/livekit
```

## Verify
```bash
# Public endpoints should return 200/400 (reached)
curl -s -o /dev/null -w "%{http_code}" "http://localhost:8765/join/testroom?invite=x"     # 200
curl -s -o /dev/null -w "%{http_code}" "http://localhost:8765/guest/join" -X POST -d '{}' # 400
curl -s -o /dev/null -w "%{http_code}" "http://localhost:8765/livekit/testroom"            # 200

# Admin endpoints should return 404 via Funnel
curl -s -o /dev/null -w "%{http_code}" "http://localhost:8765/guest/invite"                # 200 (tailnet reachable)
curl -s -o /dev/null -w "%{http_code}" "http://localhost:8765/pair/scan"                    # 200 (tailnet reachable)
```

## Full guest flow test
```bash
# 1. Create invite (tailnet)
INVITE=$(curl -s http://localhost:8765/guest/invite -X POST \
  -H 'Content-Type: application/json' \
  -d '{"room":"verify-room","single_use":true}')
TOKEN=$(echo "$INVITE" | python3 -c "import sys,json; print(json.load(sys.stdin)['invite_token'])")

# 2. Join page reachable
curl -s -o /dev/null -w "%{http_code}" "https://noroc2027.tail204f0c.ts.net:10000/join/verify-room?invite=$TOKEN"

# 3. Guest join (returns LiveKit token)
curl -s -X POST "https://noroc2027.tail204f0c.ts.net:10000/guest/join" \
  -H 'Content-Type: application/json' \
  -d "{\"room\":\"verify-room\",\"invite_token\":\"$TOKEN\",\"display_name\":\"Alice\"}" | python3 -m json.tool
```
