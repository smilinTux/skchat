#!/usr/bin/env bash
# Safely add the Shape-B media grant to the tailnet ACL via the Tailscale API.
# GET current policy -> back it up -> add the accept rule IF ABSENT -> validate -> PUT.
# Idempotent. Never replaces the policy blindly.
#
# Requires:
#   TS_API_KEY   a Tailscale API access token with ACL write scope
#                (https://login.tailscale.com/admin/settings/keys -> "API access tokens")
#   TS_TAILNET   tailnet name (default: the token's default tailnet "-")
#
# Usage:  TS_API_KEY=tskey-api-xxxx ./apply-acl.sh
set -euo pipefail

TS_TAILNET="${TS_TAILNET:--}"
API="https://api.tailscale.com/api/v2/tailnet/${TS_TAILNET}/acl"
: "${TS_API_KEY:?set TS_API_KEY to a Tailscale API access token with ACL write scope}"
HERE="$(cd "$(dirname "$0")" && pwd)"
TS="$(date -u +%Y%m%dT%H%M%SZ)"
BACKUP="/tmp/tailnet-acl-backup-${TS}.json"

echo "== 1. fetch current ACL (as JSON) =="
curl -sf -u "${TS_API_KEY}:" -H "Accept: application/json" "$API" -o "$BACKUP"
echo "   backed up current policy -> $BACKUP"

echo "== 2. merge the media grant if absent =="
MERGED="$(python3 - "$BACKUP" "$HERE/tailnet-policy-grant.json" <<'PY'
import json, sys
cur = json.load(open(sys.argv[1]))
grant = json.load(open(sys.argv[2]))["acls"][0]
acls = cur.setdefault("acls", [])
def same(a, b):
    return (a.get("action")==b.get("action")
            and sorted(a.get("src",[]))==sorted(b.get("src",[]))
            and sorted(a.get("dst",[]))==sorted(b.get("dst",[])))
if any(same(r, grant) for r in acls):
    print("ALREADY_PRESENT", file=sys.stderr); print(json.dumps(cur))
else:
    acls.append(grant)
    print("ADDED", file=sys.stderr); print(json.dumps(cur, indent=2))
PY
)"
if [ "${MERGED}" = "" ]; then echo "merge failed" >&2; exit 1; fi
echo "$MERGED" > "/tmp/tailnet-acl-new-${TS}.json"

echo "== 3. validate the merged policy =="
curl -sf -u "${TS_API_KEY}:" -H "Content-Type: application/json" \
  --data-binary @"/tmp/tailnet-acl-new-${TS}.json" "${API}/validate" \
  && echo "   validation OK"

echo "== 4. apply (PUT) =="
curl -sf -u "${TS_API_KEY}:" -H "Content-Type: application/json" \
  --data-binary @"/tmp/tailnet-acl-new-${TS}.json" "$API" >/dev/null
echo "   applied. backup at $BACKUP (revert: curl -u \$TS_API_KEY: -H 'Content-Type: application/json' --data-binary @$BACKUP $API)"

echo "== 5. verify media path from .158 =="
curl -s -m6 -o /dev/null -w "   .41:7880 -> %{http_code} (000=still blocked, anything else=reachable)\n" http://100.86.156.5:7880/ || true
