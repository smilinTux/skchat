# Runbook: Two-Phone Guest Call + Video Conference (off-tailnet, cellular)

Audience: Chef (operator). Goal: prove end to end, from two phones on cellular
data (NOT on the tailnet), that a guest can join a 1:1 call and a multi-party
conference through the public Funnel, over the sovereign coturn relay (never
openrelay).

This is the GCV sprint acceptance path. If every "what good looks like" box
below is green, the sprint is delivered.

---

## 0. The URLs (memorize these three)

Public Funnel base (works from any network, including cellular):

    https://noroc2027.tail204f0c.ts.net:10000

| What | URL |
| --- | --- |
| Flutter PWA (the app) | `https://noroc2027.tail204f0c.ts.net:10000/app/` |
| Guest join link (per room + invite) | `https://noroc2027.tail204f0c.ts.net:10000/join/<room>?invite=<token>` |
| Conference room page | `https://noroc2027.tail204f0c.ts.net:10000/conf/<room>` |

The Funnel proxies `/api`, `/app`, `/join`, `/conf`, `/livekit`, `/guest/join`
to the webui on `:8765`, and `/daemon` to `:9385`. LiveKit SFU media rides
`100.108.59.57:7880` (tailnet wss `noroc2027.tail204f0c.ts.net:8443`, Funnel
`:10001`). Relay is the sovereign coturn on `:3478` (LAN + tailnet) with
ephemeral HMAC credentials.

Note the trailing slash on `/app/`. Serve the PWA from `/app/`, not `/app`.

---

## 1. One-time server checks (operator, on .158)

Confirm the media plane env is wired so the sovereign relay is emitted and
openrelay stays suppressed. On .158, the webui process must have these set
(from the `webui-*.env` drop-in, secret sourced from skvault / `coturn.secret`):

    SKCHAT_TURN_SECRET   = <coturn use-auth-secret>        # presence selects sovereign relay
    SKCHAT_TURN_URLS     = turn:noroc2027.tail204f0c.ts.net:443?transport=tls,turn:noroc2027.tail204f0c.ts.net:3478?transport=udp
    SKCHAT_LIVEKIT_API_KEY / SKCHAT_LIVEKIT_API_SECRET     # for guest + conf token mint
    SKCHAT_GUEST_TOKEN_SECRET                              # HS256 signer for invite JWTs
    SKCHAT_FUNNEL_PUBLIC_URL = https://noroc2027.tail204f0c.ts.net:10000
    SKCHAT_GUEST_OPERATOR_TOKEN = <operator bearer>        # gates /guest/invite off-tailnet

Rule that makes this bulletproof (`src/skchat/connectivity.py`): when
`SKCHAT_TURN_SECRET` + `SKCHAT_TURN_URLS` are both set, `connectivity.py` emits
ONLY the sovereign relay off-tailnet. openrelay (`openrelay.metered.ca`) is
never added alongside it, and is only ever reachable when
`SKCHAT_ALLOW_OPENRELAY` is explicitly on AND no sovereign coturn is configured.
Keep `SKCHAT_ALLOW_OPENRELAY` OFF (default) in production.

Do NOT set the funnel URL to anything ending in a slash; the invite builder
appends `/join/<room>?invite=<token>` directly.

---

## 2. Mint a guest invite (operator API)

`/guest/invite` is operator-gated. Off-tailnet you MUST present the operator
bearer token (`SKCHAT_GUEST_OPERATOR_TOKEN`) as `Authorization: Bearer <tok>`
or `X-Operator-Token: <tok>`. On the tailnet, loopback/private-IP callers are
trusted without a token.

From .158 (loopback, no token needed), mint an invite for a room named `demo`:

    curl -sS -X POST http://127.0.0.1:8765/guest/invite \
      -H 'content-type: application/json' \
      -d '{"room":"demo","display":"Guest","ttl":3600}' | python3 -m json.tool

From off-box (with the operator bearer):

    curl -sS -X POST https://noroc2027.tail204f0c.ts.net:10000/guest/invite \
      -H "authorization: Bearer $SKCHAT_GUEST_OPERATOR_TOKEN" \
      -H 'content-type: application/json' \
      -d '{"room":"demo","display":"Guest","ttl":3600}' | python3 -m json.tool

Response (the field you want is `invite_url`):

    {
      "invite_token": "<jwt>",
      "invite_url":   "https://noroc2027.tail204f0c.ts.net:10000/join/demo?invite=<jwt>",
      "jti":          "<hex>",
      "room":         "demo",
      "expires_at":   <unix>,
      "ttl":          3600,
      "single_use":   false
    }

