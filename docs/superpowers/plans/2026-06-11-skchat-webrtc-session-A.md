# WebRTC Session After Pairing — Sub-project A Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Let any two paired peers (human-in-browser or agent) start a real-time A/V call after pairing — over our existing LiveKit server, each carrying their capauth FQID identity, with a sovereign-first connectivity ladder (Tailscale → LAN → coturn TURN).

**Architecture:** A deterministic per-pair LiveKit room (both sides hash the sorted FQIDs → identical room, zero negotiation). The caller mints a LiveKit JWT (identity = its capauth FQID) and rings the peer with a `CALL_INVITE` over the existing skcomms mailbox; the callee surfaces a ring, accepts, recomputes the same room locally, and joins via the already-working `livekit.html`. A shared `connectivity` module supplies the ICE policy (tier 1 Tailscale = no relay; tier 3 = ephemeral coturn creds against the shared skstack coturn `skhub.<cluster>.<domain>:3478`, the same one Nextcloud Talk + netbird use).

**Tech Stack:** Python 3.11, FastAPI, pytest + pytest-asyncio, `livekit-api` (JWT mint), `skcomms` (peers/tofu/mailbox), `capauth` (identity), LiveKit JS client (existing page).

**Spec:** `docs/superpowers/specs/2026-06-11-skchat-webrtc-session-A-design.md`

**Shared infra note:** TURN/coturn and Cloudflare Tunnel are **two different planes** —
reachability (cloudflared/Tailscale serve, how clients reach our HTTP/WS) vs media/ICE
(coturn relays WebRTC UDP). cloudflared is a valid *reachability* option but **won't do
TURN**. The coturn is deployed in the **skhub stack** at `skhub.<cluster>.<domain>:3478`
(turn,turns / udp,tcp) — the *same* coturn used by Nextcloud Talk + netbird (verified in
`SKStacks/v1/ansible/optional/skhub/deploy_skhub-prod.yml`). Tier 3 reuses it via the
`use-auth-secret` REST scheme; the static-auth-secret is the vault var `skhub.turn_secret`,
sourced into env `SKCHAT_TURN_SECRET`, **never committed** (the `#COTURN_SECRET=` line in
`skhub.env.j2` is a placeholder example — do not use it).
⚠️ **Host to confirm:** config says `skhub.<cluster>.<domain>:3478`; Chef recalled
`signal.nativeassetmanagement.com`. Set the live value via `SKCHAT_TURN_URLS` at
provisioning (Task 8) — do not hardcode. **coturn-breakout** (decouple from the
month-down skhub stack) is a recommended ops follow-up, out of scope for A's code.

**Key existing APIs (verified):**
- `capauth.resolve_agent_identity(agent=None) -> AgentIdentity` → `.fqid` (e.g. `lumina@chef.skworld`), `.capauth_uri`, `.fingerprint`, `.agent`
- `skcomms.peers.list_peers() -> dict` keyed by FQID; value `{added_at, fingerprint, syncthing_device_id}`
- `skcomms.mailbox.send_message(to_fqid, message, *, agent=None, subject=None, thread_id=None, ...) -> dict`
- `skcomms.mailbox.read_inbox(agent=None) -> list[tuple[Envelope, VerificationResult]]`; `Envelope` fields: `version,id,from_fqid,to_fqid,created_at,content_type,body,subject,thread_id,reply_to,headers`
- `skchat.livekit_routes._mint_token(identity, name, room, ttl) -> str`, `LIVEKIT_URL`, `_have_creds()`

---

## File Structure

- **Create** `src/skchat/call_session.py` — pure: `derive_room()`, CALL_INVITE subject const + `build_invite_body()`/`parse_invite_body()`.
- **Create** `src/skchat/connectivity.py` — `ice_config()`, coturn ephemeral-cred derivation, tier detection.
- **Create** `src/skchat/call_routes.py` — `register_call_routes(app)`: `/call/start`, `/call/answer`, `/call/incoming`, `/connectivity/ice`. Imports `_mint_token`/`LIVEKIT_URL`/`_have_creds` from `livekit_routes`.
- **Modify** `src/skchat/webui.py` — call `register_call_routes(app)`; add Call button + ring banner JS; pass ICE to the call page.
- **Modify** `src/skchat/mcp_server.py` — add `call_peer` MCP tool.
- **Config** (not repo): `~/.config/livekit/livekit.yaml` (`skchat-opus` key), `~/.config/skchat/webui-opus.env` (LiveKit + TURN vars).
- **Tests**: `tests/test_call_session.py`, `tests/test_connectivity.py`, `tests/test_call_routes.py`, `tests/test_call_integration.py`, plus a `call_peer` case in `tests/test_mcp_server.py`.

**Not a code task (Plane 1 — reachability):** exposing the webui/signaling via Tailscale
serve (current) vs Cloudflare Tunnel (cloudflared→Traefik) is an ingress/deployment
choice, not skchat code — A's HTTP/WS is agnostic to it. cloudflared fronts signaling +
the webui but cannot relay WebRTC UDP (still needs tier-3 TURN). Documented here so it
isn't mistaken for missing work; wiring a cloudflared ingress is an ops follow-up.

---

## Task 1: Deterministic room derivation

