# SK Spaces — S2 Moderation + Mutual-Consent Raise-Hand Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add live moderation to a Space — promote/demote/mute/kick and a mutual-consent raise-hand that turns a listener into a speaker, all via LiveKit's `update_participant` (no rejoin).

**Architecture:** The consent rule is a pure state machine (`apply_action`) — heavily tested, SFU-free. A thin `Moderator` wraps `api.LiveKitAPI(...).room` (update/remove/mute) behind an injectable factory so tests use a `FakeRoomService` with no live SFU. New host-gated routes drive it; `space.html` gains the ✋ button, the host control surface, and the `ParticipantPermissionsChanged`→enable-mic handler.

**Tech Stack:** Python 3.10+, `livekit-api` (`UpdateParticipantRequest`, `ParticipantPermission`, `RoomParticipantIdentity`, `MuteRoomTrackRequest` — all confirmed present), FastAPI TestClient. Line length 99, ruff.

**Spec:** `docs/superpowers/specs/2026-06-13-sk-spaces-design.md` §4 (promote/demote), §5 (mutual-consent raise-hand). **Depends on S1** (`src/skchat/spaces/` exists). Coord: `b55286e0`.

**Confirmed LiveKit API (python):**
- `svc = api.LiveKitAPI(http_url, key, secret).room`
- `await svc.update_participant(api.UpdateParticipantRequest(room=, identity=, metadata=, permission=api.ParticipantPermission(can_publish=, can_subscribe=, can_publish_data=)))`
- `await svc.remove_participant(api.RoomParticipantIdentity(room=, identity=))`
- `await svc.mute_published_track(api.MuteRoomTrackRequest(room=, identity=, track_sid=, muted=True))`
- read current metadata: `await svc.get_participant(api.RoomParticipantIdentity(room=, identity=))` → `.metadata`

**Run tests from repo root:** `~/.skenv/bin/python -m pytest tests/ -q`.

---

## Task 1: Consent state machine (pure logic)

**Files:**
- Create: `src/skchat/spaces/moderation.py`
- Test: `tests/test_spaces_consent.py`

The rule (spec §5): a listener goes on stage only when **both** `hand_raised` AND
`invited_to_stage` are true — host invited *and* user consented.

- [ ] **Step 1: Write the failing test**

Create `tests/test_spaces_consent.py`:

```python
import json

import pytest

from skchat.spaces.moderation import StageState, apply_action, dump_meta, parse_meta


def test_default_state_is_off_stage():
    s = StageState()
    assert s.hand_raised is False
    assert s.invited_to_stage is False
    assert s.on_stage is False


def test_both_flags_required_for_stage():
    assert StageState(hand_raised=True, invited_to_stage=False).on_stage is False
    assert StageState(hand_raised=False, invited_to_stage=True).on_stage is False
    assert StageState(hand_raised=True, invited_to_stage=True).on_stage is True


def test_raise_hand_alone_does_not_publish():
    state, can_publish = apply_action(StageState(), "raise_hand")
    assert state.hand_raised is True
    assert can_publish is False                 # host hasn't invited yet


def test_invite_then_already_raised_goes_live():
    raised, _ = apply_action(StageState(), "raise_hand")
    state, can_publish = apply_action(raised, "invite")
    assert state.on_stage is True
    assert can_publish is True                   # mutual consent reached


def test_invite_first_then_raise_goes_live():
    invited, cp1 = apply_action(StageState(), "invite")
    assert cp1 is False                          # user hasn't consented yet
    state, can_publish = apply_action(invited, "raise_hand")
    assert can_publish is True


def test_remove_resets_both_and_demotes():
    on, _ = apply_action(StageState(hand_raised=True, invited_to_stage=True), "noop")
    state, can_publish = apply_action(
        StageState(hand_raised=True, invited_to_stage=True), "remove")
    assert state.hand_raised is False
    assert state.invited_to_stage is False
    assert can_publish is False


def test_lower_hand_and_uninvite():
    s1, _ = apply_action(StageState(hand_raised=True, invited_to_stage=True), "lower_hand")
    assert s1.hand_raised is False and s1.on_stage is False
    s2, _ = apply_action(StageState(hand_raised=True, invited_to_stage=True), "uninvite")
    assert s2.invited_to_stage is False and s2.on_stage is False


def test_meta_round_trip():
    s = StageState(hand_raised=True, invited_to_stage=False)
    assert parse_meta(dump_meta(s)) == s
    assert parse_meta("") == StageState()        # empty metadata → default
    assert parse_meta("not json") == StageState()


def test_unknown_action_raises():
    with pytest.raises(ValueError):
        apply_action(StageState(), "explode")
```

