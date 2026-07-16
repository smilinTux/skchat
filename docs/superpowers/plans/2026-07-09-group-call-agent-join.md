# Group-Call Agent Auto-Join Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** When a human starts/rings a group video call, an agent member (Lumina/Opus/etc.) automatically joins the SAME LiveKit room the human is in — so "only one person in the room" stops happening. v1 = the agent is verifiably present in the room (fixes the reported bug); full-duplex STT→LLM→TTS voice is explicitly deferred (see "v1 vs deferred" below).

## Root cause (from the group-call bug-hunt, `wave-a/04-bughunt-calls.md`)

The signaling/membership/token-mint plumbing is real and already works:
`daemon_proxy_groupcall.py` (`derive_group_room`, `mint_member_token`, `ring_members`)
+ `POST /v1/groups/{id}/call/{start,join}` (`daemon_proxy.py:834-907`) mint a
room-scoped token for whoever calls them and fan out a signed `CALL_INVITE`
(`call_session.build_invite_body`, decorated with `group_id`) to every other
member over the **skcomms signed mailbox** (`skcomms.mailbox.send_message` /
`read_inbox` — a Syncthing-replicated per-agent inbox directory, `scaffold()["inbox"]`,
**completely separate** from `ChatTransport.poll_inbox()` / `SKComms.receive()`,
which is what `ChatDaemon`'s poll loop actually drains).

Nothing ever reads that mailbox on the agent side and turns an invite into a
`room.connect()`. `daemon.py` has zero `CALL_INVITE` handling. The only code
in the repo that actually calls `rtc.Room().connect()` + publishes a track is
`scripts/lumina-join-call.py` — a **manual** script that targets a **static**
default room (`SKCHAT_LIVEKIT_DEFAULT_ROOM`) via the **unscoped** `/livekit/token`
route, bypassing the group membership gate entirely. So today, for an agent to
appear in a group call, a human has to hand-run that script with the right
`gcall-` room hash — nothing wires the ring to it.

## Design decisions (resolves the 6 open questions)

1. **Trigger:** the agent's daemon polls its own signed mailbox
   (`skcomms.mailbox.read_inbox(agent=...)`) for verified `CALL_INVITE`
   envelopes that carry a `group_id` — the same envelope shape the browser's
   `GET /call/incoming` reads, reusing Wave B's from-fqid spoof check
   (`call_routes.py:191-203`). This is a **new, separate poll** from
   `transport.poll_inbox()` (different store), added as its own cheap step in
   the existing daemon loop (same pattern as the reaper/presence/watchdog
   ticks) — NOT a chat message, so it never touches `AdvocacyEngine`/
   `GroupResponder`.
2. **Room derivation:** `daemon_proxy_groupcall.derive_group_room(group_id)` —
   the same deterministic hash the human's `/call/start` used. The daemon
   never trusts the invite body's `room` field as authoritative; it re-derives
   from `group_id` (mirrors how `_prepare_call` re-derives the 1:1 room
   server-side rather than trusting client input).
3. **Token:** `daemon_proxy_groupcall.mint_member_token(group, identity, room)`
   — the membership-gated mint (raises `PermissionError` for non-members),
   loading the group via `daemon_proxy_groups.load_group(group_id)`. Never the
   raw, unscoped `/livekit/token` route `lumina-join-call.py` uses today.
4. **Media (v1 scope):** connect to the room and stay present
   (`rtc.Room().connect()`), so the roster shows the agent — that alone fixes
   the reported symptom. An optional short spoken greeting (reusing the
   existing Piper/VoxCPM TTS path, same as `lumina-join-call.py`'s
   `synthesize()`/`push_pcm()`) is a documented, env-gated nice-to-have, OFF by
   default in this plan's tasks. Full STT→LLM→TTS duplex (routing a subscribed
   remote track through `voice.py`) is **deferred** — see below.
5. **Off the poll thread + idempotent:** each join runs on its **own**
   dedicated thread (`call_agent_join.start_join`, one thread per active
   room), started from — but not executed on — the daemon's main poll loop.
   It deliberately does **NOT** go through the existing chat-reply
   `_genqueue`/`_genworker` from the async-generation plan: a call session
   lasts minutes-to-hours, and that queue exists specifically to keep ordered,
   bounded (~10s) chat replies flowing — routing a call join through it would
   stall every subsequent chat reply for the life of the call. Idempotency is
   a `dict[room] -> JoinHandle` + a bounded, ts-pruned seen-nonce map, both
   checked/reserved before minting a token or spawning a thread.
