"""LiveKitBus (spec §7) — MeshBus over a LiveKit room data channel. publishData
broadcasts to all participants (the reliable mesh bus). Lazy livekit import so the
module loads without a live room; live-tested in a running Space later."""

from __future__ import annotations

from skchat.glossa_mesh.bus import LeaveCb, MeshBus, ReceiveCb


class LiveKitBus(MeshBus):
    def __init__(
        self, *, member_id: str, room_url: str, token: str, topic: str = "skglossa.mesh"
    ) -> None:
        self.member_id = member_id
        self.room_url = room_url
        self.token = token
        self.topic = topic
        self._room = None
        self._cb: ReceiveCb | None = None
        self._leave_cb: LeaveCb | None = None
        self.running = False

    async def start(self) -> None:
        from livekit import rtc  # lazy

        self._room = rtc.Room()

        # verify announce/message src via capauth signature (anti-spoof) before
        # trusting it — live phase
        @self._room.on("data_received")
        def _on_data(packet) -> None:  # rtc.DataPacket
            if getattr(packet, "topic", self.topic) != self.topic:
                return
            src = getattr(getattr(packet, "participant", None), "identity", "")
            if self._cb is not None:
                self._cb(bytes(packet.data), src)

        @self._room.on("participant_disconnected")
        def _on_disconnected(participant) -> None:  # rtc.RemoteParticipant
            self._on_participant_disconnected(participant)

        await self._room.connect(self.room_url, self.token)
        self.running = True

    async def stop(self) -> None:
        if self._room is not None:
            await self._room.disconnect()
        self.running = False

    async def broadcast(self, data: bytes) -> None:
        if self._room is None:
            raise RuntimeError("bus not started")
        await self._room.local_participant.publish_data(data, reliable=True, topic=self.topic)

    def on_receive(self, cb: ReceiveCb) -> None:
        self._cb = cb

    def on_leave(self, cb: LeaveCb) -> None:
        self._leave_cb = cb

    def _on_participant_disconnected(self, participant) -> None:
        """Fire the leave callback with the departed member id. A 'participant_disconnected'
        event un-caps that peer (the Node wires this to forget_peer). Identity-less or
        unregistered events are a no-op. Test-driveable without a live room: call directly
        with a participant carrying an `identity`."""
        member_id = getattr(participant, "identity", "")
        if member_id and self._leave_cb is not None:
            self._leave_cb(member_id)
