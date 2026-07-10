# SKChat Web App + API Architecture

> **Why this doc exists:** someone once spent hours patching `/api/send` +
> `/inbox` in `webui.py` expecting it to change what the Flutter app does.
> It did nothing — the app doesn't call those routes. This doc exists so
> that never happens again.

## 0. TL;DR — read this before touching anything

**The Flutter app (`static/app/`, served at `/app`) talks to `/api/v1/*`,
implemented in `src/skchat/daemon_proxy.py`.**

**It does NOT use `/api/send`, `/send`, or `/inbox` in `src/skchat/webui.py`.**
Those three routes are a legacy/parallel HTML+HTMX web-UI surface (the
"`/legacy`" page) plus an older native-client JSON contract that predates
the Flutter build. They are still mounted and still work, but editing them
changes nothing the app does.

If you are debugging "the app isn't doing X" or "I fixed Y but nothing
changed":

1. Grep `main.dart.js` (`src/skchat/static/app/main.dart.js`) for the path
   you're chasing — it's compiled-in, so `grep -o '"/api/v1/[a-z/]*"'` will
   show you the real contract the running build uses.
2. Set `SKCHAT_DEBUG_REQ=1` on the webui unit and `journalctl --user -u
   skchat-webui@lumina -f` — the debug middleware
   (`webui.py:_debug_log_app_requests`, line ~45) logs every POST/PUT and
   every call/rtc/livekit/group request with method + path + body. This is
   the fastest way to see literally which route the app hit.
3. If the log shows `/api/v1/...` you are in `daemon_proxy.py`. If it shows
   `/api/send` or `/send`, something old (a cached PWA, a stale client, a
   curl test) is calling the legacy surface, not the app.

Both surfaces are mounted on the **same FastAPI app** in `webui.py`:

```python
# webui.py — near line 189
from .daemon_proxy import router as daemon_api_router
app.include_router(daemon_api_router)   # adds /api/v1/*, /api/health, /api/board, ...
```

`daemon_api_router = APIRouter(prefix="/api")`, so every route declared in
`daemon_proxy.py` as `@router.get("/v1/...")` actually resolves as
`GET /api/v1/...`. The legacy `@app.post("/api/send")` in `webui.py` lives on
the exact same app object, one path away. They are easy to confuse in a
diff or a grep for `/api/` — always check the file, not just the path.

---

## 1. API reference — `/api/v1/*` (the real contract, `daemon_proxy.py`)

All routes below are declared on `router = APIRouter(prefix="/api")` in
`src/skchat/daemon_proxy.py`. Identity constants used throughout:

```python
LUMINA_ID   = "lumina@chef.skworld"              # fqid-form (see §4)
LUMINA_URI  = "capauth:lumina@skworld.io"         # capauth wire-form
LUMINA_NAME = "Lumina"
OPERATOR_ID = "chef@skworld.io"                   # bare-form (no capauth: prefix)
```

### Chat surface

| Method | Path | Body | Returns |
|---|---|---|---|
| GET | `/api/v1/peers` | — | `[conversation-shape, ...]` — Lumina **always first**, then `_other_peers()` read from `~/.skcapstone/peers/*.json` |
| GET | `/api/v1/conversations` | — | `[Lumina] + [other peers] + [groups]` (Lumina pinned first; groups carry `is_group:true`) |
| GET | `/api/v1/conversations/{peer_id}` | — | One thread, oldest-first. `peer_id` = Lumina alias → her thread; a known group id → `_group_messages()`; anything else → `[]` |
| GET | `/api/v1/inbox` | — | `{"messages": [...]}` — **only the operator↔Lumina thread** (`_lumina_messages(limit=500)`), despite the generic name. Group/peer messages are NOT in this payload — use `/conversations/{id}` per-thread. |
| POST | `/api/v1/send` | see §1.1 | persists + (for Lumina) brain reply, or fan-out for a group, or plain persist for another peer |
| POST | `/api/v1/react` | `{conversation_id, message_id, emoji, op:"add"|"remove", sender?}` | updated message (full contract) |
| POST | `/api/v1/edit` | `{message_id, body}` | updated message; 403 outside the 24h edit window |
| POST | `/api/v1/receipt` | `{message_id, kind:"delivered"|"read", sender?}` | updated message |
| GET | `/api/v1/thread/{thread_id}` | — | `{thread_id, messages:[...]}` |
| POST | `/api/v1/presence` | — | `{"ok": true}` (stub — see §7 presence-event noise) |