**Files:**
- Create: `src/skchat/call_session.py`
- Test: `tests/test_call_session.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_call_session.py
from skchat.call_session import derive_room


def test_derive_room_is_order_independent():
    a, b = "lumina@chef.skworld", "opus@chef.skworld"
    assert derive_room(a, b) == derive_room(b, a)


def test_derive_room_is_stable_and_well_formed():
    room = derive_room("lumina@chef.skworld", "opus@chef.skworld")
    assert room.startswith("call-")
    assert room == derive_room("lumina@chef.skworld", "opus@chef.skworld")
    # opaque: neither raw FQID appears in the room name
    assert "lumina" not in room and "opus" not in room
    # 16 lowercase base32 chars after the prefix
    suffix = room[len("call-"):]
    assert len(suffix) == 16 and suffix == suffix.lower()


def test_derive_room_distinct_pairs_differ():
    assert derive_room("a@x.y", "b@x.y") != derive_room("a@x.y", "c@x.y")
```

- [ ] **Step 2: Run test to verify it fails**

Run: `~/.skenv/bin/python -m pytest tests/test_call_session.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'skchat.call_session'`

- [ ] **Step 3: Write minimal implementation**

```python
# src/skchat/call_session.py
"""Deterministic per-pair call room + CALL_INVITE envelope helpers.

A call room is derived purely from the two participants' capauth FQIDs, so both
sides compute the same room with zero negotiation. The room name is an opaque
hash (FQIDs are not leaked to the LiveKit server's room logs).
"""

from __future__ import annotations

import base64
import hashlib


def derive_room(fqid_a: str, fqid_b: str) -> str:
    """Return a stable, order-independent room name for a pair of FQIDs.

    Args:
        fqid_a: one participant's capauth FQID (e.g. ``lumina@chef.skworld``).
        fqid_b: the other participant's FQID.

    Returns:
        ``"call-" + <16 lowercase base32 chars>`` — identical regardless of
        argument order.
    """
    joined = "\n".join(sorted([fqid_a.strip(), fqid_b.strip()]))
    digest = hashlib.sha256(joined.encode("utf-8")).digest()
    b32 = base64.b32encode(digest).decode("ascii").rstrip("=").lower()
    return "call-" + b32[:16]
```

- [ ] **Step 4: Run test to verify it passes**

Run: `~/.skenv/bin/python -m pytest tests/test_call_session.py -v`
Expected: PASS (3 passed)

- [ ] **Step 5: Commit**

```bash
git add src/skchat/call_session.py tests/test_call_session.py
git commit -m "feat(call): deterministic per-pair room derivation"
```

---

## Task 2: CALL_INVITE envelope helpers

**Files:**
- Modify: `src/skchat/call_session.py`
- Test: `tests/test_call_session.py`

- [ ] **Step 1: Write the failing test** (append)

```python
# tests/test_call_session.py  (append)
from skchat.call_session import (
    CALL_INVITE_SUBJECT,
    build_invite_body,
    parse_invite_body,
)


def test_invite_body_roundtrip():
    body = build_invite_body(
        from_fqid="opus@chef.skworld",
        to_fqid="lumina@chef.skworld",
        room="call-abc",
        livekit_url="wss://noroc2027.tail204f0c.ts.net:8443",
    )
    inv = parse_invite_body(body)
    assert inv["type"] == "CALL_INVITE"
    assert inv["from_fqid"] == "opus@chef.skworld"
    assert inv["to_fqid"] == "lumina@chef.skworld"
    assert inv["room"] == "call-abc"
    assert inv["transport"] == "livekit"
    assert "nonce" in inv and "ts" in inv


def test_parse_invite_rejects_non_invite():
    import pytest
    with pytest.raises(ValueError):
        parse_invite_body('{"type":"SOMETHING_ELSE"}')
```

- [ ] **Step 2: Run test to verify it fails**

Run: `~/.skenv/bin/python -m pytest tests/test_call_session.py -k invite -v`
Expected: FAIL — `ImportError: cannot import name 'CALL_INVITE_SUBJECT'`

- [ ] **Step 3: Write minimal implementation** (append to `call_session.py`)

```python
# src/skchat/call_session.py  (append)
import json
import os
import time
import uuid

CALL_INVITE_SUBJECT = "CALL_INVITE"
CALL_ACCEPT_SUBJECT = "CALL_ACCEPT"
CALL_DECLINE_SUBJECT = "CALL_DECLINE"


def build_invite_body(
    *, from_fqid: str, to_fqid: str, room: str, livekit_url: str
) -> str:
    """Serialize a CALL_INVITE control payload (JSON string) for skcomms."""
    return json.dumps(
        {
            "type": CALL_INVITE_SUBJECT,
            "from_fqid": from_fqid,
            "to_fqid": to_fqid,
            "room": room,
            "transport": "livekit",
            "livekit_url": livekit_url,
            "ts": int(time.time()),
            "nonce": uuid.uuid4().hex,
        }
    )


def parse_invite_body(body: str) -> dict:
    """Parse + validate a CALL_INVITE payload. Raises ValueError if not one."""
    data = json.loads(body)
    if data.get("type") != CALL_INVITE_SUBJECT:
        raise ValueError(f"not a CALL_INVITE: type={data.get('type')!r}")
    return data
```

> Note: `uuid.uuid4()` is fine here (runtime code, not a workflow script).

- [ ] **Step 4: Run test to verify it passes**

Run: `~/.skenv/bin/python -m pytest tests/test_call_session.py -v`
Expected: PASS (5 passed)

