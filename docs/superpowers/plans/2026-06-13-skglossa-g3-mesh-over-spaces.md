# SKGlossa G3 — Mesh over Spaces (data-channel mode)

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development. Repo: `skchat`, branch `feat/sk-spaces`. Run tests from repo root: `~/.skenv/bin/python -m pytest tests/ -q`.

**Goal:** The convergence — N agents mesh in SKGlossa over a LiveKit **data channel** in a Space (the reliable broadcast bus; no media-access collisions), with the weakest participant capping the density and humans able to read the English gloss of the whole mesh. All CI-tested with a `FakeBus`; the live LiveKit wiring is written but live-tested later.

**Architecture:** A new `src/skchat/glossa_mesh/` package. A `MeshBus` seam abstracts the broadcast medium (`FakeBus` for CI, `LiveKitBus` for the real Space data channel). A `GlossaMeshNode` wires `skcomms.glossa` to a bus: it **announces** its capability descriptor, computes a **group density level** = the minimum over all heard peers (weakest caps the room) via the existing pairwise `negotiate`, **sends** SKGlossa messages level-tagged on the wire, and **receives + glosses** inbound traffic. Frames carry a type byte (announce/message) and messages carry the encode level, so a receiver decodes correctly even if its peer-view differs transiently.

**Tech Stack:** Python 3.10+, reuses `skcomms.glossa` (now in skcomms `main`: `handshake`, `codec`, `gloss`, `message`, `codebook`, `macros`). `pytest` + `pytest-asyncio` (`asyncio_mode=auto`). Line 99, ruff.

**Spec:** `skcomms` `docs/superpowers/specs/2026-06-13-skglossa-design.md` §7 (mesh over Spaces, data-channel mode). **Depends on:** SKGlossa G1+G2 (in skcomms main) + the Spaces infra (this branch). Coord: `61502c3b`.

**Reused `skcomms.glossa` APIs (verify before use):**
- `from skcomms.glossa.handshake import CapabilityDescriptor, negotiate` → `negotiate(local, remote) -> Session(level, macros_enabled, lexicon_version, codebook_version)`
- `from skcomms.glossa import codec, gloss` → `codec.encode(msg, level, codebook)` / `codec.decode(raw, level, codebook)`; `gloss.to_english(msg)`; level consts `codec.L0_ENGLISH/L1_SCHEMA/L2_CODEBOOK`
- `from skcomms.glossa.message import Message`
- `from skcomms.glossa.codebook import default_codebook`

---

## Task 1: `MeshBus` seam + `FakeBus` broadcast medium

**Files:** Create `src/skchat/glossa_mesh/__init__.py`, `src/skchat/glossa_mesh/bus.py`. Test `tests/test_glossa_mesh_bus.py`.

- [ ] **Step 1:** Create `src/skchat/glossa_mesh/__init__.py`:

```python
"""SKGlossa mesh over Spaces (G3) — N agents mesh in SKGlossa over a LiveKit data
channel (the reliable broadcast bus). FakeBus drives CI; LiveKitBus is the real
Space transport. See skcomms .../specs/2026-06-13-skglossa-design.md §7.
"""

__all__ = []
```

- [ ] **Step 2: Failing test** — `tests/test_glossa_mesh_bus.py`:

```python
import asyncio

import pytest

from skchat.glossa_mesh.bus import FakeBus, FakeBusMedium


@pytest.mark.asyncio
async def test_broadcast_reaches_all_other_members():
    medium = FakeBusMedium()
    a, b, c = FakeBus("a", medium), FakeBus("b", medium), FakeBus("c", medium)
    got_b, got_c = [], []
    b.on_receive(lambda data, src: got_b.append((data, src)))
    c.on_receive(lambda data, src: got_c.append((data, src)))
    await a.start(); await b.start(); await c.start()
    await a.broadcast(b"hi-mesh")
    await asyncio.sleep(0.01)
    assert got_b == [(b"hi-mesh", "a")]
    assert got_c == [(b"hi-mesh", "a")]


@pytest.mark.asyncio
async def test_sender_does_not_receive_its_own_broadcast():
    medium = FakeBusMedium()
    a = FakeBus("a", medium)
    got = []
    a.on_receive(lambda data, src: got.append(data))
    await a.start()
    await a.broadcast(b"x")
    await asyncio.sleep(0.01)
    assert got == []
```

