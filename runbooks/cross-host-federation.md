# Cross-Host Federation — Live Test Runbook (U8)

**Use case U8:** an agent on **another machine** (`jarvis@.41`) joins a Space hosted
on **`.158`** (federated), discovering the elected SFU focus via Nostr, minting a
cross-host LiveKit token at the remote host's live `/sfu/get`, and joining the
remote focus from a browser.

This runbook exercises the **just-shipped client leg**: `FederationDiscoveryClient`
(`src/skchat/spaces/federation/discovery.py`) + `GET /sfu/candidates`
(`src/skchat/spaces/routes.py`) + the live `POST /sfu/get` authd mint
(`authd.authorize`). The **server token-mint leg is already LIVE ✅** (matrix §1f
"Cross-host token mint", G-FED); the **discovery client + full browser join is the
LIVE ⏳ leg this runbook closes.**

> **Honesty rule (this repo):** CI-green ≠ done. Every step below tags whether it is
> **CI-proven** (test exists), **needs-this-live-run**, or **gated-on-wave-5 deploy**
> (the actual two-host SFU + Nostr relay). Do not mark U8 fully LIVE ✅ until the
> browser-join PASS criterion (Step 8) is observed.

---

## Purpose

Prove the end-to-end federated join path with real infra:

1. Start a Space on **`.158`** (the focus host).
2. Advertise its SFU focus descriptor + a membership event to a **Nostr relay**.
3. From **`.41`** (jarvis), query `GET /sfu/candidates` and run
   `FederationDiscoveryClient.discover_and_elect(space_id)` → resolve the elected
   focus on `.158`.
4. Build a capauth-signed FQID assertion and `get_token()` against the remote
   `POST /sfu/get` → a **capped, cross-host LiveKit token**.
5. A browser on `.41` loads the **`.158` webui's** livekit page with that token and
   joins the remote focus's room (room name == `space_id`).

---

## Prerequisites

### Hosts / services
- **`.158`** (`noroc2027`, focus host): `skchat-webui` running (`:8765`), with a
  **live LiveKit SFU** reachable over the tailnet with a **browser-trusted TLS cert**
  (e.g. `wss://noroc2027.tail204f0c.ts.net:8443`). LiveKit creds set in the webui's
  environment (`SKCHAT_LIVEKIT_API_KEY` / `SKCHAT_LIVEKIT_API_SECRET` /
  `SKCHAT_LIVEKIT_URL`) — `GET /livekit/config` must report `"available": true`.
- **`.41`** (jarvis): `~/.skenv` with `skchat` installed; Python for the discovery
  client; Playwright + a full Chromium build **if** running the headless browser leg.
- A **Nostr relay** reachable from both hosts (or a seeded relay) for focus/membership
  events. Set `SKCHAT_NOSTR_RELAYS=wss://<relay>` in the **`.158`** webui env (so
  `/sfu/candidates` can resolve), and pass the same relay list to the discovery
  client on `.41`.
  - **GATED note:** if no relay is available, the discovery legs can be exercised
    with an injected `FederationNostr(query=<fake>)` / seeded events (CI-equivalent),
    but that is **not** a true live discovery — flag it as such.

### Tokens / keys / trust (the cross-host auth gate)
- **jarvis's capauth identity key** on `.41`: private armor at
  `~/.skcapstone/agents/jarvis/capauth/identity/private.asc` (passphrase-less — see
  `assertion._default_sign`). This signs the FQID assertion.
- **`.158` must pin jarvis's pubkey** for the FULL fqid. The keystore
  (`src/skchat/spaces/federation/keystore.py`, `federation_pubkey`) resolves the pin
  at `~/.skchat/federation-peers/<safe-fqid>.asc` on `.158`. The filename is the fqid
  with `/ \ \x00` → `_` and `..` stripped. Example: copy jarvis's **public** armor to
  `.158`:
  ```bash
  # on .41 — export jarvis's public key armor
  cat ~/.skcapstone/agents/jarvis/capauth/identity/public.asc
  # on .158 — pin it under the FULL fqid (agent@host.realm), e.g. jarvis@chef.skworld
  mkdir -p ~/.skchat/federation-peers
  # write the armor to ~/.skchat/federation-peers/jarvis@chef.skworld.asc
  ```
  (No pin ⇒ `verify_signed` → `AssertionError: no pubkey` ⇒ `/sfu/get` 403.)
