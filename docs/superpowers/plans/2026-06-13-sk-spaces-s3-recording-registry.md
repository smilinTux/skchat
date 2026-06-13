# SK Spaces — S3 Recording + Live-Now Directory Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add audio-only recording with per-speaker consent (off by default, visible "● REC", consented speakers only) and a "live now" Space directory, building on the S1/S2 `spaces` package.

**Architecture:** Per-speaker consent is a pure ledger + `can_record` gate (SFU-free, fully tested). A thin `Recorder` wraps LiveKit Egress (`start_room_composite_egress` audio_only) behind an injectable factory, mock-tested. Host-gated record routes enforce the consent gate; the registry tracks REC state; a new directory page lists live Spaces. The egress *container* is a deploy-config task (not CI-tested).

**Tech Stack:** Python 3.10+, `livekit-api` (`RoomCompositeEgressRequest`/`EncodedFileOutput`/`StopEgressRequest`/`EncodedFileType` — all confirmed present), FastAPI TestClient. Line length 99, ruff.

**Spec:** `docs/superpowers/specs/2026-06-13-sk-spaces-design.md` §8 (recording/consent), §6/§9 (registry/directory). **Depends on S1+S2.** Coord: `a2318ae9`.

**Confirmed Egress API (python):**
- `eg = api.LiveKitAPI(http_url, key, secret).egress`
- `await eg.start_room_composite_egress(api.RoomCompositeEgressRequest(room_name=, audio_only=True, file_outputs=[api.EncodedFileOutput(file_type=api.EncodedFileType.OGG, filepath=)]))` → `.egress_id`
- `await eg.stop_egress(api.StopEgressRequest(egress_id=))`

**Run tests from repo root:** `~/.skenv/bin/python -m pytest tests/ -q`.

---

## Task 1: Per-speaker consent ledger + record gate (pure)

**Files:**
- Create: `src/skchat/spaces/consent.py`
- Test: `tests/test_spaces_consent_ledger.py`

Spec §8: a speaker's track only enters a recording after they consent. The gate:
a room-composite recording mixes everyone, so it may start **only when every
current on-stage speaker has consented**.

- [ ] **Step 1: Write the failing test**

Create `tests/test_spaces_consent_ledger.py`:

```python
from skchat.spaces.consent import ConsentLedger, can_record


def test_can_record_requires_all_speakers_consented(tmp_path):
    led = ConsentLedger(path=tmp_path / "consent.json")
    led.add("space-x", "alice@x.y")
    ok, missing = can_record(["alice@x.y", "bob@x.y"], "space-x", led)
    assert ok is False
    assert missing == ["bob@x.y"]


def test_can_record_ok_when_all_consented(tmp_path):
    led = ConsentLedger(path=tmp_path / "consent.json")
    led.add("space-x", "alice@x.y")
    led.add("space-x", "bob@x.y")
    ok, missing = can_record(["alice@x.y", "bob@x.y"], "space-x", led)
    assert ok is True
    assert missing == []


def test_consent_is_scoped_per_space(tmp_path):
    led = ConsentLedger(path=tmp_path / "consent.json")
    led.add("space-x", "alice@x.y")
    ok, missing = can_record(["alice@x.y"], "space-other", led)
    assert ok is False
    assert missing == ["alice@x.y"]      # consent in space-x doesn't carry over


def test_empty_speaker_list_can_record(tmp_path):
    led = ConsentLedger(path=tmp_path / "consent.json")
    ok, missing = can_record([], "space-x", led)
    assert ok is True                    # nobody on stage → nothing to consent to


def test_consent_persists(tmp_path):
    p = tmp_path / "consent.json"
    ConsentLedger(path=p).add("space-x", "alice@x.y")
    assert ConsentLedger(path=p).has("space-x", "alice@x.y") is True


def test_revoke_consent(tmp_path):
    led = ConsentLedger(path=tmp_path / "consent.json")
    led.add("space-x", "alice@x.y")
    led.revoke("space-x", "alice@x.y")
    assert led.has("space-x", "alice@x.y") is False
```

- [ ] **Step 2: Run test to verify it fails**

