# Tier 2 — Server-side Lane Dispatcher + Persistence

> **For agentic workers:** REQUIRED SUB-SKILL: superpowers:subagent-driven-development. Repo: `skchat`, branch `feat/sk-spaces`. Run tests from **`~`** (avoid skmemory namespace collision): `cd ~ && ~/.skenv/bin/python -m pytest tests/<file> -q`.

**Goal:** Give the collaborative lanes (chat/whiteboard/watch/doc/term) a **server-side persistence + replay** substrate so a late joiner catches up and lane state survives a refresh — without putting the server in the LiveKit media path.

**Architecture:** Clients keep publishing lane envelopes over the LiveKit data channel (live, peer-to-peer via SFU) AND mirror each envelope to a new server endpoint. The server persists per `(space_id, lane)`. On join, the client fetches replay state. Two lane kinds: **snapshot** (`whiteboard` — latest full state wins) and **log** (`chat`,`watch`,`doc`,`term` — append + replay recent). `screen` is a media track, not a data lane — not persisted.

**Tech Stack:** Python 3.10+, SQLite (mirror `history.py`), FastAPI (extend `register_spaces_routes`), `pytest`, ruff line-99.

**Files:**
- Create `src/skchat/spaces/lanes.py` — `LaneStore` + `LaneDispatcher` + taxonomy.
- Modify `src/skchat/spaces/routes.py` — add `POST /spaces/{id}/lanes/event` + `GET /spaces/{id}/lanes/{lane}/state`.
- Modify `src/skchat/static/livekit.html` — mirror `publishLane` to server + catch-up on join.
- Tests: `tests/test_lane_store.py`, `tests/test_lane_dispatcher.py`, `tests/test_lane_routes.py`, `tests/test_lane_client_markup.py`.

**Grounding (existing):**
- `src/skchat/history.py` — `ChatHistory` SQLite pattern: `__init__(self, db_path=...)`, `sqlite3.connect`, `CREATE TABLE IF NOT EXISTS`, parametrized inserts. Mirror this (connection per call, `check_same_thread=False` not needed if opened per op).
- `src/skchat/spaces/routes.py` — `register_spaces_routes(app, *, registry=None, ...)`; routes are `@app.post("/spaces/{space_id}/...")` returning `JSONResponse`; body parsed via `await request.json()`. Add new routes inside this function.
- `src/skchat/static/livekit.html` — `publishLane(lanePayload)` at line 862 (`room.localParticipant.publishData(...)`); lane envelopes documented inline (chat/whiteboard/watch/doc/term schemas at lines 686/722/776/1133/1300).

---

## Task 1: `LaneStore` — SQLite persistence (snapshot + log)

**Files:** Create `src/skchat/spaces/lanes.py`. Test `tests/test_lane_store.py`.

- [ ] **Step 1: Failing test** — `tests/test_lane_store.py`:

```python
from skchat.spaces.lanes import LaneStore


def _store(tmp_path):
    return LaneStore(db_path=tmp_path / "lanes.db")


def test_log_lane_appends_and_replays_in_order(tmp_path):
    s = _store(tmp_path)
    s.append("space-1", "chat", {"lane": "chat", "from": "a", "text": "hi", "ts": 1})
    s.append("space-1", "chat", {"lane": "chat", "from": "b", "text": "yo", "ts": 2})
    out = s.replay("space-1", "chat")
    assert [e["text"] for e in out] == ["hi", "yo"]


def test_snapshot_lane_keeps_only_latest(tmp_path):
    s = _store(tmp_path)
    s.snapshot("space-1", "whiteboard", {"lane": "whiteboard", "elements": [1]})
    s.snapshot("space-1", "whiteboard", {"lane": "whiteboard", "elements": [1, 2]})
    out = s.replay("space-1", "whiteboard")
    assert out == [{"lane": "whiteboard", "elements": [1, 2]}]   # only latest


def test_replay_scoped_per_space_and_lane(tmp_path):
    s = _store(tmp_path)
    s.append("space-1", "chat", {"text": "one"})
    s.append("space-2", "chat", {"text": "two"})
    assert [e["text"] for e in s.replay("space-1", "chat")] == ["one"]


def test_log_replay_respects_limit(tmp_path):
    s = _store(tmp_path)
    for i in range(10):
        s.append("space-1", "watch", {"i": i})
    out = s.replay("space-1", "watch", limit=3)
    assert [e["i"] for e in out] == [7, 8, 9]   # most-recent 3, chronological


def test_empty_replay_is_empty_list(tmp_path):
    assert _store(tmp_path).replay("nope", "chat") == []
```

