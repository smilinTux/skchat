# Tailscale Funnel Ingress — Verification Runbook

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