### Groups

| Method | Path | Body | Notes |
|---|---|---|---|
| GET | `/api/v1/groups` | — | all groups, conversation shape |
| POST | `/api/v1/groups` | `{name, members?:[{identity}|str], description?, acl?}` | creates via `daemon_proxy_groups.create_group`; creator = operator, always admin |
| PUT | `/api/v1/groups/{group_id}` | `{name?, description?, acl?}` | |
| DELETE | `/api/v1/groups/{group_id}` | — | admin-only (403 otherwise); writes a `.deleted.json` tombstone |
| GET | `/api/v1/groups/{group_id}/members` | — | member list, app shape |
| POST | `/api/v1/groups/{group_id}/members` | `{identity, role?}` | adds a member; if `group_id` isn't a group yet, **promotes** a 1:1 thread of that id into a group (`promote_one_to_one`, history migrated) |
| DELETE | `/api/v1/groups/{group_id}/members/self` | — | operator leaves (key rotates) |
| DELETE | `/api/v1/groups/{group_id}/members/{identity}` | — | admin-only remove (key rotates) |

### Group calls (LiveKit, Phase 3)

| Method | Path | Notes |
|---|---|---|
| POST | `/api/v1/groups/{group_id}/call/start` | body `{topic?, ring?:bool=true}`; 403 if caller not a member, 503 if LiveKit creds missing; rings other members via the 1:1 `CALL_INVITE` mechanism |
| POST | `/api/v1/groups/{group_id}/call/join` | same gate, no ring |
| GET | `/api/v1/groups/{group_id}/call/participants` | queries the SFU `RoomService.ListParticipants`; degrades to `active:0` rather than 500 |

### PQC / prekeys (Q5 hybrid KEM)