- [ ] **Step 3:** Implement `src/skchat/glossa_mesh/bus.py`:

```python
"""MeshBus seam + FakeBus (spec §7). A reliable broadcast medium: every started
member hears every other member's broadcast (the LiveKit data-channel model)."""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Callable

ReceiveCb = Callable[[bytes, str], None]  # (data, source_member_id)


class MeshBus(ABC):
    @abstractmethod
    async def start(self) -> None: ...

    @abstractmethod
    async def stop(self) -> None: ...

    @abstractmethod
    async def broadcast(self, data: bytes) -> None: ...

    @abstractmethod
    def on_receive(self, cb: ReceiveCb) -> None: ...


class FakeBusMedium:
    def __init__(self) -> None:
        self._members: dict[str, "FakeBus"] = {}

    def register(self, bus: "FakeBus") -> None:
        self._members[bus.member_id] = bus

    async def deliver(self, src: str, data: bytes) -> None:
        for mid, bus in self._members.items():
            if mid != src and bus.running:
                bus._inbound(data, src)


class FakeBus(MeshBus):
    def __init__(self, member_id: str, medium: FakeBusMedium) -> None:
        self.member_id = member_id
        self._medium = medium
        self._cb: ReceiveCb | None = None
        self.running = False
        medium.register(self)

    async def start(self) -> None:
        self.running = True

    async def stop(self) -> None:
        self.running = False

    async def broadcast(self, data: bytes) -> None:
        if not self.running:
            raise RuntimeError("bus not started")
        await self._medium.deliver(self.member_id, data)

    def on_receive(self, cb: ReceiveCb) -> None:
        self._cb = cb

    def _inbound(self, data: bytes, src: str) -> None:
        if self._cb is not None:
            self._cb(data, src)
```

- [ ] **Step 4: Run → PASS. Step 5: Commit** `feat(glossa-mesh): MeshBus seam + FakeBus broadcast medium`.

---

## Task 2: Mesh framing — announce vs message, level-tagged

**Files:** Create `src/skchat/glossa_mesh/protocol.py`. Test `tests/test_glossa_mesh_protocol.py`.

- [ ] **Step 1: Failing test** — `tests/test_glossa_mesh_protocol.py`:

```python
from skchat.glossa_mesh import protocol
from skcomms.glossa.handshake import CapabilityDescriptor


def test_announce_frame_roundtrip():
    d = CapabilityDescriptor(fqid="a@x.y", model_tier="large", max_level=2,
                             codebook_version="cb1", lexicon_version="lx1")
    raw = protocol.frame_announce(d)
    kind, payload = protocol.parse_frame(raw)
    assert kind == protocol.ANNOUNCE
    out = protocol.read_announce(payload)
    assert out == d


def test_message_frame_carries_level():
    raw = protocol.frame_message(level=2, body=b"\x01\x02")
    kind, payload = protocol.parse_frame(raw)
    assert kind == protocol.MESSAGE
    level, body = protocol.read_message(payload)
    assert level == 2
    assert body == b"\x01\x02"


def test_parse_rejects_empty():
    import pytest
    with pytest.raises(ValueError):
        protocol.parse_frame(b"")
```

- [ ] **Step 2: Run → FAIL. Step 3:** Implement `src/skchat/glossa_mesh/protocol.py`:

```python
"""Mesh wire framing (spec §7). type byte + payload. ANNOUNCE carries a JSON
capability descriptor; MESSAGE carries [level byte][codec bytes] so a receiver
decodes at the SENDER's level even if its own peer-view differs transiently.
"""

from __future__ import annotations

import json
from dataclasses import asdict

from skcomms.glossa.handshake import CapabilityDescriptor

ANNOUNCE = 0
MESSAGE = 1


def frame_announce(d: CapabilityDescriptor) -> bytes:
    return bytes([ANNOUNCE]) + json.dumps(asdict(d)).encode()


def frame_message(level: int, body: bytes) -> bytes:
    return bytes([MESSAGE, level & 0xFF]) + body


def parse_frame(raw: bytes) -> tuple[int, bytes]:
    if not raw:
        raise ValueError("empty frame")
    return raw[0], raw[1:]


def read_announce(payload: bytes) -> CapabilityDescriptor:
    return CapabilityDescriptor(**json.loads(payload.decode()))


def read_message(payload: bytes) -> tuple[int, bytes]:
    if not payload:
        raise ValueError("empty message payload")
    return payload[0], payload[1:]
```