Options:
- `ttl`: seconds, default 4h (`SKCHAT_INVITE_WINDOW_TTL`), hard cap 8h.
- `single_use: true`: the invite burns on first successful join (good for a
  one-off share). Default is multi-use-until-expiry.

Send `invite_url` to the guest's phone (SMS, Signal, whatever). That single link
is all the guest needs; no app install, no account, no tailnet.

To revoke early: `DELETE /guest/revoke/<jti>` (same operator auth).

---

## 3. Scenario A: 1:1 guest call from two phones (cellular)

Roles: Phone 1 = you (Chef, signed in). Phone 2 = the guest (invite link only).
Both phones on cellular data, Wi-Fi off, tailnet OFF on both. This forces the
cross-NAT relay tier so you actually exercise the sovereign coturn.

1. Phone 1: open the PWA `https://noroc2027.tail204f0c.ts.net:10000/app/`, sign
   in as yourself. This is your identity endpoint for the call.
2. Operator: mint an invite for a room (say `call-demo`) per section 2. Text the
   `invite_url` to Phone 2.
3. Phone 2: tap the link. The `/join/<room>` chooser page loads. Pick
   "Join as guest", type a display name, tap join. The page validates the
   invite JWT (`POST /guest/join`), gets a LiveKit token, and lands in the room.
4. Phone 2 (guest composer): tap the 1:1 call button. This fires the guest call
   path (`/guest/call`, wired in `guest_room_screen.dart`).
5. Phone 1: accept the incoming call. Both phones land in the same
   `call-<room>` and subscribe to each other's audio/video track.

What good looks like:
- Both phones show the other person's live video within ~5-10s of accept.
- Two-way audio, no one-way-only (one-way usually means relay not reached: see
  section 5).
- Grant camera + mic permission when the browser prompts on each phone. Mobile
  Safari and Chrome both require a user gesture; the join button provides it.

---

## 4. Scenario B: multi-party conference with a guest (cellular)

1. Host (Phone 1 or a desktop, signed in): create the conf. Operator/tailnet:

       curl -sS -X POST http://127.0.0.1:8765/conf/create \
         -H 'content-type: application/json' \
         -d '{"host_fqid":"lumina@chef.skworld","title":"Demo Conf"}' \
         | python3 -m json.tool

   Response carries `room`, a SOVEREIGN host `token` (room_admin), and
   `join_url`. Open `https://noroc2027.tail204f0c.ts.net:10000/conf/<room>` on
   the host device. The host is auto-admitted (tailnet) or joins with its token.

2. Guest (Phone 2, cellular): you have two ways in.
   - Guest invite link (section 2) minted for the SAME `<room>`, OR
   - The conf page `https://noroc2027.tail204f0c.ts.net:10000/conf/<room>` with
     a chosen display name.
   Off-tailnet guests do NOT auto-admit. They enter the waiting room
   (`POST /conf/<room>/waiting` returns `admitted: false` with a queue
   position) and see "Waiting for host to admit you".

3. Host admit/deny flow (this is the moderation gate):
   - Host polls the waiting room:

         curl -sS http://127.0.0.1:8765/conf/<room>/waiting | python3 -m json.tool
         # -> {"waiting":[{identity,display,ip,is_tailnet,...}], "admitted":[...], "denied":[...]}

   - Admit a guest by identity:

         curl -sS -X POST http://127.0.0.1:8765/conf/<room>/admit \
           -H 'content-type: application/json' \
           -d '{"requester":"lumina@chef.skworld","identity":"<guest-identity>"}'

   - Deny a guest by identity (they get 403 on retry):

         curl -sS -X POST http://127.0.0.1:8765/conf/<room>/deny \
           -H 'content-type: application/json' \
           -d '{"requester":"lumina@chef.skworld","identity":"<guest-identity>"}'

   `requester` must be the conf host (enforced by `_require_host`). In the
   Flutter PWA the host sees the pending lobby and taps Admit/Deny directly
   (`conf_screen.dart` waiting-room UI); the curl form above is the API it calls.

4. Guest, once admitted, transitions from waiting to joined and their video
   appears. Repeat for as many guests as you want; each waits, host admits.

5. Optional: pull the Lumina agent into the room:

       curl -sS -X POST http://127.0.0.1:8765/conf/<room>/invite-agent \
         -H 'content-type: application/json' \
         -d '{"requester":"lumina@chef.skworld","greeting":"Hi all"}'

6. End the conf (host only): `POST /conf/<room>/end` with `{"requester":"<host>"}`.

What good looks like:
- Guest sits in the lobby until admitted (never auto-joins from cellular).
- On admit, guest video/audio appears for everyone within ~10s.
- Deny keeps the guest out and a re-join attempt is refused (403).
- 3+ participants (host + 2 guests) all see and hear each other.

---