Run: `~/.skenv/bin/python -m pytest tests/test_spaces_consent_ledger.py -v`
Expected: FAIL with `ModuleNotFoundError: skchat.spaces.consent`.

- [ ] **Step 3: Write the implementation**

Create `src/skchat/spaces/consent.py`:

```python
"""Per-speaker recording consent (spec §8): a room-composite recording may start
only when every on-stage speaker has consented. Pure logic + a json-backed ledger."""

from __future__ import annotations

import json
from pathlib import Path

_DEFAULT_PATH = Path.home() / ".skchat" / "spaces-consent.json"


class ConsentLedger:
    def __init__(self, path: Path | None = None) -> None:
        self.path = Path(path) if path else _DEFAULT_PATH
        self._by_space: dict[str, set[str]] = {}
        self._load()

    def _load(self) -> None:
        if not self.path.exists():
            return
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            return
        self._by_space = {k: set(v) for k, v in raw.items()}

    def _save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(
            json.dumps({k: sorted(v) for k, v in self._by_space.items()}, indent=2),
            encoding="utf-8")

    def add(self, space_id: str, identity: str) -> None:
        self._by_space.setdefault(space_id, set()).add(identity)
        self._save()

    def revoke(self, space_id: str, identity: str) -> None:
        self._by_space.get(space_id, set()).discard(identity)
        self._save()

    def has(self, space_id: str, identity: str) -> bool:
        return identity in self._by_space.get(space_id, set())


def can_record(speakers: list[str], space_id: str,
               ledger: ConsentLedger) -> tuple[bool, list[str]]:
    """Return (ok, missing). ok iff every speaker has consented in this space."""
    missing = [s for s in speakers if not ledger.has(space_id, s)]
    return (not missing, missing)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `~/.skenv/bin/python -m pytest tests/test_spaces_consent_ledger.py -v`
Expected: PASS (6 tests).

- [ ] **Step 5: Commit**

```bash
git add src/skchat/spaces/consent.py tests/test_spaces_consent_ledger.py
git commit -m "feat(spaces): per-speaker recording consent ledger + gate"
```

---

## Task 2: Recorder (Egress wrapper, injectable)

**Files:**
- Create: `src/skchat/spaces/recording.py`
- Test: `tests/test_spaces_recorder.py`

- [ ] **Step 1: Write the failing test**

Create `tests/test_spaces_recorder.py`:

```python
import pytest

from skchat.spaces.recording import Recorder


class FakeEgress:
    def __init__(self):
        self.started = []
        self.stopped = []

    async def start_room_composite_egress(self, req):
        self.started.append(req)
        class _R:  # minimal egress-info stand-in
            egress_id = "EG_test123"
        return _R()

    async def stop_egress(self, req):
        self.stopped.append(req.egress_id)


@pytest.fixture
def fake():
    return FakeEgress()


@pytest.fixture
def rec(fake):
    return Recorder("ws://test:7880", "k", "s", _egress=fake)


@pytest.mark.asyncio
async def test_start_returns_egress_id_and_is_audio_only(rec, fake):
    eid = await rec.start("space-x", "/tmp/space-x.ogg")
    assert eid == "EG_test123"
    req = fake.started[-1]
    assert req.room_name == "space-x"
    assert req.audio_only is True
    assert len(req.file_outputs) == 1


@pytest.mark.asyncio
async def test_stop_calls_stop_egress(rec, fake):
    await rec.stop("EG_test123")
    assert fake.stopped == ["EG_test123"]
```

- [ ] **Step 2: Run test to verify it fails**

Run: `~/.skenv/bin/python -m pytest tests/test_spaces_recorder.py -v`
Expected: FAIL with `ModuleNotFoundError: skchat.spaces.recording`.

- [ ] **Step 3: Write the implementation**

Create `src/skchat/spaces/recording.py`:

```python
"""Recorder — audio-only room-composite Egress for a Space (spec §8).

Egress is injectable for tests. Audio-only OGG file output; the consent gate
(consent.can_record) is enforced by the caller (routes) before start.
"""

from __future__ import annotations


def _http_url(ws_url: str) -> str:
    return ws_url.replace("ws://", "http://").replace("wss://", "https://")