- [ ] **Step 2: Run test to verify it fails**

Run: `~/.skenv/bin/python -m pytest tests/test_spaces_consent.py -v`
Expected: FAIL with `ModuleNotFoundError: skchat.spaces.moderation`.

- [ ] **Step 3: Write the implementation**

Create `src/skchat/spaces/moderation.py`:

```python
"""Mutual-consent raise-hand state machine + a thin LiveKit moderation wrapper.

The consent rule (spec §5): a listener goes on stage only when BOTH the host
invited them AND they raised their hand. `apply_action` is pure; `Moderator`
(Task 2) applies the result via LiveKit's update_participant.
"""

from __future__ import annotations

import json
from dataclasses import dataclass

_ACTIONS = {"raise_hand", "lower_hand", "invite", "uninvite", "remove", "noop"}


@dataclass(eq=True)
class StageState:
    hand_raised: bool = False
    invited_to_stage: bool = False

    @property
    def on_stage(self) -> bool:
        return self.hand_raised and self.invited_to_stage


def parse_meta(metadata: str) -> StageState:
    if not metadata:
        return StageState()
    try:
        d = json.loads(metadata)
    except (json.JSONDecodeError, TypeError):
        return StageState()
    return StageState(
        hand_raised=bool(d.get("hand_raised", False)),
        invited_to_stage=bool(d.get("invited_to_stage", False)),
    )


def dump_meta(state: StageState) -> str:
    return json.dumps({"hand_raised": state.hand_raised,
                       "invited_to_stage": state.invited_to_stage})


def apply_action(state: StageState, action: str) -> tuple[StageState, bool]:
    """Return (new_state, can_publish). can_publish is the AND-gate: True only
    when both flags are set after the action."""
    if action not in _ACTIONS:
        raise ValueError(f"unknown stage action: {action!r}")
    s = StageState(state.hand_raised, state.invited_to_stage)
    if action == "raise_hand":
        s.hand_raised = True
    elif action == "lower_hand":
        s.hand_raised = False
    elif action == "invite":
        s.invited_to_stage = True
    elif action == "uninvite":
        s.invited_to_stage = False
    elif action == "remove":
        s.hand_raised = False
        s.invited_to_stage = False
    # "noop" leaves state unchanged
    return s, s.on_stage
```

- [ ] **Step 4: Run test to verify it passes**

Run: `~/.skenv/bin/python -m pytest tests/test_spaces_consent.py -v`
Expected: PASS (9 tests).

- [ ] **Step 5: Commit**

```bash
git add src/skchat/spaces/moderation.py tests/test_spaces_consent.py
git commit -m "feat(spaces): mutual-consent raise-hand state machine"
```

---

## Task 2: Moderator (LiveKit wrapper, injectable for tests)

**Files:**
- Modify: `src/skchat/spaces/moderation.py` (add `Moderator`)
- Test: `tests/test_spaces_moderator.py`

- [ ] **Step 1: Write the failing test**

Create `tests/test_spaces_moderator.py`. A `FakeRoomService` records calls and
returns a stored participant for `get_participant`, so no live SFU is needed:

```python
import json

import pytest

from skchat.spaces.moderation import Moderator, StageState


class FakeParticipant:
    def __init__(self, metadata=""):
        self.metadata = metadata


class FakeRoomService:
    def __init__(self):
        self.updates = []
        self.removed = []
        self.muted = []
        self._participants = {}

    def set_participant(self, identity, metadata):
        self._participants[identity] = FakeParticipant(metadata)

    async def get_participant(self, req):
        return self._participants.get(req.identity, FakeParticipant(""))

    async def update_participant(self, req):
        self.updates.append(req)
        # reflect new metadata so subsequent reads see it
        self._participants[req.identity] = FakeParticipant(req.metadata or "")

    async def remove_participant(self, req):
        self.removed.append(req.identity)

    async def mute_published_track(self, req):
        self.muted.append((req.identity, req.track_sid, req.muted))


@pytest.fixture
def fake():
    return FakeRoomService()


@pytest.fixture
def mod(fake):
    return Moderator("ws://test:7880", "k", "s", _room_service=fake)


@pytest.mark.asyncio
async def test_raise_hand_sets_metadata_but_not_publish(mod, fake):
    cp = await mod.stage_action("space-x", "alice", "raise_hand")
    assert cp is False
    assert len(fake.updates) == 1
    meta = json.loads(fake.updates[-1].metadata)
    assert meta["hand_raised"] is True
    # permission.can_publish must be False (no premature publish)
    assert fake.updates[-1].permission.can_publish is False


@pytest.mark.asyncio
async def test_invite_after_raise_promotes_to_publisher(mod, fake):
    fake.set_participant("alice", json.dumps({"hand_raised": True,
                                              "invited_to_stage": False}))
    cp = await mod.stage_action("space-x", "alice", "invite")
    assert cp is True
    assert fake.updates[-1].permission.can_publish is True


@pytest.mark.asyncio
async def test_remove_from_stage_demotes(mod, fake):
    fake.set_participant("alice", json.dumps({"hand_raised": True,
                                              "invited_to_stage": True}))
    cp = await mod.stage_action("space-x", "alice", "remove")
    assert cp is False
    assert fake.updates[-1].permission.can_publish is False


@pytest.mark.asyncio
async def test_kick_removes_participant(mod, fake):
    await mod.kick("space-x", "troll")
    assert fake.removed == ["troll"]


@pytest.mark.asyncio
async def test_mute_mutes_track(mod, fake):
    await mod.mute("space-x", "loud", "TR_abc")
    assert fake.muted == [("loud", "TR_abc", True)]
```

- [ ] **Step 2: Run test to verify it fails**

Run: `~/.skenv/bin/python -m pytest tests/test_spaces_moderator.py -v`
Expected: FAIL with `ImportError: cannot import name 'Moderator'`.

- [ ] **Step 3: Add `Moderator` to `moderation.py`**

Append to `src/skchat/spaces/moderation.py`:

```python
# --- LiveKit moderation wrapper ---------------------------------------------

def _http_url(ws_url: str) -> str:
    return ws_url.replace("ws://", "http://").replace("wss://", "https://")


class Moderator:
    """Applies stage transitions + mute/kick via LiveKit's room service.

    `_room_service` is injectable for tests; in production it's built lazily from
    `api.LiveKitAPI(...).room`.
    """

    def __init__(self, ws_url: str, api_key: str, api_secret: str,
                 *, _room_service=None) -> None:
        self._ws_url = ws_url
        self._key = api_key
        self._secret = api_secret
        self._svc = _room_service

    def _service(self):
        if self._svc is not None:
            return self._svc
        from livekit import api
        self._svc = api.LiveKitAPI(_http_url(self._ws_url), self._key,
                                   self._secret).room
        return self._svc

    async def stage_action(self, room: str, identity: str, action: str) -> bool:
        """Read current metadata, apply the consent action, push the new
        metadata + can_publish permission. Returns the resulting can_publish."""
        from livekit import api
        svc = self._service()
        current = await svc.get_participant(
            api.RoomParticipantIdentity(room=room, identity=identity))
        state = parse_meta(getattr(current, "metadata", "") or "")
        new_state, can_publish = apply_action(state, action)
        await svc.update_participant(api.UpdateParticipantRequest(
            room=room, identity=identity, metadata=dump_meta(new_state),
            permission=api.ParticipantPermission(
                can_publish=can_publish, can_subscribe=True, can_publish_data=True),
        ))
        return can_publish

    async def kick(self, room: str, identity: str) -> None:
        from livekit import api
        await self._service().remove_participant(
            api.RoomParticipantIdentity(room=room, identity=identity))

    async def mute(self, room: str, identity: str, track_sid: str) -> None:
        from livekit import api
        await self._service().mute_published_track(api.MuteRoomTrackRequest(
            room=room, identity=identity, track_sid=track_sid, muted=True))
```

> **NOTE for implementer:** verify the exact field names of
> `UpdateParticipantRequest` / `ParticipantPermission` / `MuteRoomTrackRequest` in
> the installed `livekit-api` (they're protobuf-generated; confirmed present by
> name). If `ParticipantPermission` doesn't accept `can_publish` as a kwarg (some
> generated stubs require positional or `setattr`), build it field-by-field. The
> tests pin the behavior (metadata JSON + `permission.can_publish` value), not the
> construction style — make the wrapper satisfy them.

