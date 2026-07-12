"""HLS egress endpoints — /livekit/hls/{start,stop,status} + /hls proxy.

TV-casting Sprint 1 (coord cd5d81de): the webui starts/stops a RoomComposite
HLS egress for a room and hands back a plain HLS URL a TV / cast receiver can
play. These tests mock the LiveKit egress client (no live SFU) and assert the
endpoint shapes + the returned ``hls_url``.

The start/stop/status endpoints reuse the exact /livekit/token gate
(loopback/tailnet OR operator token), so we drive them from a loopback client
host (mirrors ``tests/test_livekit_token_gate.py``). The egress seam is
``livekit_routes._livekit_api_client`` — patching it swaps the real SFU client
for a fake, while the request-object building (SegmentedFileOutput /
RoomCompositeEgressRequest) still runs for real against the installed SDK.
"""

from __future__ import annotations

import pytest

fastapi = pytest.importorskip("fastapi")
pytest.importorskip("livekit.api")

from fastapi import FastAPI  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from skchat import livekit_routes  # noqa: E402
from skchat.livekit_routes import register_livekit_routes  # noqa: E402


# ── Fake egress client ───────────────────────────────────────────────────────
class _FakeInfo:
    def __init__(self, egress_id: str, status: int, room: str = "") -> None:
        self.egress_id = egress_id
        self.status = status
        self.room_name = room


class _FakeEgress:
    def __init__(self) -> None:
        self.started = []
        self.stopped = []
        self.listed = 0

    async def start_room_composite_egress(self, req):
        self.started.append(req)
        # status 1 == EGRESS_ACTIVE
        return _FakeInfo("EG_fake123", 1, req.room_name)

    async def stop_egress(self, req):
        self.stopped.append(req.egress_id)
        # status 2 == EGRESS_ENDING
        return _FakeInfo(req.egress_id, 2)

    async def list_egress(self, req):  # noqa: ARG002
        self.listed += 1
        return type("_Resp", (), {"items": [_FakeInfo("EG_fake123", 1, "lumina-and-chef")]})()


class _FakeLK:
    def __init__(self) -> None:
        self.egress = _FakeEgress()
        self.closed = False

    async def aclose(self) -> None:
        self.closed = True


@pytest.fixture
def wired(monkeypatch):
    """App + loopback client + credentialed + faked egress client."""
    monkeypatch.setattr(livekit_routes, "LIVEKIT_API_KEY", "k", raising=False)
    monkeypatch.setattr(livekit_routes, "LIVEKIT_API_SECRET", "s", raising=False)
    monkeypatch.delenv("SKCHAT_GUEST_OPERATOR_TOKEN", raising=False)
    monkeypatch.setenv("SKCHAT_FUNNEL_PUBLIC_URL", "https://noroc2027.tail204f0c.ts.net")
    monkeypatch.delenv("SKCHAT_HLS_PUBLIC_BASE", raising=False)

    fake = _FakeLK()
    monkeypatch.setattr(livekit_routes, "_livekit_api_client", lambda: fake)

    app = FastAPI()
    register_livekit_routes(app)
    client = TestClient(app, client=("127.0.0.1", 12345))
    return client, fake


def test_hls_start_returns_egress_id_and_url(wired):
    client, fake = wired
    r = client.post("/livekit/hls/start", json={"room": "lumina-and-chef"})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["ok"] is True
    assert body["egress_id"] == "EG_fake123"
    assert body["status"] == "EGRESS_ACTIVE"
    assert body["room"] == "lumina-and-chef"
    assert body["hls_url"] == (
        "https://noroc2027.tail204f0c.ts.net/hls/lumina-and-chef/index.m3u8"
    )
    assert body["playlist"] == "/hls/lumina-and-chef/index.m3u8"
    # The real SegmentedFileOutput (HLS) request was built and handed to the SFU.
    assert len(fake.egress.started) == 1
    req = fake.egress.started[0]
    assert req.room_name == "lumina-and-chef"
    assert req.segment_outputs[0].playlist_name.endswith("/lumina-and-chef/index.m3u8")
    assert fake.closed is True


def test_hls_start_sanitizes_room(wired):
    client, fake = wired
    r = client.post("/livekit/hls/start", json={"room": "../etc/passwd room!!"})
    assert r.status_code == 200, r.text
    room = r.json()["room"]
    assert "/" not in room and ".." not in room and " " not in room
    assert r.json()["hls_url"].endswith(f"/hls/{room}/index.m3u8")


def test_hls_start_defaults_room(wired):
    client, _fake = wired
    r = client.post("/livekit/hls/start", json={})
    assert r.status_code == 200, r.text
    # DEFAULT_ROOM is lumina-and-chef.
    assert r.json()["room"] == "lumina-and-chef"