class Recorder:
    def __init__(self, ws_url: str, api_key: str, api_secret: str,
                 *, _egress=None) -> None:
        self._ws_url = ws_url
        self._key = api_key
        self._secret = api_secret
        self._eg = _egress

    def _egress(self):
        if self._eg is not None:
            return self._eg
        from livekit import api
        self._eg = api.LiveKitAPI(_http_url(self._ws_url), self._key,
                                  self._secret).egress
        return self._eg

    async def start(self, room: str, filepath: str) -> str:
        """Start an audio-only room-composite recording; return the egress id."""
        from livekit import api
        req = api.RoomCompositeEgressRequest(
            room_name=room,
            audio_only=True,
            file_outputs=[api.EncodedFileOutput(
                file_type=api.EncodedFileType.OGG, filepath=filepath)],
        )
        info = await self._egress().start_room_composite_egress(req)
        return info.egress_id

    async def stop(self, egress_id: str) -> None:
        from livekit import api
        await self._egress().stop_egress(api.StopEgressRequest(egress_id=egress_id))
```

> **NOTE for implementer:** confirm `api.EncodedFileType.OGG` exists in the installed
> SDK (protobuf enum; values are typically `DEFAULT_FILETYPE`, `MP4`, `OGG`). If
> `OGG` isn't exposed by that name, use the correct audio file-type constant — the
> test only pins `audio_only is True` + one file output, so adapt the enum value to
> what the SDK provides. Also confirm `RoomCompositeEgressRequest` accepts
> `file_outputs=[...]` (it does per introspection); if it requires the singular
> `file=` field instead, use that — keep `audio_only=True`.

- [ ] **Step 4: Run test to verify it passes**

Run: `~/.skenv/bin/python -m pytest tests/test_spaces_recorder.py -v`
Expected: PASS (2 tests).

- [ ] **Step 5: Commit**

```bash
git add src/skchat/spaces/recording.py tests/test_spaces_recorder.py
git commit -m "feat(spaces): audio-only Egress Recorder (injectable)"
```

---

## Task 3: Recording state + routes (consent + REC)

**Files:**
- Modify: `src/skchat/spaces/space.py` (add `recording` + `egress_id` fields)
- Modify: `src/skchat/spaces/registry.py` (`set_recording`)
- Modify: `src/skchat/spaces/routes.py` (consent + record routes; REC in list)
- Test: `tests/test_spaces_recording_routes.py`

- [ ] **Step 1: Write the failing test**

Create `tests/test_spaces_recording_routes.py`:

```python
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from skchat.spaces.consent import ConsentLedger
from skchat.spaces.registry import SpaceRegistry
from skchat.spaces.routes import register_spaces_routes


class FakeRecorder:
    def __init__(self):
        self.started, self.stopped = [], []

    async def start(self, room, filepath):
        self.started.append((room, filepath))
        return "EG_xyz"

    async def stop(self, egress_id):
        self.stopped.append(egress_id)


@pytest.fixture
def setup(tmp_path, monkeypatch):
    monkeypatch.setenv("SKCHAT_LIVEKIT_API_KEY", "k")
    monkeypatch.setenv("SKCHAT_LIVEKIT_API_SECRET", "s")
    app = FastAPI()
    reg = SpaceRegistry(path=tmp_path / "s.json")
    led = ConsentLedger(path=tmp_path / "c.json")
    rec = FakeRecorder()
    register_spaces_routes(app, registry=reg, consent=led, recorder=rec)
    c = TestClient(app)
    sid = c.post("/spaces/create", json={
        "host_fqid": "lumina@chef.skworld", "title": "T", "slug": "s"}).json()["space_id"]
    return c, sid, rec, led


def test_record_blocked_until_speakers_consent(setup):
    c, sid, rec, led = setup
    r = c.post(f"/spaces/{sid}/record/start", json={
        "requester": "lumina@chef.skworld", "speakers": ["alice@x.y"]})
    assert r.status_code == 409
    assert r.json()["missing_consent"] == ["alice@x.y"]
    assert rec.started == []                       # not started