- [ ] **Step 4: Run → PASS. Step 5: Commit** `feat(glossa-mesh): mesh framing (announce/message, level-tagged)`.

---

## Task 3: `GlossaMeshNode` — group-level negotiation + say/receive

**Files:** Create `src/skchat/glossa_mesh/node.py`. Test `tests/test_glossa_mesh_node.py`.

- [ ] **Step 1: Failing test** — `tests/test_glossa_mesh_node.py`:

```python
import asyncio

import pytest

from skcomms.glossa import codec
from skcomms.glossa.codebook import default_codebook
from skcomms.glossa.handshake import CapabilityDescriptor
from skcomms.glossa.message import Message
from skchat.glossa_mesh.bus import FakeBus, FakeBusMedium
from skchat.glossa_mesh.node import GlossaMeshNode


def _desc(fqid, max_level):
    return CapabilityDescriptor(fqid=fqid, model_tier="large", max_level=max_level,
                                codebook_version=default_codebook().version,
                                lexicon_version="")


def _node(fqid, medium, max_level=codec.L2_CODEBOOK):
    return GlossaMeshNode(descriptor=_desc(fqid, max_level),
                          bus=FakeBus(fqid, medium), codebook=default_codebook())


@pytest.mark.asyncio
async def test_two_nodes_announce_handshake_and_exchange():
    medium = FakeBusMedium()
    a, b = _node("a@x.y", medium), _node("b@x.y", medium)
    inbox = []
    b.on_message(lambda fqid, m: inbox.append((fqid, m)))
    await a.start(); await b.start()
    await a.announce(); await b.announce()
    await asyncio.sleep(0.02)
    assert a.group_level == codec.L2_CODEBOOK  # both strong → L2

    await a.say(Message(intent="coord.claim", args={"task": "abc"}))
    await asyncio.sleep(0.02)
    assert inbox == [("a@x.y", Message(intent="coord.claim", args={"task": "abc"}))]


@pytest.mark.asyncio
async def test_weakest_peer_caps_group_level():
    medium = FakeBusMedium()
    a = _node("a@x.y", medium, max_level=codec.L2_CODEBOOK)
    b = _node("b@x.y", medium, max_level=codec.L0_ENGLISH)  # weak
    c = _node("c@x.y", medium, max_level=codec.L2_CODEBOOK)
    inbox_b, inbox_c = [], []
    b.on_message(lambda f, m: inbox_b.append(m))
    c.on_message(lambda f, m: inbox_c.append(m))
    for n in (a, b, c):
        await n.start()
    for n in (a, b, c):
        await n.announce()
    await asyncio.sleep(0.03)
    assert a.group_level == codec.L0_ENGLISH  # capped to the weak peer
    await a.say(Message(intent="ack"))
    await asyncio.sleep(0.02)
    assert inbox_b == [Message(intent="ack")]   # the weak peer still decodes
    assert inbox_c == [Message(intent="ack")]
```

- [ ] **Step 2: Run → FAIL. Step 3:** Implement `src/skchat/glossa_mesh/node.py`:

```python
"""GlossaMeshNode (spec §7) — wires skcomms.glossa to a MeshBus for N-way meshing.

Announces its capability descriptor; computes a GROUP density level = the minimum
over all heard peers (the weakest participant caps the room) via the pairwise
`negotiate`; sends level-tagged messages; receives, decodes, and exposes the
English gloss (the audit view)."""

from __future__ import annotations

from typing import Callable

from skcomms.glossa import codec, gloss
from skcomms.glossa.codebook import Codebook
from skcomms.glossa.handshake import CapabilityDescriptor, negotiate
from skcomms.glossa.message import Message
from skchat.glossa_mesh import protocol
from skchat.glossa_mesh.bus import MeshBus

MessageCb = Callable[[str, Message], None]   # (sender_fqid, message)


class GlossaMeshNode:
    def __init__(self, *, descriptor: CapabilityDescriptor, bus: MeshBus,
                 codebook: Codebook) -> None:
        self.descriptor = descriptor
        self.bus = bus
        self.codebook = codebook
        self._peers: dict[str, CapabilityDescriptor] = {}
        self._on_message: MessageCb | None = None
        self.audit_log: list[str] = []
        bus.on_receive(self._on_frame)

    def on_message(self, cb: MessageCb) -> None:
        self._on_message = cb

    async def start(self) -> None:
        await self.bus.start()

    async def stop(self) -> None:
        await self.bus.stop()

    @property
    def group_level(self) -> int:
        """Weakest-peer-caps: min over the pairwise negotiated level with each
        known peer. With no peers, fall back to our own max."""
        if not self._peers:
            return self.descriptor.max_level
        return min(negotiate(self.descriptor, p).level for p in self._peers.values())

    async def announce(self) -> None:
        await self.bus.broadcast(protocol.frame_announce(self.descriptor))

    async def say(self, m: Message) -> None:
        level = self.group_level
        body = codec.encode(m, level, self.codebook)
        self.audit_log.append(f"[tx L{level}] {gloss.to_english(m)}")
        await self.bus.broadcast(protocol.frame_message(level, body))

    def _on_frame(self, data: bytes, src: str) -> None:
        try:
            kind, payload = protocol.parse_frame(data)
        except ValueError:
            return
        if kind == protocol.ANNOUNCE:
            try:
                self._peers[src] = protocol.read_announce(payload)
            except Exception:
                return
        elif kind == protocol.MESSAGE:
            try:
                level, body = protocol.read_message(payload)
                m = codec.decode(body, level, self.codebook)
            except Exception:
                return
            self.audit_log.append(f"[rx L{level}] {src}: {gloss.to_english(m)}")
            if self._on_message is not None:
                self._on_message(src, m)
```

- [ ] **Step 4: Run → PASS** (2 tests). **Step 5: Commit** `feat(glossa-mesh): GlossaMeshNode — group-level mesh negotiation + say/receive`.

---

## Task 4: Multi-agent mesh integration

**Files:** Test `tests/test_glossa_mesh_integration.py`.

- [ ] **Step 1: Failing test** — the headline proof: N agents mesh, one broadcasts, all receive + the audit log holds the English gloss:

```python
import asyncio

import pytest

from skcomms.glossa import codec
from skcomms.glossa.codebook import default_codebook
from skcomms.glossa.handshake import CapabilityDescriptor
from skcomms.glossa.message import Message
from skchat.glossa_mesh.bus import FakeBus, FakeBusMedium
from skchat.glossa_mesh.node import GlossaMeshNode


def _node(fqid, medium):
    d = CapabilityDescriptor(fqid=fqid, model_tier="large", max_level=codec.L2_CODEBOOK,
                             codebook_version=default_codebook().version, lexicon_version="")
    return GlossaMeshNode(descriptor=d, bus=FakeBus(fqid, medium),
                          codebook=default_codebook())


@pytest.mark.asyncio
async def test_ten_agent_mesh_broadcast_reaches_all_and_glosses():
    medium = FakeBusMedium()
    fqids = [f"agent{i}@x.y" for i in range(10)]
    nodes = [_node(f, medium) for f in fqids]
    inboxes = {f: [] for f in fqids}
    for f, n in zip(fqids, nodes):
        n.on_message(lambda src, m, _f=f: inboxes[_f].append(m))
    for n in nodes:
        await n.start()
    for n in nodes:
        await n.announce()
    await asyncio.sleep(0.05)

    speaker = nodes[0]
    msg = Message(intent="status.report", args={"oof": 42}, text="nominal")
    await speaker.say(msg)
    await asyncio.sleep(0.05)

    # every OTHER agent received it; the speaker did not receive its own
    for f in fqids[1:]:
        assert inboxes[f] == [msg]
    assert inboxes[fqids[0]] == []
    # the speaker's audit log holds the human-readable gloss
    assert any("status.report" in line for line in speaker.audit_log)
    # a listener's audit log glosses the inbound traffic to English
    assert any("status.report" in line for line in nodes[1].audit_log)
```

- [ ] **Step 2: Run → FAIL (or PASS if Tasks 1–3 suffice). Step 3:** If it fails, fix the minimal cause in `node.py` (it should pass with Tasks 1–3 in place — this task is the integration assertion). **Step 4: Run → PASS. Step 5: Commit** `test(glossa-mesh): 10-agent mesh broadcast + gloss integration`.

---

## Task 5: `LiveKitBus` (real Space data channel; live test deferred)

**Files:** Create `src/skchat/glossa_mesh/livekit_bus.py`. Test `tests/test_glossa_mesh_livekit.py`.