- **`.158` trust policy** (`~/.skchat/federation-trust.json`,
  `src/skchat/spaces/federation/trust.py`) must grant jarvis access. Either the FULL
  fqid `jarvis@chef.skworld` or the host suffix `chef.skworld` in `full_access`, e.g.:
  ```json
  {"full_access": ["jarvis@chef.skworld"], "default": "deny", "remote_max_role": "speaker"}
  ```
  - `default: deny` + no match ⇒ 403. `remote_max_role: "listener"` caps a
    FULL-trust remote at LISTENER (the cap proven in `test_fed_authd_policy.py` /
    `test_fed_trust_remote_cap.py`).

### Identity values (substitute your realm)
- `SPACE_HOST_FQID` — the Space host on `.158`, e.g. `lumina@chef.skworld`.
- `JOINER_FQID` — the federated joiner on `.41`, e.g. `jarvis@chef.skworld`.
- `RELAY` — `wss://<your-relay>`.

---

## Setup commands

> Run **on `.158`** unless tagged `[.41]`.

### A. Confirm the `.158` webui + SFU are live  *(needs-this-live-run)*
```bash
curl -s http://localhost:8765/livekit/config | python3 -m json.tool
#   expect: {"url":"wss://noroc2027.tail204f0c.ts.net:8443", ..., "available": true}

systemctl --user status skchat-webui.service --no-pager
```

### B. Set the relay for `/sfu/candidates`  *(gated-on-wave-5 deploy: needs a relay)*
```bash
# .158 webui env: add SKCHAT_NOSTR_RELAYS, then restart so the route can resolve
systemctl --user edit --full skchat-webui.service   # add Environment=SKCHAT_NOSTR_RELAYS=wss://<relay>
systemctl --user daemon-reload && systemctl --user restart skchat-webui.service
```

### C. Pin jarvis's key + grant trust on `.158`  *(needs-this-live-run)*
See "Tokens / keys / trust" above — write
`~/.skchat/federation-peers/<JOINER_FQID>.asc` and
`~/.skchat/federation-trust.json`.

---

## Step-by-step procedure

### Step 1 — Start a Space on `.158`  *(CI-proven: `test_spaces_routes.py`; LIVE ✅ create path via G-SPACE)*
```bash
# on .158
curl -s -X POST http://localhost:8765/spaces/create \
  -H 'Content-Type: application/json' \
  -d '{"host_fqid":"'"$SPACE_HOST_FQID"'","title":"Fed Test","slug":"fed-test"}' \
  | python3 -m json.tool
```
**Expected:** HTTP 200 JSON with `space_id`, `room`, `url`, `token`, `role:"host"`.
Capture `space_id` → `SPACE_ID`. (If `503 "livekit not configured"`, creds are
missing — fix env first.) The host should then open `/livekit?room=$SPACE_ID&identity=$SPACE_HOST_FQID`
in a browser on `.158` and connect so the LiveKit room actually exists.

