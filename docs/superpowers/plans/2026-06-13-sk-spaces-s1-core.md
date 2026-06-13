# SK Spaces — S1 Single-Host Core Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Stand up a single-host SK Space — host opens an audio room on the tailnet LiveKit SFU, members and guest-link listeners join (listen-only), via a new `src/skchat/spaces/` package wired into the webui.

**Architecture:** A Space is a LiveKit room with role-scoped tokens. `roles.py` is pure logic (role→grant flags); `tokens.py` turns a role into a LiveKit JWT; `routes.py` exposes create/join/list/end over FastAPI, reusing `guest.py` for invite-link listeners. No SFU call is needed at create time — LiveKit auto-creates the room when the host connects — so the whole package is testable with a TestClient + dummy creds, no live SFU.

**Tech Stack:** Python 3.10+, FastAPI (`register_*_routes(app)` pattern), `livekit-api` (`api.VideoGrants`/`api.AccessToken` — already a dep), `PyJWT` (`jwt`, already used by `guest.py`) to verify minted grants in tests. Line length 99, ruff.

**Spec:** `docs/superpowers/specs/2026-06-13-sk-spaces-design.md` (§4 roles, §6 components/flow).
**Grounding (existing code to mirror):**
- `src/skchat/livekit_routes.py:51-71` — `_mint_token` shows the `VideoGrants`/`AccessToken` API.
- `src/skchat/livekit_routes.py:74` — `register_livekit_routes(app)` route-registration pattern.
- `src/skchat/call_session.py:20-34` — `derive_room` (base32 hash) to mirror for `derive_space_id`.
- `src/skchat/guest.py` — `InviteIssuer`/`InviteVerifier`/`GuestToken` for the guest-link path.
- `src/skchat/webui.py:80-89` — where routers are registered.

**Run tests from repo root:** `~/.skenv/bin/python -m pytest tests/ -q` (skchat namespace-collision note in CLAUDE.md only affects running from `smilintux-org/`; repo root is fine).

---

## Task 0: Package scaffold

**Files:**
- Create: `src/skchat/spaces/__init__.py`

- [ ] **Step 1: Create the package init**

```python
"""SK Spaces — sovereign audio-only community rooms over a tailnet LiveKit SFU.

S1 = single-host core: a Space is a LiveKit room with role-scoped tokens
(host/speaker/listener). See docs/superpowers/specs/2026-06-13-sk-spaces-design.md.
"""

__all__ = []
```

- [ ] **Step 2: Verify import**

Run: `~/.skenv/bin/python -c "import skchat.spaces; print('ok')"`
Expected: `ok`

- [ ] **Step 3: Commit**

```bash
git add src/skchat/spaces/__init__.py
git commit -m "feat(spaces): scaffold spaces package"
```

---

## Task 1: Space model + deterministic Space id

**Files:**
- Create: `src/skchat/spaces/space.py`
- Test: `tests/test_spaces_space.py`

- [ ] **Step 1: Write the failing test**

Create `tests/test_spaces_space.py`:

```python
from skchat.spaces.space import Space, SpaceStatus, derive_space_id


def test_space_id_is_deterministic_and_prefixed():
    a = derive_space_id("lumina@chef.skworld", "town-hall")
    b = derive_space_id("lumina@chef.skworld", "town-hall")
    assert a == b
    assert a.startswith("space-")
    # 16 base32 chars after the prefix
    assert len(a) == len("space-") + 16
    assert a[len("space-"):].isalnum()


def test_space_id_varies_by_host_and_slug():
    assert derive_space_id("lumina@chef.skworld", "town-hall") != \
        derive_space_id("opus@chef.skworld", "town-hall")
    assert derive_space_id("lumina@chef.skworld", "town-hall") != \
        derive_space_id("lumina@chef.skworld", "after-party")


def test_space_dataclass_defaults_and_room_equals_id():
    s = Space(space_id="space-abcd1234abcd1234", host_fqid="lumina@chef.skworld",
              title="Town Hall", slug="town-hall")
    assert s.status == SpaceStatus.OPEN
    assert s.room == s.space_id          # the LiveKit room name IS the space id
    assert s.speaker_cap == 10           # configurable default (spec §1 / Chef)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `~/.skenv/bin/python -m pytest tests/test_spaces_space.py -v`
Expected: FAIL with `ModuleNotFoundError: skchat.spaces.space`.

- [ ] **Step 3: Write the implementation**

Create `src/skchat/spaces/space.py`:

```python
"""Space model + deterministic Space id (mirrors call_session.derive_room)."""

from __future__ import annotations

import base64
import hashlib
from dataclasses import dataclass, field
from enum import Enum


