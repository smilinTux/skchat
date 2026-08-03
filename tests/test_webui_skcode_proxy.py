"""skcode funnel proxy: the GET/POST reverse proxy and the WebSocket session-tail
proxy that make skcode-hostd reachable same-origin over the 443 funnel.

skcode-hostd binds a Tailscale IP only and runs its own deny-all gate, so these
tests assert two things the funnel path depends on:

  * the GET proxy forwards the request to the host under the right path AND passes
    the operator ``Authorization`` header through (the webui adds no auth of its
    own; the host's gate still decides), and
  * the WebSocket proxy bridges the host's session-tail stream to the browser,
    forwards the query-string token verbatim (browsers cannot set a WS header),
    and relays a host handshake rejection as a 1008 policy close.

Upstreams are monkeypatched so the tests never touch the tailnet host or a
socket. Run from ~ per skchat/CLAUDE.md (skmemory namespace collision).
"""

from __future__ import annotations

import json

import pytest
from starlette.testclient import TestClient

from skchat import webui


# --------------------------------------------------------------------------- #
# Pure helper: http(s) upstream + proxied path -> ws(s) host URL
# --------------------------------------------------------------------------- #
def test_ws_url_mapping_http_and_https_and_query():
    # http -> ws, query preserved verbatim (the token rides here).
    assert webui._skcode_ws_url(
        "http://100.108.59.57:9394", "api/v1/sessions/abc/stream", "token=xyz"
    ) == "ws://100.108.59.57:9394/api/v1/sessions/abc/stream?token=xyz"
    # https -> wss, no query.
    assert webui._skcode_ws_url(
        "https://host:443", "api/v1/sessions/abc/stream", ""
    ) == "wss://host:443/api/v1/sessions/abc/stream"


# --------------------------------------------------------------------------- #
# GET proxy: path mapping + Authorization pass-through
# --------------------------------------------------------------------------- #
class _FakeResp:
    def __init__(self, body: bytes, status: int = 200, ctype: str = "application/json"):
        self._body = body
        self.status = status
        self.headers = {"content-type": ctype}

    def read(self) -> bytes:
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def test_get_proxy_forwards_path_and_authorization(monkeypatch):
    captured = {}

    def _fake_urlopen(req, timeout=10):
        captured["url"] = req.full_url
        captured["method"] = req.get_method()
        # urllib title-cases header keys.
        captured["auth"] = req.headers.get("Authorization")
        return _FakeResp(json.dumps({"sessions": []}).encode())

    import urllib.request

    monkeypatch.setattr(urllib.request, "urlopen", _fake_urlopen)
    monkeypatch.setenv("SKCODE_HOSTD_URL", "http://10.0.0.9:9394")

    client = TestClient(webui.app)
    r = client.get(
        "/skcode/api/v1/sessions", headers={"Authorization": "Bearer op-token"}
    )
    assert r.status_code == 200
    assert r.json() == {"sessions": []}
    # Proxied under the host base with the /skcode prefix stripped.
    assert captured["url"] == "http://10.0.0.9:9394/api/v1/sessions"
    assert captured["method"] == "GET"
    # The operator credential is preserved, not dropped or rewritten.
    assert captured["auth"] == "Bearer op-token"


# --------------------------------------------------------------------------- #
# WebSocket proxy: bridges the tail stream, forwards the token, relays rejects
# --------------------------------------------------------------------------- #
class _FakeUpstreamWS:
    """Minimal stand-in for a websockets client connection: yields queued frames
    then stops, records what was sent to it, and can be closed."""

    def __init__(self, frames):
        self._frames = list(frames)
        self.sent = []
        self.closed = False

    def __aiter__(self):
        return self

    async def __anext__(self):
        if self._frames:
            return self._frames.pop(0)
        raise StopAsyncIteration

    async def send(self, msg):
        self.sent.append(msg)

    async def close(self):
        self.closed = True


def test_ws_proxy_bridges_stream_and_forwards_token(monkeypatch):
    seen = {}

    async def _fake_connect(url, **kw):
        seen["url"] = url
        return _FakeUpstreamWS(
            [json.dumps({"type": "status", "text": "hello"}), json.dumps({"type": "diff"})]
        )

    import websockets

    monkeypatch.setattr(websockets, "connect", _fake_connect)
    monkeypatch.setenv("SKCODE_HOSTD_URL", "http://10.0.0.9:9394")

    client = TestClient(webui.app)
    with client.websocket_connect(
        "/skcode/api/v1/sessions/sid1/stream?token=abc"
    ) as ws:
        first = ws.receive_json()
        second = ws.receive_json()

    assert first == {"type": "status", "text": "hello"}
    assert second == {"type": "diff"}
    # The token rode the query string through to the tailnet host verbatim.
    assert seen["url"] == "ws://10.0.0.9:9394/api/v1/sessions/sid1/stream?token=abc"


def test_ws_proxy_relays_host_rejection_as_1008(monkeypatch):
    async def _reject(url, **kw):
        # Genuine InvalidStatus instance without needing its response arg: the
        # route catches the host's deny-all handshake reject and closes 1008.
        from websockets.exceptions import InvalidStatus

        raise InvalidStatus.__new__(InvalidStatus)

    import websockets

    monkeypatch.setattr(websockets, "connect", _reject)
    monkeypatch.setenv("SKCODE_HOSTD_URL", "http://10.0.0.9:9394")

    client = TestClient(webui.app)
    from starlette.websockets import WebSocketDisconnect

    with pytest.raises(WebSocketDisconnect) as excinfo:
        with client.websocket_connect(
            "/skcode/api/v1/sessions/sid1/stream?token=bad"
        ) as ws:
            ws.receive_text()
    assert excinfo.value.code == 1008