- [ ] **Step 4: Run test to verify it passes**

Run: `~/.skenv/bin/python -m pytest tests/test_spaces_moderator.py -v`
Expected: PASS (5 tests).

- [ ] **Step 5: Commit**

```bash
git add src/skchat/spaces/moderation.py tests/test_spaces_moderator.py
git commit -m "feat(spaces): Moderator — stage/mute/kick over LiveKit room service"
```

---

## Task 3: Moderation routes (host-gated)

**Files:**
- Modify: `src/skchat/spaces/routes.py`
- Test: `tests/test_spaces_moderation_routes.py`

- [ ] **Step 1: Write the failing test**

Create `tests/test_spaces_moderation_routes.py`:

```python
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from skchat.spaces.registry import SpaceRegistry
from skchat.spaces.routes import register_spaces_routes


class FakeModerator:
    def __init__(self):
        self.calls = []

    async def stage_action(self, room, identity, action):
        self.calls.append(("stage", room, identity, action))
        return action == "invite"  # pretend invite reaches stage

    async def kick(self, room, identity):
        self.calls.append(("kick", room, identity))

    async def mute(self, room, identity, track_sid):
        self.calls.append(("mute", room, identity, track_sid))


@pytest.fixture
def setup(tmp_path, monkeypatch):
    monkeypatch.setenv("SKCHAT_LIVEKIT_API_KEY", "k")
    monkeypatch.setenv("SKCHAT_LIVEKIT_API_SECRET", "s")
    app = FastAPI()
    reg = SpaceRegistry(path=tmp_path / "s.json")
    mod = FakeModerator()
    register_spaces_routes(app, registry=reg, moderator=mod)
    c = TestClient(app)
    sid = c.post("/spaces/create", json={
        "host_fqid": "lumina@chef.skworld", "title": "T", "slug": "s"}).json()["space_id"]
    return c, sid, mod


def test_listener_can_raise_own_hand(setup):
    c, sid, mod = setup
    r = c.post(f"/spaces/{sid}/raise-hand", json={"identity": "alice@x.y"})
    assert r.status_code == 200
    assert ("stage", sid, "alice@x.y", "raise_hand") in mod.calls


def test_host_can_invite(setup):
    c, sid, mod = setup
    r = c.post(f"/spaces/{sid}/invite", json={
        "requester": "lumina@chef.skworld", "identity": "alice@x.y"})
    assert r.status_code == 200
    assert r.json()["on_stage"] is True
    assert ("stage", sid, "alice@x.y", "invite") in mod.calls


def test_non_host_cannot_invite(setup):
    c, sid, mod = setup
    r = c.post(f"/spaces/{sid}/invite", json={
        "requester": "rando@x.y", "identity": "alice@x.y"})
    assert r.status_code == 403
    assert all(call[0] != "stage" or call[3] != "invite" for call in mod.calls)


def test_non_host_cannot_kick(setup):
    c, sid, mod = setup
    r = c.post(f"/spaces/{sid}/kick", json={
        "requester": "rando@x.y", "identity": "alice@x.y"})
    assert r.status_code == 403


def test_host_can_kick_and_mute(setup):
    c, sid, mod = setup
    assert c.post(f"/spaces/{sid}/kick", json={
        "requester": "lumina@chef.skworld", "identity": "troll@x.y"}).status_code == 200
    assert c.post(f"/spaces/{sid}/mute", json={
        "requester": "lumina@chef.skworld", "identity": "loud@x.y",
        "track_sid": "TR_1"}).status_code == 200
    assert ("kick", sid, "troll@x.y") in mod.calls
    assert ("mute", sid, "loud@x.y", "TR_1") in mod.calls


def test_self_can_remove_from_stage(setup):
    c, sid, mod = setup
    r = c.post(f"/spaces/{sid}/remove-from-stage", json={
        "requester": "alice@x.y", "identity": "alice@x.y"})
    assert r.status_code == 200  # self-removal allowed
```

- [ ] **Step 2: Run test to verify it fails**

Run: `~/.skenv/bin/python -m pytest tests/test_spaces_moderation_routes.py -v`
Expected: FAIL — `register_spaces_routes` has no `moderator` kwarg / routes 404.