| Method | Path | Notes |
|---|---|---|
| POST | `/api/v1/prekey` | app publishes its (`chef`'s) hybrid prekey bundle |
| GET | `/api/v1/prekey/{peer}` | `peer=lumina` returns **Lumina's own** hybrid prekey (generated on demand); anything else returns the stored/classical bundle |

### Misc / passthrough

| Method | Path | Proxies to | Notes |
|---|---|---|---|
| GET | `/api/health` | `http://127.0.0.1:9385/health` | skchat daemon health |
| GET | `/api/v1/status` | `http://127.0.0.1:9383/api/v1/household/agents` | |
| GET | `/api/board` | `http://127.0.0.1:7778/api/board` | skcapstone dashboard; same-origin proxy since `:7778` isn't tailscale-served |
| GET | `/api/v1/capabilities` | `http://127.0.0.1:9384/api/v1/capabilities` (`_SKCOMMS_API`) | skcomms-api service-discovery doc |
| GET | `/api/v1/household/agents` | `http://127.0.0.1:9383/api/v1/household/agents` | |
| GET | `/api/v1/identity` | — | static `{identity: LUMINA_ID, ...}` |
| GET | `/api/v1/webrtc/ice-config` | — | via `connectivity.ice_config()` |
| GET | `/api/v1/webrtc/peers` | — | always `[]` (stub) |
| POST | `/api/v1/access/token` | `http://127.0.0.1:9384/api/v1/access/token` | proxies the capauth access-token mint so the app (same-origin) can reach skos Ops surfaces without holding the PGP key itself |
| GET | `/api` (i.e. `/api/`) | — | `{"status":"ok","service":"skchat-daemon-proxy"}` |

### 1.1 `POST /api/v1/send` in detail (`daemon_proxy.py:949`)

**Body fields** (all optional except you need `recipient` + text):

| Field | Meaning |
|---|---|
| `recipient` (or `peer_id`) | target peer/group id |
| `message` (or `content`) | the text |
| `group_id` | explicit group id (falls back to `recipient` if unset) |
| `reply_to_id` (or `reply_to`) | message being replied to |
| `thread_id` | thread linkage |
| `content_type` | typed-message contract (P1) — e.g. `"location"`; unknown types still carry `body` (Golden rule, see `models.py: ContentType`) |
| `rich` | typed payload dict paired with `content_type` |

**Three send paths, decided in this order:**

1. **Group fan-out** — if `target_group = group_id or recipient` resolves to
   a persisted `GroupChat` (`daemon_proxy_groups.load_group`) **and**
   `recipient` isn't a Lumina alias (a stray same-id group file must never
   hijack the brain-reply path), the ACL is checked
   (`G.can_post`, 403 if read-only/announcement and sender isn't admin) and
   `G.fan_out_send(...)` runs (see §2). Returns
   `{ok, id, recipient, group_id, ts, message}` where `message` is the group
   message in the shared contract.
2. **Non-Lumina 1:1** — persist-only, no brain, no network delivery. Returns
   `{ok, id, recipient, ts, message}`. ("Keeps the route honest" — a message
   to a peer that isn't Lumina and isn't a group is stored, not delivered.)
3. **Lumina 1:1 (the "real brain" path)**:
   - Persist the operator's turn (`_persist(hist, OPERATOR_ID, LUMINA_URI, ...)`).
   - If the incoming `content` starts with `pqdm1:` (hybrid-sealed by the
     app), open it with Lumina's hybrid private key
     (`_open_hybrid_inbound`, via `skcomms.pqdm.open_sealed` +
     `pq_prekeys.lumina_private()`) — the plaintext feeds history + the
     brain; `convo_is_hybrid=True` is remembered so the reply is sealed
     symmetrically.
   - Build prior-turn context: `_lumina_messages(limit=40)` mapped to
     `{role, content}` (skip the just-stored turn).
   - Invoke `_get_brain().reply(content, history=convo, sender="chef")` —
     `_get_brain()` lazily constructs a `LuminaBrain` from
     `scripts/bridge_consciousness.py` (the **same module** the Telegram
     bridge uses — same soul + FEB + qwen3.6, not a generic assistant).
     Any exception → a graceful `"…(thinking failed — ...)"` fallback is
     persisted instead of a 500.
   - If `convo_is_hybrid`, seal the reply back with `_seal_hybrid_outbound`
     (using the operator's stored prekey via `pq_prekeys.load_peer_bundle`);
     otherwise store plaintext.
   - Persist the reply linked via `reply_to_id=user_msg.id`.
   - Broadcast `{"type":"new"}` over the legacy webui's `/ws/chat` (best
     effort — `webui._ws_broadcast`) so any open legacy web client refreshes.
   - Returns `{ok, id: user_msg.id, recipient: LUMINA_ID, ts, reply: <full
     message contract>}`.

All three paths call `webui._ws_broadcast({"type": "new"})` — this is the
**only** coupling between `daemon_proxy.py` and the legacy `webui.py` surface;
it exists purely so a still-open legacy `/legacy` page also refreshes.

---

## 2. Group send + fan-out — end to end

```
Flutter app
  │  POST /api/v1/send  {recipient: <group_id>, message: "..."}
  ▼
daemon_proxy.api_send()                      [daemon_proxy.py:949]
  │  target_group = group_id or recipient; G.load_group(target_group)
  │  G.can_post(group, OPERATOR_ID)  → 403 if read-only/announcement & not admin
  ▼
daemon_proxy_groups.fan_out_send()           [daemon_proxy_groups.py:438]
  │  1. Save the canonical group-thread ChatMessage
  │     (recipient="group:<id>", thread_id=<id>) to local ChatHistory.
  │  2. Build a delivery transport: _delivery_transport(sender_uri)
  │     → SKComms.from_config() + ChatTransport (best-effort; None if
  │       skcomms has no configured/available transports).
  │  3. For every OTHER member:
  │       a. Save a per-member copy locally (recipient=member_uri,
  │          thread_id=<group_id>) — so the OPERATOR's own inbox/thread
  │          views show it immediately regardless of network delivery.
  │       b. If a transport was built: transport.send_message(member_msg)
  │          — this is the actual NETWORK hop to that member's daemon.
  │          Best-effort per member; one failure doesn't abort the others.
  ▼
Member's skchat daemon (e.g. skchat-daemon-opus)          [daemon.py]
  │  Polls its transport, receives the message, ChatHistory.save()'s it.
  │  _is_group_message(msg, group_cfg.groups) → true if recipient starts
  │  "group:" or thread_id is set (and, if SKCHAT_GROUPS is non-empty,
  │  the group key is in that allow-list).
  ▼
GroupResponder.respond(msg)                   [group_responder.py:202]
  │  should_respond(): the agent only replies if a NON-agent human
  │  explicitly @-mentions it (loop-breaker — see §7).
  │  Builds soul+FEB system prompt, skmemory recall, calls skgateway
  │  (SKCHAT_GROUP_BACKEND_URL, default role sk-default @ :18780 — registry-routed).
  ▼
daemon.py reply delivery                       [daemon.py:421-460]
  │  Loads the group, persists the reply LOCALLY via grp.send(reply,
  │  transport=None, ...) (no raw skcomms multicast — see §7 "'*' cooldown"),
  │  THEN fans it out per-member over the normal 1:1 DM transport
  │  (transport.send_message(...) to each member's identity, thread_id=gid).
  ▼
Back through each member's daemon → their own fan-out-style delivery →
eventually reaches the operator's app, which sees it via
GET /api/v1/conversations/{group_id} on its next poll/refresh.
```

**Key point:** `fan_out_send` used to be described as "persists locally
AND now network-delivers" — that "now" matters. Before the
`_delivery_transport` wiring, group sends only ever reached the operator's
own local history; other members' daemons never saw the message and could
never respond. If group auto-replies stop working, the first thing to check
is whether `_delivery_transport()` is returning `None` (no configured/
available skcomms transport) — it fails silently by design (best-effort).

---

## 3. Identity model — three forms, one member

SKChat identities show up in **three different string shapes** depending on
which layer produced them. All three must resolve to "the same person" for
ACL/`get_member`/routing checks to work, and mismatches between them cause
**silent** failures (a message not delivered, an ACL check that returns
"not a member" for someone who obviously is).

| Form | Example | Produced by |
|---|---|---|
| **capauth (wire)** | `capauth:chef@skworld.io` | `capauth.resolve_agent_identity().capauth_uri` — always `capauth:<agent>@skworld.io` |
| **fqid (sovereign, three-tier)** | `chef@chef.skworld` | `capauth.resolve_agent_identity().fqid` — `<agent>@<operator>.<realm>`, requires `cluster.json` |
| **bare** | `chef@skworld.io` | ad-hoc — just `<handle>@skworld.io`, no `capauth:` prefix |

**Concrete proof this is a real trap, not a hypothetical:** in
`daemon_proxy.py` itself:

```python
LUMINA_ID   = "lumina@chef.skworld"       # <- fqid form
LUMINA_URI  = "capauth:lumina@skworld.io" # <- capauth form
OPERATOR_ID = "chef@skworld.io"           # <- bare form
```

Three different agents' identity constants are declared in **three
different forms** in the same file. `_is_lumina()` (line 138) has to
enumerate every alias by hand (`"lumina"`, `LUMINA_ID.lower()`,
`LUMINA_URI.lower()`, `"lumina@skworld.io"`, the fingerprint) because there
is no single canonical string to compare against.

### `GroupChat.get_member()` — the matching that saves you

`group.py:317` `get_member(identity_uri)` matches **either** the exact
stored string **or** the "handle" (lowercased, `capauth:` prefix stripped,
`@...` suffix stripped):

```python
def _handle(u: str) -> str:
    return (u or "").lower().split(":", 1)[-1].split("@", 1)[0]
```

So `chef@skworld.io`, `capauth:chef@skworld.io`, and `chef@chef.skworld`
all resolve to the member stored as any one of those forms, because they
all reduce to the handle `chef`. **This is the safety net** — but it only
works inside `group.py`. Code that does a raw string `==` comparison
against `OPERATOR_ID`/`LUMINA_ID` (e.g. `_group_msg_to_app`'s
`outbound = sender in (OPERATOR_ID, "chef", "chef@skworld.io")`) does NOT
get this normalization, so if a sender arrives in a fourth spelling it will
silently be treated as inbound-from-someone-else / not-me.

**Rule of thumb:** when adding a new identity comparison, either route it
through `GroupChat.get_member()` / `_handle()`-style normalization, or
enumerate all three known forms explicitly (as `_is_lumina()` does) — never
assume one canonical spelling exists elsewhere in the codebase.

---

## 4. Port / webui / daemon / serve topology

Confirmed live 2026-07-04 via `systemctl --user list-units 'skchat-*'` and
`tailscale serve status`.

| Component | Unit | Port | State (confirmed) |
|---|---|---|---|
| Lumina webui (Flutter app host + `/api/v1/*` + legacy `/api/*`) | `skchat-webui@lumina.service` | `:8765` | **active**, enabled |
| Opus webui | `skchat-webui@opus.service` | `:8766` (env file exists, `~/.config/skchat/webui-opus.env`) | **disabled, inactive** — the unit template and per-agent env exist but nothing is currently listening on 8766. Don't assume opus has a live web surface without checking. |
| Lumina daemon (poll/receive) | `skchat-daemon.service` | health `:9385` (default) | active |
| Opus daemon (isolated store) | `skchat-daemon-opus.service` | `SKCHAT_HOME=~/.skchat-opus`, health `:9388` | active |
| Chef daemon (human, receive-only) | `skchat-daemon-chef.service` | `SKCHAT_HOME=~/.skchat-chef`, health `:9389` | defined; check `systemctl --user status` for current state |
| Piper TTS (fast CPU) | `skchat-piper-tts.service` | `:18797` | active |
| Nostr discovery relay | `skchat-nostr-relay.service` | `:7447` | active |
| Telegram bridges | `skchat-telegram-{lumina,opus}.service` | — | active |
| Lumina LiveKit conversational agent | `skchat-lumina-call.service` | — | active |
| **Stray/legacy** static Flutter build server | `skchat-app-web.service` | `:8088`, `python3 -m http.server` in `~/clawd/skcapstone-repos/skchat-app/build/web` | **active** — a SEPARATE static build, in a SEPARATE repo checkout (`skchat-app`, not `skchat`), served with no API behind it at all. This is NOT the same `/app` the webui serves at `:8765/app` (which comes from `src/skchat/static/app/` inside THIS repo). Don't confuse the two when chasing "which build is the user actually looking at." |

### tailscale serve / funnel mapping

```
https://noroc2027.tail204f0c.ts.net (tailnet-only, no port = 443)
  /                              → http://localhost:8765            (webui root, redirects to /app/)
  /daemon                        → http://127.0.0.1:9385            (chat daemon health/API)
  /livekit-ws                    → http://100.108.59.57:7880        (LiveKit SFU)
  /api/v1/inbox                  → http://localhost:9384/api/v1/inbox   ⚠ see pitfall below
  /.well-known/skfed/directory   → http://localhost:9384/.well-known/skfed/directory

https://noroc2027.tail204f0c.ts.net:8443 (tailnet-only)
  /                              → http://100.108.59.57:7880        (LiveKit SFU, alt port)

https://noroc2027.tail204f0c.ts.net:10000 (Funnel — PUBLIC)
  /api        → http://localhost:8765/api        (→ webui → daemon_proxy /api/v1/*)
  /app        → http://localhost:8765/app        (the real Flutter build)
  /join       → http://localhost:8765/join
  /conf       → http://localhost:8765/conf
  /daemon     → http://127.0.0.1:9385
  /livekit    → http://localhost:8765/livekit
  /guest/join → http://localhost:8765/guest/join

https://noroc2027.tail204f0c.ts.net:10001 (Funnel — PUBLIC)
  /  → http://100.108.59.57:7880                  (LiveKit SFU, public)
```

**The public app URL** `https://noroc2027.tail204f0c.ts.net/app` (or
`:10000/app`) is Lumina's webui — but the app itself sends as
`OPERATOR_ID = chef@skworld.io`, not as Lumina. "Lumina's webui" describes
which systemd unit/port is serving the static build and `/api/v1/*`
backend, not who the human user is inside the chat.

⚠ **Pitfall — a shadow `/api/v1/inbox`:** the bare-domain (no-port,
tailnet-only) serve config maps `/api/v1/inbox` **directly to
`localhost:9384`** (skcomms-api), completely bypassing `daemon_proxy.py`
and its Lumina-thread-only inbox. That is a **different implementation**
of a route with the same name. If you test against
`https://noroc2027.tail204f0c.ts.net/api/v1/inbox` (no port) you get
skcomms-api's answer; if you test against `:10000/api/v1/inbox` (Funnel) or
same-origin from the app (`:8765/api/v1/inbox`) you get
`daemon_proxy.api_inbox()`. Always check which serve entry (and which port)
a request actually went through before concluding a route is broken.

---

## 5. LiveKit / calls

- **SFU**: `livekit-server.service`, config `~/.config/livekit/livekit.yaml`.
  - `port: 7880`, `rtc.tcp_port: 7881`, `rtc.port_range_start/end: 50000-50200`,
    `rtc.node_ip: 100.108.59.57` (tailnet IP — `use_external_ip: false`, no
    STUN/TURN needed on-tailnet).
  - `keys:` map holds one shared secret **per agent**: `skchat-lumina`,
    `skchat-opus`, `skchat-chef` — these must match each webui's
    `SKCHAT_LIVEKIT_API_KEY`/`SKCHAT_LIVEKIT_API_SECRET` env pair, or token
    minting will succeed locally but the SFU will reject the room join.
- **Webui env** (`~/.config/skchat/webui-{lumina,opus}.env`):
  `SKCHAT_LIVEKIT_URL` (`wss://noroc2027.tail204f0c.ts.net:8443`),
  `SKCHAT_LIVEKIT_API_KEY`, `SKCHAT_LIVEKIT_API_SECRET`,
  `SKCHAT_LIVEKIT_DEFAULT_ROOM` (`lumina-and-chef` / `opus-and-chef`),
  plus TURN (`SKCHAT_TURN_*`) and `SKCHAT_LIVEKIT_PUBLIC_URL`
  (`wss://noroc2027.tail204f0c.ts.net/livekit-ws`).
- **1:1 call flow** (`call_routes.py`, registered into `webui.py`'s `app`
  via `register_call_routes`):
  - `POST /call/start {peer}` — resolves the peer to an FQID
    (`_resolve_peer`), derives a deterministic per-pair room
    (`call_session.derive_room`), mints a LiveKit token, **sends a signed
    `CALL_INVITE`** over `skcomms.mailbox` to the peer, and alerts the
    operator (best-effort, never raises).
  - `POST /call/answer {peer}` — same prep, **no** invite sent (answering
    never rings).
  - `GET /call/incoming` — surfaces only **signature-verified** invites
    addressed to `self` (`_self_fqid()` via `capauth.resolve_agent_identity`).
  - `GET /call/peers`, `GET /connectivity/ice` — roster + ICE ladder
    (`connectivity.ice_config`, tailnet-first).
- **Group call flow** lives in `daemon_proxy.py` (§1, "Group calls"), not
  `call_routes.py` — it reuses the 1:1 ring mechanism but gates on group
  membership (`daemon_proxy_groupcall.group_call_context`) instead of
  pairing.
- **`/api/facetime/*`** (`facetime.py`) is a **separate, older** aiortc/
  MuseTalk video path — `/facetime`, `/facetime/{agent}`,
  `/ws/facetime/{agent}` (WS fallback), `/api/facetime/sessions` /
  `/api/facetime/agents`. This is not the LiveKit stack; it's kept as a
  fallback for non-LiveKit clients. Don't conflate a "facetime" bug report
  with the LiveKit call path — check which route was actually hit.

---

## 6. The legacy surface (`webui.py`) — for completeness, not for app work

These exist, work, and are safe to leave alone — but changes here have
**no effect** on the Flutter app:

| Route | Purpose |
|---|---|
| `GET /` | redirects to `/app/` (the Flutter build) |
| `GET /legacy` | the original server-rendered HTMX chat page |
| `POST /send` (form body) | HTMX form-post send, re-renders the message list HTML |
| `POST /api/send` (JSON body `{recipient, content}`) | an older **JSON** native-client contract — predates `/api/v1/send`; still fans out to a `group:` recipient (webui.py:1094) using its own copy of the fan-out logic (NOT `daemon_proxy_groups.fan_out_send`) |
| `GET /inbox` | JSON message dump (`{id, sender, recipient, content, timestamp, delivery_status, thread_id}`), unrelated to `/api/v1/inbox`'s Lumina-only payload |
| `GET /groups` | JSON group listing enriched from `PeerDiscovery` — different shape from `/api/v1/groups` |
| `GET /health` | webui health, includes resolved agent name + OOF level |
| `WS /ws/chat` | pushes `{"type":"new"}`; both the legacy page and, incidentally, `daemon_proxy.api_send()` broadcast on it |
| `SKCHAT_DEBUG_REQ` middleware | `_debug_log_app_requests` (webui.py:44) — logs method/path/query/body(400 chars) for POST/PUT or any path containing `call`/`rtc`/`livekit`/`group`, when `SKCHAT_DEBUG_REQ=1`/`true`/`yes`. **This is the fastest way to prove which surface a client actually hit** — turn it on before guessing. |
| `GET /federation/status` | registered separately via `federation_status.py` — read-only observability (identity, relays, trust policy, live conf/space counters) |

**The one real coupling**: `daemon_proxy.api_send()` calls
`webui._ws_broadcast({"type": "new"})` so a still-open legacy page also
refreshes. That's the only place the two surfaces touch.

---

## 7. Common pitfalls

1. **Wrong API — the one this doc exists to prevent.** The app uses
   `/api/v1/*` in `daemon_proxy.py`. `webui.py`'s `/api/send`, `/send`,
   `/inbox` are a separate legacy surface. Confirm which file owns a route
   before editing (`grep -rn "your/path" src/skchat/*.py`), and confirm
   which surface a live client is hitting with `SKCHAT_DEBUG_REQ=1` before
   assuming your fix landed.

2. **Identity-form mismatch.** Three shapes exist (capauth wire / fqid /
   bare — §3). A hardcoded `==` comparison against one form silently
   fails against another. `GroupChat.get_member()` normalizes via
   handle-matching; ad-hoc comparisons elsewhere (e.g.
   `_group_msg_to_app`'s `sender in (OPERATOR_ID, "chef", "chef@skworld.io")`)
   do not. When wiring a new identity check, either reuse the
   `get_member`/handle-normalization pattern or enumerate every known form.

3. **`fan_out_send` looking local-only.** It persists to the operator's
   local `ChatHistory` unconditionally, but only **network-delivers** to
   member daemons if `_delivery_transport()` successfully builds a
   `ChatTransport` from `SKComms.from_config()`. That call is best-effort
   and silently returns `None` on any failure — group replies will look
   "stuck at me" with no error if the transport can't be built. Check
   `logger.debug("group delivery transport unavailable: ...")` in the
   daemon log.

4. **Advocacy/GroupResponder loop.** Agents never auto-respond to another
   agent's message, even a literal `@mention` — `should_respond()`
   (`group_responder.py:89`) refuses if the sender's handle is in
   `cfg.peer_agents` (defaults to "every known agent except self":
   lumina/opus/jarvis/ava/artisan/herald/sentinel/architect/scholar/
   steward/coder) or is the agent itself. Only a **human** mention
   triggers a reply. `SKCHAT_GROUPS` gates which group ids a given
   daemon's `GroupResponder` is even wired for; `SKCHAT_ADVOCACY_DISABLED`
   disables the separate 1:1 `AdvocacyEngine` (not the group responder).
   If two agents seem to be silently not-replying to each other, that is
   the loop-breaker working as designed, not a bug.

5. **Presence-event noise on `'*'`.** The daemon broadcasts presence
   roughly every 60s (`daemon.py`, `presence_counter >= 12` at a 5s poll
   interval) to the broadcast recipient `'*'`. `skcomms/router.py`
   explicitly excludes `recipient == '*'` from the transport failure-
   cooldown counter (`_try_send`, "a presence heartbeat to `'*'` every 60s
   starving real DMs" — see router.py ~line 729) — so a broadcast failure
   never blocks real 1:1 sends, but it does mean `'*'` traffic is easy to
   mistake for real message volume in logs/inbox dumps if you're not
   filtering it out.

6. **Transport cooldown on `'*'` — the flip side.** Because `'*'` sends
   never arm the cooldown, `GroupChat.send(transport=...)`'s original
   design (a single `transport.send(member_uri, payload)` multicast) is
   NOT what actually gets used for delivering GroupResponder replies —
   `daemon.py` (~line 436) explicitly avoids the raw multicast ("group
   multicast over raw skcomms mis-routes to `'*'`") and instead calls
   `grp.send(reply, transport=None, ...)` (local persist only) followed by
   a manual per-member loop calling `transport.send_message(...)` — the
   same reliable 1:1 DM path fan-out uses on the send side (§2). If you
   see a reply persisted in the group thread but never delivered to other
   members' daemons, check this per-member loop, not `GroupChat.send()`.

7. **The shadow `/api/v1/inbox`.** See §4 — the bare tailnet-domain serve
   entry (no port) maps `/api/v1/inbox` straight to skcomms-api (`:9384`),
   a completely different handler than `daemon_proxy.api_inbox()`. Always
   note which host:port a failing request actually went through.

8. **Two Flutter builds.** `skchat-app-web.service` serves a *separate*
   static build (`~/clawd/skcapstone-repos/skchat-app/build/web`, port
   `:8088`, no API behind it) that is easy to confuse with the real
   `/app` served by the webui at `:8765` (built from
   `src/skchat/static/app/` in *this* repo). If a UI fix "isn't showing
   up," check which of the two the person is actually looking at.

9. **`skchat-webui@opus` isn't necessarily running.** The per-agent unit
   template and env file (`:8766`) exist, but as of the last check it was
   `disabled`/`inactive`. Don't assume opus has a live web surface without
   checking `systemctl --user status skchat-webui@opus.service`.