- [ ] **Step 2: Run → FAIL. Step 3: Implement** `src/skchat/spaces/lanes.py`:

```python
"""Server-side lane persistence + dispatch (Tier 2).

Lanes split into two kinds:
  - SNAPSHOT (whiteboard): the latest full-state envelope wins; replay returns it.
  - LOG (chat/watch/doc/term): append-only; replay returns recent envelopes in order.
The server is NOT in the LiveKit media path — clients mirror their data-channel
envelopes here for persistence + late-joiner catch-up."""

from __future__ import annotations

import json
import sqlite3
import time
from pathlib import Path

SNAPSHOT_LANES = frozenset({"whiteboard"})
LOG_LANES = frozenset({"chat", "watch", "doc", "term"})
KNOWN_LANES = SNAPSHOT_LANES | LOG_LANES


class LaneStore:
    def __init__(self, *, db_path: Path | str) -> None:
        self._db = str(db_path)
        with self._conn() as c:
            c.execute(
                """CREATE TABLE IF NOT EXISTS lane_events (
                       id INTEGER PRIMARY KEY AUTOINCREMENT,
                       space_id TEXT NOT NULL,
                       lane TEXT NOT NULL,
                       payload TEXT NOT NULL,
                       ts REAL NOT NULL,
                       kind TEXT NOT NULL)"""
            )
            c.execute(
                "CREATE INDEX IF NOT EXISTS idx_lane ON lane_events(space_id, lane, id)"
            )

    def _conn(self) -> sqlite3.Connection:
        return sqlite3.connect(self._db)

    def append(self, space_id: str, lane: str, payload: dict) -> None:
        with self._conn() as c:
            c.execute(
                "INSERT INTO lane_events(space_id, lane, payload, ts, kind) "
                "VALUES (?,?,?,?,?)",
                (space_id, lane, json.dumps(payload), time.time(), "log"),
            )

    def snapshot(self, space_id: str, lane: str, payload: dict) -> None:
        with self._conn() as c:
            c.execute(
                "DELETE FROM lane_events WHERE space_id=? AND lane=? AND kind='snapshot'",
                (space_id, lane),
            )
            c.execute(
                "INSERT INTO lane_events(space_id, lane, payload, ts, kind) "
                "VALUES (?,?,?,?,?)",
                (space_id, lane, json.dumps(payload), time.time(), "snapshot"),
            )

    def replay(self, space_id: str, lane: str, *, limit: int = 500) -> list[dict]:
        with self._conn() as c:
            rows = c.execute(
                "SELECT payload FROM lane_events WHERE space_id=? AND lane=? "
                "ORDER BY id DESC LIMIT ?",
                (space_id, lane, limit),
            ).fetchall()
        return [json.loads(r[0]) for r in reversed(rows)]
```

- [ ] **Step 4: Run → PASS. Step 5: Commit** `feat(lanes): LaneStore — snapshot + log persistence`.

---

## Task 2: `LaneDispatcher` — validate + route by lane kind

**Files:** Modify `src/skchat/spaces/lanes.py`. Test `tests/test_lane_dispatcher.py`.

- [ ] **Step 1: Failing test** — `tests/test_lane_dispatcher.py`:

```python
import pytest

from skchat.spaces.lanes import LaneDispatcher, LaneStore


def _disp(tmp_path):
    return LaneDispatcher(store=LaneStore(db_path=tmp_path / "l.db"))


def test_dispatch_log_lane_appends(tmp_path):
    d = _disp(tmp_path)
    d.dispatch("s1", {"lane": "chat", "text": "hi"})
    d.dispatch("s1", {"lane": "chat", "text": "yo"})
    assert [e["text"] for e in d.store.replay("s1", "chat")] == ["hi", "yo"]


def test_dispatch_snapshot_lane_replaces(tmp_path):
    d = _disp(tmp_path)
    d.dispatch("s1", {"lane": "whiteboard", "elements": [1]})
    d.dispatch("s1", {"lane": "whiteboard", "elements": [1, 2]})
    assert d.store.replay("s1", "whiteboard") == [{"lane": "whiteboard", "elements": [1, 2]}]


def test_unknown_lane_rejected(tmp_path):
    with pytest.raises(ValueError, match="lane"):
        _disp(tmp_path).dispatch("s1", {"lane": "bogus", "x": 1})


def test_missing_lane_field_rejected(tmp_path):
    with pytest.raises(ValueError, match="lane"):
        _disp(tmp_path).dispatch("s1", {"text": "no lane key"})


def test_non_dict_payload_rejected(tmp_path):
    with pytest.raises(ValueError):
        _disp(tmp_path).dispatch("s1", ["not", "a", "dict"])
```