class SpaceStatus(str, Enum):
    OPEN = "open"      # created, not necessarily anyone live yet
    LIVE = "live"      # at least one speaker publishing
    ENDED = "ended"


def derive_space_id(host_fqid: str, slug: str) -> str:
    """Deterministic Space id from host FQID + slug.

    SHA-256 over "host_fqid/slug", base32 (lowercased, no padding), first 16
    chars → ~80 bits. Stable so a host can re-derive the same room for a slug.
    """
    digest = hashlib.sha256(f"{host_fqid.strip()}/{slug.strip()}".encode()).digest()
    b32 = base64.b32encode(digest).decode().lower().rstrip("=")
    return "space-" + b32[:16]


@dataclass
class Space:
    space_id: str
    host_fqid: str
    title: str
    slug: str
    status: SpaceStatus = SpaceStatus.OPEN
    speaker_cap: int = 10
    created_at: float = 0.0
    speakers: list[str] = field(default_factory=list)

    @property
    def room(self) -> str:
        """The LiveKit room name is the Space id (room auto-created on join)."""
        return self.space_id
```

- [ ] **Step 4: Run test to verify it passes**

Run: `~/.skenv/bin/python -m pytest tests/test_spaces_space.py -v`
Expected: PASS (3 tests).

- [ ] **Step 5: Commit**

```bash
git add src/skchat/spaces/space.py tests/test_spaces_space.py
git commit -m "feat(spaces): Space model + deterministic space id"
```

---

## Task 2: Roles → grant flags (pure logic)

**Files:**
- Create: `src/skchat/spaces/roles.py`
- Test: `tests/test_spaces_roles.py`

- [ ] **Step 1: Write the failing test**

Create `tests/test_spaces_roles.py`:

```python
import pytest

from skchat.spaces.roles import Role, RoleGrant, grant_for


def test_host_can_publish_and_is_admin():
    g = grant_for(Role.HOST, "space-x")
    assert isinstance(g, RoleGrant)
    assert g.room == "space-x"
    assert g.room_join is True
    assert g.can_publish is True
    assert g.can_subscribe is True
    assert g.can_publish_data is True
    assert g.room_admin is True


def test_speaker_is_mic_only_not_admin():
    g = grant_for(Role.SPEAKER, "space-x")
    assert g.can_publish is True
    assert g.can_publish_sources == ["microphone"]   # no camera/screen
    assert g.room_admin is False


def test_listener_is_subscribe_only_but_can_signal():
    g = grant_for(Role.LISTENER, "space-x")
    assert g.can_publish is False
    assert g.can_subscribe is True
    assert g.can_publish_data is True                # raise-hand / react / chat
    assert g.room_admin is False


def test_unknown_role_raises():
    with pytest.raises(ValueError):
        grant_for("emperor", "space-x")  # type: ignore[arg-type]