6. **Lifecycle:** the join session tracks "last time another participant was
   present" via `participant_connected`/`remote_participants`; it leaves
   (`room.disconnect()`) after an idle timeout with zero other participants
   (`SKCHAT_CALL_IDLE_TIMEOUT`, default 90s) or a hard session cap
   (`SKCHAT_CALL_MAX_SESSION`, default 2h) — whichever comes first — and
   always on daemon shutdown (`ChatDaemon.stop()` requests + joins every
   active session's thread).

## v1 vs deferred

- **v1 (this plan):** invite detection, room re-derivation, membership-gated
  token mint, `room.connect()` presence, idle/max-session leave, clean
  shutdown. This is what fixes "only one person in the room."
  Feature-gated: only runs when LiveKit creds are configured
  (`livekit_routes._have_creds()`); a missing/uninstalled `livekit` client
  package degrades to a logged skip, never a crash (matches every other
  optional subsystem in `daemon.py`).
- **Deferred (separate follow-up plan):** subscribing to remote audio tracks
  and piping them through `voice.py`'s STT→LLM→TTS loop for real two-way
  conversation in a group call; a `CALL_END`/hangup signal (today there is
  none — `call_session.py` defines `CALL_DECLINE_SUBJECT` but nothing sends
  it — so lifecycle is timeout-based, not event-based); recording/Egress
  (already scoped as "Phase 6" in `daemon_proxy_groupcall.py`'s
  `RECORDING_SEAM`); persisting the seen-nonce set across daemon restarts.

**Architecture:** Two new, independently-testable pieces plumbed together in
`daemon.py`'s existing poll loop:
(a) `daemon_proxy_groupcall.fetch_new_group_invites()` — a pure-ish, injectable
function that turns the raw signed mailbox into a filtered list of "new group
call invites for me" (valid signature, has `group_id`, from_fqid not spoofed,
fresh, not already seen).
(b) `call_agent_join.start_join()` — spawns a dedicated thread that runs an
`asyncio` LiveKit session (connect → idle-watch → disconnect), returning a
`JoinHandle` the daemon can idempotency-check and shut down.
`ChatDaemon` wires them: a new per-cycle step scans for invites and, for each
new one, mints a token (module (c), reusing existing `daemon_proxy_groupcall`
plumbing) and dispatches a join — never blocking the poll loop or the chat
`_genqueue`.

**Tech Stack:** Python 3.10+, `livekit`/`livekit-api` (already installed in
`~/.skenv`, soft-imported — not a hard `pyproject.toml` dependency, same
posture as `livekit_routes.py`), `asyncio`, `threading`, pytest. Package
`skchat`, editable-installed in `~/.skenv`.

## Global Constraints

- Run all commands and tests from `~/` (NOT the repo dir): `cd ~ && ~/.skenv/bin/python -m pytest <path> -q`.
- Line length 99 (ruff E501 ignored); target Python 3.10+.
- `skcomms.mailbox.read_inbox`/`send_message` (the signed, Syncthing-replicated
  per-agent mailbox) is a **different store** from `ChatTransport.poll_inbox()`
  (`self._skcomms.receive()`). Do not conflate them — the new invite scan is
  an ADDITIONAL step in the loop, not a change to message routing.
- Never trust the invite body's `room`/`livekit_url` as authoritative for
  *authorization* — `room` is re-derived from `group_id` via
  `derive_group_room`; the agent always connects to its OWN configured
  `livekit_routes.LIVEKIT_URL` (tailnet), not whatever URL the ringing human's
  request happened to compute (which may be a public/Funnel URL — irrelevant
  and wrong for an agent that lives on the tailnet).