Implements `MeshBus` over a LiveKit room's data channel (`publishData` broadcast +
`data_received`). Written against the SDK; the live test (a running Space + ≥2
agents) is deferred — this task only verifies the class shape + that it imports
without a live room.

- [ ] **Step 1: Failing test** — `tests/test_glossa_mesh_livekit.py`:

```python
from skchat.glossa_mesh.bus import MeshBus
from skchat.glossa_mesh.livekit_bus import LiveKitBus


def test_is_a_meshbus_and_constructs_without_a_live_room():
    assert issubclass(LiveKitBus, MeshBus)
    bus = LiveKitBus(member_id="lumina@chef.skworld",
                     room_url="wss://noroc2027.tail204f0c.ts.net:8443",
                     token="x", topic="skglossa.mesh")
    assert bus.member_id == "lumina@chef.skworld"
    assert bus.topic == "skglossa.mesh"
```

- [ ] **Step 2: Run → FAIL. Step 3:** Implement `src/skchat/glossa_mesh/livekit_bus.py` — lazy `livekit` import inside `start()`; broadcast via `room.local_participant.publish_data(data, reliable=True, topic=...)`; receive via the room's `data_received` callback. (Verify the exact `livekit` rtc API names against the installed package when the live test runs in a later phase; keep the import lazy so the module loads without a room.)

```python
"""LiveKitBus (spec §7) — MeshBus over a LiveKit room data channel. publishData
broadcasts to all participants (the reliable mesh bus). Lazy livekit import so the
module loads without a live room; live-tested in a running Space later."""

from __future__ import annotations

from skchat.glossa_mesh.bus import MeshBus, ReceiveCb


class LiveKitBus(MeshBus):
    def __init__(self, *, member_id: str, room_url: str, token: str,
                 topic: str = "skglossa.mesh") -> None:
        self.member_id = member_id
        self.room_url = room_url
        self.token = token
        self.topic = topic
        self._room = None
        self._cb: ReceiveCb | None = None
        self.running = False

    async def start(self) -> None:
        from livekit import rtc  # lazy
        self._room = rtc.Room()

        @self._room.on("data_received")
        def _on_data(packet) -> None:  # rtc.DataPacket
            if getattr(packet, "topic", self.topic) != self.topic:
                return
            src = getattr(getattr(packet, "participant", None), "identity", "")
            if self._cb is not None:
                self._cb(bytes(packet.data), src)

        await self._room.connect(self.room_url, self.token)
        self.running = True

    async def stop(self) -> None:
        if self._room is not None:
            await self._room.disconnect()
        self.running = False

    async def broadcast(self, data: bytes) -> None:
        from livekit import rtc  # lazy
        if self._room is None:
            raise RuntimeError("bus not started")
        await self._room.local_participant.publish_data(
            data, reliable=True, topic=self.topic)

    def on_receive(self, cb: ReceiveCb) -> None:
        self._cb = cb
```

> **NOTE for implementer:** the exact `livekit` rtc API (`Room`, `publish_data`
> signature, the `data_received` event payload shape) must be verified against the
> installed `livekit` package when the live two-agent Space test is run (a later
> phase). For G3 the test only requires the class subclasses `MeshBus`, constructs
> without a room, and the `livekit` import stays inside `start()`. If `livekit` is
> not importable at module load, the lazy import guarantees the module + test pass.

- [ ] **Step 4: Run → PASS. Step 5: Commit** `feat(glossa-mesh): LiveKitBus (real data channel; live test deferred)`.

---

## Final verification

- [ ] **Full mesh suite + whole skchat suite:**
Run: `~/.skenv/bin/python -m pytest tests/test_glossa_mesh_*.py -v && ~/.skenv/bin/python -m pytest tests/ -q`
Expected: all mesh tests pass; no regressions in the existing skchat suite (incl. spaces).

- [ ] **Lint:** `~/.skenv/bin/ruff check src/skchat/glossa_mesh/ tests/test_glossa_mesh_*.py` → no errors.

## What G3 delivers

The convergence: N agents mesh in SKGlossa over a broadcast bus, the weakest
participant capping the room's density, every message level-tagged so it decodes
correctly across transient peer-view differences, and a per-node **audit log that
glosses the whole mesh to English** — the human oversight seat. CI-proven with a
10-agent FakeBus mesh; `LiveKitBus` carries it onto a real Space's data channel
(live two-agent test is the next phase, needing a running Space). This is the
moment Spaces + the language + the agents become one running mesh.