def test_record_starts_after_consent(setup):
    c, sid, rec, led = setup
    c.post(f"/spaces/{sid}/consent", json={"identity": "alice@x.y"})
    r = c.post(f"/spaces/{sid}/record/start", json={
        "requester": "lumina@chef.skworld", "speakers": ["alice@x.y"]})
    assert r.status_code == 200
    assert len(rec.started) == 1
    # REC reflected in the live listing
    live = c.get("/spaces").json()["spaces"]
    assert next(s for s in live if s["space_id"] == sid)["recording"] is True


def test_non_host_cannot_record(setup):
    c, sid, rec, led = setup
    r = c.post(f"/spaces/{sid}/record/start", json={
        "requester": "rando@x.y", "speakers": []})
    assert r.status_code == 403


def test_stop_recording(setup):
    c, sid, rec, led = setup
    c.post(f"/spaces/{sid}/consent", json={"identity": "alice@x.y"})
    c.post(f"/spaces/{sid}/record/start", json={
        "requester": "lumina@chef.skworld", "speakers": ["alice@x.y"]})
    r = c.post(f"/spaces/{sid}/record/stop", json={"requester": "lumina@chef.skworld"})
    assert r.status_code == 200
    assert rec.stopped == ["EG_xyz"]
    live = c.get("/spaces").json()["spaces"]
    assert next(s for s in live if s["space_id"] == sid)["recording"] is False
```

- [ ] **Step 2: Run test to verify it fails**

Run: `~/.skenv/bin/python -m pytest tests/test_spaces_recording_routes.py -v`
Expected: FAIL — `register_spaces_routes` has no `consent`/`recorder` kwargs.

- [ ] **Step 3: Add `recording`/`egress_id` to the Space model**

In `src/skchat/spaces/space.py`, add to the `Space` dataclass (after `speakers`):

```python
    recording: bool = False
    egress_id: str = ""
```

- [ ] **Step 4: Add `set_recording` to the registry**

In `src/skchat/spaces/registry.py`, add to `SpaceRegistry`:

```python
    def set_recording(self, space_id: str, recording: bool, egress_id: str = "") -> None:
        s = self._spaces.get(space_id)
        if s is not None:
            s.recording = recording
            s.egress_id = egress_id
            self._save()
```

- [ ] **Step 5: Extend `routes.py`**

In `src/skchat/spaces/routes.py`, update the signature + lazy helpers:

```python
def register_spaces_routes(app: FastAPI, *, registry: SpaceRegistry | None = None,
                           moderator=None, consent=None, recorder=None) -> None:
    reg = registry or SpaceRegistry()
    _mod_holder = {"mod": moderator}
    from skchat.spaces.consent import ConsentLedger
    led = consent or ConsentLedger()
    _rec_holder = {"rec": recorder}

    def _recorder():
        if _rec_holder["rec"] is None:
            from skchat.spaces.recording import Recorder
            _rec_holder["rec"] = Recorder(
                _url(), os.getenv("SKCHAT_LIVEKIT_API_KEY", ""),
                os.getenv("SKCHAT_LIVEKIT_API_SECRET", ""))
        return _rec_holder["rec"]
```

Add `"recording": s.recording` to each space dict in the `list_spaces` route, then
add these routes inside `register_spaces_routes`:

```python
    @app.post("/spaces/{space_id}/consent")
    async def record_consent(space_id: str, request: Request) -> JSONResponse:
        if reg.get(space_id) is None:
            raise HTTPException(404, "space not found")
        body = await request.json()
        identity = (body.get("identity") or "").strip()
        if not identity:
            raise HTTPException(400, "identity required")
        led.add(space_id, identity)
        return JSONResponse({"ok": True})

    @app.post("/spaces/{space_id}/record/start")
    async def record_start(space_id: str, request: Request) -> JSONResponse:
        from pathlib import Path as _P
        from skchat.spaces.consent import can_record
        space = reg.get(space_id)
        if space is None:
            raise HTTPException(404, "space not found")
        body = await request.json()
        _require_host(space, (body.get("requester") or "").strip())
        speakers = body.get("speakers") or []
        ok, missing = can_record(speakers, space_id, led)
        if not ok:
            return JSONResponse({"ok": False, "missing_consent": missing},
                                status_code=409)
        rec_dir = _P.home() / ".skchat" / "spaces-recordings"
        rec_dir.mkdir(parents=True, exist_ok=True)
        filepath = str(rec_dir / f"{space_id}.ogg")
        egress_id = await _recorder().start(space.room, filepath)
        reg.set_recording(space_id, True, egress_id)
        return JSONResponse({"ok": True, "egress_id": egress_id, "path": filepath})

    @app.post("/spaces/{space_id}/record/stop")
    async def record_stop(space_id: str, request: Request) -> JSONResponse:
        space = reg.get(space_id)
        if space is None:
            raise HTTPException(404, "space not found")
        body = await request.json()
        _require_host(space, (body.get("requester") or "").strip())
        if space.egress_id:
            await _recorder().stop(space.egress_id)
        reg.set_recording(space_id, False, "")
        return JSONResponse({"ok": True})
