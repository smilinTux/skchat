"""Tests for the raw-CDP layer (skchat.browser_qa.cdp).

NO TEST HERE OPENS A SOCKET OR STARTS A BROWSER. The websocket framing is
exercised against an in-memory fake socket, and the HTTP endpoint helpers
are exercised against a monkeypatched ``_http``. The autouse ``sealed``
fixture makes the real primitives raise so a forgotten patch fails loudly.

What these tests actually protect, all of it learned the hard way:

  * ``/json/new`` must be a PUT. A GET returns 405 on newer Chrome and the
    failure does not surface there; it surfaces much later as a confusing
    ``StopIteration`` on a target lookup.
  * The default port must not be 9229 (the daily chrome-cdp instance) or
    9222/9223 (the agent instances).
  * Screenshot responses are megabytes, so the 8-byte extended websocket
    length is the NORMAL path, not an edge case.
"""

from __future__ import annotations

import base64
import json
import socket
import struct
import subprocess

import pytest

from skchat.browser_qa import cdp as cdp_mod
from skchat.browser_qa.cdp import CdpError, CdpPage, ConsoleEntry


@pytest.fixture(autouse=True)
def sealed(monkeypatch):
    def refuse(*_a, **_k):
        raise AssertionError("a test tried to open a real socket or spawn a browser")

    monkeypatch.setattr(socket, "create_connection", refuse)
    monkeypatch.setattr(subprocess, "Popen", refuse)
    yield


# ------------------------------------------------------- websocket framing --


def _server_frame(payload: bytes, opcode: int = 0x1, fin: bool = True) -> bytes:
    """Build an UNMASKED server->client frame, exactly as Chrome sends."""
    head = bytearray([(0x80 if fin else 0x00) | opcode])
    n = len(payload)
    if n < 126:
        head.append(n)
    elif n < (1 << 16):
        head.append(126)
        head += struct.pack(">H", n)
    else:
        head.append(127)
        head += struct.pack(">Q", n)
    return bytes(head) + payload


class FakeSocket:
    """An in-memory stand-in: hands back queued bytes, records what was sent."""

    def __init__(self, inbound: bytes = b""):
        self.inbound = inbound
        self.sent = bytearray()

    def sendall(self, data):
        self.sent += data

    def recv(self, n):
        if not self.inbound:
            raise BlockingIOError("no more data")
        chunk, self.inbound = self.inbound[:n], self.inbound[n:]
        return chunk

    def settimeout(self, _t):
        pass

    def close(self):
        pass


def _ws_with(frames: bytes) -> cdp_mod._WebSocket:
    """A _WebSocket wired to a FakeSocket, skipping the real handshake."""
    ws = object.__new__(cdp_mod._WebSocket)
    ws._sock = FakeSocket(frames)
    ws._buf = b""
    return ws


def test_a_short_text_frame_round_trips():
    ws = _ws_with(_server_frame(b'{"id":1}'))
    assert ws.recv_text() == '{"id":1}'


def test_a_two_byte_length_frame_round_trips():
    body = json.dumps({"data": "x" * 1000}).encode()
    ws = _ws_with(_server_frame(body))
    assert json.loads(ws.recv_text())["data"] == "x" * 1000


def test_a_screenshot_sized_frame_round_trips():
    """A captured PNG base64s well past 64KB, so the 8-byte extended length
    is the normal path for the one message this lane cares most about."""
    body = json.dumps({"result": {"data": "A" * 200_000}}).encode()
    assert len(body) > (1 << 16)
    ws = _ws_with(_server_frame(body))
    assert len(json.loads(ws.recv_text())["result"]["data"]) == 200_000


def test_fragmented_frames_are_reassembled():
    frames = _server_frame(b'{"a":', opcode=0x1, fin=False) + _server_frame(
        b"1}", opcode=0x0, fin=True
    )
    assert _ws_with(frames).recv_text() == '{"a":1}'