- [ ] **Step 3: Extend `routes.py`**

In `src/skchat/spaces/routes.py`, change the signature and add a lazy moderator +
the routes. Update the function definition:

```python
def register_spaces_routes(app: FastAPI, *, registry: SpaceRegistry | None = None,
                           moderator=None) -> None:
    reg = registry or SpaceRegistry()
    _mod_holder = {"mod": moderator}

    def _moderator():
        if _mod_holder["mod"] is None:
            from skchat.spaces.moderation import Moderator
            _mod_holder["mod"] = Moderator(
                _url(), os.getenv("SKCHAT_LIVEKIT_API_KEY", ""),
                os.getenv("SKCHAT_LIVEKIT_API_SECRET", ""))
        return _mod_holder["mod"]

    def _require_host(space, requester: str) -> None:
        if requester != space.host_fqid:
            raise HTTPException(403, "host-only action")
```

Then add these routes inside `register_spaces_routes` (after `end_space`):

```python
    @app.post("/spaces/{space_id}/raise-hand")
    async def raise_hand(space_id: str, request: Request) -> JSONResponse:
        space = reg.get(space_id)
        if space is None:
            raise HTTPException(404, "space not found")
        body = await request.json()
        identity = (body.get("identity") or "").strip()
        if not identity:
            raise HTTPException(400, "identity required")
        on_stage = await _moderator().stage_action(space.room, identity, "raise_hand")
        return JSONResponse({"ok": True, "on_stage": on_stage})

    @app.post("/spaces/{space_id}/invite")
    async def invite(space_id: str, request: Request) -> JSONResponse:
        space = reg.get(space_id)
        if space is None:
            raise HTTPException(404, "space not found")
        body = await request.json()
        _require_host(space, (body.get("requester") or "").strip())
        identity = (body.get("identity") or "").strip()
        if not identity:
            raise HTTPException(400, "identity required")
        on_stage = await _moderator().stage_action(space.room, identity, "invite")
        return JSONResponse({"ok": True, "on_stage": on_stage})

    @app.post("/spaces/{space_id}/remove-from-stage")
    async def remove_from_stage(space_id: str, request: Request) -> JSONResponse:
        space = reg.get(space_id)
        if space is None:
            raise HTTPException(404, "space not found")
        body = await request.json()
        requester = (body.get("requester") or "").strip()
        identity = (body.get("identity") or "").strip()
        # host OR self may remove from stage
        if requester != space.host_fqid and requester != identity:
            raise HTTPException(403, "host-or-self only")
        await _moderator().stage_action(space.room, identity, "remove")
        return JSONResponse({"ok": True})

    @app.post("/spaces/{space_id}/mute")
    async def mute(space_id: str, request: Request) -> JSONResponse:
        space = reg.get(space_id)
        if space is None:
            raise HTTPException(404, "space not found")
        body = await request.json()
        _require_host(space, (body.get("requester") or "").strip())
        await _moderator().mute(space.room, (body.get("identity") or "").strip(),
                                (body.get("track_sid") or "").strip())
        return JSONResponse({"ok": True})

    @app.post("/spaces/{space_id}/kick")
    async def kick(space_id: str, request: Request) -> JSONResponse:
        space = reg.get(space_id)
        if space is None:
            raise HTTPException(404, "space not found")
        body = await request.json()
        _require_host(space, (body.get("requester") or "").strip())
        await _moderator().kick(space.room, (body.get("identity") or "").strip())
        return JSONResponse({"ok": True})
```

> **SECURITY NOTE (track for S2-followup / S5):** `requester` is currently taken
> from the request body — trust-on-assertion, not cryptographic. A malicious caller
> could claim `requester == host_fqid`. This is acceptable for the tailnet-only S1/S2
> surface but MUST be hardened: host actions should require a **host token** (the
> JWT minted with `roomAdmin`) or a capauth signature. Add a coord follow-up; the
> federation work (S5, `sk-lk-authd`) introduces the signed-assertion path this
> should adopt.

- [ ] **Step 4: Run test to verify it passes**

Run: `~/.skenv/bin/python -m pytest tests/test_spaces_moderation_routes.py -v`
Expected: PASS (6 tests).

- [ ] **Step 5: Commit**