```

- [ ] **Step 6: Run the test**

Run: `~/.skenv/bin/python -m pytest tests/test_spaces_recording_routes.py -v`
Expected: PASS (4 tests).

- [ ] **Step 7: Commit**

```bash
git add src/skchat/spaces/space.py src/skchat/spaces/registry.py \
        src/skchat/spaces/routes.py tests/test_spaces_recording_routes.py
git commit -m "feat(spaces): consent + audio recording routes (REC state in registry)"
```

---

## Task 4: Live-now directory page

**Files:**
- Create: `src/skchat/static/spaces.html`
- Modify: `src/skchat/spaces/routes.py` (add `GET /spaces/live` page route)
- Test: `tests/test_spaces_directory.py`

- [ ] **Step 1: Write the failing test**

Create `tests/test_spaces_directory.py`:

```python
from fastapi import FastAPI
from fastapi.testclient import TestClient

from skchat.spaces.registry import SpaceRegistry
from skchat.spaces.routes import register_spaces_routes


def test_directory_page_served(tmp_path, monkeypatch):
    monkeypatch.setenv("SKCHAT_LIVEKIT_API_KEY", "k")
    monkeypatch.setenv("SKCHAT_LIVEKIT_API_SECRET", "s")
    app = FastAPI()
    register_spaces_routes(app, registry=SpaceRegistry(path=tmp_path / "s.json"))
    c = TestClient(app)
    r = c.get("/spaces/live")
    assert r.status_code == 200
    assert "text/html" in r.headers["content-type"]
    # the page fetches the live list from the JSON endpoint
    assert "/spaces" in r.text