def test_a_ping_is_answered_and_the_next_message_still_arrives():
    ws = _ws_with(_server_frame(b"hi", opcode=0x9) + _server_frame(b'"ok"'))
    assert ws.recv_text() == '"ok"'
    assert bytes(ws._sock.sent)[0] & 0x0F == 0xA  # a pong went back


class DribblingSocket(FakeSocket):
    """Hands back one byte at a time and raises a timeout in between, which
    is what a settle loop with a short socket timeout actually sees."""

    def recv(self, _n):
        if not self.inbound:
            raise BlockingIOError("no more data")
        self.timeouts = getattr(self, "timeouts", 0) + 1
        if self.timeouts % 2:
            raise TimeoutError("read timed out")
        chunk, self.inbound = self.inbound[:1], self.inbound[1:]
        return chunk


def test_a_read_timeout_mid_frame_does_not_desync_the_stream():
    """The settle loop reads with a short timeout on purpose. A parse that
    consumed bytes before timing out would drop them and corrupt every later
    message, so the frame parser must be non-destructive on a partial read."""
    ws = _ws_with(b"")
    ws._sock = DribblingSocket(_server_frame(b'{"first":1}') + _server_frame(b'{"second":2}'))

    def read_through_timeouts():
        while True:
            try:
                return ws.recv_text()
            except (TimeoutError, OSError):
                continue

    assert read_through_timeouts() == '{"first":1}'
    assert read_through_timeouts() == '{"second":2}'


def test_fragments_survive_a_timeout_between_them():
    ws = _ws_with(b"")
    ws._sock = DribblingSocket(
        _server_frame(b'{"a":', opcode=0x1, fin=False) + _server_frame(b"1}", opcode=0x0, fin=True)
    )
    while True:
        try:
            assert ws.recv_text() == '{"a":1}'
            break
        except (TimeoutError, OSError):
            continue


def test_a_close_frame_raises_rather_than_hanging():
    with pytest.raises(CdpError):
        _ws_with(_server_frame(b"", opcode=0x8)).recv_text()


def test_client_frames_are_masked():
    """RFC6455 requires client->server masking; Chrome drops unmasked frames."""
    ws = _ws_with(b"")
    ws.send_text("hello")
    sent = bytes(ws._sock.sent)
    assert sent[0] == 0x81
    assert sent[1] & 0x80, "the mask bit must be set on every client frame"
    key = sent[2:6]
    assert bytes(b ^ key[i % 4] for i, b in enumerate(sent[6:])) == b"hello"


def test_a_large_client_frame_uses_the_extended_length():
    ws = _ws_with(b"")
    ws.send_text("z" * 70_000)
    sent = bytes(ws._sock.sent)
    assert sent[1] & 0x7F == 127
    assert struct.unpack(">Q", sent[2:10])[0] == 70_000


# ----------------------------------------------------------- HTTP endpoint --


def test_new_target_uses_PUT_not_GET(monkeypatch):
    """A GET here returns 405 on newer Chrome, and the failure surfaces far
    away as a StopIteration on a target lookup."""
    seen = {}

    def fake_http(method, url, **_kw):
        seen["method"], seen["url"] = method, url
        return json.dumps({"id": "T1", "webSocketDebuggerUrl": "ws://127.0.0.1:9232/devtools/x"})

    monkeypatch.setattr(cdp_mod, "_http", fake_http)
    target = cdp_mod.new_target(9232, "about:blank")
    assert seen["method"] == "PUT"
    assert "/json/new?" in seen["url"]
    assert target["id"] == "T1"


def test_new_target_without_a_debugger_url_fails_immediately(monkeypatch):
    monkeypatch.setattr(cdp_mod, "_http", lambda *a, **k: json.dumps({"id": "T1"}))
    with pytest.raises(CdpError, match="webSocketDebuggerUrl"):
        cdp_mod.new_target(9232)