### Step 2 — Advertise the focus descriptor + membership to the relay  *(CI-proven: `test_fed_events.py`, `test_fed_nostr_io.py`; live publish needs-this-live-run)*
The focus host publishes (a) its focus descriptor and (b) a membership event for the
Space, so discovery on `.41` can find + elect it. Run on `.158`:
```bash
cd ~ && ~/.skenv/bin/python - <<'PY'
import os, time
from skchat.spaces.federation.nostr_io import FederationNostr
relays = [r for r in os.environ["SKCHAT_NOSTR_RELAYS"].split(",") if r.strip()]
host = os.environ["SPACE_HOST_FQID"]            # e.g. lumina@chef.skworld
space_id = os.environ["SPACE_ID"]
auth_url = "https://noroc2027.tail204f0c.ts.net:8765/sfu/get"   # .158 authd
sfu_ws   = "wss://noroc2027.tail204f0c.ts.net:8443"             # .158 SFU
n = FederationNostr(relays=relays)
print("focus:", n.publish_focus(host_fqid=host, auth_url=auth_url, sfu_ws_url=sfu_ws))
print("space:", n.publish_space(space_id=space_id, title="Fed Test", host_fqid=host, status="live"))
print("member:", n.publish_membership(fqid=host, space_id=space_id,
                                      foci_preferred=host, issued_at=int(time.time())))
PY
```
**Expected:** three `True` lines (publish landed on ≥1 relay). The `auth_url` here is
the descriptor's advertised `/sfu/get` — it is what `discover_and_elect` returns as
`ElectedHost.auth_url` and what `get_token()` POSTs to.
> **Election rule (CI-proven, `test_fed_focus.py`):** `select_focus` picks the
> **oldest** membership's `foci_preferred` (ties → lowest fqid). With one membership,
> that host wins. The elected fqid is then matched against the focus descriptors;
> a missing descriptor ⇒ `DiscoveryError`.

### Step 3 — Bootstrap candidates from `.41`  *(CI-proven: `test_fed_discovery_integration.py`; live needs-this-live-run)*
```bash
# [.41]
curl -s "https://noroc2027.tail204f0c.ts.net:8765/sfu/candidates" | python3 -m json.tool
```
**Expected:** `{"hosts": [{"fqid": "<SPACE_HOST_FQID>", "auth_url": ".../sfu/get",
"sfu_ws_url": "wss://...:8443"}]}`. This route is **never-fatal**: a relay/parse
failure returns `{"hosts": []}` (HTTP 200), not a 500 — so an empty list means the
relay had no focus descriptor (re-check Step 2), not a crash.

### Step 4 — Discover + elect the focus from `.41`  *(CI-proven: `test_fed_discovery.py::test_discover_and_elect_*`; live needs-this-live-run)*
```bash
# [.41]
cd ~ && SKAGENT=jarvis ~/.skenv/bin/python - <<'PY'
import os
from skchat.spaces.federation.discovery import FederationDiscoveryClient, DiscoveryError
relays = [r for r in os.environ["SKCHAT_NOSTR_RELAYS"].split(",") if r.strip()]
space_id = os.environ["SPACE_ID"]
client = FederationDiscoveryClient(relays=relays)
host = client.discover_and_elect(space_id)   # -> ElectedHost(fqid, auth_url, sfu_ws_url)
print("elected:", host.fqid)
print("auth_url:", host.auth_url)
print("sfu_ws_url:", host.sfu_ws_url)
PY
```
**Expected:** prints the `.158` host fqid + its `/sfu/get` auth_url + SFU ws url. A
`DiscoveryError("no focus elected ...")` means no membership was found (Step 2 relay
issue); `DiscoveryError("no focus descriptor ...")` means the membership elected a
host with no advertised descriptor.