```

- [ ] **Step 2: Run test to verify it fails**

Run: `~/.skenv/bin/python -m pytest tests/test_spaces_roles.py -v`
Expected: FAIL with `ModuleNotFoundError: skchat.spaces.roles`.

- [ ] **Step 3: Write the implementation**

Create `src/skchat/spaces/roles.py`:

```python
"""Space roles → LiveKit grant flags (pure logic; tokens.py turns these into a JWT).

The speaker/listener switch is `can_publish`. Speakers are mic-only so no camera
or screen can be pushed into an audio room. Listeners are subscribe-only but keep
`can_publish_data` so they can raise hand / react / chat.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum


class Role(str, Enum):
    HOST = "host"
    SPEAKER = "speaker"
    LISTENER = "listener"


@dataclass
class RoleGrant:
    room: str
    room_join: bool = True
    can_publish: bool = False
    can_subscribe: bool = True
    can_publish_data: bool = True
    can_publish_sources: list[str] = field(default_factory=list)
    room_admin: bool = False


def grant_for(role: "Role | str", space_id: str) -> RoleGrant:
    try:
        role = Role(role)
    except ValueError as exc:
        raise ValueError(f"unknown space role: {role!r}") from exc

    if role is Role.HOST:
        return RoleGrant(room=space_id, can_publish=True, can_publish_data=True,
                         room_admin=True)
    if role is Role.SPEAKER:
        return RoleGrant(room=space_id, can_publish=True, can_publish_data=True,
                         can_publish_sources=["microphone"])
    # LISTENER
    return RoleGrant(room=space_id, can_publish=False, can_publish_data=True)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `~/.skenv/bin/python -m pytest tests/test_spaces_roles.py -v`
Expected: PASS (4 tests).

- [ ] **Step 5: Commit**

```bash
git add src/skchat/spaces/roles.py tests/test_spaces_roles.py
git commit -m "feat(spaces): role→grant mapping (host/speaker/listener)"
```

---

## Task 3: Mint role-scoped Space tokens

**Files:**
- Create: `src/skchat/spaces/tokens.py`
- Test: `tests/test_spaces_tokens.py`

- [ ] **Step 1: Write the failing test**

Create `tests/test_spaces_tokens.py`. It decodes the minted LiveKit JWT with the API
secret and asserts the `video` grant claims (camelCase, as LiveKit emits them):

```python
import jwt  # PyJWT (already used by guest.py)

from skchat.spaces.roles import Role
from skchat.spaces.tokens import mint_space_token

_KEY, _SECRET = "test-key", "test-secret-0123456789"


def _claims(token):
    return jwt.decode(token, _SECRET, algorithms=["HS256"],
                      options={"verify_aud": False})


def test_listener_token_is_subscribe_only():
    tok = mint_space_token("guest:abc", "Guest", Role.LISTENER, "space-x", 3600,
                           api_key=_KEY, api_secret=_SECRET)
    v = _claims(tok)["video"]
    assert v["room"] == "space-x"
    assert v["roomJoin"] is True
    assert v.get("canPublish", False) is False
    assert v["canSubscribe"] is True
    assert v["canPublishData"] is True


def test_host_token_is_admin_publisher():
    tok = mint_space_token("lumina@chef.skworld", "Lumina", Role.HOST, "space-x",
                           3600, api_key=_KEY, api_secret=_SECRET)
    v = _claims(tok)["video"]
    assert v["canPublish"] is True
    assert v["roomAdmin"] is True


def test_speaker_token_is_mic_only():
    tok = mint_space_token("dave@chef.skworld", "Dave", Role.SPEAKER, "space-x",
                           3600, api_key=_KEY, api_secret=_SECRET)
    v = _claims(tok)["video"]
    assert v["canPublish"] is True
    assert v["canPublishSources"] == ["microphone"]


def test_identity_and_ttl_round_trip():
    tok = mint_space_token("x@y.z", "X", Role.LISTENER, "space-x", 120,
                           api_key=_KEY, api_secret=_SECRET)
    c = _claims(tok)
    assert c["sub"] == "x@y.z"        # LiveKit puts identity in sub
    assert c["exp"] - c["iat"] == 120
```

- [ ] **Step 2: Run test to verify it fails**

Run: `~/.skenv/bin/python -m pytest tests/test_spaces_tokens.py -v`
Expected: FAIL with `ModuleNotFoundError: skchat.spaces.tokens`.

- [ ] **Step 3: Write the implementation**

Create `src/skchat/spaces/tokens.py`:

```python
"""Mint role-scoped LiveKit JWTs for a Space (mirrors livekit_routes._mint_token)."""

from __future__ import annotations

import os
from datetime import timedelta

from skchat.spaces.roles import Role, grant_for


def mint_space_token(
    identity: str,
    name: str,
    role: "Role | str",
    space_id: str,
    ttl: int,
    *,
    api_key: str | None = None,
    api_secret: str | None = None,
) -> str:
    """Build a participant JWT scoped to `role` in `space_id`.

    Creds default to the same env vars livekit_routes uses; tests pass explicit
    dummy creds so no live SFU is required. Raises ImportError if livekit-api is
    not installed, ValueError on an unknown role.
    """
    from livekit import api  # soft dep, local import

    key = api_key or os.getenv("SKCHAT_LIVEKIT_API_KEY", "")
    secret = api_secret or os.getenv("SKCHAT_LIVEKIT_API_SECRET", "")
    g = grant_for(role, space_id)

    grants = api.VideoGrants(
        room_join=g.room_join,
        room=g.room,
        can_publish=g.can_publish,
        can_subscribe=g.can_subscribe,
        can_publish_data=g.can_publish_data,
        can_publish_sources=g.can_publish_sources or None,
        room_admin=g.room_admin,
    )
    token = (
        api.AccessToken(key, secret)
        .with_identity(identity)
        .with_name(name)
        .with_grants(grants)
        .with_ttl(timedelta(seconds=ttl))
    )
    return token.to_jwt()
```

> **NOTE for implementer:** if `api.VideoGrants` in the installed `livekit-api`
> rejects `can_publish_sources=None`, pass the field only when non-empty (build the
> kwargs dict conditionally). Verify against the installed version; the test pins
> the required claim output, not the call shape.

- [ ] **Step 4: Run test to verify it passes**

Run: `~/.skenv/bin/python -m pytest tests/test_spaces_tokens.py -v`
Expected: PASS (4 tests). If `canPublishSources` claim casing differs in the
installed SDK, adjust the assertion to match what the SDK actually emits — but it
should be camelCase `canPublishSources`.

- [ ] **Step 5: Commit**

```bash
git add src/skchat/spaces/tokens.py tests/test_spaces_tokens.py
git commit -m "feat(spaces): mint role-scoped LiveKit tokens"
```

---

## Task 4: Live-now registry (minimal)

**Files:**
- Create: `src/skchat/spaces/registry.py`
- Test: `tests/test_spaces_registry.py`

S1 keeps it in-memory + a JSON file so the directory survives a restart; S3 expands
it. One process owns the webui, so a module-level singleton is fine.

- [ ] **Step 1: Write the failing test**

Create `tests/test_spaces_registry.py`:

```python
from skchat.spaces.registry import SpaceRegistry
from skchat.spaces.space import Space, SpaceStatus


def _space(sid="space-aaaa1111aaaa1111"):
    return Space(space_id=sid, host_fqid="lumina@chef.skworld",
                 title="Town Hall", slug="town-hall")


def test_register_and_list_live(tmp_path):
    reg = SpaceRegistry(path=tmp_path / "spaces.json")
    reg.add(_space())
    live = reg.live()
    assert len(live) == 1
    assert live[0].space_id == "space-aaaa1111aaaa1111"


def test_end_removes_from_live(tmp_path):
    reg = SpaceRegistry(path=tmp_path / "spaces.json")
    s = _space()
    reg.add(s)
    reg.end(s.space_id)
    assert reg.live() == []
    assert reg.get(s.space_id).status == SpaceStatus.ENDED


def test_persists_across_instances(tmp_path):
    p = tmp_path / "spaces.json"
    SpaceRegistry(path=p).add(_space())
    reloaded = SpaceRegistry(path=p)
    assert len(reloaded.live()) == 1


def test_get_unknown_returns_none(tmp_path):
    assert SpaceRegistry(path=tmp_path / "spaces.json").get("nope") is None
```

- [ ] **Step 2: Run test to verify it fails**

Run: `~/.skenv/bin/python -m pytest tests/test_spaces_registry.py -v`
Expected: FAIL with `ModuleNotFoundError: skchat.spaces.registry`.

- [ ] **Step 3: Write the implementation**

Create `src/skchat/spaces/registry.py`:

```python
"""In-memory + JSON-backed registry of Spaces on this host (the 'live now' list)."""

from __future__ import annotations

import json
from dataclasses import asdict
from pathlib import Path

from skchat.spaces.space import Space, SpaceStatus

_DEFAULT_PATH = Path.home() / ".skchat" / "spaces.json"


class SpaceRegistry:
    def __init__(self, path: Path | None = None) -> None:
        self.path = Path(path) if path else _DEFAULT_PATH
        self._spaces: dict[str, Space] = {}
        self._load()

    def _load(self) -> None:
        if not self.path.exists():
            return
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            return
        for d in raw.get("spaces", []):
            d = dict(d)
            d["status"] = SpaceStatus(d.get("status", "open"))
            self._spaces[d["space_id"]] = Space(**d)

    def _save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        data = {"spaces": []}
        for s in self._spaces.values():
            d = asdict(s)
            d["status"] = s.status.value
            data["spaces"].append(d)
        self.path.write_text(json.dumps(data, indent=2), encoding="utf-8")

    def add(self, space: Space) -> None:
        self._spaces[space.space_id] = space
        self._save()

    def get(self, space_id: str) -> Space | None:
        return self._spaces.get(space_id)

    def end(self, space_id: str) -> None:
        s = self._spaces.get(space_id)
        if s is not None:
            s.status = SpaceStatus.ENDED
            self._save()

    def live(self) -> list[Space]:
        return [s for s in self._spaces.values() if s.status != SpaceStatus.ENDED]
```

- [ ] **Step 4: Run test to verify it passes**

Run: `~/.skenv/bin/python -m pytest tests/test_spaces_registry.py -v`
Expected: PASS (4 tests).

- [ ] **Step 5: Commit**

```bash
git add src/skchat/spaces/registry.py tests/test_spaces_registry.py
git commit -m "feat(spaces): live-now Space registry (json-backed)"
```

---

## Task 5: REST routes (create / join / guest-join / list / end)

**Files:**
- Create: `src/skchat/spaces/routes.py`
- Test: `tests/test_spaces_routes.py`

- [ ] **Step 1: Write the failing test**

Create `tests/test_spaces_routes.py`:

```python
import jwt
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from skchat.spaces.registry import SpaceRegistry
from skchat.spaces.routes import register_spaces_routes

_KEY, _SECRET = "test-key", "test-secret-0123456789"


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("SKCHAT_LIVEKIT_API_KEY", _KEY)
    monkeypatch.setenv("SKCHAT_LIVEKIT_API_SECRET", _SECRET)
    monkeypatch.setenv("SKCHAT_LIVEKIT_URL", "ws://test-sfu:7880")
    app = FastAPI()
    # inject a tmp-path registry so tests don't touch ~/.skchat
    register_spaces_routes(app, registry=SpaceRegistry(path=tmp_path / "spaces.json"))
    return TestClient(app)


def _video(token):
    return jwt.decode(token, _SECRET, algorithms=["HS256"],
                      options={"verify_aud": False})["video"]


def test_create_returns_host_token_and_registers(client):
    r = client.post("/spaces/create", json={
        "host_fqid": "lumina@chef.skworld", "title": "Town Hall", "slug": "town-hall"})
    assert r.status_code == 200
    body = r.json()
    assert body["space_id"].startswith("space-")
    assert body["role"] == "host"
    assert _video(body["token"])["roomAdmin"] is True

    live = client.get("/spaces").json()["spaces"]
    assert any(s["space_id"] == body["space_id"] for s in live)


def test_member_join_gets_listener_token(client):
    sid = client.post("/spaces/create", json={
        "host_fqid": "lumina@chef.skworld", "title": "T", "slug": "s"}).json()["space_id"]
    r = client.post(f"/spaces/{sid}/join", json={
        "identity": "opus@chef.skworld", "name": "Opus"})
    assert r.status_code == 200
    v = _video(r.json()["token"])
    assert v.get("canPublish", False) is False
    assert v["canSubscribe"] is True


def test_join_unknown_space_404(client):
    r = client.post("/spaces/space-doesnotexist00/join", json={"identity": "x@y.z"})
    assert r.status_code == 404


def test_end_marks_not_live(client):
    sid = client.post("/spaces/create", json={
        "host_fqid": "lumina@chef.skworld", "title": "T", "slug": "s"}).json()["space_id"]
    assert client.post(f"/spaces/{sid}/end").status_code == 200
    live = client.get("/spaces").json()["spaces"]
    assert all(s["space_id"] != sid for s in live)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `~/.skenv/bin/python -m pytest tests/test_spaces_routes.py -v`
Expected: FAIL with `ModuleNotFoundError: skchat.spaces.routes`.

- [ ] **Step 3: Write the implementation**

Create `src/skchat/spaces/routes.py`:

```python
"""FastAPI routes for SK Spaces (S1: create/join/guest-join/list/end).