def test_hls_stop_requires_egress_id(wired):
    client, _fake = wired
    r = client.post("/livekit/hls/stop", json={})
    assert r.status_code == 400, r.text


def test_hls_stop_stops_egress(wired):
    client, fake = wired
    r = client.post("/livekit/hls/stop", json={"egress_id": "EG_fake123"})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["ok"] is True
    assert body["egress_id"] == "EG_fake123"
    assert body["status"] == "EGRESS_ENDING"
    assert fake.egress.stopped == ["EG_fake123"]
    assert fake.closed is True


def test_hls_status_lists_active(wired):
    client, fake = wired
    r = client.get("/livekit/hls/status")
    assert r.status_code == 200, r.text
    egresses = r.json()["egresses"]
    assert egresses == [
        {"egress_id": "EG_fake123", "room": "lumina-and-chef", "status": "EGRESS_ACTIVE"}
    ]
    assert fake.egress.listed == 1


def test_hls_control_gate_rejects_public(monkeypatch):
    """A public (non-tailnet) caller cannot start an egress."""
    monkeypatch.setattr(livekit_routes, "LIVEKIT_API_KEY", "k", raising=False)
    monkeypatch.setattr(livekit_routes, "LIVEKIT_API_SECRET", "s", raising=False)
    monkeypatch.delenv("SKCHAT_GUEST_OPERATOR_TOKEN", raising=False)
    app = FastAPI()
    register_livekit_routes(app)
    client = TestClient(app, client=("8.8.8.8", 12345))
    r = client.post("/livekit/hls/start", json={"room": "x"})
    assert r.status_code in (401, 403), r.text


def test_hls_media_route_public_and_typed(monkeypatch):
    """The /hls media proxy is UNGATED, sets HLS content types + CORS, and
    proxies bytes from the .41 origin (mocked here)."""
    app = FastAPI()
    register_livekit_routes(app)

    class _FakeResp:
        status = 200

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def read(self):
            return b"#EXTM3U\n#EXT-X-VERSION:3\n"

    class _FakeSession:
        def __init__(self, *a, **k):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        def get(self, url):
            _FakeSession.last_url = url
            return _FakeResp()

    import aiohttp

    monkeypatch.setattr(aiohttp, "ClientSession", _FakeSession)
    monkeypatch.setenv("SKCHAT_HLS_ORIGIN", "http://100.86.156.5:8099")

    # A public client host is fine — this route is intentionally ungated.
    client = TestClient(app, client=("8.8.8.8", 12345))
    r = client.get("/hls/lumina-and-chef/index.m3u8")
    assert r.status_code == 200, r.text
    assert r.headers["content-type"].startswith("application/vnd.apple.mpegurl")
    assert r.headers["access-control-allow-origin"] == "*"
    assert r.content.startswith(b"#EXTM3U")
    assert _FakeSession.last_url == "http://100.86.156.5:8099/lumina-and-chef/index.m3u8"


def test_hls_media_rejects_traversal(monkeypatch):
    app = FastAPI()
    register_livekit_routes(app)
    client = TestClient(app, client=("127.0.0.1", 12345))
    # A traversal-y segment name is rejected before any fetch.
    r = client.get("/hls/room/..%2f..%2fetc%2fpasswd")
    assert r.status_code == 404, r.text


def test_hls_url_helper_uses_public_base(monkeypatch):
    monkeypatch.delenv("SKCHAT_HLS_PUBLIC_BASE", raising=False)
    monkeypatch.setenv("SKCHAT_FUNNEL_PUBLIC_URL", "https://example.ts.net")
    assert livekit_routes._hls_url("myroom") == "https://example.ts.net/hls/myroom/index.m3u8"
    # Explicit public base wins over the funnel fallback.
    monkeypatch.setenv("SKCHAT_HLS_PUBLIC_BASE", "https://cast.example.net/")
    assert livekit_routes._hls_url("r") == "https://cast.example.net/hls/r/index.m3u8"


def test_livekit_api_url_ignores_funnel_path(monkeypatch):
    """A Funnel /path ws URL (no port) must NOT be used as the API endpoint."""
    monkeypatch.delenv("SKCHAT_LIVEKIT_API_URL", raising=False)
    monkeypatch.setenv("SKCHAT_LIVEKIT_URL", "wss://noroc2027.tail204f0c.ts.net/livekit-ws")
    assert livekit_routes._livekit_api_url() == "http://100.108.59.57:7880"
    # A bare host:port ws URL derives a matching http API base.
    monkeypatch.setenv("SKCHAT_LIVEKIT_URL", "ws://100.108.59.57:7880")
    assert livekit_routes._livekit_api_url() == "http://100.108.59.57:7880"
    # Explicit override always wins.
    monkeypatch.setenv("SKCHAT_LIVEKIT_API_URL", "http://x:9000")
    assert livekit_routes._livekit_api_url() == "http://x:9000"