### Step 5 — Mint a cross-host token via the live `/sfu/get`  *(server LIVE ✅ G-FED; client `get_token` CI-proven `test_fed_discovery.py`; this exact client→live-authd hop needs-this-live-run)*
```bash
# [.41] — continue in the same shell/script context (uses `host` from Step 4)
cd ~ && SKAGENT=jarvis ~/.skenv/bin/python - <<'PY'
import os
from skchat.spaces.federation.discovery import (
    FederationDiscoveryClient, ElectedHost, AuthDenied, DiscoveryError)
relays = [r for r in os.environ["SKCHAT_NOSTR_RELAYS"].split(",") if r.strip()]
space_id = os.environ["SPACE_ID"]
joiner   = os.environ["JOINER_FQID"]          # jarvis@chef.skworld — must match the signing key
client = FederationDiscoveryClient(relays=relays)
host = client.discover_and_elect(space_id)
try:
    out = client.get_token(host, fqid=joiner, space_id=space_id)
except AuthDenied as e:
    raise SystemExit(f"403 AuthDenied (pin/trust/replay/space-live): {e}")
except DiscoveryError as e:
    raise SystemExit(f"authd non-403 error: {e}")
print("role:", out["role"])          # speaker (or listener if remote_max_role caps)
print("identity:", out["identity"])  # == JOINER_FQID
print("space_id:", out["space_id"])  # == SPACE_ID
print("sfu_ws_url:", out["sfu_ws_url"])
print("token[:24]:", out["token"][:24], "...")
PY
```
**Expected:** a JSON payload with `token`, `role` (`speaker`, or `listener` if
`remote_max_role: "listener"`), `identity == JOINER_FQID`, `space_id == SPACE_ID`,
`sfu_ws_url` = the `.158` SFU. `get_token` signs a **fresh nonce + issued_at** per
call (replay-distinct).
> **Auth gate recap (all CI-proven in `test_fed_authd*.py`):** `/sfu/get` →
> `authd.authorize` → `verify_signed` (pinned pubkey via `federation_pubkey`,
> two-sided freshness ±`max_age=300`) → nonce replay check → `TrustPolicy.access_for`
> → role cap → `mint_space_token`. A **second call with the SAME signed body** ⇒ 403
> `replay detected`; a **tampered claim** ⇒ 403 `signature verification failed`; an
> **unpinned/untrusted fqid** ⇒ 403; an **ended/unknown space** ⇒ 403.

### Step 6 — (Negative checks — recommended)  *(CI-proven; live confirmation needs-this-live-run)*
- **Replay:** re-POST the exact `{claim, sig}` from Step 5 with `curl` →
  expect **403 `replay detected`**.
  ```bash
  # [.41]  (BODY = the {claim,sig} json from build_signed_assertion)
  curl -s -o /dev/null -w '%{http_code}\n' -X POST "$HOST_AUTH_URL" \
    -H 'Content-Type: application/json' -d "$BODY"   # second identical POST -> 403
  ```
- **Tamper:** flip a byte in `claim` and POST → **403** (sig verify fail).
- **Untrusted:** remove the trust entry / pin on `.158` and POST → **403**.
> The cross-host **tamper/replay→403** behaviour is the leg already recorded
> **LIVE ✅** in the matrix (§1f, jarvis@.41 → .158 manual mint). This runbook
> re-runs it through the **client** (`get_token`) rather than a hand-built curl.

### Step 7 — Browser joins the remote focus from `.41`  *(gated-on-wave-5 deploy: needs the live SFU + trusted TLS + Chromium)*
The browser must connect to the **`.158` SFU** using the cross-host token. **Key
nuance:** `static/livekit.html` connects with a pre-authorized `?token=` against
**`cfg.url`** (the page's own webui `/livekit/config` `url`), **not** the token's
`sfu_ws_url`. So the browser must load the **`.158`** webui's livekit page (whose
`cfg.url` is the `.158` SFU), supplying the federation-minted token:
```
https://noroc2027.tail204f0c.ts.net:8765/livekit?room=<SPACE_ID>&identity=<JOINER_FQID>&token=<FED_TOKEN>
```
- The page reads `room`/`identity`/`token` from the query string
  (`URLSearchParams`), and with `qpRoom && qpIdentity` set it auto-`connect()`s,
  using `qpToken` against `cfg.url` (livekit.html lines ~2315/2343 + ~2007).
- Headless option (closes the manual gap): adapt **`scripts/qa_two_browser.py`**,
  which already joins `/livekit?room=&identity=&token=` with a Space-minted token and
  asserts data-channel delivery. For federation, substitute the **federation token
  from Step 5** for one context's `?token=` and point `--base` at the `.158` webui.
  (A dedicated `scripts/qa_fed_browser.py` is the roadmap's Task 5.1 — **not yet
  present**; see Current status.)

