"""Recorder — audio-only room-composite Egress for a Space (spec §8).

Egress is injectable for tests. Audio-only OGG file output; the consent gate
(consent.can_record) is enforced by the caller (routes) before start.
"""

from __future__ import annotations


def _http_url(ws_url: str) -> str:
    return ws_url.replace("ws://", "http://").replace("wss://", "https://")


class Recorder:
    def __init__(self, ws_url: str, api_key: str, api_secret: str, *, _egress=None) -> None:
        self._ws_url = ws_url
        self._key = api_key
        self._secret = api_secret
        self._eg = _egress
        self._client = None  # the LiveKitAPI instance (built lazily) to aclose()

    def _egress(self):
        if self._eg is not None:
            return self._eg
        from livekit import api

        self._client = api.LiveKitAPI(_http_url(self._ws_url), self._key, self._secret)
        self._eg = self._client.egress
        return self._eg

    async def aclose(self) -> None:
        """Close the cached LiveKit client, if one was built/injected. Safe to
        call when never built (no-op). Callers should invoke this on shutdown."""
        client = self._client if self._client is not None else self._eg
        if client is not None and hasattr(client, "aclose"):
            await client.aclose()

    async def start(self, room: str, filepath: str) -> str:
        """Start an audio-only room-composite recording; return the egress id."""
        from livekit import api

        req = api.RoomCompositeEgressRequest(
            room_name=room,
            audio_only=True,
            file_outputs=[
                api.EncodedFileOutput(file_type=api.EncodedFileType.OGG, filepath=filepath)
            ],
        )
        info = await self._egress().start_room_composite_egress(req)
        return info.egress_id

    async def stop(self, egress_id: str) -> None:
        from livekit import api

        await self._egress().stop_egress(api.StopEgressRequest(egress_id=egress_id))