No SFU call at create time — LiveKit auto-creates the room when the host first
connects — so these routes are fully testable with a dummy key/secret.
"""

from __future__ import annotations

import logging
import os
import time

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse

from skchat.spaces.registry import SpaceRegistry
from skchat.spaces.roles import Role
from skchat.spaces.space import Space, derive_space_id
from skchat.spaces.tokens import mint_space_token

logger = logging.getLogger("skchat.spaces.routes")

_DEFAULT_TTL = int(os.getenv("SKCHAT_LIVEKIT_TOKEN_TTL", "21600"))


def _url() -> str:
    return os.getenv("SKCHAT_LIVEKIT_URL", "ws://skworld-100:7880")


def _have_creds() -> bool:
    return bool(os.getenv("SKCHAT_LIVEKIT_API_KEY") and
                os.getenv("SKCHAT_LIVEKIT_API_SECRET"))


def register_spaces_routes(app: FastAPI, *, registry: SpaceRegistry | None = None) -> None:
    reg = registry or SpaceRegistry()

    def _token_response(identity: str, name: str, role: Role, space: Space) -> dict:
        token = mint_space_token(identity, name, role, space.space_id, _DEFAULT_TTL)
        return {
            "space_id": space.space_id, "room": space.room, "url": _url(),
            "identity": identity, "name": name, "role": role.value, "token": token,
            "title": space.title,
        }

    @app.post("/spaces/create")
    async def create_space(request: Request) -> JSONResponse:
        if not _have_creds():
            raise HTTPException(503, "livekit not configured")
        body = await request.json()
        host = (body.get("host_fqid") or "").strip()
        title = (body.get("title") or "").strip()
        slug = (body.get("slug") or "").strip()
        if not (host and title and slug):
            raise HTTPException(400, "host_fqid, title, slug required")
        sid = derive_space_id(host, slug)
        space = Space(space_id=sid, host_fqid=host, title=title, slug=slug,
                      created_at=time.time())
        reg.add(space)
        return JSONResponse(_token_response(host, host.split("@")[0], Role.HOST, space))

    @app.post("/spaces/{space_id}/join")
    async def join_space(space_id: str, request: Request) -> JSONResponse:
        space = reg.get(space_id)
        if space is None or space.status.value == "ended":
            raise HTTPException(404, "space not found or ended")
        body = await request.json()
        identity = (body.get("identity") or "").strip()
        if not identity:
            raise HTTPException(400, "identity required")
        name = body.get("name") or identity.split("@")[0]
        return JSONResponse(_token_response(identity, name, Role.LISTENER, space))

    @app.post("/spaces/{space_id}/join-guest")
    async def join_space_guest(space_id: str, request: Request) -> JSONResponse:
        """Guest-link listener: verify a guest.py invite, then mint a LISTENER token."""
        space = reg.get(space_id)
        if space is None or space.status.value == "ended":
            raise HTTPException(404, "space not found or ended")
        body = await request.json()
        invite = (body.get("invite_token") or "").strip()
        display = (body.get("display") or "Guest").strip()
        if not invite:
            raise HTTPException(400, "invite_token required")
        from skchat.guest import GuestJoinError, InviteVerifier
        try:
            guest = InviteVerifier().verify(invite, expected_room=space_id,
                                            display_name=display)
        except GuestJoinError as exc:
            raise HTTPException(403, f"invalid invite: {exc}") from exc
        return JSONResponse(_token_response(guest.identity, guest.display or display,
                                            Role.LISTENER, space))

    @app.get("/spaces")
    async def list_spaces() -> JSONResponse:
        return JSONResponse({"spaces": [
            {"space_id": s.space_id, "title": s.title, "host_fqid": s.host_fqid,
             "status": s.status.value, "speakers": s.speakers}
            for s in reg.live()
        ]})

    @app.post("/spaces/{space_id}/end")
    async def end_space(space_id: str) -> JSONResponse:
        if reg.get(space_id) is None:
            raise HTTPException(404, "space not found")
        reg.end(space_id)
        return JSONResponse({"ok": True, "space_id": space_id})
```

> **NOTE for implementer:** the guest path imports `InviteVerifier`/`GuestJoinError`
> from `skchat.guest`. Confirm those names against `guest.py` (the recon shows
> `InviteVerifier.verify(invite_token, expected_room, display_name) -> GuestToken`
> and `GuestJoinError`). If a name differs, adapt the import/call — the
> `test_spaces_routes.py` tests don't exercise the guest path (it needs a signed
> invite), so add a focused guest-path test only if `guest.py`'s test helpers make
> it easy; otherwise leave it covered by S1's manual runbook.

- [ ] **Step 4: Run test to verify it passes**

Run: `~/.skenv/bin/python -m pytest tests/test_spaces_routes.py -v`
Expected: PASS (4 tests).

- [ ] **Step 5: Commit**

```bash
git add src/skchat/spaces/routes.py tests/test_spaces_routes.py
git commit -m "feat(spaces): REST routes — create/join/guest-join/list/end"
```

---

## Task 6: Wire routes into the webui

**Files:**
- Modify: `src/skchat/webui.py:85-89` (after the call-routes registration)
- Test: `tests/test_spaces_webui_wired.py`

- [ ] **Step 1: Write the failing test**

Create `tests/test_spaces_webui_wired.py`:

```python
def test_spaces_routes_are_registered_on_the_app():
    # Import the webui app and confirm a /spaces route exists.
    from skchat.webui import app
    paths = {r.path for r in app.routes}
    assert "/spaces" in paths
    assert any(p == "/spaces/{space_id}/join" for p in paths)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `~/.skenv/bin/python -m pytest tests/test_spaces_webui_wired.py -v`
Expected: FAIL — `/spaces` not in the app's routes.

- [ ] **Step 3: Add the registration**

In `src/skchat/webui.py`, immediately after the `register_call_routes(app)` block
(around line 89), add:

```python
try:
    from .spaces.routes import register_spaces_routes as _register_spaces_routes
    _register_spaces_routes(app)
except ImportError as _e:
    logger.warning("spaces routes not registered: %s", _e)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `~/.skenv/bin/python -m pytest tests/test_spaces_webui_wired.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/skchat/webui.py tests/test_spaces_webui_wired.py
git commit -m "feat(spaces): register spaces routes in webui"
```

---

## Task 7: Audio-room web UI (2027) + page route

**Files:**
- Create: `src/skchat/static/space.html`
- Modify: `src/skchat/spaces/routes.py` (add `GET /space/{space_id}` page route)
- Test: `tests/test_spaces_page.py`

S1 ships a functional, on-brand audio-room page: connect with a token, render the
speaker rings + listener count, host "End" button. Raise-hand/promote UI is S2 —
this page just needs to join and play audio.

- [ ] **Step 1: Write the failing test**

Create `tests/test_spaces_page.py`:

```python
import os

from fastapi import FastAPI
from fastapi.testclient import TestClient

from skchat.spaces.registry import SpaceRegistry
from skchat.spaces.routes import register_spaces_routes


def test_space_page_served(tmp_path, monkeypatch):
    monkeypatch.setenv("SKCHAT_LIVEKIT_API_KEY", "k")
    monkeypatch.setenv("SKCHAT_LIVEKIT_API_SECRET", "s")
    app = FastAPI()
    register_spaces_routes(app, registry=SpaceRegistry(path=tmp_path / "s.json"))
    c = TestClient(app)
    r = c.get("/space/space-anything0000000")
    assert r.status_code == 200
    assert "text/html" in r.headers["content-type"]
    assert "livekit" in r.text.lower()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `~/.skenv/bin/python -m pytest tests/test_spaces_page.py -v`
Expected: FAIL — no `/space/{space_id}` route.

- [ ] **Step 3: Create the page**

Create `src/skchat/static/space.html` (2027 tokens: near-black `#0b0d10`, one teal
accent `#2dd4bf`, flat-with-depth — never glass; Inter/JetBrains Mono):

```html
<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8" />
<meta name="viewport" content="width=device-width, initial-scale=1" />
<title>SK Space</title>
<script src="https://cdn.jsdelivr.net/npm/livekit-client/dist/livekit-client.umd.min.js"></script>
<style>
  :root {
    --bg:#0b0d10; --surface:#13161b; --line:#222831; --text:#e6e9ee;
    --muted:#8b94a3; --accent:#2dd4bf; --accent-2:#14b8a6; --self:#3b82f6;
    --radius:14px; --gap:14px;
  }
  * { box-sizing:border-box; }
  body { margin:0; background:var(--bg); color:var(--text);
    font-family:Inter,system-ui,sans-serif; }
  header { padding:18px 20px; border-bottom:1px solid var(--line);
    display:flex; align-items:center; gap:12px; }
  header h1 { font-size:17px; margin:0; font-weight:600; }
  #rec { display:none; color:#ef4444; font-size:12px; font-weight:600; }
  main { padding:20px; max-width:920px; margin:0 auto; }
  .speakers { display:flex; flex-wrap:wrap; gap:var(--gap); }
  .speaker { display:flex; flex-direction:column; align-items:center; gap:8px;
    width:96px; }
  .ring { width:72px; height:72px; border-radius:50%; background:var(--surface);
    border:2px solid var(--line); display:grid; place-items:center;
    font-size:22px; transition:box-shadow .18s, border-color .18s; }
  .ring.speaking { border-color:var(--accent);
    box-shadow:0 0 0 4px rgba(45,212,191,.18); animation:pulse 1s infinite; }
  @keyframes pulse { 50% { box-shadow:0 0 0 7px rgba(45,212,191,.10); } }
  .name { font-size:12px; color:var(--muted); }
  .bar { display:flex; gap:10px; align-items:center; margin-top:24px; }
  button { background:var(--accent); color:#04201c; border:0; border-radius:11px;
    padding:10px 16px; font-weight:600; cursor:pointer; }
  button.ghost { background:transparent; color:var(--text);
    border:1px solid var(--line); }
  .count { color:var(--muted); font-size:13px; margin-left:auto; }
  .step { color:var(--muted); font-size:13px; min-height:18px; margin-top:12px; }
</style>
</head>
<body>
<header>
  <h1 id="title">SK Space</h1>
  <span id="rec">● REC</span>
</header>
<main>
  <div class="speakers" id="speakers"></div>
  <div class="bar">
    <button id="join">Join</button>
    <button class="ghost" id="hand" disabled>✋ Raise hand</button>
    <button class="ghost" id="end" style="display:none">End</button>
    <span class="count" id="count">—</span>
  </div>
  <div class="step" id="step"></div>
</main>
<script>
  const LK = window.LivekitClient;
  const qs = new URLSearchParams(location.search);
  const spaceId = location.pathname.split("/").pop();
  const step = (t) => document.getElementById("step").textContent = t;
  let room;

  async function getToken() {
    // identity provided via ?identity= (member) or minted by the caller; here we
    // join as a listener using the /spaces/{id}/join path with a chosen name.
    const identity = qs.get("identity") || ("listener-" + Math.random().toString(36).slice(2,8));
    const res = await fetch(`/spaces/${spaceId}/join`, {
      method:"POST", headers:{"content-type":"application/json"},
      body: JSON.stringify({ identity, name: qs.get("name") || identity })
    });
    if (!res.ok) throw new Error("join failed: " + res.status);
    return res.json();
  }

  function renderSpeakers() {
    const el = document.getElementById("speakers");
    el.innerHTML = "";
    const parts = [room.localParticipant, ...room.remoteParticipants.values()];
    let listeners = 0;
    for (const p of parts) {
      const canSpeak = p.permissions?.canPublish;
      if (!canSpeak) { listeners++; continue; }
      const wrap = document.createElement("div"); wrap.className = "speaker";
      const ring = document.createElement("div");
      ring.className = "ring" + (p.isSpeaking ? " speaking" : "");
      ring.id = "ring-" + p.identity; ring.textContent = "🎙️";
      const name = document.createElement("div"); name.className = "name";
      name.textContent = p.name || p.identity;
      wrap.append(ring, name); el.append(wrap);
    }
    document.getElementById("count").textContent = `${listeners} listening`;
  }

  document.getElementById("join").onclick = async () => {
    try {
      step("Connecting…");
      const { url, token, title } = await getToken();
      document.getElementById("title").textContent = title || "SK Space";
      room = new LK.Room({ adaptiveStream:true });
      room.on(LK.RoomEvent.ActiveSpeakersChanged, renderSpeakers);
      room.on(LK.RoomEvent.ParticipantConnected, renderSpeakers);
      room.on(LK.RoomEvent.ParticipantDisconnected, renderSpeakers);
      room.on(LK.RoomEvent.TrackSubscribed, (track) => {
        if (track.kind === "audio") track.attach();  // play speaker audio
      });
      await room.connect(url, token);
      renderSpeakers();
      document.getElementById("join").style.display = "none";
      document.getElementById("hand").disabled = false;
      step("Connected — listening.");
    } catch (e) { step("Error: " + e.message); }
  };

  document.getElementById("end").onclick = async () => {
    await fetch(`/spaces/${spaceId}/end`, { method:"POST" });
    if (room) room.disconnect();
    step("Space ended.");
  };
</script>
</body>
</html>
```

- [ ] **Step 4: Add the page route to `routes.py`**

In `src/skchat/spaces/routes.py`, add these imports at the top:

```python
from pathlib import Path

from fastapi.responses import FileResponse, HTMLResponse
```

and inside `register_spaces_routes`, add:

```python
    @app.get("/space/{space_id}", response_class=HTMLResponse)
    async def space_page(space_id: str) -> HTMLResponse:  # noqa: ARG001
        static = Path(__file__).resolve().parent.parent / "static" / "space.html"
        if static.exists():
            return FileResponse(static, media_type="text/html")
        return HTMLResponse("space.html missing", status_code=500)
```

- [ ] **Step 5: Run test to verify it passes**

Run: `~/.skenv/bin/python -m pytest tests/test_spaces_page.py -v`
Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add src/skchat/static/space.html src/skchat/spaces/routes.py tests/test_spaces_page.py
git commit -m "feat(spaces): 2027 audio-room page + /space/{id} route"
```

---

## Final verification

- [ ] **Run the full spaces suite + the whole skchat suite (no regressions)**

Run: `~/.skenv/bin/python -m pytest tests/test_spaces_*.py -v && ~/.skenv/bin/python -m pytest tests/ -q`
Expected: all spaces tests pass; the existing skchat suite still passes (note any
pre-existing failures unrelated to this work, don't block on them).

- [ ] **Lint the new package**

Run: `~/.skenv/bin/ruff check src/skchat/spaces/ tests/test_spaces_*.py`
Expected: no errors.

- [ ] **Manual smoke (optional, needs live SFU creds)**

With `SKCHAT_LIVEKIT_*` set and the webui running:
`curl -s localhost:<port>/spaces/create -d '{"host_fqid":"lumina@chef.skworld","title":"Town Hall","slug":"town-hall"}' -H content-type:application/json`
→ open `/space/<space_id>` in a browser, click Join, confirm audio connects.

---

## What S1 delivers

A working single-host Space: a host creates one (`/spaces/create`), it appears in
the live-now list (`/spaces`), members join listen-only (`/spaces/{id}/join`),
guest-link holders join via `guest.py` (`/spaces/{id}/join-guest`), everyone lands
in one tailnet LiveKit room and hears the speakers, and the host can end it. Roles
are real (host/speaker/listener grants), tokens are verified down to the grant
claims, and the whole thing is tested without a live SFU. **S2** adds the
moderation + mutual-consent raise-hand that promotes a listener into a speaker.
```