```bash
git add src/skchat/spaces/routes.py tests/test_spaces_moderation_routes.py
git commit -m "feat(spaces): host-gated moderation routes (raise-hand/invite/remove/mute/kick)"
```

---

## Task 4: Wire the UI — ✋ raise-hand, promote→mic, host controls

**Files:**
- Modify: `src/skchat/static/space.html`
- Test: `tests/test_spaces_ui_markup.py`

The interactive JS can't be unit-tested headlessly here, so the test pins that the
required hooks exist in the markup; the behavior is verified in the S2 runbook.

- [ ] **Step 1: Write the failing test**

Create `tests/test_spaces_ui_markup.py`:

```python
from pathlib import Path


def _html():
    p = Path("src/skchat/static/space.html")
    return p.read_text(encoding="utf-8")


def test_raise_hand_posts_to_endpoint():
    html = _html()
    assert "/raise-hand" in html


def test_permissions_changed_enables_mic():
    html = _html()
    assert "ParticipantPermissionsChanged" in html
    assert "setMicrophoneEnabled" in html


def test_host_controls_present():
    html = _html()
    # host control endpoints wired in the page
    assert "/invite" in html
    assert "/kick" in html
```

- [ ] **Step 2: Run test to verify it fails**

Run: `~/.skenv/bin/python -m pytest tests/test_spaces_ui_markup.py -v`
Expected: FAIL — the S1 page has none of these hooks yet.

- [ ] **Step 3: Extend `space.html`**

In `src/skchat/static/space.html`, wire the ✋ button, the promotion handler, and a
minimal host-control surface. Replace the `<script>` block's `join` handler region
and the `hand` button wiring with this (keep the existing token/render code; add):

Add to the `room.on(...)` registrations inside the `join` handler (right after the
`TrackSubscribed` handler):

```javascript
      room.on(LK.RoomEvent.ParticipantPermissionsChanged, async (prev, p) => {
        if (p === room.localParticipant && p.permissions?.canPublish) {
          await room.localParticipant.setMicrophoneEnabled(true);  // promoted → talk
          step("You're on stage — mic live.");
        }
        renderSpeakers();
      });
```

Wire the ✋ button (replace the disabled stub):

```javascript
  document.getElementById("hand").onclick = async () => {
    const identity = room?.localParticipant?.identity;
    if (!identity) return;
    await fetch(`/spaces/${spaceId}/raise-hand`, {
      method:"POST", headers:{"content-type":"application/json"},
      body: JSON.stringify({ identity })
    });
    step("✋ hand raised — waiting for the host.");
  };
```

Add a minimal host-control surface — when the page is opened with `?host=<fqid>`,
clicking a speaker/listener ring offers invite/remove/kick. Append inside the
`<script>`:

```javascript
  const hostFqid = qs.get("host");          // present → host controls enabled
  async function hostAction(path, identity, extra = {}) {
    await fetch(`/spaces/${spaceId}/${path}`, {
      method:"POST", headers:{"content-type":"application/json"},
      body: JSON.stringify({ requester: hostFqid, identity, ...extra })
    });
  }
  // delegated click: host clicks a participant ring → prompt action
  document.getElementById("speakers").addEventListener("click", async (e) => {
    if (!hostFqid) return;
    const ring = e.target.closest(".ring"); if (!ring) return;
    const identity = ring.id.replace("ring-", "");
    const act = prompt(`Action for ${identity}: invite / remove-from-stage / kick`);
    if (!act) return;
    await hostAction(act.trim(), identity);
    step(`${act} → ${identity}`);
  });
```

Also update `renderSpeakers()` so listeners with a raised hand are visible to the
host: when building the speaker list, also surface raised-hand listeners. Add,
inside the participant loop where listeners are counted, before `continue`:

```javascript
      if (!canSpeak) {
        listeners++;
        try {
          const meta = JSON.parse(p.metadata || "{}");
          if (meta.hand_raised && hostFqid) {
            const wrap = document.createElement("div"); wrap.className = "speaker";
            const ring = document.createElement("div");
            ring.className = "ring"; ring.id = "ring-" + p.identity;
            ring.textContent = "✋"; ring.style.borderColor = "var(--accent)";
            const name = document.createElement("div"); name.className = "name";
            name.textContent = p.name || p.identity;
            wrap.append(ring, name); el.append(wrap);
          }
        } catch (_) {}
        continue;
      }
```