def test_close_target_swallows_a_tab_that_is_already_gone(monkeypatch):
    def gone(*_a, **_k):
        raise CdpError("HTTP 404")

    monkeypatch.setattr(cdp_mod, "_http", gone)
    cdp_mod.close_target(9232, "T1")  # a tab already gone is the outcome we wanted


def test_the_default_port_avoids_the_ports_humans_and_agents_drive():
    """9229 is the daily chrome-cdp instance; 9222/9223 are the agent
    instances. Seizing any of them means fighting another session for tabs."""
    assert cdp_mod.DEFAULT_CDP_PORT not in (9222, 9223, 9229)


def test_the_cdp_port_is_configurable(monkeypatch):
    monkeypatch.setenv("SKCHAT_BROWSER_QA_CDP_PORT", "9999")
    captured = {}
    monkeypatch.setattr(
        cdp_mod, "connect_page", lambda port, *a, **k: captured.setdefault("port", port)
    )
    cdp_mod.default_page_factory()()
    assert captured["port"] == 9999


# ------------------------------------------------------------------- page ---


class ScriptedWs:
    """Replays a list of frames as CDP JSON, recording what was sent."""

    def __init__(self, replies):
        self.replies = list(replies)
        self.sent = []
        self._sock = FakeSocket()

    def send_text(self, text):
        self.sent.append(json.loads(text))

    def recv_text(self):
        if not self.replies:
            raise CdpError("no more frames")
        item = self.replies.pop(0)
        if callable(item):
            item = item(self.sent[-1])
        return json.dumps(item)

    def close(self):
        pass


def _page(replies):
    return CdpPage(port=9232, target_id="T1", _ws=ScriptedWs(replies))


def test_a_call_skips_interleaved_events_and_returns_its_own_reply():
    page = _page(
        [
            {"method": "Log.entryAdded", "params": {"entry": {"level": "error", "text": "bad"}}},
            lambda sent: {"id": sent["id"], "result": {"frameId": "F"}},
        ]
    )
    page.navigate("http://h/app/")
    assert [e.text for e in page.console()] == ["bad"]


def test_a_navigation_error_is_raised_not_swallowed():
    page = _page([lambda sent: {"id": sent["id"], "result": {"errorText": "ERR_ABORTED"}}])
    with pytest.raises(CdpError, match="ERR_ABORTED"):
        page.navigate("http://h/app/")


def test_a_protocol_error_is_raised():
    page = _page([lambda sent: {"id": sent["id"], "error": {"message": "nope"}}])
    with pytest.raises(CdpError, match="nope"):
        page.evaluate("1+1")


def test_screenshot_decodes_the_base64_payload():
    raw = b"\x89PNG\r\n\x1a\n-not-really"
    page = _page(
        [lambda sent: {"id": sent["id"], "result": {"data": base64.b64encode(raw).decode()}}]
    )
    assert page.screenshot() == raw


def test_an_empty_screenshot_payload_raises():
    page = _page([lambda sent: {"id": sent["id"], "result": {"data": ""}}])
    with pytest.raises(CdpError):
        page.screenshot()


def test_an_uncaught_exception_is_captured_as_a_console_error():
    page = _page(
        [
            {
                "method": "Runtime.exceptionThrown",
                "params": {
                    "exceptionDetails": {
                        "text": "Uncaught",
                        "exception": {"description": "TypeError: null is not a subtype"},
                    }
                },
            },
            lambda sent: {"id": sent["id"], "result": {"result": {"value": True}}},
        ]
    )
    assert page.evaluate("true") is True
    entries = page.console()
    assert entries[0].level == "error" and "null is not a subtype" in entries[0].text


def test_console_api_calls_are_captured():
    page = _page(
        [
            {
                "method": "Runtime.consoleAPICalled",
                "params": {"type": "warning", "args": [{"value": "careful"}]},
            },
            lambda sent: {"id": sent["id"], "result": {"result": {"value": 2}}},
        ]
    )
    page.evaluate("1+1")
    assert page.console() == [ConsoleEntry(level="warning", text="careful", source="console")]