- [ ] **Step 2: Run → FAIL. Step 3: Add to `lanes.py`:**

```python
class LaneDispatcher:
    """Validates an inbound lane envelope and routes it to the store by lane kind."""

    def __init__(self, *, store: LaneStore) -> None:
        self.store = store

    def dispatch(self, space_id: str, envelope: dict) -> None:
        if not isinstance(envelope, dict):
            raise ValueError("lane envelope must be an object")
        lane = envelope.get("lane")
        if lane not in KNOWN_LANES:
            raise ValueError(f"unknown or missing lane {lane!r}")
        if lane in SNAPSHOT_LANES:
            self.store.snapshot(space_id, lane, envelope)
        else:
            self.store.append(space_id, lane, envelope)
```

- [ ] **Step 4: Run → PASS. Step 5: Commit** `feat(lanes): LaneDispatcher — validate + route by lane kind`.

---

## Task 3: REST routes — persist + replay, wired into spaces router

**Files:** Modify `src/skchat/spaces/routes.py`. Test `tests/test_lane_routes.py`.

**Wiring:** In `register_spaces_routes`, construct one module-level `LaneDispatcher`
(db at `~/.skchat/lanes.db`, override via param for tests). Add a `lane_store`
optional kwarg to `register_spaces_routes(app, *, registry=None, lane_store=None, ...)`
defaulting to `LaneStore(db_path=Path.home()/".skchat"/"lanes.db")`. Build the
dispatcher from it.

- [ ] **Step 1: Failing test** — `tests/test_lane_routes.py`:

```python
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from skchat.spaces.lanes import LaneStore
from skchat.spaces.routes import register_spaces_routes


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("SKCHAT_LIVEKIT_API_KEY", "k")
    monkeypatch.setenv("SKCHAT_LIVEKIT_API_SECRET", "s")
    app = FastAPI()
    register_spaces_routes(app, lane_store=LaneStore(db_path=tmp_path / "l.db"))
    return TestClient(app)


def test_post_event_then_replay_log_lane(client):
    r = client.post("/spaces/s1/lanes/event", json={"lane": "chat", "text": "hello"})
    assert r.status_code == 200
    out = client.get("/spaces/s1/lanes/chat/state").json()
    assert out["events"][-1]["text"] == "hello"


def test_post_snapshot_then_replay_returns_latest(client):
    client.post("/spaces/s1/lanes/event", json={"lane": "whiteboard", "elements": [1]})
    client.post("/spaces/s1/lanes/event", json={"lane": "whiteboard", "elements": [1, 2]})
    out = client.get("/spaces/s1/lanes/whiteboard/state").json()
    assert out["events"] == [{"lane": "whiteboard", "elements": [1, 2]}]


def test_unknown_lane_is_400(client):
    r = client.post("/spaces/s1/lanes/event", json={"lane": "bogus"})
    assert r.status_code == 400


def test_replay_unknown_lane_is_400(client):
    assert client.get("/spaces/s1/lanes/bogus/state").status_code == 400


def test_empty_state_ok(client):
    out = client.get("/spaces/s1/lanes/chat/state").json()
    assert out["events"] == []
```

- [ ] **Step 2: Run → FAIL. Step 3:** In `routes.py`: import `from .lanes import LaneStore, LaneDispatcher, KNOWN_LANES` and `from pathlib import Path` (if absent). Change signature to add `lane_store: LaneStore | None = None`. Near the top of the function body:

```python
    _lane_store = lane_store or LaneStore(db_path=Path.home() / ".skchat" / "lanes.db")
    _lane_dispatch = LaneDispatcher(store=_lane_store)
```

Add the two routes (place beside the other `/spaces/...` routes):

```python
    @app.post("/spaces/{space_id}/lanes/event")
    async def lanes_event(space_id: str, request: Request) -> JSONResponse:
        body = await request.json()
        try:
            _lane_dispatch.dispatch(space_id, body)
        except ValueError as exc:
            return JSONResponse({"error": str(exc)}, status_code=400)
        return JSONResponse({"ok": True})

    @app.get("/spaces/{space_id}/lanes/{lane}/state")
    async def lanes_state(space_id: str, lane: str) -> JSONResponse:
        if lane not in KNOWN_LANES:
            return JSONResponse({"error": f"unknown lane {lane!r}"}, status_code=400)
        return JSONResponse({"events": _lane_store.replay(space_id, lane)})
```