- [ ] **Step 4: Run test to verify it passes**

Run: `~/.skenv/bin/python -m pytest tests/test_spaces_ui_markup.py -v`
Expected: PASS (3 tests).

- [ ] **Step 5: Commit**

```bash
git add src/skchat/static/space.html tests/test_spaces_ui_markup.py
git commit -m "feat(spaces): UI — raise-hand, promote→mic, host controls + ✋ queue"
```

---

## Task 5: S1-review hardening (host-gate `end`, registry robustness, guest-path test)

The S1 code review found two Important auth gaps that are spec deviations (§8 says
end/mute/kick are host-only) plus a registry brittleness and a guest-path coverage
gap. Fix them here, now that `_require_host` exists.

**Files:**
- Modify: `src/skchat/spaces/routes.py` (`end_space` host-gate + `create_space` auth note)
- Modify: `src/skchat/spaces/registry.py` (schema-drift-safe load)
- Modify: `tests/test_spaces_routes.py` (update the `end` test for the new host-gate)
- Modify: `src/skchat/static/space.html` (end button posts `requester`)
- Test: `tests/test_spaces_guest_join.py` (new — guest path mints a LISTENER, no escalation)

- [ ] **Step 1: Host-gate `end_space` + update the existing S1 end test**

In `src/skchat/spaces/routes.py`, change `end_space` to require the host:

```python
    @app.post("/spaces/{space_id}/end")
    async def end_space(space_id: str, request: Request) -> JSONResponse:
        space = reg.get(space_id)
        if space is None:
            raise HTTPException(404, "space not found")
        body = await request.json()
        _require_host(space, (body.get("requester") or "").strip())
        reg.end(space_id)
        return JSONResponse({"ok": True, "space_id": space_id})
```

In `tests/test_spaces_routes.py`, update `test_end_marks_not_live` to pass the host
requester, and add a non-host rejection:

```python
def test_end_marks_not_live(client):
    sid = client.post("/spaces/create", json={
        "host_fqid": "lumina@chef.skworld", "title": "T", "slug": "s"}).json()["space_id"]
    assert client.post(f"/spaces/{sid}/end",
                       json={"requester": "lumina@chef.skworld"}).status_code == 200
    live = client.get("/spaces").json()["spaces"]
    assert all(s["space_id"] != sid for s in live)


def test_non_host_cannot_end(client):
    sid = client.post("/spaces/create", json={
        "host_fqid": "lumina@chef.skworld", "title": "T", "slug": "s"}).json()["space_id"]
    assert client.post(f"/spaces/{sid}/end",
                       json={"requester": "rando@x.y"}).status_code == 403
```

- [ ] **Step 2: Document the `create_space` trust assumption**

In `src/skchat/spaces/routes.py`, add a comment at the top of `create_space` (it
mints a host/roomAdmin token from a body-supplied `host_fqid`):

```python
        # SECURITY: S1/S2 trust the tailnet — host_fqid is asserted, not proven, so
        # this endpoint mints a roomAdmin token for whoever asks. Tailnet-only until
        # S5 sk-lk-authd verifies a capauth-signed operator assertion. Do NOT expose
        # this route publicly before that hardening lands.
```

- [ ] **Step 3: Make registry load schema-drift-safe**

In `src/skchat/spaces/registry.py`, filter persisted dicts to known dataclass
fields before the splat (a future/legacy `spaces.json` must not crash `_load`).
Change the import line to include `fields`:

```python
from dataclasses import asdict, fields
```

and in `_load`, replace the per-record build:

```python
        known = {f.name for f in fields(Space)}
        for d in raw.get("spaces", []):
            d = {k: v for k, v in d.items() if k in known}
            if "space_id" not in d:
                continue
            d["status"] = SpaceStatus(d.get("status", "open"))
            try:
                self._spaces[d["space_id"]] = Space(**d)
            except (TypeError, ValueError):
                continue  # skip malformed record, keep the rest
```

- [ ] **Step 4: End button in `space.html` posts the requester**

In `src/skchat/static/space.html`, update the `#end` click handler to include the
host requester (the page already reads `hostFqid` from `?host=`):

```javascript
  document.getElementById("end").onclick = async () => {
    await fetch(`/spaces/${spaceId}/end`, {
      method:"POST", headers:{"content-type":"application/json"},
      body: JSON.stringify({ requester: hostFqid })
    });
    if (room) room.disconnect();
    step("Space ended.");
  };
```