- [ ] **Step 5: Commit**

```bash
git add src/skchat/call_session.py tests/test_call_session.py
git commit -m "feat(call): CALL_INVITE envelope build/parse helpers"
```

---

## Task 3: Connectivity ICE policy (Tailscale → LAN → coturn)

**Files:**
- Create: `src/skchat/connectivity.py`
- Test: `tests/test_connectivity.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_connectivity.py
import base64
import hashlib
import hmac

from skchat.connectivity import ice_config


def test_tier1_both_on_tailnet_has_no_relay(monkeypatch):
    monkeypatch.delenv("SKCHAT_TURN_SECRET", raising=False)
    cfg = ice_config(
        local_fqid="lumina@chef.skworld",
        peer_fqid="opus@chef.skworld",
        peer_hint={"on_tailnet": True},
    )
    assert cfg["preferred_tier"] == 1
    assert cfg["on_tailnet"] is True
    assert cfg["ice_servers"] == []


def test_tier3_cross_nat_emits_ephemeral_turn(monkeypatch):
    monkeypatch.setenv("SKCHAT_TURN_SECRET", "s3cr3t")
    monkeypatch.setenv(
        "SKCHAT_TURN_URLS", "turn:signal.nativeassetmanagement.com:3478?transport=udp"
    )
    cfg = ice_config(
        local_fqid="lumina@chef.skworld",
        peer_fqid="opus@chef.skworld",
        peer_hint={"on_tailnet": False},
    )
    assert cfg["preferred_tier"] == 3
    turn = [s for s in cfg["ice_servers"] if any("turn:" in u for u in s["urls"])]
    assert turn, "expected a TURN server entry"
    entry = turn[0]
    # coturn REST scheme: username = "<expiry>:<peer>", credential = base64(hmac-sha1)
    assert ":" in entry["username"]
    expiry, _, who = entry["username"].partition(":")
    assert who == "lumina@chef.skworld"
    expected = base64.b64encode(
        hmac.new(b"s3cr3t", entry["username"].encode(), hashlib.sha1).digest()
    ).decode()
    assert entry["credential"] == expected


def test_secret_never_appears_in_config(monkeypatch):
    monkeypatch.setenv("SKCHAT_TURN_SECRET", "topsecret-xyz")
    monkeypatch.setenv("SKCHAT_TURN_URLS", "turn:signal.nativeassetmanagement.com:3478")
    cfg = ice_config("a@x.y", "b@x.y", peer_hint={"on_tailnet": False})
    assert "topsecret-xyz" not in repr(cfg)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `~/.skenv/bin/python -m pytest tests/test_connectivity.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'skchat.connectivity'`

- [ ] **Step 3: Write minimal implementation**

```python
# src/skchat/connectivity.py
"""Connectivity / ICE policy — sovereign-first ladder.

Tier 1: Tailscale (both peers on the tailnet) — no relay needed.
Tier 2: same-network / LAN — host candidates only (no servers emitted).
Tier 3: coturn TURN via the shared skstack coturn (skhub.<cluster>.<domain>:3478),
        ephemeral REST credentials (use-auth-secret). Same coturn as Nextcloud/netbird.
Tier 4: skmesh / netbird overlay — designed-for, not emitted here yet.

The static-auth-secret is read from SKCHAT_TURN_SECRET (sourced from the skstack
coturn config); only short-lived derived credentials ever leave this module.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import os
import time

# Local-import note: callers pass a peer_hint dict so this stays pure + testable.

_TURN_TTL_SECONDS = int(os.getenv("SKCHAT_TURN_TTL", "300"))


def _turn_credentials(local_fqid: str, secret: str, ttl: int) -> tuple[str, str]:
    """coturn `use-auth-secret` REST credentials.

    username = "<unix-expiry>:<identity>"; credential = base64(HMAC-SHA1(secret, username)).
    """
    expiry = int(time.time()) + ttl
    username = f"{expiry}:{local_fqid}"
    credential = base64.b64encode(
        hmac.new(secret.encode(), username.encode(), hashlib.sha1).digest()
    ).decode("ascii")
    return username, credential


def ice_config(local_fqid: str, peer_fqid: str, peer_hint: dict | None = None) -> dict:
    """Return ICE config + preferred-tier policy for a call to ``peer_fqid``.

    Args:
        local_fqid: our capauth FQID (becomes the TURN credential identity).
        peer_fqid: the peer's FQID (informational; tier comes from peer_hint).
        peer_hint: {"on_tailnet": bool, "same_subnet": bool} — reachability hints.

    Returns:
        {ice_servers, policy, preferred_tier, on_tailnet}.
    """
    hint = peer_hint or {}
    on_tailnet = bool(hint.get("on_tailnet"))
    same_subnet = bool(hint.get("same_subnet"))

    if on_tailnet:
        return {"ice_servers": [], "policy": "all", "preferred_tier": 1, "on_tailnet": True}
    if same_subnet:
        return {"ice_servers": [], "policy": "all", "preferred_tier": 2, "on_tailnet": False}

    # Tier 3 — coturn relay.
    secret = os.getenv("SKCHAT_TURN_SECRET", "")
    urls_raw = os.getenv("SKCHAT_TURN_URLS", "")
    ice_servers: list[dict] = []
    stun = os.getenv("SKCHAT_STUN_URLS", "")
    if stun:
        ice_servers.append({"urls": [u.strip() for u in stun.split(",") if u.strip()]})
    if secret and urls_raw:
        username, credential = _turn_credentials(local_fqid, secret, _TURN_TTL_SECONDS)
        ice_servers.append(
            {
                "urls": [u.strip() for u in urls_raw.split(",") if u.strip()],
                "username": username,
                "credential": credential,
            }
        )
    return {
        "ice_servers": ice_servers,
        "policy": "all",
        "preferred_tier": 3,
        "on_tailnet": False,
    }
```

- [ ] **Step 4: Run test to verify it passes**

Run: `~/.skenv/bin/python -m pytest tests/test_connectivity.py -v`
Expected: PASS (3 passed)

- [ ] **Step 5: Commit**

```bash
git add src/skchat/connectivity.py tests/test_connectivity.py
git commit -m "feat(call): connectivity ICE ladder (tailscale->lan->coturn)"
```

---

## Task 4: `/call/start` + `/call/answer` routes

**Files:**
- Create: `src/skchat/call_routes.py`
- Test: `tests/test_call_routes.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_call_routes.py
import json

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

import skchat.call_routes as cr
from skchat.call_session import derive_room


@pytest.fixture
def client(monkeypatch):
    # paired peer set: only lumina is paired
    monkeypatch.setattr(
        cr, "_list_peers", lambda: {"lumina@chef.skworld": {"fingerprint": "FP"}}
    )
    monkeypatch.setattr(cr, "_self_fqid", lambda: "opus@chef.skworld")
    monkeypatch.setattr(cr, "_have_creds", lambda: True)
    monkeypatch.setattr(cr, "_mint_token", lambda identity, name, room, ttl: f"tok::{identity}::{room}")
    sent = []
    monkeypatch.setattr(cr, "_send_invite", lambda **kw: sent.append(kw))
    app = FastAPI()
    cr.register_call_routes(app)
    c = TestClient(app)
    c._sent = sent  # type: ignore[attr-defined]
    return c


def test_call_start_rejects_unpaired(client):
    r = client.post("/call/start", json={"peer": "stranger@x.y"})
    assert r.status_code == 404


def test_call_start_mints_and_rings(client):
    r = client.post("/call/start", json={"peer": "lumina@chef.skworld"})
    assert r.status_code == 200
    data = r.json()
    expected_room = derive_room("opus@chef.skworld", "lumina@chef.skworld")
    assert data["room"] == expected_room
    assert data["token"] == f"tok::opus@chef.skworld::{expected_room}"
    assert data["peer_fqid"] == "lumina@chef.skworld"
    assert len(client._sent) == 1  # exactly one CALL_INVITE


def test_call_answer_mints_same_room_without_ringing(client):
    r = client.post("/call/answer", json={"peer": "lumina@chef.skworld"})
    assert r.status_code == 200
    data = r.json()
    assert data["room"] == derive_room("opus@chef.skworld", "lumina@chef.skworld")
    assert len(client._sent) == 0  # answer must NOT send an invite
```

- [ ] **Step 2: Run test to verify it fails**

Run: `~/.skenv/bin/python -m pytest tests/test_call_routes.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'skchat.call_routes'`

- [ ] **Step 3: Write minimal implementation**

```python
# src/skchat/call_routes.py
"""Call orchestration routes: start (ring), answer (no ring), incoming, ICE.