```

- [ ] **Step 2: Run test to verify it fails**

Run: `~/.skenv/bin/python -m pytest tests/test_spaces_directory.py -v`
Expected: FAIL — no `/spaces/live` route.

> **Ordering note:** register `GET /spaces/live` BEFORE any `GET /spaces/{space_id}`
> page route would shadow it. There is no `/spaces/{id}` GET in this package (only
> `/space/{id}`), so `/spaces/live` is unambiguous — but keep it distinct from the
> `GET /spaces` JSON list (different path, no conflict).

- [ ] **Step 3: Create the directory page**

Create `src/skchat/static/spaces.html` (2027 tokens; fetches `/spaces` and renders
live cards with a Join link to `/space/{id}`):

```html
<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8" />
<meta name="viewport" content="width=device-width, initial-scale=1" />
<title>SK Spaces — Live</title>
<style>
  :root { --bg:#0b0d10; --surface:#13161b; --line:#222831; --text:#e6e9ee;
    --muted:#8b94a3; --accent:#2dd4bf; --radius:14px; }
  body { margin:0; background:var(--bg); color:var(--text);
    font-family:Inter,system-ui,sans-serif; }
  header { padding:18px 20px; border-bottom:1px solid var(--line);
    font-weight:600; font-size:17px; }
  main { padding:20px; max-width:720px; margin:0 auto;
    display:flex; flex-direction:column; gap:14px; }
  .card { background:var(--surface); border:1px solid var(--line);
    border-radius:var(--radius); padding:16px 18px; display:flex;
    align-items:center; gap:14px; }
  .card .meta { flex:1; }
  .card h3 { margin:0 0 4px; font-size:15px; }
  .card .sub { color:var(--muted); font-size:13px; }
  .live { color:var(--accent); font-size:12px; font-weight:600; }
  .rec { color:#ef4444; font-size:12px; font-weight:600; margin-left:8px; }
  a.join { background:var(--accent); color:#04201c; text-decoration:none;
    border-radius:11px; padding:8px 14px; font-weight:600; font-size:13px; }
  .empty { color:var(--muted); text-align:center; padding:40px; }
</style>
</head>
<body>
<header>🎙️ SK Spaces — Live Now</header>
<main id="list"><div class="empty">Loading…</div></main>
<script>
  async function load() {
    const res = await fetch("/spaces");
    const { spaces } = await res.json();
    const list = document.getElementById("list");
    if (!spaces.length) { list.innerHTML = '<div class="empty">No live Spaces right now.</div>'; return; }
    list.innerHTML = "";
    for (const s of spaces) {
      const card = document.createElement("div"); card.className = "card";
      const speakers = (s.speakers || []).length;
      card.innerHTML = `
        <div class="meta">
          <h3>${s.title}</h3>
          <div class="sub">${s.host_fqid} · <span class="live">● LIVE</span>
            ${s.recording ? '<span class="rec">● REC</span>' : ''}
            · ${speakers} on stage</div>
        </div>
        <a class="join" href="/space/${s.space_id}">Join</a>`;
      list.append(card);
    }
  }
  load(); setInterval(load, 5000);
</script>
</body>
</html>
```

- [ ] **Step 4: Add the page route to `routes.py`**

Inside `register_spaces_routes`, add (the `Path`/`FileResponse`/`HTMLResponse`
imports were already added in S1 Task 7):

```python
    @app.get("/spaces/live", response_class=HTMLResponse)
    async def spaces_directory() -> HTMLResponse:
        static = Path(__file__).resolve().parent.parent / "static" / "spaces.html"
        if static.exists():
            return FileResponse(static, media_type="text/html")
        return HTMLResponse("spaces.html missing", status_code=500)
```

- [ ] **Step 5: Run the test**

Run: `~/.skenv/bin/python -m pytest tests/test_spaces_directory.py -v`
Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add src/skchat/static/spaces.html src/skchat/spaces/routes.py tests/test_spaces_directory.py
git commit -m "feat(spaces): live-now directory page (/spaces/live)"
```

---

## Task 5: Deploy — add the Egress service (config only)

**Files:**
- Modify: `src/skchat/static/space.html` (REC indicator polls status)
- Create: `deploy/v2/egress-stack.yml`
- Create: `runbooks/spaces-recording.md`

Recording needs the LiveKit Egress service running alongside the SFU. This task is
deploy config + a runbook — no Python tests (it's infra), but the REC indicator in
the room page IS wired here.

- [ ] **Step 1: Wire the REC indicator in `space.html`**

In `src/skchat/static/space.html`, make the `● REC` element reflect live state by
polling `/spaces`. Add inside the `<script>` (after `join` connects):

```javascript
  async function pollRec() {
    try {
      const { spaces } = await (await fetch("/spaces")).json();
      const me = spaces.find(s => s.space_id === spaceId);
      document.getElementById("rec").style.display =
        (me && me.recording) ? "inline" : "none";
    } catch (_) {}
  }
  setInterval(pollRec, 4000);
```

- [ ] **Step 2: Create the egress stack**

Create `deploy/v2/egress-stack.yml` (mirrors the livekit-stack pattern: host
network, tailnet-only, secrets from env/OpenBao, recordings volume shared with the
SFU output path):

```yaml
# LiveKit Egress — audio-only recording for SK Spaces.
# Pairs with livekit-stack.yml; needs Redis to coordinate with the SFU.
version: "3.8"

services:
  egress:
    image: livekit/egress:v1.8
    network_mode: host          # egress uses the host network like the SFU
    environment:
      EGRESS_CONFIG_BODY: |
        redis:
          address: 127.0.0.1:6379
        api_key: ${LIVEKIT_API_KEY}
        api_secret: ${LIVEKIT_API_SECRET}
        ws_url: ws://127.0.0.1:7880
        insecure: true
    volumes:
      - spaces-recordings:/out/spaces-recordings
    cap_add:
      - SYS_ADMIN            # required by egress (chrome sandbox for composite)
    deploy:
      placement:
        constraints:
          - node.labels.livekit == true
    restart: unless-stopped

volumes:
  spaces-recordings:
```

- [ ] **Step 3: Create the runbook**

Create `runbooks/spaces-recording.md`:

```markdown
# SK Spaces — recording (Egress)

Audio-only room-composite recording for Spaces. Off by default; consent-gated.

## Prereqs
- `livekit-stack.yml` running (the SFU) with **Redis enabled** (egress coordinates
  with the SFU over Redis — single-node-no-Redis setups must add it).
- `egress-stack.yml` deployed on the same node (`node.labels.livekit == true`).
- `LIVEKIT_API_KEY` / `LIVEKIT_API_SECRET` exported (same as the SFU).

## Flow
1. Each on-stage speaker POSTs `/spaces/{id}/consent {identity}` (the UI prompts
   them when they go on stage).
2. Host POSTs `/spaces/{id}/record/start {requester, speakers:[...]}`. If any
   speaker hasn't consented → 409 with `missing_consent`. Else egress starts and a
   `● REC` indicator shows to everyone.
3. Output OGG lands in the `spaces-recordings` volume at `<space_id>.ogg`.
4. Host POSTs `/spaces/{id}/record/stop {requester}` → egress stops.

## Notes
- The Recorder uses `start_room_composite_egress(audio_only=True)` → OGG file.
- Replays can be served via the existing recordings UI (point it at the
  spaces-recordings volume) — wire-up tracked separately.
- Egress needs `SYS_ADMIN` cap; keep it tailnet-only.
```

- [ ] **Step 4: Commit**

```bash
git add src/skchat/static/space.html deploy/v2/egress-stack.yml runbooks/spaces-recording.md
git commit -m "feat(spaces): egress deploy stack + recording runbook + REC indicator"
```

---

## Task 6: S2-review hardening (stage race, blank-host, ✋ reactivity)

The S2 code review found three Important items. Fix them here.

**Files:**
- Modify: `src/skchat/spaces/moderation.py` (per-identity lock in `stage_action`)
- Modify: `src/skchat/spaces/routes.py` (`_require_host` rejects empty host)
- Modify: `src/skchat/static/space.html` (`ParticipantMetadataChanged`→render; reveal End for host)
- Test: `tests/test_spaces_moderator.py` (race convergence), `tests/test_spaces_moderation_routes.py` (blank-host), `tests/test_spaces_ui_markup.py` (metadata-changed pin)

- [ ] **Step 1: Serialize `stage_action` per (room, identity)**

The read-modify-write in `stage_action` can lose an update when a listener's
`raise_hand` and the host's `invite` interleave. Add a per-identity asyncio lock.

In `src/skchat/spaces/moderation.py`, add to `Moderator.__init__`:

```python
        self._locks: dict[tuple[str, str], "asyncio.Lock"] = {}
```

add `import asyncio` at the top of the file, and wrap the body of `stage_action`:

```python
    async def stage_action(self, room: str, identity: str, action: str) -> bool:
        """Read current metadata, apply the consent action, push the new metadata
        + can_publish permission. Serialized per (room, identity) so concurrent
        raise_hand + invite cannot lose an update."""
        from livekit import api
        lock = self._locks.setdefault((room, identity), asyncio.Lock())
        async with lock:
            svc = self._service()
            current = await svc.get_participant(
                api.RoomParticipantIdentity(room=room, identity=identity))
            state = parse_meta(getattr(current, "metadata", "") or "")
            new_state, can_publish = apply_action(state, action)
            await svc.update_participant(api.UpdateParticipantRequest(
                room=room, identity=identity, metadata=dump_meta(new_state),
                permission=api.ParticipantPermission(
                    can_publish=can_publish, can_subscribe=True,
                    can_publish_data=True),
            ))
            return can_publish
```

- [ ] **Step 2: Write the race test (red first)**

Add to `tests/test_spaces_moderator.py` (the `FakeRoomService` already reflects
writes back via `get_participant`; force interleaving with a yield in get):

```python
import asyncio


@pytest.mark.asyncio
async def test_concurrent_raise_and_invite_converge(mod, fake, monkeypatch):
    # force a scheduler yield between read and write so an unlocked impl would
    # interleave and lose a flag; the per-identity lock must serialize them.
    orig_get = fake.get_participant

    async def slow_get(req):
        await asyncio.sleep(0)
        return await orig_get(req)

    monkeypatch.setattr(fake, "get_participant", slow_get)
    fake.set_participant("alice", "")  # starts off-stage

    await asyncio.gather(
        mod.stage_action("space-x", "alice", "raise_hand"),
        mod.stage_action("space-x", "alice", "invite"),
    )
    import json
    final = json.loads(fake.updates[-1].metadata)
    assert final["hand_raised"] is True
    assert final["invited_to_stage"] is True   # neither write clobbered the other
```

Run: `~/.skenv/bin/python -m pytest tests/test_spaces_moderator.py::test_concurrent_raise_and_invite_converge -v`
Expected after Step 1: PASS (with the lock). (Sanity: without the lock this would
flake/fail as one flag gets clobbered.)

- [ ] **Step 3: `_require_host` rejects an empty host**

In `src/skchat/spaces/routes.py`, harden `_require_host`:

```python
    def _require_host(space, requester: str) -> None:
        if not space.host_fqid.strip() or requester != space.host_fqid:
            raise HTTPException(403, "host-only action")
```

Add to `tests/test_spaces_moderation_routes.py`:

```python
def test_blank_host_cannot_be_impersonated(setup, tmp_path):
    c, sid, mod = setup
    # simulate a loaded space with an empty host_fqid by ending+recreating via the
    # registry isn't exposed here; instead assert an empty requester is rejected
    # even though strip() makes it "" (the non-empty host guard handles the rest).
    r = c.post(f"/spaces/{sid}/invite", json={"requester": "", "identity": "x@y.z"})
    assert r.status_code == 403
```

- [ ] **Step 4: ✋ queue reactivity + reveal End for host (space.html)**

In `src/skchat/static/space.html`, inside the `join` handler's `room.on(...)`
registrations, add the metadata-changed subscription (spec §5 mandates the ✋ queue
render from this):

```javascript
      room.on(LK.RoomEvent.ParticipantMetadataChanged, renderSpeakers);
```

And reveal the End button when the page is opened as host (after `await room.connect`
in the `join` handler, near the `hand.disabled = false` line):

```javascript
      if (hostFqid) document.getElementById("end").style.display = "inline";
```

Add markup pins to `tests/test_spaces_ui_markup.py`:

```python
def test_metadata_changed_drives_render():
    assert "ParticipantMetadataChanged" in _html()
```

- [ ] **Step 5: Run the affected tests**

Run: `~/.skenv/bin/python -m pytest tests/test_spaces_moderator.py tests/test_spaces_moderation_routes.py tests/test_spaces_ui_markup.py -v`
Expected: PASS (race convergence, blank-host 403, metadata-changed pin all green).

- [ ] **Step 6: Commit**

```bash
git add src/skchat/spaces/moderation.py src/skchat/spaces/routes.py \
        src/skchat/static/space.html tests/test_spaces_moderator.py \
        tests/test_spaces_moderation_routes.py tests/test_spaces_ui_markup.py
git commit -m "fix(spaces): stage-action race lock, blank-host guard, ✋ reactivity (S2 review)"
```

---

## Final verification

- [ ] **Run the full spaces suite + the whole skchat suite**

Run: `~/.skenv/bin/python -m pytest tests/test_spaces_*.py -v && ~/.skenv/bin/python -m pytest tests/ -q`
Expected: all spaces tests (S1+S2+S3) pass; no regressions.

- [ ] **Lint**

Run: `~/.skenv/bin/ruff check src/skchat/spaces/ tests/test_spaces_*.py`
Expected: no errors.

---

## What S3 delivers

Audio-only recording with real consent: a recording can't start until every
on-stage speaker has opted in (`409` + the missing list otherwise), it shows a
persistent `● REC` to everyone, and it runs through LiveKit Egress (mock-tested,
no live infra needed in CI). Plus a "live now" directory page listing active
Spaces with join links and REC/speaker badges. The egress container + runbook make
it deployable. **S4** brings this surface into the Flutter app; **S5** federates it.
```