- [ ] **Step 5: Guest-path test — invite mints a LISTENER, cannot escalate**

Create `tests/test_spaces_guest_join.py`:

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
    monkeypatch.setenv("SKCHAT_GUEST_TOKEN_SECRET", "guest-secret-xyz")
    app = FastAPI()
    register_spaces_routes(app, registry=SpaceRegistry(path=tmp_path / "s.json"))
    return TestClient(app)


def test_guest_invite_joins_as_listener_only(client):
    sid = client.post("/spaces/create", json={
        "host_fqid": "lumina@chef.skworld", "title": "T", "slug": "s"}).json()["space_id"]

    # host mints a guest invite bound to THIS space id (room == space_id)
    from skchat.guest import InviteIssuer
    invite = InviteIssuer().create_invite(room=sid, display="Visitor", ttl=3600,
                                          issuer="lumina@chef.skworld")
    r = client.post(f"/spaces/{sid}/join-guest", json={
        "invite_token": invite["invite_token"], "display": "Visitor"})
    assert r.status_code == 200
    v = jwt.decode(r.json()["token"], _SECRET, algorithms=["HS256"],
                   options={"verify_aud": False})["video"]
    assert v.get("canPublish", False) is False   # guest cannot publish
    assert v["canSubscribe"] is True
    assert r.json()["role"] == "listener"


def test_guest_invite_for_other_space_rejected(client):
    sid = client.post("/spaces/create", json={
        "host_fqid": "lumina@chef.skworld", "title": "T", "slug": "s"}).json()["space_id"]
    from skchat.guest import InviteIssuer
    other = InviteIssuer().create_invite(room="space-someotherroom0", display="X",
                                         ttl=3600, issuer="lumina@chef.skworld")
    r = client.post(f"/spaces/{sid}/join-guest", json={
        "invite_token": other["invite_token"], "display": "X"})
    assert r.status_code == 403   # invite bound to a different room
```

> **NOTE for implementer:** confirm `InviteIssuer.create_invite(room=, display=, ttl=,
> issuer=)` against `src/skchat/guest.py` (recon shows this signature returning a dict
> with `invite_token`). If the kwargs differ, adapt the test call — the assertions
> (guest gets a listener grant; wrong-room invite is 403) are the point.

- [ ] **Step 6: Run the affected tests**

Run: `~/.skenv/bin/python -m pytest tests/test_spaces_routes.py tests/test_spaces_guest_join.py -v`
Expected: PASS (the updated end tests + both guest-path tests green).

- [ ] **Step 7: Commit**

```bash
git add src/skchat/spaces/routes.py src/skchat/spaces/registry.py \
        src/skchat/static/space.html tests/test_spaces_routes.py \
        tests/test_spaces_guest_join.py
git commit -m "fix(spaces): host-gate end, harden registry load, guest-path tests (S1 review)"
```

---

## Final verification

- [ ] **Run the full spaces suite + the whole skchat suite**

Run: `~/.skenv/bin/python -m pytest tests/test_spaces_*.py -v && ~/.skenv/bin/python -m pytest tests/ -q`
Expected: all spaces tests (S1 + S2) pass; no regressions in the existing suite.

- [ ] **Lint**

Run: `~/.skenv/bin/ruff check src/skchat/spaces/ tests/test_spaces_*.py`
Expected: no errors.

- [ ] **Manual smoke (optional, needs live SFU + 2 browsers)**

Host opens `/space/<id>?host=lumina@chef.skworld`, a listener opens `/space/<id>`,
listener clicks ✋, host clicks the listener's ✋ ring → `invite` → listener's mic
goes live (promoted with no rejoin). Host clicks a speaker → `remove-from-stage` →
demoted.

---

## What S2 delivers

Live moderation: a listener raises a hand, the host invites, and **only when both
agree** does LiveKit flip `canPublish` so the listener's mic goes live — no rejoin.
Hosts can remove-from-stage, mute, and kick; the consent rule is a pure,
exhaustively-tested state machine and the LiveKit calls are mock-tested with no
live SFU. The one known gap — `requester` is asserted, not cryptographically proven
— is flagged for hardening alongside the S5 `sk-lk-authd` signed-assertion path.
```
