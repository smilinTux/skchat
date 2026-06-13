"""MeshBus seam + FakeBus (spec §7). A reliable broadcast medium: every started
member hears every other member's broadcast (the LiveKit data-channel model)."""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Callable

ReceiveCb = Callable[[bytes, str], None]  # (data, source_member_id)
LeaveCb = Callable[[str], None]  # (departed_member_id)


class MeshBus(ABC):
    @abstractmethod
    async def start(self) -> None: ...

    @abstractmethod
    async def stop(self) -> None: ...

    @abstractmethod
    async def broadcast(self, data: bytes) -> None: ...

    @abstractmethod
    def on_receive(self, cb: ReceiveCb) -> None: ...

    def on_leave(self, cb: LeaveCb) -> None:
        """Register a callback fired with a member id when that member leaves the
        mesh. No-op-able seam: subclasses that have no leave signal may ignore it
        (the LiveKit wiring to 'participant_disconnected' is the live phase)."""


class FakeBusMedium:
    def __init__(self) -> None:
        self._members: dict[str, "FakeBus"] = {}

    def register(self, bus: "FakeBus") -> None:
        self._members[bus.member_id] = bus

    async def deliver(self, src: str, data: bytes) -> None:
        for mid, bus in self._members.items():
            if mid != src and bus.running:
                bus._inbound(data, src)

    async def simulate_leave(self, member_id: str) -> None:
        """Test hook: fire every OTHER member's on_leave callbacks with the
        departed member id (the LiveKit 'participant_disconnected' analogue)."""
        for mid, bus in self._members.items():
            if mid != member_id:
                bus._leave(member_id)


class FakeBus(MeshBus):
    def __init__(self, member_id: str, medium: FakeBusMedium) -> None:
        self.member_id = member_id
        self._medium = medium
        self._cb: ReceiveCb | None = None
        self._leave_cb: LeaveCb | None = None
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

    def on_leave(self, cb: LeaveCb) -> None:
        self._leave_cb = cb

    def _inbound(self, data: bytes, src: str) -> None:
        if self._cb is not None:
            self._cb(data, src)

    def _leave(self, member_id: str) -> None:
        if self._leave_cb is not None:
            self._leave_cb(member_id)