## 5. Confirm sovereign TURN is used (NOT openrelay)

Two independent checks. Do at least the server-side one; do the browser one when
you have a laptop handy.

### 5a. Server-side ICE-config check (fast, definitive)

Ask the server what relay it hands an off-tailnet caller. The `/connectivity/ice`
route derives on_tailnet from the actual connection, so query it as an off-net
client (through the Funnel), or force the relay tier:

    curl -sS "https://noroc2027.tail204f0c.ts.net:10000/connectivity/ice?peer=<peer-fqid>" \
      | python3 -m json.tool

Good output:
- `iceServers` contains a `turn:noroc2027.tail204f0c.ts.net:443?transport=tls`
  entry WITH a `username` (`<expiry>:<fqid>`) and a `credential` (base64 HMAC).
- `iceServers` contains NO `openrelay.metered.ca` entry.
- `preferred_tier` is 3 (relay) for an off-tailnet peer.

Alert-on-use metric: `connectivity.openrelay_fallback_count()` MUST stay 0. Any
nonzero value means the process fell back to free public TURN (sovereign coturn
was unavailable while `SKCHAT_ALLOW_OPENRELAY` was on). Wire an alert to it.

### 5b. Browser check with chrome://webrtc-internals (desktop)

On a desktop Chrome that is NOT on the tailnet (or with Tailscale down), join the
room, then open a second tab to `chrome://webrtc-internals`.

- Expand the active peer connection. Under the ICE candidate pairs / selected
  candidate, the remote (and, for a relayed path, the local) candidate `type`
  should be `relay` and the relay address should be the sovereign realm
  `noroc2027.tail204f0c.ts.net` (coturn), NOT `openrelay.metered.ca` or any
  `*.metered.ca` host.
- In the `getStats` -> `RTCIceCandidatePair` you should see `state: succeeded`
  on a pair whose relayed candidate resolves to the sovereign coturn.
- If you ever see a `metered.ca` relay in the selected pair, stop: sovereign
  TURN was not configured or not reachable. Fix the env in section 1 and confirm
  coturn is up on `:3478` / TLS `:443`.

---

## 6. Automated E2E (optional, operator, headless)

The CDP harness drives two dedicated throwaway Chrome instances (may be on
different machines) through the live Funnel and asserts real WebRTC:

    ~/.skenv/bin/python scripts/gcv_e2e.py --scenario all \
      --base https://noroc2027.tail204f0c.ts.net:10000 \
      --operator-token "$SKCHAT_GUEST_OPERATOR_TOKEN" \
      --host-fqid lumina@chef.skworld

Scenarios: `guest-join-conf`, `call-1to1`, `admit-deny`, `turn-path` (asserts the
sovereign `turn:<realm>:443` is present and openrelay is absent). Each Chrome
uses a fresh `--user-data-dir=/tmp/cdp-gcv-<role>-<pid>` and its own debug port
(9250 / 9251). It NEVER touches port 9229 or Chef's daily profile, and only kills
Chrome it launched. Exit 0 = all requested scenarios passed.

---

## 7. Troubleshooting quick table

| Symptom | Likely cause | Fix |
| --- | --- | --- |
| Guest link says "invalid or expired invite" | invite TTL passed or wrong room | mint a fresh invite (section 2); room in URL must match |
| `/guest/invite` returns 401/403 | missing/wrong operator bearer off-tailnet | set `Authorization: Bearer $SKCHAT_GUEST_OPERATOR_TOKEN` |
| Guest never leaves the lobby | host not admitting | host runs `/conf/<room>/admit` or taps Admit in PWA |
| One-way or no audio/video off cellular | relay not reached | check section 5a; confirm coturn up on `:3478` / TLS `:443` |
| Sees `metered.ca` relay | sovereign TURN unset | set `SKCHAT_TURN_SECRET` + `SKCHAT_TURN_URLS`, keep `SKCHAT_ALLOW_OPENRELAY` off |
| PWA blank at `/app` | missing trailing slash | use `/app/` |
| 503 on guest/conf join | LiveKit creds unset | set `SKCHAT_LIVEKIT_API_KEY` / `_SECRET` |

---

Source map: guest invite mint + join `src/skchat/guest.py`; ICE ladder
`src/skchat/connectivity.py`; 1:1 call `src/skchat/call_routes.py` +
`call_session.py`; guest 1:1 `src/skchat/guest_group_routes.py` (`/guest/call`);
conf create/token/waiting/admit/deny/end `src/skchat/conf/routes.py`; E2E
`scripts/gcv_e2e.py` + `scripts/qa_two_browser.py`. Flutter: guest room + landing
`skchat-app/lib/features/guest/`, conf + waiting-room UI
`skchat-app/lib/features/conf/`.