- Membership is the actual authorization gate (`daemon_proxy_groupcall.is_member`
  / `mint_member_token`'s `PermissionError`), not fqid string matching — do
  not add a stricter `to_fqid == self fqid` check on top; `read_inbox(agent=...)`
  already scopes to that agent's own inbox directory, and this codebase's
  established convention for agent-identity comparison is bare-handle matching
  (`group_responder._sender_handle`/`_is_self`), because of `GroupChat.get_member`'s
  own local-part-only matching (bughunt finding #3, NOT being fixed here).
- Soft-import `livekit`/`livekit.rtc` at module top of `call_agent_join.py`
  guarded by `try/except ImportError` (mirrors `daemon.py`'s
  `try: from skcomms import SKComms except ImportError: SKComms = None`) so
  tests can `monkeypatch.setattr("skchat.call_agent_join.rtc", FakeRtc)`.
- A join session runs on its OWN thread per room — never on `self._genqueue`
  (chat-reply queue) and never blocking the main poll loop.
- The daemon is editable-installed; a live rollout needs
  `systemctl --user restart skchat-daemon` (and per-agent units) — this plan
  only requires pytest, not a live restart.

---

### Task 1: `fetch_new_group_invites()` — pure, injectable invite scanner

Turns the raw signed mailbox into "new group-call invites for me": valid
signature, `CALL_INVITE` subject, has `group_id` (1:1 calls — no `group_id` —
are out of scope for this poller and skipped), from_fqid not spoofed (reuses
Wave B's `call_routes.py:191-203` check), within a freshness window, and not
already in the caller-supplied `already_seen` nonce set.

**Files:**
- Modify: `src/skchat/daemon_proxy_groupcall.py` — add `fetch_new_group_invites()`.
- Test: `tests/test_daemon_proxy_groupcall.py` (extend).

**Interfaces:**
- Produces: `fetch_new_group_invites(agent: str, *, already_seen: set[str] | None = None, max_age_seconds: float = 90.0, read_inbox=None, now=None) -> list[dict]`. Each dict is the parsed `CALL_INVITE` body (`type`, `from_fqid`, `to_fqid`, `room`, `transport`, `livekit_url`, `topic`, `ts`, `nonce`, `group_id`) — the same shape `call_session.parse_invite_body` + the `group_id` decoration from `_default_send_group_invite` produce. Sorted oldest-first (ts ascending) so a caller processing in order dispatches joins in ring order.
- Consumes: `skcomms.mailbox.read_inbox` (injectable, defaults to the real one), `call_session.CALL_INVITE_SUBJECT`/`parse_invite_body`.

- [ ] **Step 1: Write the failing tests**

Add to `tests/test_daemon_proxy_groupcall.py`:

```python
from types import SimpleNamespace
from skchat.call_session import build_invite_body


def _invite_env(*, from_fqid, to_fqid, room, group_id, ts=None, valid=True, spoof_from=None):
    import json, time
    body = build_invite_body(
        from_fqid=from_fqid, to_fqid=to_fqid, room=room, livekit_url="ws://x:7880",
    )
    data = json.loads(body)
    data["group_id"] = group_id
    if ts is not None:
        data["ts"] = ts
    body = json.dumps(data)
    env = SimpleNamespace(
        subject="CALL_INVITE",
        from_fqid=spoof_from or from_fqid,
        to_fqid=to_fqid,
        body=body,
    )
    return env, SimpleNamespace(valid=valid)


def test_fetch_new_group_invites_filters_correctly():
    now = 1_000_000.0
    good, stale, unsigned, non_group, spoofed = (
        _invite_env(from_fqid="chef@x.y", to_fqid="lumina@x.y", room="gcall-aaa",
                    group_id="g1", ts=now - 5),
        _invite_env(from_fqid="chef@x.y", to_fqid="lumina@x.y", room="gcall-bbb",
                    group_id="g1", ts=now - 500),
        _invite_env(from_fqid="chef@x.y", to_fqid="lumina@x.y", room="gcall-ccc",
                    group_id="g1", ts=now - 1, valid=False),
        _invite_env(from_fqid="chef@x.y", to_fqid="lumina@x.y", room="call-ddd",
                    group_id=None, ts=now - 1),
        _invite_env(from_fqid="chef@x.y", to_fqid="lumina@x.y", room="gcall-eee",
                    group_id="g1", ts=now - 1, spoof_from="attacker@evil.io"),
    )
    inbox = [good, stale, unsigned, non_group, spoofed]

    invites = GC.fetch_new_group_invites(
        "lumina", read_inbox=lambda agent: inbox, now=lambda: now,
    )
    rooms = [i["room"] for i in invites]
    assert rooms == ["gcall-aaa"]  # only the valid, fresh, group, unspoofed one


def test_fetch_new_group_invites_dedupes_seen_nonces():
    now = 1_000_000.0
    env, verify = _invite_env(
        from_fqid="chef@x.y", to_fqid="lumina@x.y", room="gcall-aaa", group_id="g1", ts=now - 1,
    )
    import json
    nonce = json.loads(env.body)["nonce"]

    first = GC.fetch_new_group_invites("lumina", read_inbox=lambda a: [(env, verify)], now=lambda: now)
    assert len(first) == 1

    second = GC.fetch_new_group_invites(
        "lumina", already_seen={nonce}, read_inbox=lambda a: [(env, verify)], now=lambda: now,
    )
    assert second == []
```

- [ ] **Step 2: Run it to verify it fails**

Run: `cd ~ && ~/.skenv/bin/python -m pytest tests/test_daemon_proxy_groupcall.py -q -k fetch_new_group_invites`
Expected: FAIL — `AttributeError: module 'skchat.daemon_proxy_groupcall' has no attribute 'fetch_new_group_invites'`.

- [ ] **Step 3: Implement `fetch_new_group_invites`**

In `src/skchat/daemon_proxy_groupcall.py`, add near the bottom (after `_default_send_group_invite`):

```python
def _default_read_inbox(agent: str):
    from skcomms.mailbox import read_inbox

    return read_inbox(agent=agent)


def fetch_new_group_invites(
    agent: str,
    *,
    already_seen: Optional[set] = None,
    max_age_seconds: float = 90.0,
    read_inbox=None,
    now=None,
) -> list[dict]:
    """Return new, verified group-call CALL_INVITEs addressed to *agent*.

    Reads *agent*'s own signed mailbox (``skcomms.mailbox.read_inbox`` —
    NOT ``ChatTransport.poll_inbox()``, a separate store). Filters to
    invites that are: signature-valid, ``CALL_INVITE`` subject, carry a
    ``group_id`` (1:1 calls have none and are out of scope here), whose
    body's self-reported ``from_fqid`` matches the cryptographically
    verified envelope sender (Wave B's spoof check, mirrored from
    ``call_routes.call_incoming``), fresher than *max_age_seconds*, and not
    already in *already_seen* (caller-owned nonce dedup — this function is
    stateless and never mutates *already_seen*).

    Returns invites sorted oldest-first (ts ascending).
    """
    import time as _time

    from .call_session import CALL_INVITE_SUBJECT, parse_invite_body

    reader = read_inbox or _default_read_inbox
    clock = now or _time.time
    seen = already_seen or set()
    cutoff = clock() - max_age_seconds

    out: list[dict] = []
    for env, verify in reader(agent):
        if not getattr(verify, "valid", False):
            continue
        if getattr(env, "subject", None) != CALL_INVITE_SUBJECT:
            continue
        try:
            inv = parse_invite_body(env.body)
        except ValueError:
            continue
        if not inv.get("group_id"):
            continue  # 1:1 call — not this poller's concern
        env_from = getattr(env, "from_fqid", None)
        if inv.get("from_fqid") != env_from:
            logger.warning(
                "dropping group CALL_INVITE with spoofed from_fqid: body=%r envelope=%r",
                inv.get("from_fqid"), env_from,
            )
            continue
        if inv.get("ts", 0) < cutoff:
            continue
        nonce = inv.get("nonce")
        if not nonce or nonce in seen:
            continue
        out.append(inv)

    out.sort(key=lambda i: i.get("ts", 0))
    return out
```

- [ ] **Step 4: Run — passes**

Run: `cd ~ && ~/.skenv/bin/python -m pytest tests/test_daemon_proxy_groupcall.py -q`
Expected: PASS (all existing + 2 new tests).

- [ ] **Step 5: Commit**

```bash
cd ~/clawd/skcapstone-repos/skchat
git add src/skchat/daemon_proxy_groupcall.py tests/test_daemon_proxy_groupcall.py
git commit -m "feat(groupcall): fetch_new_group_invites — verified, deduped invite scanner

Pure, injectable scan of the agent's own signed skcomms mailbox for fresh
group-call CALL_INVITEs: signature-valid, group_id present, from_fqid
spoof-checked (mirrors Wave B's call_routes fix), freshness-windowed,
nonce-deduped. No I/O side effects — read_inbox/now are injected. Prep for
wiring the daemon's auto-join poll.

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_01X2Uu2UH6c7rhdrbNMwsz2L"
```

---

### Task 2: `call_agent_join.py` — spawn/manage a LiveKit join session on its own thread

A dedicated module (mirrors `lumina-join-call.py`'s connect/publish logic, made
importable + testable) that connects to a room, watches for idle/max-session,
and disconnects — all on a thread it owns, never the caller's thread.

**Files:**
- Create: `src/skchat/call_agent_join.py`.
- Test: `tests/test_call_agent_join.py` (new).

**Interfaces:**
- Produces: `JoinHandle` (dataclass: `room: str`, `.request_stop(timeout=5.0)`),
  `start_join(*, room, token, livekit_url, identity, display_name, on_exit=None, idle_timeout=IDLE_TIMEOUT_SECONDS, max_session=MAX_SESSION_SECONDS) -> JoinHandle`.
  `on_exit(room: str)` is called (best-effort) exactly once when the session
  thread ends, regardless of why (idle timeout, max session, `request_stop`,
  or an exception) — this is the daemon's hook to clear its active-room
  bookkeeping.
- Module-level soft dep: `rtc` (`from livekit import rtc`, `None` on
  `ImportError`) — monkeypatchable as `skchat.call_agent_join.rtc`.

- [ ] **Step 1: Write the failing tests**

Create `tests/test_call_agent_join.py`:

```python
"""Tests for call_agent_join — spawn/manage a LiveKit group-call join session."""

from __future__ import annotations

import time

import pytest

from skchat import call_agent_join as CJ


class _FakeLocalParticipant:
    identity = "lumina"


class _FakeRoom:
    """Minimal fake standing in for livekit.rtc.Room for unit tests."""

    instances: list["_FakeRoom"] = []

    def __init__(self):
        self.remote_participants: dict[str, object] = {}
        self.connected_with: tuple | None = None
        self.disconnected = False
        self.local_participant = _FakeLocalParticipant()
        self._handlers: dict[str, list] = {}
        _FakeRoom.instances.append(self)

    def on(self, event):
        def _register(fn):
            self._handlers.setdefault(event, []).append(fn)
            return fn
        return _register

    async def connect(self, url, token):
        self.connected_with = (url, token)

    async def disconnect(self):
        self.disconnected = True


class _FakeRtc:
    Room = _FakeRoom


@pytest.fixture(autouse=True)
def _fake_rtc(monkeypatch):
    _FakeRoom.instances = []
    monkeypatch.setattr(CJ, "rtc", _FakeRtc)


def test_start_join_connects_with_room_and_token():
    handle = CJ.start_join(
        room="gcall-aaa", token="tok123", livekit_url="ws://x:7880",
        identity="capauth:lumina@skworld.io", display_name="Lumina",
        idle_timeout=0.1, max_session=5,
    )
    deadline = time.time() + 2
    while time.time() < deadline and not _FakeRoom.instances:
        time.sleep(0.01)
    assert _FakeRoom.instances, "no Room was constructed"
    fake = _FakeRoom.instances[0]
    deadline = time.time() + 2
    while time.time() < deadline and fake.connected_with is None:
        time.sleep(0.01)
    assert fake.connected_with == ("ws://x:7880", "tok123")
    handle.request_stop(timeout=3)
    assert fake.disconnected


def test_idle_timeout_leaves_the_room_automatically():
    on_exit_calls = []
    handle = CJ.start_join(
        room="gcall-idle", token="t", livekit_url="ws://x:7880",
        identity="lumina", display_name="Lumina",
        on_exit=lambda room: on_exit_calls.append(room),
        idle_timeout=0.2, max_session=5,
    )
    handle.thread.join(timeout=3)
    assert not handle.thread.is_alive()
    assert on_exit_calls == ["gcall-idle"]


def test_request_stop_is_idempotent_and_calls_on_exit_once():
    on_exit_calls = []
    handle = CJ.start_join(
        room="gcall-stop", token="t", livekit_url="ws://x:7880",
        identity="lumina", display_name="Lumina",
        on_exit=lambda room: on_exit_calls.append(room),
        idle_timeout=5, max_session=5,
    )
    time.sleep(0.05)
    handle.request_stop(timeout=3)
    handle.request_stop(timeout=3)  # must not raise / must not double-call on_exit
    assert on_exit_calls == ["gcall-stop"]
```

- [ ] **Step 2: Run it to verify it fails**

Run: `cd ~ && ~/.skenv/bin/python -m pytest tests/test_call_agent_join.py -q`
Expected: FAIL — `ModuleNotFoundError: No module named 'skchat.call_agent_join'`.

- [ ] **Step 3: Implement `call_agent_join.py`**

Create `src/skchat/call_agent_join.py`:

```python
"""Spawn/manage an agent's LiveKit group-call join session.

v1 scope: connect to the room and stay present — this alone fixes "only one
person in the room" (the agent shows up in the roster). Full STT->LLM->TTS
duplex is a documented follow-up (see the plan this module implements,
2026-07-09-group-call-agent-join.md): the seam is `_session`'s
`participant_connected`/track-subscription hooks, where a future iteration
would route a subscribed remote audio track through `skchat.voice`'s
STT->LLM->TTS loop instead of only idle-watching.

Runs entirely on ITS OWN thread (one per active room) — never the caller's
thread, and never the chat-reply generation queue (`ChatDaemon._genqueue`):
a call session lasts minutes to hours, and that queue exists to keep ordered,
bounded (~10s) chat replies flowing.
"""

from __future__ import annotations

import asyncio
import logging
import os
import threading
import time
from dataclasses import dataclass, field
from typing import Callable, Optional

try:
    from livekit import rtc
except ImportError:  # pragma: no cover - optional dep, soft failure
    rtc = None  # type: ignore[assignment]

logger = logging.getLogger("skchat.call_agent_join")

# Leave an otherwise-empty room after this many idle seconds (no other
# participants present). Env-overridable per agent/deployment.
IDLE_TIMEOUT_SECONDS = float(os.getenv("SKCHAT_CALL_IDLE_TIMEOUT", "90"))
# Hard backstop regardless of activity — never stay in a room forever.
MAX_SESSION_SECONDS = float(os.getenv("SKCHAT_CALL_MAX_SESSION", "7200"))  # 2h
_POLL_SECONDS = 2.0


@dataclass
class JoinHandle:
    """Handle to a running (or finished) group-call join session."""

    room: str
    thread: threading.Thread
    _stop_event: threading.Event = field(default_factory=threading.Event, repr=False)

    def request_stop(self, timeout: Optional[float] = 5.0) -> None:
        """Ask the session to leave the room; idempotent, blocks until joined."""
        self._stop_event.set()
        if self.thread.is_alive():
            self.thread.join(timeout=timeout)


def start_join(
    *,
    room: str,
    token: str,
    livekit_url: str,
    identity: str,
    display_name: str,
    on_exit: Optional[Callable[[str], None]] = None,
    idle_timeout: float = IDLE_TIMEOUT_SECONDS,
    max_session: float = MAX_SESSION_SECONDS,
) -> JoinHandle:
    """Spawn a dedicated thread that joins *room* and stays present.

    Returns immediately with a :class:`JoinHandle` — never blocks the caller
    on the actual `rtc.Room().connect()` I/O.
    """
    stop_event = threading.Event()

    def _runner() -> None:
        try:
            asyncio.run(
                _session(
                    room=room,
                    token=token,
                    livekit_url=livekit_url,
                    identity=identity,
                    display_name=display_name,
                    stop_event=stop_event,
                    idle_timeout=idle_timeout,
                    max_session=max_session,
                )
            )
        except Exception:
            logger.exception("group-call join session for room=%s failed", room)
        finally:
            if on_exit is not None:
                try:
                    on_exit(room)
                except Exception:
                    logger.exception("on_exit callback failed for room=%s", room)

    thread = threading.Thread(target=_runner, daemon=True, name=f"skchat-callin-{room}")
    handle = JoinHandle(room=room, thread=thread)
    handle._stop_event = stop_event
    thread.start()
    return handle


async def _session(
    *,
    room: str,
    token: str,
    livekit_url: str,
    identity: str,
    display_name: str,
    stop_event: threading.Event,
    idle_timeout: float,
    max_session: float,
) -> None:
    if rtc is None:
        raise ImportError("livekit client SDK not installed (pip install livekit)")

    lk_room = rtc.Room()
    last_nonempty = time.monotonic()

    @lk_room.on("participant_connected")
    def _on_connect(_p) -> None:  # noqa: ANN001
        nonlocal last_nonempty
        last_nonempty = time.monotonic()

    await lk_room.connect(livekit_url, token)
    logger.info("agent %s (%s) joined group call room=%s", identity, display_name, room)
    started = time.monotonic()
    try:
        while not stop_event.is_set():
            await asyncio.sleep(_POLL_SECONDS)
            now = time.monotonic()
            if len(lk_room.remote_participants) > 0:
                last_nonempty = now
            if now - last_nonempty > idle_timeout:
                logger.info(
                    "room=%s idle >%ss with no other participants — leaving",
                    room, idle_timeout,
                )
                break
            if now - started > max_session:
                logger.info("room=%s hit max session cap %ss — leaving", room, max_session)
                break
    finally:
        await lk_room.disconnect()
        logger.info("agent %s left group call room=%s", identity, room)
```

Note: `_POLL_SECONDS` is intentionally coarser than a chat poll — this loop
only exists to notice idleness/timeout, not to drive media.

- [ ] **Step 4: Run — passes**

Run: `cd ~ && ~/.skenv/bin/python -m pytest tests/test_call_agent_join.py -q`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
cd ~/clawd/skcapstone-repos/skchat
git add src/skchat/call_agent_join.py tests/test_call_agent_join.py
git commit -m "feat(call): call_agent_join — dedicated-thread LiveKit join/leave session

start_join() spawns one thread per room that connects via rtc.Room(), stays
present, and leaves on idle timeout / max-session cap / explicit stop —
never the caller's thread, never the chat-reply genqueue. Soft-imports
livekit.rtc (monkeypatchable as call_agent_join.rtc for tests). v1 is
presence-only; full duplex voice is a documented follow-up.

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_01X2Uu2UH6c7rhdrbNMwsz2L"
```

---

### Task 3: Wire invite-polling + auto-join into `ChatDaemon`

Adds a new per-cycle step to the existing poll loop (same pattern as the
reaper/presence/watchdog ticks): scan for new group-call invites, and for each
new one not already active, mint a token and dispatch a join — idempotently,
never blocking the poll loop or `_genqueue`. Cleans up on `stop()`.

**Files:**
- Modify: `src/skchat/daemon.py` — `__init__` (new attributes), `start()`
  (new closure + call site in the main loop), `stop()` (leave active rooms).
- Test: `tests/test_daemon_call_join.py` (new).

**Interfaces:**
- Consumes: `daemon_proxy_groupcall.fetch_new_group_invites`/`is_member`/
  `mint_member_token` (Task 1 + existing), `daemon_proxy_groups.load_group`
  (existing), `call_agent_join.start_join` (Task 2), `livekit_routes._have_creds`/
  `LIVEKIT_URL` (existing).
- Produces: `ChatDaemon._active_call_rooms: dict[str, JoinHandle]`,
  `ChatDaemon._call_seen_nonces: dict[str, float]` (nonce -> first-seen ts,
  pruned each cycle), `ChatDaemon._call_rooms_lock: threading.Lock`, a
  `start()`-local closure `_poll_group_call_invites()` called once per poll
  cycle right after the message-receive block.

- [ ] **Step 1: Write the failing test — new invite triggers exactly one join, repeat invite doesn't double-join**

Create `tests/test_daemon_call_join.py`:

```python
"""Tests for the daemon's group-call auto-join wiring (Task 3)."""

from __future__ import annotations

import threading
import time
from unittest.mock import MagicMock

import pytest

from skchat.daemon import ChatDaemon


def _invite(room="gcall-aaa", group_id="g1", nonce="n1", ts=None):
    return {
        "type": "CALL_INVITE",
        "from_fqid": "chef@skworld.io",
        "to_fqid": "lumina@skworld.io",
        "room": room,
        "transport": "livekit",
        "livekit_url": "ws://public-or-whatever:7880",
        "topic": "",
        "ts": ts if ts is not None else time.time(),
        "nonce": nonce,
        "group_id": group_id,
    }


@pytest.fixture
def daemon():
    d = ChatDaemon(interval=0.01, quiet=True)
    return d


def test_new_invite_dispatches_exactly_one_join(daemon, monkeypatch):
    import skchat.daemon_proxy_groupcall as GC
    import skchat.daemon_proxy_groups as G
    import skchat.call_agent_join as CJ
    import skchat.livekit_routes as LK

    monkeypatch.setattr(LK, "_have_creds", lambda: True)
    monkeypatch.setattr(LK, "LIVEKIT_URL", "ws://tailnet:7880")

    invites = [_invite()]
    monkeypatch.setattr(GC, "fetch_new_group_invites", lambda agent, **kw: invites)

    fake_group = MagicMock()
    monkeypatch.setattr(G, "load_group", lambda gid: fake_group)
    monkeypatch.setattr(GC, "is_member", lambda group, identity: True)
    monkeypatch.setattr(GC, "mint_member_token", lambda group, identity, room, **kw: "tok-xyz")

    joins = []

    def fake_start_join(*, room, token, livekit_url, identity, display_name, on_exit=None, **kw):
        joins.append((room, token, livekit_url, identity))
        t = threading.Thread(target=lambda: None)
        t.start()
        t.join()
        return CJ.JoinHandle(room=room, thread=t)

    monkeypatch.setattr(CJ, "start_join", fake_start_join)

    daemon._poll_group_call_invites("lumina", "capauth:lumina@skworld.io")

    assert len(joins) == 1
    assert joins[0][0] == "gcall-aaa"
    assert joins[0][2] == "ws://tailnet:7880"  # agent's OWN configured URL, not the invite's
    assert "gcall-aaa" in daemon._active_call_rooms

    # Second poll cycle: same nonce comes back (mailbox file still there) —
    # must NOT dispatch a second join.
    invites[:] = [_invite()]  # identical nonce
    daemon._poll_group_call_invites("lumina", "capauth:lumina@skworld.io")
    assert len(joins) == 1


def test_non_member_invite_is_skipped(daemon, monkeypatch):
    import skchat.daemon_proxy_groupcall as GC
    import skchat.daemon_proxy_groups as G
    import skchat.call_agent_join as CJ
    import skchat.livekit_routes as LK

    monkeypatch.setattr(LK, "_have_creds", lambda: True)
    monkeypatch.setattr(GC, "fetch_new_group_invites", lambda agent, **kw: [_invite(nonce="n2")])
    monkeypatch.setattr(G, "load_group", lambda gid: MagicMock())
    monkeypatch.setattr(GC, "is_member", lambda group, identity: False)  # not a member

    called = []
    monkeypatch.setattr(CJ, "start_join", lambda **kw: called.append(kw))

    daemon._poll_group_call_invites("lumina", "capauth:lumina@skworld.io")
    assert called == []
    assert daemon._active_call_rooms == {}


def test_stop_leaves_all_active_rooms(daemon):
    handle = MagicMock()
    daemon._active_call_rooms["gcall-aaa"] = handle
    daemon.stop()
    handle.request_stop.assert_called_once()
```

- [ ] **Step 2: Run it to verify it fails**

Run: `cd ~ && ~/.skenv/bin/python -m pytest tests/test_daemon_call_join.py -q`
Expected: FAIL — `AttributeError: 'ChatDaemon' object has no attribute '_poll_group_call_invites'` (and `_active_call_rooms`).

- [ ] **Step 3: Add `__init__` state**

In `ChatDaemon.__init__`, after the existing `self._send_lock = threading.Lock()` line, add:

```python
        # Group-call auto-join (Task 3, 2026-07-09-group-call-agent-join.md):
        # active/in-flight sessions keyed by room name, a lock guarding both
        # dicts, and a bounded (ts-pruned) seen-nonce map for idempotent
        # dispatch across poll cycles.
        self._active_call_rooms: dict = {}
        self._call_seen_nonces: dict = {}
        self._call_rooms_lock = threading.Lock()
```

- [ ] **Step 4: Add the `_poll_group_call_invites` method**

Add as a regular method on `ChatDaemon` (not a `start()`-local closure, since
it takes `agent`/`identity` as explicit args and only touches `self.*` state —
this keeps it directly unit-testable without booting `start()`):

```python
    def _poll_group_call_invites(self, agent: str, identity: str) -> None:
        """Scan for new group-call CALL_INVITEs and auto-join, idempotently.

        Cheap I/O only (reads the agent's own signed mailbox) — safe to run
        inline on the poll thread every cycle. The actual room join runs on
        its own dedicated thread (call_agent_join.start_join), never here and
        never on self._genqueue.
        """
        try:
            from .livekit_routes import _have_creds

            if not _have_creds():
                return
            from . import call_agent_join as CJ
            from . import daemon_proxy_groupcall as GC
            from .daemon_proxy_groups import load_group
            from .livekit_routes import LIVEKIT_URL

            now = time.time()
            with self._call_rooms_lock:
                self._call_seen_nonces = {
                    n: t for n, t in self._call_seen_nonces.items() if now - t < 300
                }
                seen = set(self._call_seen_nonces)

            invites = GC.fetch_new_group_invites(agent, already_seen=seen)
        except Exception as exc:
            logger.debug("group-call invite poll skipped: %s", exc)
            return

        for inv in invites:
            nonce = inv.get("nonce")
            room = inv.get("room")
            group_id = inv.get("group_id")
            with self._call_rooms_lock:
                if nonce:
                    self._call_seen_nonces[nonce] = now
                if not room or not group_id or room in self._active_call_rooms:
                    continue  # missing fields, or already joined/joining this room
                self._active_call_rooms[room] = None  # reserve the slot

            try:
                group = load_group(group_id)
                if group is None or not GC.is_member(group, identity):
                    self._log(
                        f"Group-call invite for {group_id}: not a known member — skipping",
                        "debug",
                    )
                    with self._call_rooms_lock:
                        self._active_call_rooms.pop(room, None)
                    continue

                token = GC.mint_member_token(group, identity, room)
                handle = CJ.start_join(
                    room=room,
                    token=token,
                    livekit_url=LIVEKIT_URL,  # agent's OWN tailnet URL — never the invite's
                    identity=identity,
                    display_name=agent.capitalize(),
                    on_exit=self._on_call_room_exit,
                )
                with self._call_rooms_lock:
                    self._active_call_rooms[room] = handle
                self._log(f"Auto-joined group call room={room} (group={group_id})")
            except Exception as exc:
                logger.warning("group-call auto-join failed for room=%s: %s", room, exc)
                with self._call_rooms_lock:
                    self._active_call_rooms.pop(room, None)

    def _on_call_room_exit(self, room: str) -> None:
        """Callback from call_agent_join when a session ends — clear bookkeeping."""
        with self._call_rooms_lock:
            self._active_call_rooms.pop(room, None)
```

`_log(..., "debug")` — check `_log`'s `level` param accepts arbitrary
`logging` level names via `getattr(logger, level, logger.info)`; `"debug"` is
valid.

- [ ] **Step 5: Call it from the main poll loop**

In `start()`, right after the existing message-receive `try/except` block
(after the `else:` "No new messages" branch closes, before `# --- Reap
expired ephemeral messages`), add:

```python
                # --- Group-call auto-join: scan for new CALL_INVITEs (cheap;
                # actual join runs on its own thread — see call_agent_join) ---
                try:
                    self._poll_group_call_invites(
                        os.environ.get("SKAGENT", "lumina"), identity
                    )
                except Exception as exc:
                    logger.warning("Group-call invite poll error: %s", exc)
```

- [ ] **Step 6: Leave all active rooms in `stop()`**

In `ChatDaemon.stop()`, after the existing genworker drain/join block, add:

```python
        # Leave every active/in-flight group-call room cleanly.
        with self._call_rooms_lock:
            handles = [h for h in self._active_call_rooms.values() if h is not None]
        for handle in handles:
            try:
                handle.request_stop(timeout=5)
            except Exception as exc:
                logger.warning("failed to leave call room %s cleanly: %s", handle.room, exc)
```

- [ ] **Step 7: Run the new test — passes**

Run: `cd ~ && ~/.skenv/bin/python -m pytest tests/test_daemon_call_join.py -q`
Expected: PASS.

- [ ] **Step 8: Full regression — daemon + groupcall + call-join suites green**

Run:
```bash
cd ~ && ~/.skenv/bin/python -m pytest \
  tests/test_daemon.py tests/test_daemon_group.py tests/test_daemon_async.py \
  tests/test_daemon_proxy_groupcall.py tests/test_call_agent_join.py \
  tests/test_daemon_call_join.py tests/test_call_routes.py -q
```
Expected: PASS.

- [ ] **Step 9: Commit**

```bash
cd ~/clawd/skcapstone-repos/skchat
git add src/skchat/daemon.py tests/test_daemon_call_join.py
git commit -m "feat(daemon): auto-join group calls on a verified CALL_INVITE

Wires fetch_new_group_invites() + call_agent_join.start_join() into the
poll loop's per-cycle ticks: a signed, membership-gated group CALL_INVITE
now makes the agent actually connect to the LiveKit room a human started,
instead of requiring a manual lumina-join-call.py run against a guessed
room hash. Idempotent (room-keyed active-session dict + pruned seen-nonce
map), never blocks the poll loop or the chat _genqueue (own thread per
room), clean leave on idle/max-session/shutdown. Fixes 'only one person in
the room' for agent members of a group call.

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_01X2Uu2UH6c7rhdrbNMwsz2L"
```

---

## Notes for the executor

- **Live smoke test (optional, not required by the plan):**
  `systemctl --user restart skchat-daemon` (and any per-agent units), then
  from the webui/Flutter app start a group call in a group that includes an
  agent member (`skchat group add-member <gid> lumina`) and confirm the
  daemon log (`journalctl --user -u skchat-daemon -f` or `~/.skchat/daemon.log`)
  shows `Auto-joined group call room=...` and the LiveKit room roster shows
  2+ participants.
- **Known reliance on an existing bug, not fixed here:** `GroupChat.get_member()`
  matches on bare local-part only (bughunt finding #3) — this plan's
  `GC.is_member(group, identity)` check inherits that looseness. If/when #3 is
  fixed to be FQID-scheme-aware, double-check what identity string form group
  membership is actually stored in for agent members and make sure `identity`
  (passed here as the daemon's own `capauth:<agent>@skworld.io` wire URI)
  still resolves correctly — it may need switching to the agent's `.fqid`
  form instead.
- **Biggest open risk:** there is no live-fire verification in this plan
  (deliberately — it's a plan, not an implementation) that the skcomms
  Syncthing-replicated mailbox actually delivers a group `CALL_INVITE` into an
  agent's own inbox directory promptly in the real multi-host deployment
  (vs. the same-host/loopback case the unit tests exercise) — if Syncthing
  replication lag is more than a few seconds, or if `ring_members`' fqid
  resolution (`identity_bridge.resolve_peer_name`) produces an address that
  doesn't route to the agent's actual inbox path, the invite never arrives at
  all and this whole poller sees nothing. Recommend a live two-host smoke
  test before considering the feature done, watching both the sender's
  `peer_inbox_path` (from `send_message`'s return value) and the receiving
  agent's own `~/.skcomms/<agent>/inbox/` (or wherever `scaffold()` resolves)
  to confirm the file actually lands.
- **Second risk:** `livekit` (the client SDK, for `rtc.Room()`) is installed
  in `~/.skenv` today but is NOT a declared `pyproject.toml` dependency (only
  `livekit-api`, the JWT-mint server SDK, is soft-imported elsewhere) — a
  future `pip install --upgrade` of the editable install without also
  installing `livekit` would silently degrade this feature to "logs an
  ImportError, never joins" rather than failing loudly. Consider adding an
  optional-dependencies extra (e.g. `[project.optional-dependencies].call-agent
  = ["livekit>=1.0"]`) in a follow-up if this becomes an operational
  surprise.