**Expected:** the `.41` browser reaches LiveKit room `connected`, the room name
equals `SPACE_ID`, and it sees the Step-1 host participant (remoteParticipants ≥ 1).

### Step 8 — Verify cross-host presence + (optional) data-channel  *(gated-on-wave-5 deploy)*
With both the `.158` host (Step 1) and the `.41` joiner (Step 7) connected to the
**same** room on the **`.158`** SFU:
- Each side's participant list shows the other (2 participants).
- (Optional) the joiner publishes a chat-lane envelope over the WebRTC data channel
  (`publishLane({lane:'chat', ...})`, exactly as `qa_two_browser.py` does) and the
  host renders it in `#chat-messages` — proving real-time cross-host peer delivery.

---

## Federated join (in-browser discovery path)  *(U8 Phase-3 browser wiring)*

Steps 7–8 above join with a **pre-minted `?token=`** against the page's own
`cfg.url`. Phase-3 adds a second, discovery-driven browser path in
`static/livekit.html`: the browser itself discovers the elected focus and
connects to that **remote** SFU `sfu_ws_url` — no hand-carried token URL.

**Deep link** (or check the **"Remote space"** box in the header, then Connect):
```
https://<webui-host>/livekit?federation=1&room=<SPACE_ID>&identity=<JOINER_FQID>
# ?discovery=1 is accepted as an alias for ?federation=1
# ?space=<SPACE_ID> may name the space explicitly (defaults to the room name)
```

**Exact browser steps** (all endpoints below are real, CI-tested routes):
1. On load, `livekit.html` parses `?federation=1`/`?discovery=1` →
   `qpFederation`, and `?space=` → `qpSpace`. With an `identity` present it
   auto-`connect()`s (mirrors the existing `qpRoom && qpIdentity` auto-join).
2. `connect()` calls `discoverFocus()` → **`GET /sfu/candidates`**
   (`routes.py:412`, tested by `tests/test_fed_discovery_integration.py`),
   which returns `{hosts:[{fqid, auth_url, sfu_ws_url}]}` built from the realm's
   focus descriptors **in election order** (oldest-host-first — server-side
   `federation/focus.py::select_focus`, `min` by `(issued_at, fqid)`).
3. The browser elects the focus = **`hosts[0]`** (the server-provided ordering /
   oldest host). Empty list ⇒ a clear "no federated focus advertised" error,
   never a silent local fallback.
4. A participant token is minted via the **existing** `POST /livekit/token`
   flow (the same path a local call uses).
5. `connect()` then **overrides the LiveKit URL** with the elected focus's
   `sfu_ws_url` and runs the **unchanged** `room.connect(livekitUrl, token)`
   path — so the browser lands on the **remote** SFU.

**Honest scope / what the browser does NOT do:** the *cross-host signed
redemption* at the focus's `auth_url` (`POST /sfu/get` with a capauth
`{claim,sig}` assertion) is a **server-side** capauth-signed flow — the browser
holds no capauth signing key, so it is not performed in-page. That redemption is
the server-side `FederationDiscoveryClient.get_token` (`discovery.py`,
unit-tested in `test_fed_discovery.py`) and the live `/sfu/get` authd
(Steps 4–6 above). The in-browser path here reuses the local `/livekit/token`
mint; tightening it to consume a federation-redeemed token end-to-end is a
follow-up. **This browser path is verified by THIS live runbook, not a unit
test** (JS is not unit-tested in this repo).

**Verify:** load the deep link from `.41`, confirm the log shows
`federated focus elected: …` and `federated: overriding SFU URL …`, the status
reaches `connected`, and the Step-1 host appears in the participant list.

---

## Pass / Fail criteria