Builds on the deterministic room (call_session) + LiveKit token mint
(livekit_routes) + skcomms mailbox for the CALL_INVITE ring.
"""

from __future__ import annotations

import logging

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse

from .call_session import (
    CALL_INVITE_SUBJECT,
    build_invite_body,
    derive_room,
    parse_invite_body,
)
from .connectivity import ice_config
from .livekit_routes import LIVEKIT_URL, _have_creds, _mint_token

logger = logging.getLogger("skchat.call_routes")
_TOKEN_TTL = 21600


# --- thin wrappers (monkeypatchable seams; keep I/O out of route bodies) -----
def _list_peers() -> dict:
    from skcomms.peers import list_peers
    return list_peers()


def _self_fqid() -> str:
    from capauth import resolve_agent_identity
    return resolve_agent_identity().fqid


def _send_invite(*, from_fqid: str, to_fqid: str, room: str, livekit_url: str) -> None:
    from skcomms.mailbox import send_message
    body = build_invite_body(
        from_fqid=from_fqid, to_fqid=to_fqid, room=room, livekit_url=livekit_url
    )
    send_message(to_fqid, body, subject=CALL_INVITE_SUBJECT)


def _read_inbox() -> list:
    from skcomms.mailbox import read_inbox
    return read_inbox()


def _resolve_peer(peer: str) -> str:
    """Resolve a peer arg (FQID or bare name) to a paired FQID, or 404."""
    peers = _list_peers()
    if peer in peers:
        return peer
    matches = [fqid for fqid in peers if fqid.split("@", 1)[0] == peer]
    if len(matches) == 1:
        return matches[0]
    raise HTTPException(status_code=404, detail=f"peer not paired: {peer}")


def _prepare_call(peer: str) -> dict:
    if not _have_creds():
        raise HTTPException(status_code=503, detail="livekit not configured")
    peer_fqid = _resolve_peer(peer)
    local_fqid = _self_fqid()
    room = derive_room(local_fqid, peer_fqid)
    token = _mint_token(local_fqid, local_fqid.split("@", 1)[0], room, _TOKEN_TTL)
    return {
        "room": room,
        "token": token,
        "livekit_url": LIVEKIT_URL,
        "peer_fqid": peer_fqid,
        "local_fqid": local_fqid,
        "identity": local_fqid,
    }


async def _peer_arg(request: Request) -> str:
    try:
        body = await request.json()
    except Exception:
        body = dict(await request.form())
    peer = (body.get("peer") or "").strip()
    if not peer:
        raise HTTPException(status_code=400, detail="peer required")
    return peer


def register_call_routes(app: FastAPI) -> None:
    @app.post("/call/start")
    async def call_start(request: Request) -> JSONResponse:
        peer = await _peer_arg(request)
        ctx = _prepare_call(peer)
        _send_invite(
            from_fqid=ctx["local_fqid"],
            to_fqid=ctx["peer_fqid"],
            room=ctx["room"],
            livekit_url=ctx["livekit_url"],
        )
        return JSONResponse(
            {k: ctx[k] for k in ("room", "token", "livekit_url", "peer_fqid", "identity")}
        )

    @app.post("/call/answer")
    async def call_answer(request: Request) -> JSONResponse:
        peer = await _peer_arg(request)
        ctx = _prepare_call(peer)  # NB: no _send_invite — answering never rings
        return JSONResponse(
            {k: ctx[k] for k in ("room", "token", "livekit_url", "peer_fqid", "identity")}
        )
```

- [ ] **Step 4: Run test to verify it passes**

Run: `~/.skenv/bin/python -m pytest tests/test_call_routes.py -v`
Expected: PASS (3 passed)

- [ ] **Step 5: Commit**

```bash
git add src/skchat/call_routes.py tests/test_call_routes.py
git commit -m "feat(call): /call/start (ring) + /call/answer (no ring) routes"
```

---

## Task 5: `/call/incoming` (ring source)

**Files:**
- Modify: `src/skchat/call_routes.py`
- Test: `tests/test_call_routes.py`

- [ ] **Step 1: Write the failing test** (append)

```python
# tests/test_call_routes.py  (append)
from types import SimpleNamespace

from skchat.call_session import build_invite_body


def _env(subject, from_fqid, to_fqid, room):
    body = build_invite_body(
        from_fqid=from_fqid, to_fqid=to_fqid, room=room,
        livekit_url="wss://x:8443",
    )
    return SimpleNamespace(subject=subject, from_fqid=from_fqid, to_fqid=to_fqid, body=body)


def test_incoming_returns_only_invites_for_self(client, monkeypatch):
    inbox = [
        (_env("CALL_INVITE", "lumina@chef.skworld", "opus@chef.skworld", "call-r1"), None),
        (_env("text/plain note", "lumina@chef.skworld", "opus@chef.skworld", "call-x"), None),
        (_env("CALL_INVITE", "stranger@x.y", "someone@else.z", "call-r2"), None),  # not for us
    ]
    monkeypatch.setattr(cr, "_read_inbox", lambda: inbox)
    r = client.get("/call/incoming")
    assert r.status_code == 200
    invites = r.json()["invites"]
    assert len(invites) == 1
    assert invites[0]["from_fqid"] == "lumina@chef.skworld"
    assert invites[0]["room"] == "call-r1"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `~/.skenv/bin/python -m pytest tests/test_call_routes.py -k incoming -v`
Expected: FAIL — 404 (route not registered)

- [ ] **Step 3: Write minimal implementation** (add inside `register_call_routes`)

```python
    @app.get("/call/incoming")
    async def call_incoming() -> JSONResponse:
        """Surface CALL_INVITE envelopes addressed to us, newest first."""
        me = _self_fqid()
        invites = []
        for env, _verify in _read_inbox():
            if getattr(env, "subject", None) != CALL_INVITE_SUBJECT:
                continue
            if getattr(env, "to_fqid", None) != me:
                continue  # never trust an invite not addressed to us
            try:
                inv = parse_invite_body(env.body)
            except ValueError:
                continue
            invites.append(inv)
        invites.sort(key=lambda i: i.get("ts", 0), reverse=True)
        return JSONResponse({"invites": invites})
```

- [ ] **Step 4: Run test to verify it passes**

Run: `~/.skenv/bin/python -m pytest tests/test_call_routes.py -v`
Expected: PASS (4 passed)

- [ ] **Step 5: Commit**

```bash
git add src/skchat/call_routes.py tests/test_call_routes.py
git commit -m "feat(call): /call/incoming surfaces CALL_INVITE rings for self"
```

---

## Task 6: `/connectivity/ice` endpoint

**Files:**
- Modify: `src/skchat/call_routes.py`
- Test: `tests/test_call_routes.py`

- [ ] **Step 1: Write the failing test** (append)

```python
# tests/test_call_routes.py  (append)
def test_connectivity_ice_for_paired_peer(client):
    r = client.get("/connectivity/ice", params={"peer": "lumina@chef.skworld"})
    assert r.status_code == 200
    data = r.json()
    assert "ice_servers" in data and "preferred_tier" in data


def test_connectivity_ice_rejects_unpaired(client):
    r = client.get("/connectivity/ice", params={"peer": "nobody@x.y"})
    assert r.status_code == 404
```

- [ ] **Step 2: Run test to verify it fails**

Run: `~/.skenv/bin/python -m pytest tests/test_call_routes.py -k connectivity -v`
Expected: FAIL — 404 for the paired peer too (route not registered)

- [ ] **Step 3: Write minimal implementation** (add inside `register_call_routes`)

```python
    @app.get("/connectivity/ice")
    async def connectivity_ice(peer: str) -> JSONResponse:
        peer_fqid = _resolve_peer(peer)
        local_fqid = _self_fqid()
        # On-tailnet detection is best-effort; default optimistic (tier 1) on the
        # tailnet-served deployment. A peer_hint can be threaded later for tiers 2/3.
        cfg = ice_config(local_fqid, peer_fqid, peer_hint={"on_tailnet": True})
        return JSONResponse(cfg)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `~/.skenv/bin/python -m pytest tests/test_call_routes.py -v`
Expected: PASS (6 passed)

- [ ] **Step 5: Commit**

```bash
git add src/skchat/call_routes.py tests/test_call_routes.py
git commit -m "feat(call): /connectivity/ice returns tier-aware ICE for a paired peer"
```

---

## Task 7: Wire routes into the webui + Call button + ring banner

**Files:**
- Modify: `src/skchat/webui.py` (route registration near line 81-83; pairing/peers UI)
- Test: `tests/test_call_routes.py` (registration smoke)

- [ ] **Step 1: Write the failing test** (append)

```python
# tests/test_call_routes.py  (append)
def test_webui_registers_call_routes():
    from skchat.webui import app
    paths = {r.path for r in app.routes}
    assert "/call/start" in paths
    assert "/call/answer" in paths
    assert "/call/incoming" in paths
    assert "/connectivity/ice" in paths
```

- [ ] **Step 2: Run test to verify it fails**

Run: `~/.skenv/bin/python -m pytest tests/test_call_routes.py -k webui_registers -v`
Expected: FAIL — paths not present (call routes not yet wired into `webui.app`)

- [ ] **Step 3: Wire registration in `webui.py`**

Find (around line 81-83):
```python
    from .livekit_routes import register_livekit_routes as _register_livekit_routes

    _register_livekit_routes(app)
```
Replace with:
```python
    from .livekit_routes import register_livekit_routes as _register_livekit_routes
    from .call_routes import register_call_routes as _register_call_routes

    _register_livekit_routes(app)
    _register_call_routes(app)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `~/.skenv/bin/python -m pytest tests/test_call_routes.py -k webui_registers -v`
Expected: PASS

- [ ] **Step 5: Add the Call button + ring banner to the peers UI (manual, then smoke)**

In the paired-peers list section of `webui.py` (the HTML served at `/pair` / peers view),
add per-peer a button and a small poller. Insert this `<script>` into that page's HTML:
```html
<div id="ring-banner" style="display:none;position:fixed;top:0;left:0;right:0;
  background:#143;color:#fff;padding:12px;text-align:center;z-index:9999"></div>
<script>
async function callPeer(fqid){
  const r = await fetch('/call/start',{method:'POST',
    headers:{'Content-Type':'application/json'},body:JSON.stringify({peer:fqid})});
  if(!r.ok){alert('call failed: '+r.status);return;}
  const d = await r.json();
  location.href = '/livekit?room='+encodeURIComponent(d.room)
    +'&identity='+encodeURIComponent(d.identity);
}
async function answerPeer(fqid){
  const r = await fetch('/call/answer',{method:'POST',
    headers:{'Content-Type':'application/json'},body:JSON.stringify({peer:fqid})});
  if(!r.ok){alert('answer failed: '+r.status);return;}
  const d = await r.json();
  location.href = '/livekit?room='+encodeURIComponent(d.room)
    +'&identity='+encodeURIComponent(d.identity);
}
async function pollRing(){
  try{
    const r = await fetch('/call/incoming'); if(!r.ok)return;
    const {invites} = await r.json();
    const b = document.getElementById('ring-banner');
    if(invites && invites.length){
      const inv = invites[0];
      b.innerHTML = '📞 Incoming call from '+inv.from_fqid+' '
        +'<button onclick="answerPeer(\''+inv.from_fqid+'\')">Accept</button>';
      b.style.display='block';
    } else { b.style.display='none'; }
  }catch(e){}
}
setInterval(pollRing, 4000); pollRing();
</script>
```
For each peer row, add: `<button onclick="callPeer('FQID')">Call</button>` (substitute the
row's FQID). Reuse the existing peers-listing loop in that handler.

- [ ] **Step 6: Smoke-test the page renders + routes work**

Run: `~/.skenv/bin/python -m pytest tests/test_call_routes.py -v`
Then manual: `curl -s -o /dev/null -w "%{http_code}\n" http://localhost:8765/pair` → 200.
Expected: tests PASS; `/pair` serves 200 with the Call button + ring poller.

- [ ] **Step 7: Commit**

```bash
git add src/skchat/webui.py tests/test_call_routes.py
git commit -m "feat(webui): wire call routes + Call button + incoming-call ring banner"
```

---

## Task 8: opus LiveKit key + TURN secret provisioning (config)

**Files:**
- Modify: `~/.config/livekit/livekit.yaml` (add `skchat-opus` key)
- Modify: `~/.config/skchat/webui-opus.env` (LiveKit + TURN vars)
- (No repo code; this is host config. Verify with a check, then commit a note if any repo doc references it.)

- [ ] **Step 1: Generate a fresh opus LiveKit secret + add the key**

Run:
```bash
OPUS_SECRET=$(~/.skenv/bin/python -c "import secrets;print(secrets.token_hex(32))")
# append under keys: in ~/.config/livekit/livekit.yaml
~/.skenv/bin/python - "$OPUS_SECRET" <<'PY'
import sys, pathlib, re
secret = sys.argv[1]
p = pathlib.Path.home()/".config/livekit/livekit.yaml"
txt = p.read_text()
if "skchat-opus:" not in txt:
    txt = re.sub(r"(\nkeys:\n)", rf"\1  skchat-opus: {secret}\n", txt, count=1)
    p.write_text(txt)
    print("added skchat-opus key")
else:
    print("skchat-opus already present")
PY
echo "$OPUS_SECRET"   # capture for the env file in the next step
```
Expected: "added skchat-opus key" + the secret printed once.

- [ ] **Step 2: Add LiveKit + TURN vars to `webui-opus.env`**

Replace the `# LiveKit/WebRTC for opus is task 7f28ac51 — no room wired yet.` line in
`~/.config/skchat/webui-opus.env` with (substitute `<OPUS_SECRET>` from Step 1; source
`<TURN_SECRET>` from the skstack coturn static-auth-secret — do NOT invent it):
```
SKCHAT_LIVEKIT_URL=wss://noroc2027.tail204f0c.ts.net:8443
SKCHAT_LIVEKIT_API_KEY=skchat-opus
SKCHAT_LIVEKIT_API_SECRET=<OPUS_SECRET>
SKCHAT_LIVEKIT_DEFAULT_ROOM=opus-and-chef
SKCHAT_TURN_URLS=turn:<COTURN_HOST>:3478?transport=udp,turn:<COTURN_HOST>:3478?transport=tcp
SKCHAT_STUN_URLS=stun:<COTURN_HOST>:3478
SKCHAT_TURN_SECRET=<TURN_SECRET>
```
Add the same three `SKCHAT_TURN_*`/`SKCHAT_STUN_*` lines to `webui-lumina.env` too
(lumina already has its LiveKit block).

> 🔴 `<COTURN_HOST>` — confirm the live skhub coturn host (config: `skhub.<cluster>.<domain>`,
> e.g. `skhub.nativeassetmanagement.com`; Chef recalled `signal.nativeassetmanagement.com`).
> `<TURN_SECRET>` = the vault `skhub.turn_secret` static-auth-secret (the SAME one Nextcloud
> Talk + netbird use). Source it from the skstack vault — do NOT use the placeholder
> `#COTURN_SECRET=` in `skhub.env.j2`, never reuse the leaked NC_PASS, never commit it.
> If the skhub stack is down (it has been ~1mo), tier-3 TURN is unavailable until coturn is
> reachable — calls still work on tier 1 (Tailscale). Consider the coturn-breakout ops task.

- [ ] **Step 3: Reload services + verify both agents mint tokens**

Run:
```bash
systemctl --user restart livekit-server.service
systemctl --user restart skchat-webui@lumina skchat-webui@opus
sleep 5
for p in 8765 8766; do
  curl -s -X POST http://localhost:$p/livekit/token \
    -H 'Content-Type: application/json' \
    -d '{"identity":"test","room":"smoke"}' -o /dev/null -w ":$p token -> %{http_code}\n"
done
```
Expected: both `:8765 token -> 200` and `:8766 token -> 200`.

- [ ] **Step 4: Commit (repo doc note only)**

No repo secrets. If a repo doc references the env, update it; otherwise:
```bash
git commit --allow-empty -m "chore(call): opus LiveKit key + shared coturn TURN provisioned (host config)"
```

---

## Task 9: `call_peer` MCP tool (headless agents)

**Files:**
- Modify: `src/skchat/mcp_server.py`
- Test: `tests/test_mcp_server.py`

- [ ] **Step 1: Write the failing test** (append to test_mcp_server.py)

```python
# tests/test_mcp_server.py  (append)
class TestCallPeer:
    def test_call_peer_returns_room_and_token(self, monkeypatch):
        import skchat.mcp_server as m
        monkeypatch.setattr(
            m, "_prepare_call_for", lambda peer: {
                "room": "call-xyz", "token": "tok", "peer_fqid": "lumina@chef.skworld",
                "livekit_url": "wss://x:8443", "identity": "opus@chef.skworld",
            }
        )
        sent = []
        monkeypatch.setattr(m, "_ring_peer", lambda **kw: sent.append(kw))
        out = m.call_peer("lumina")
        assert out["room"] == "call-xyz"
        assert out["peer_fqid"] == "lumina@chef.skworld"
        assert len(sent) == 1
```

- [ ] **Step 2: Run test to verify it fails**

Run: `~/.skenv/bin/python -m pytest tests/test_mcp_server.py -k CallPeer -v`
Expected: FAIL — `AttributeError: module 'skchat.mcp_server' has no attribute '_prepare_call_for'`

- [ ] **Step 3: Write minimal implementation** (add to `mcp_server.py`, near the other tools)

```python
# src/skchat/mcp_server.py  (add)
def _prepare_call_for(peer: str) -> dict:
    """Resolve + mint a call context for a peer (reuses call_routes logic)."""
    from .call_routes import _prepare_call
    return _prepare_call(peer)


def _ring_peer(*, from_fqid: str, to_fqid: str, room: str, livekit_url: str) -> None:
    from .call_routes import _send_invite
    _send_invite(from_fqid=from_fqid, to_fqid=to_fqid, room=room, livekit_url=livekit_url)


def call_peer(peer: str) -> dict:
    """MCP tool: place a call to a paired peer. Returns {room, token, ...} and rings them.

    Args:
        peer: paired peer FQID or bare name.
    """
    ctx = _prepare_call_for(peer)
    _ring_peer(
        from_fqid=ctx["identity"], to_fqid=ctx["peer_fqid"],
        room=ctx["room"], livekit_url=ctx["livekit_url"],
    )
    return {k: ctx[k] for k in ("room", "token", "livekit_url", "peer_fqid", "identity")}
```
Then register `call_peer` as an MCP tool following the existing tool-registration pattern
in this file (mirror how `initiate_call` is exposed — same decorator/registration call).

- [ ] **Step 4: Run test to verify it passes**

Run: `~/.skenv/bin/python -m pytest tests/test_mcp_server.py -k CallPeer -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add src/skchat/mcp_server.py tests/test_mcp_server.py
git commit -m "feat(mcp): call_peer tool — place a call to a paired peer"
```

---

## Task 10: Integration — opus↔lumina land in the same room

**Files:**
- Test: `tests/test_call_integration.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_call_integration.py
"""Cross-agent invariant: opus (start) and lumina (answer) land in the SAME room
with DISTINCT identities — driven through the real route handlers with stubbed I/O."""
from fastapi import FastAPI
from fastapi.testclient import TestClient

import skchat.call_routes as cr
from skchat.call_session import CALL_INVITE_SUBJECT, derive_room


def _make_client(monkeypatch, self_fqid, paired_fqid, sent):
    """A TestClient for one agent: it sees `paired_fqid` as its only peer."""
    monkeypatch.setattr(cr, "_have_creds", lambda: True)
    monkeypatch.setattr(cr, "_mint_token", lambda i, n, r, t: f"tok::{i}::{r}")
    monkeypatch.setattr(cr, "_self_fqid", lambda: self_fqid)
    monkeypatch.setattr(cr, "_list_peers", lambda: {paired_fqid: {"fingerprint": "x"}})
    monkeypatch.setattr(cr, "_send_invite", lambda **kw: sent.append(kw))
    app = FastAPI()
    cr.register_call_routes(app)
    return TestClient(app)


def test_opus_starts_lumina_answers_same_room(monkeypatch):
    sent: list = []
    # opus is the local agent here; it starts the call to lumina.
    opus = _make_client(monkeypatch, "opus@chef.skworld", "lumina@chef.skworld", sent)
    r_start = opus.post("/call/start", json={"peer": "lumina@chef.skworld"})
    assert r_start.status_code == 200
    start = r_start.json()

    # Now re-point the same module seams to lumina and have it answer opus.
    lumina = _make_client(monkeypatch, "lumina@chef.skworld", "opus@chef.skworld", sent)
    r_ans = lumina.post("/call/answer", json={"peer": "opus@chef.skworld"})
    assert r_ans.status_code == 200
    answer = r_ans.json()

    # Same room, distinct identities, exactly one CALL_INVITE (from start, not answer).
    assert start["room"] == answer["room"] == derive_room(
        "opus@chef.skworld", "lumina@chef.skworld"
    )
    assert start["identity"] != answer["identity"]
    assert len(sent) == 1
    assert sent[0]["to_fqid"] == "lumina@chef.skworld"
```

> This locks the cross-agent invariant (same room, distinct identities, one ring) through
> the real handlers with stubbed I/O. A true end-to-end live call is verified manually in
> the runbook (two browsers / a join-agent), not in CI.

- [ ] **Step 2: Run the test**

Run: `~/.skenv/bin/python -m pytest tests/test_call_integration.py -v`
Expected: PASS (it drives the real `call_routes` handlers built in Tasks 1-6 with stubbed
I/O). This is an invariant-lock, not a red→green step — if it FAILS, a prior task
regressed (e.g. `_prepare_call` stopped deriving the room order-independently); fix that.

- [ ] **Step 3: (No new impl — the invariant is provided by Tasks 1-6.)**

- [ ] **Step 4: Run the full suite**

Run: `~/.skenv/bin/python -m pytest tests/test_call_session.py tests/test_connectivity.py tests/test_call_routes.py tests/test_call_integration.py -v`
Expected: ALL PASS.

- [ ] **Step 5: Commit**

```bash
git add tests/test_call_integration.py
git commit -m "test(call): cross-agent same-room + distinct-identity invariant"
```

---

## Final verification (after all tasks)

- [ ] Full suite green: `~/.skenv/bin/python -m pytest tests/ -q` (no regressions).
- [ ] Both webuis serve `/pair` (200) with Call button + ring poller.
- [ ] Both `/livekit/token` mint (200) for lumina(:8765) + opus(:8766).
- [ ] Manual live call (runbook): pair two clients → Call → ring → Accept → both in the
  derived room on LiveKit, media flows. Update `runbooks/qr-pairing-phone-test.md` with
  the call step.
- [ ] Open the PR for `feature/webrtc-session-after-pairing` (sub-project A only); B/C
  remain separate.