- [ ] **Step 4: Run → PASS + existing spaces route tests still green** (`cd ~ && ~/.skenv/bin/python -m pytest tests/test_spaces_routes.py tests/test_lane_routes.py tests/test_fed_sfu_get_policy.py -q`). **Step 5: Commit** `feat(lanes): /spaces/{id}/lanes event+state routes`.

---

## Task 4: Client — mirror publishLane to server + catch-up on join

**Files:** Modify `src/skchat/static/livekit.html`. Test `tests/test_lane_client_markup.py`.

This lane is browser JS (no DOM test harness here), so the test asserts the **wiring
markup is present** (the mirror fetch + catch-up fetch exist and target the right
endpoints). The behavioral proof is the live two-browser test in Tier 5.

- [ ] **Step 1: Failing test** — `tests/test_lane_client_markup.py`:

```python
from pathlib import Path

HTML = Path("src/skchat/static/livekit.html").read_text()


def test_publishlane_mirrors_to_server_endpoint():
    # publishLane must POST the envelope to the persistence endpoint
    assert "/lanes/event" in HTML
    assert "mirrorLaneToServer" in HTML


def test_catch_up_fetches_lane_state_on_join():
    assert "/lanes/" in HTML and "/state" in HTML
    assert "catchUpLane" in HTML
```

- [ ] **Step 2: Run → FAIL. Step 3:** In `livekit.html`, just after `publishLane` (line ~862), add a mirror helper and call it from `publishLane`; add a `catchUpLane` helper invoked after room connect. Use the page's space id (already in scope as the room/space identifier — confirm the existing variable name, e.g. `SPACE_ID`/`spaceId`/`roomName`; reuse it, do NOT invent). Minimal JS:

```javascript
// Tier 2: mirror lane envelopes to the server for persistence + late-joiner replay.
async function mirrorLaneToServer(lanePayload) {
  try {
    await fetch(`/spaces/${encodeURIComponent(SPACE_ID)}/lanes/event`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(lanePayload),
    });
  } catch (e) { log('lane mirror failed:', e.message); }
}

// Tier 2: fetch persisted lane state on join so a late joiner catches up.
async function catchUpLane(lane, apply) {
  try {
    const r = await fetch(`/spaces/${encodeURIComponent(SPACE_ID)}/lanes/${lane}/state`);
    if (!r.ok) return;
    const { events } = await r.json();
    for (const ev of events) apply(ev);
  } catch (e) { log('lane catch-up failed:', e.message); }
}
```

In `publishLane`, after the `publishData(...)` call, add `mirrorLaneToServer(lanePayload);` (fire-and-forget; do not await — keep the data-channel path low-latency). Wire `catchUpLane('chat', renderChatMsg)` and `catchUpLane('whiteboard', applyWhiteboard)` into the existing post-connect handler (reuse the existing render/apply functions — find their real names in the file; the test only checks the helpers exist).

- [ ] **Step 4: Run → PASS** (`cd ~ && ~/.skenv/bin/python -m pytest tests/test_lane_client_markup.py -q`). **Step 5: Commit** `feat(lanes): client mirrors lanes to server + catches up on join`.

---

## Final verification

- [ ] **Lane suite + spaces regression:** `cd ~ && ~/.skenv/bin/python -m pytest tests/test_lane_*.py tests/test_spaces_*.py tests/test_fed_*.py -q` → all pass, no regressions.
- [ ] **Lint:** `~/.skenv/bin/ruff check src/skchat/spaces/lanes.py src/skchat/spaces/routes.py tests/test_lane_*.py` → clean.
- [ ] **Update QA matrix:** in `docs/qa/skworld-comms-verification-matrix.md`, move U15 (collaborative lanes) from "client-JS only" to "server persistence + replay: CI ✅; live two-browser: LIVE ⏳" and add the new test files to §1.

## What this delivers

The lanes gain a real server substrate: every lane envelope is persisted per
`(space_id, lane)`, a late joiner replays whiteboard state + recent chat/watch/doc/term
on connect, and the same endpoints are what the Flutter app (Tier 4) will call. The
server stays out of the media path — the data channel remains the live transport.