**PASS (U8 → LIVE ✅) requires ALL of:**
1. **Step 3** — `/sfu/candidates` on `.158` returns the focus host (non-empty).
2. **Step 4** — `discover_and_elect(SPACE_ID)` on `.41` returns the `.158`
   `ElectedHost` (no `DiscoveryError`).
3. **Step 5** — `get_token()` returns a valid token with `identity == JOINER_FQID`,
   `space_id == SPACE_ID`, `sfu_ws_url` = the `.158` SFU, and a `role` consistent with
   the trust policy.
4. **Step 6** — replay and tamper of the Step-5 body both yield **403**.
5. **Step 7–8** — the `.41` browser connects to the **`.158`** room with the
   federation token and both participants see each other (≥ 2 in room).

**FAIL / common stalls:**
- `/sfu/candidates` empty → relay not set on `.158` or Step 2 publish didn't land.
- `DiscoveryError` → no membership (Step 2) or elected host has no descriptor.
- `403 AuthDenied` → missing pin (`federation-peers/<fqid>.asc`), trust default
  `deny`, replayed body, stale/future assertion (>±300 s clock skew), or the Space is
  ended/unknown on `.158`.
- Browser connects but to the **wrong SFU** → you loaded the `.41` livekit page
  (its `cfg.url` is the `.41` SFU); load the **`.158`** webui page instead (Step 7
  nuance).
- Browser connect timeout → SFU down, TLS cert untrusted by Chromium, or ICE can't
  complete from the runner (the qa harness reports the exact failure rather than
  faking a pass).

---

## Current status (leg-by-leg)

| Leg | Module / proof | Status |
|---|---|---|
| `FederationDiscoveryClient` core (`discover_and_elect`, `get_token`, `build_signed_assertion`) | `discovery.py` + `test_fed_discovery.py` | **CI-proven** (fakes for relay/post/sign) |
| `GET /sfu/candidates` bootstrap | `routes.py:412` + `test_fed_discovery_integration.py` | **CI-proven** (relay monkeypatched) |
| Focus election (oldest-host-wins) | `focus.select_focus` + `test_fed_focus.py` | **CI-proven** |
| Signed assertion build/verify | `assertion.py` + `test_fed_assertion.py` | **CI-proven** |
| `/sfu/get` authd (verify→trust→nonce→mint) | `authd.authorize` + `routes.py:382` + `test_fed_authd*.py`, `test_fed_sfu_get_policy.py` | **CI-proven** |
| Cross-host token mint (jarvis@.41 → .158), capped, tamper/replay→403 | matrix §1f / G-FED | **LIVE ✅** (hand-built curl) |
| **Client→live-authd hop** (`get_token` against the real `/sfu/get`) | this runbook, Steps 4–6 | **needs this live run** |
| **Live Nostr discovery** (real relay publish/query, Steps 2–3) | this runbook | **gated on wave-5 deploy** (a reachable/seeded relay) |
| **Full browser join of the remote focus** (Steps 7–8) | `scripts/qa_two_browser.py` (adapt) / roadmap Task 5.1 `qa_fed_browser.py` (absent) | **gated on wave-5 deploy** (both hosts up, live SFU, trusted TLS, Chromium) |

**Matrix rows this run advances** (`docs/qa/skworld-comms-verification-matrix.md`):
- §1f "Client discovery / focus-election" — currently **LIVE ⏳ (gap)** → flip to
  **LIVE ✅** once Steps 3–5 pass live.
- §3 U8 — "token mint **LIVE ✅**; full browser join **LIVE ⏳**" → the browser-join
  half flips on Steps 7–8.

**Roadmap reference** (`docs/sprint/epics-roadmap-2026-06.md`): U8 client code is the
shipped first-wave (Tasks 2+3); the live two-host browser join is the gated Task 5.1/5.2
("Both hosts .41 + .158 up; live LiveKit SFU; live Nostr relay; Tailscale/real TLS cert
trust for cross-host `/sfu/get` POST; Playwright headless browser").
