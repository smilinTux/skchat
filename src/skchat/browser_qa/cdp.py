"""Raw Chrome DevTools Protocol over a hand-rolled websocket.

Why not Playwright, and why not a websocket library:

  * ``playwright.sync_api.connect_over_cdp()`` TIMES OUT against the Chrome
    151 builds on this fleet. It does not report an incompatibility, it
    simply hangs, so the failure presents as a wedged test rather than a
    dependency problem. Do not "simplify" this module back onto Playwright
    without first proving the connect handshake completes on the actual
    Chrome the fleet runs.
  * No websocket library is a declared skchat dependency. ``websockets`` is
    present in some dev environments transitively, which is exactly the kind
    of accident that makes CI's clean install fail later. RFC6455 client
    framing is about a hundred lines, so this module carries its own.

Two more Chrome facts that cost real time and are encoded here rather than
in a comment somewhere else:

  * ``/json/new`` REQUIRES ``PUT`` on newer Chrome. A ``GET`` returns 405,
    and the 405 is easy to miss because it does not fail at the request. It
    surfaces much later as a confusing ``StopIteration`` when something
    tries to look the new target id up in the target list.
  * Flutter web renders into a CANVAS. ``document.body.innerText`` is EMPTY
    on a completely healthy page, so any DOM-text assertion reports a broken
    app that is in fact fine. ``Page.captureScreenshot`` is the only
    reliable inspection, which is why :class:`BrowserPage` exposes bytes and
    :mod:`skchat.browser_qa.screenshot` grades an image rather than a
    string.

:class:`BrowserPage` is the SEAM. The lane never imports this module's
concrete classes; it takes a factory. Tests inject a fake page and never
open a socket (see ``tests/test_browser_qa.py``).
"""

from __future__ import annotations

import base64
import json
import os
import socket
import struct
import subprocess
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from typing import Any, Callable, Optional, Protocol

#: Default CDP port for this lane. Deliberately NOT 9229 (the daily
#: chrome-cdp instance a human drives) and NOT 9222/9223 (the agent
#: instances). Seizing any of those means fighting another session for
#: tabs. Override with SKCHAT_BROWSER_QA_CDP_PORT.
DEFAULT_CDP_PORT = 9232

#: Seconds to wait for the CDP HTTP endpoint and for websocket reads.
HTTP_TIMEOUT_S = 5.0
WS_TIMEOUT_S = 30.0

#: How long to wait for a freshly launched Chrome to answer /json/version.
LAUNCH_WAIT_S = 20.0


class CdpError(RuntimeError):
    """Any failure reaching or driving Chrome over CDP."""


@dataclass
class ConsoleEntry:
    """One console/runtime diagnostic captured while the page settled."""

    level: str  # "error" | "warning" | "info" | "log"
    text: str
    source: str = ""

    def to_dict(self) -> dict:
        return {"level": self.level, "text": self.text, "source": self.source}


class BrowserPage(Protocol):
    """The seam the lane drives. Implemented for real by :class:`CdpPage`
    and by a fake in the tests. Intentionally tiny: navigate, wait, capture
    an image, ask one JS question, report console diagnostics, close."""

    def navigate(self, url: str) -> None: ...

    def settle(self, seconds: float) -> None: ...

    def screenshot(self) -> bytes: ...

    def evaluate(self, expression: str) -> Any: ...

    def console(self) -> list[ConsoleEntry]: ...

    def close(self) -> None: ...


# --------------------------------------------------------------- websocket --


def _mask(payload: bytes, key: bytes) -> bytes:
    return bytes(b ^ key[i % 4] for i, b in enumerate(payload))


class _WebSocket:
    """A minimal RFC6455 TEXT client. Handles fragmentation, ping/pong, and
    the 126/127 extended lengths (a screenshot response is megabytes of
    base64, so the 8-byte length path is the normal path here, not an edge
    case)."""

    def __init__(self, ws_url: str, *, timeout: float = WS_TIMEOUT_S) -> None:
        parsed = urllib.parse.urlparse(ws_url)
        if parsed.scheme not in ("ws", "http"):
            raise CdpError(f"unsupported websocket scheme in {ws_url!r}")
        host = parsed.hostname or "127.0.0.1"
        port = parsed.port or 80
        path = parsed.path or "/"
        if parsed.query:
            path = f"{path}?{parsed.query}"
        self._buf = b""
        self._frag_parts: list[bytes] = []
        self._frag_text = False
        try:
            self._sock = socket.create_connection((host, port), timeout=timeout)
        except OSError as exc:
            raise CdpError(f"cannot connect to {host}:{port}: {exc}") from exc
        self._sock.settimeout(timeout)
        self._handshake(host, port, path)

    def _handshake(self, host: str, port: int, path: str) -> None:
        key = base64.b64encode(os.urandom(16)).decode()
        req = (
            f"GET {path} HTTP/1.1\r\n"
            f"Host: {host}:{port}\r\n"
            "Upgrade: websocket\r\n"
            "Connection: Upgrade\r\n"
            f"Sec-WebSocket-Key: {key}\r\n"
            "Sec-WebSocket-Version: 13\r\n\r\n"
        )
        self._sock.sendall(req.encode())
        while b"\r\n\r\n" not in self._buf:
            chunk = self._sock.recv(4096)
            if not chunk:
                raise CdpError("websocket handshake closed before the response headers")
            self._buf += chunk
        head, _, rest = self._buf.partition(b"\r\n\r\n")
        self._buf = rest
        status = head.split(b"\r\n", 1)[0].decode("latin-1")
        if "101" not in status:
            raise CdpError(f"websocket upgrade refused: {status}")

    @staticmethod
    def _parse_frame(buf: bytes):
        """Parse one frame out of ``buf`` WITHOUT consuming anything.

        Returns ``(opcode, fin, payload, consumed)`` or None when the buffer
        does not yet hold a whole frame. Non-destructive on purpose: the
        settle loop reads with a short socket timeout, and a timeout that
        landed halfway through a consuming parse would drop the bytes it had
        already taken and desync the stream for every later message. Leaving
        the buffer untouched makes a timeout a no-op you can simply retry.
        """
        if len(buf) < 2:
            return None
        b0, b1 = buf[0], buf[1]
        fin = bool(b0 & 0x80)
        opcode = b0 & 0x0F
        masked = bool(b1 & 0x80)
        length = b1 & 0x7F
        off = 2
        if length == 126:
            if len(buf) < off + 2:
                return None
            (length,) = struct.unpack(">H", buf[off : off + 2])
            off += 2
        elif length == 127:
            if len(buf) < off + 8:
                return None
            (length,) = struct.unpack(">Q", buf[off : off + 8])
            off += 8
        key = b""
        if masked:
            if len(buf) < off + 4:
                return None
            key = buf[off : off + 4]
            off += 4
        if len(buf) < off + length:
            return None
        payload = buf[off : off + length]
        off += length
        if masked:
            payload = _mask(payload, key)
        return opcode, fin, payload, off

    def _read_frame(self) -> tuple[int, bool, bytes]:
        while True:
            parsed = self._parse_frame(self._buf)
            if parsed is not None:
                opcode, fin, payload, consumed = parsed
                self._buf = self._buf[consumed:]
                return opcode, fin, payload
            chunk = self._sock.recv(65536)  # may raise a timeout; _buf stays intact
            if not chunk:
                raise CdpError("websocket closed mid-frame")
            self._buf += chunk

    def send_text(self, text: str) -> None:
        payload = text.encode()
        key = os.urandom(4)
        header = bytearray([0x81])
        n = len(payload)
        if n < 126:
            header.append(0x80 | n)
        elif n < (1 << 16):
            header.append(0x80 | 126)
            header += struct.pack(">H", n)
        else:
            header.append(0x80 | 127)
            header += struct.pack(">Q", n)
        header += key
        self._sock.sendall(bytes(header) + _mask(payload, key))

    def _send_frame(self, opcode: int, payload: bytes) -> None:
        key = os.urandom(4)
        header = bytes([0x80 | opcode, 0x80 | len(payload)]) + key
        self._sock.sendall(header + _mask(payload, key))

    def recv_text(self) -> str:
        """Return the next complete TEXT message. Ping is answered inline;
        binary and pong frames are skipped; a close frame ends the stream.

        Partial-message state lives on the INSTANCE, not in a local, so a
        read timeout between two fragments of one message resumes where it
        left off instead of silently discarding the fragments already read.
        """
        while True:
            opcode, fin, payload = self._read_frame()
            if opcode == 0x9:  # ping
                self._send_frame(0xA, payload[:125])
                continue
            if opcode == 0xA:  # pong
                continue
            if opcode == 0x8:  # close
                raise CdpError("websocket closed by Chrome")
            if opcode == 0x1:
                self._frag_text = True
                self._frag_parts = [payload]
            elif opcode == 0x2:
                self._frag_text = False
                self._frag_parts = [payload]
            elif opcode == 0x0:
                self._frag_parts.append(payload)
            if fin:
                parts, is_text = self._frag_parts, self._frag_text
                self._frag_parts, self._frag_text = [], False
                if is_text:
                    return b"".join(parts).decode("utf-8", "replace")

    def close(self) -> None:
        try:
            self._send_frame(0x8, b"")
        except OSError:
            pass
        try:
            self._sock.close()
        except OSError:
            pass


# ------------------------------------------------------------ HTTP endpoint --


def _http(method: str, url: str, *, timeout: float = HTTP_TIMEOUT_S) -> str:
    data = b"" if method in ("PUT", "POST") else None
    req = urllib.request.Request(url, data=data, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:  # noqa: S310 - localhost CDP
            return resp.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as exc:
        raise CdpError(f"{method} {url} returned HTTP {exc.code}") from exc
    except (urllib.error.URLError, OSError) as exc:
        raise CdpError(f"{method} {url} failed: {exc}") from exc


def endpoint_base(port: int, host: str = "127.0.0.1") -> str:
    return f"http://{host}:{port}"


def version(port: int, host: str = "127.0.0.1") -> dict:
    """``GET /json/version``. The cheapest "is a CDP Chrome listening here"
    probe; raises :class:`CdpError` when nothing answers."""
    return json.loads(_http("GET", f"{endpoint_base(port, host)}/json/version"))


def new_target(port: int, url: str = "about:blank", host: str = "127.0.0.1") -> dict:
    """``PUT /json/new?<url>``. PUT, NOT GET: newer Chrome answers 405 to a
    GET here (see the module docstring). Returns the target descriptor,
    which carries ``webSocketDebuggerUrl`` and ``id``."""
    q = urllib.parse.quote(url, safe="")
    body = _http("PUT", f"{endpoint_base(port, host)}/json/new?{q}")
    target = json.loads(body)
    if not isinstance(target, dict) or not target.get("webSocketDebuggerUrl"):
        raise CdpError(f"/json/new gave no webSocketDebuggerUrl: {body[:200]!r}")
    return target


def close_target(port: int, target_id: str, host: str = "127.0.0.1") -> None:
    try:
        _http("GET", f"{endpoint_base(port, host)}/json/close/{target_id}")
    except CdpError:
        pass  # a tab that is already gone is the outcome we wanted


def launch_chrome(port: int, *, binary: str = "", user_data_dir: str = "") -> subprocess.Popen:
    """Start a DEDICATED headless Chrome on ``port`` with its own profile
    directory. Never reuses a human's profile, and the caller is responsible
    for terminating exactly what it launched (see
    :func:`skchat.browser_qa.lane.run_lane`)."""
    binary = binary or os.environ.get("SKCHAT_BROWSER_QA_CHROME", "") or _find_chrome()
    if not binary:
        raise CdpError("no chrome binary found; set SKCHAT_BROWSER_QA_CHROME")
    user_data_dir = user_data_dir or tempfile.mkdtemp(prefix="skchat-browser-qa-")
    argv = [
        binary,
        "--headless=new",
        f"--remote-debugging-port={port}",
        "--remote-allow-origins=*",
        f"--user-data-dir={user_data_dir}",
        "--no-first-run",
        "--no-default-browser-check",
        "--disable-gpu",
        "--disable-dev-shm-usage",
        "--window-size=1280,900",
        "about:blank",
    ]
    proc = subprocess.Popen(
        argv, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True
    )
    deadline = time.monotonic() + LAUNCH_WAIT_S
    while time.monotonic() < deadline:
        try:
            version(port)
            return proc
        except CdpError:
            time.sleep(0.25)
    proc.terminate()
    raise CdpError(f"chrome did not answer /json/version on {port} within {LAUNCH_WAIT_S:.0f}s")


def _find_chrome() -> str:
    import shutil

    for name in (
        "google-chrome",
        "google-chrome-stable",
        "chromium",
        "chromium-browser",
        "chrome",
    ):
        found = shutil.which(name)
        if found:
            return found
    return ""


# ------------------------------------------------------------------- page ---


@dataclass
class CdpPage:
    """One CDP-attached tab. Owns its websocket and its target id."""

    port: int
    target_id: str
    _ws: _WebSocket
    host: str = "127.0.0.1"
    _next_id: int = 0
    _console: list[ConsoleEntry] = field(default_factory=list)

    def _call(
        self, method: str, params: Optional[dict] = None, *, timeout: float = WS_TIMEOUT_S
    ) -> dict:
        self._next_id += 1
        msg_id = self._next_id
        self._ws.send_text(json.dumps({"id": msg_id, "method": method, "params": params or {}}))
        deadline = time.monotonic() + timeout
        while True:
            if time.monotonic() > deadline:
                raise CdpError(f"{method} timed out after {timeout:.0f}s")
            frame = json.loads(self._ws.recv_text())
            if frame.get("id") == msg_id:
                if "error" in frame:
                    raise CdpError(f"{method} failed: {frame['error']}")
                return frame.get("result") or {}
            self._absorb_event(frame)

    def _absorb_event(self, frame: dict) -> None:
        method = frame.get("method") or ""
        params = frame.get("params") or {}
        if method == "Runtime.consoleAPICalled":
            level = str(params.get("type") or "log")
            args = params.get("args") or []
            text = " ".join(str(a.get("value", a.get("description", ""))) for a in args).strip()
            self._console.append(ConsoleEntry(level=level, text=text[:2000], source="console"))
        elif method == "Log.entryAdded":
            entry = params.get("entry") or {}
            self._console.append(
                ConsoleEntry(
                    level=str(entry.get("level") or "info"),
                    text=str(entry.get("text") or "")[:2000],
                    source=str(entry.get("source") or "log"),
                )
            )
        elif method == "Runtime.exceptionThrown":
            details = params.get("exceptionDetails") or {}
            text = str(details.get("text") or "uncaught exception")
            exc = details.get("exception") or {}
            if exc.get("description"):
                text = f"{text}: {exc['description']}"
            self._console.append(ConsoleEntry(level="error", text=text[:2000], source="exception"))

    def enable_domains(self) -> None:
        for domain in ("Page", "Runtime", "Log"):
            self._call(f"{domain}.enable")

    def navigate(self, url: str) -> None:
        result = self._call("Page.navigate", {"url": url})
        if result.get("errorText"):
            raise CdpError(f"navigation to {url} failed: {result['errorText']}")

    def settle(self, seconds: float) -> None:
        """Drain events for ``seconds``.

        This is the deliberate 12-to-20 second wait: the Flutter shell boots
        slower than anyone expects, and capturing early manufactures exactly
        the blank-screen false positive this lane exists to detect.

        Reads use a SHORT socket timeout so the loop can check the deadline;
        a timeout is a no-op retry, which is safe only because
        ``_WebSocket._read_frame`` is non-destructive on a partial read. A
        close frame ends the settle immediately rather than spinning out the
        remaining seconds; the capture that follows then fails honestly.
        """
        deadline = time.monotonic() + seconds
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return
            self._ws._sock.settimeout(min(remaining, 1.0))
            try:
                frame = json.loads(self._ws.recv_text())
            except CdpError:
                return  # the socket is gone; nothing more will arrive
            except (socket.timeout, TimeoutError, OSError, ValueError):
                continue
            finally:
                self._ws._sock.settimeout(WS_TIMEOUT_S)
            self._absorb_event(frame)

    def screenshot(self) -> bytes:
        result = self._call(
            "Page.captureScreenshot", {"format": "png", "captureBeyondViewport": False}
        )
        data = result.get("data") or ""
        if not data:
            raise CdpError("Page.captureScreenshot returned no data")
        return base64.b64decode(data)

    def evaluate(self, expression: str) -> Any:
        result = self._call("Runtime.evaluate", {"expression": expression, "returnByValue": True})
        return (result.get("result") or {}).get("value")

    def console(self) -> list[ConsoleEntry]:
        return list(self._console)

    def close(self) -> None:
        try:
            self._ws.close()
        finally:
            close_target(self.port, self.target_id, self.host)


def connect_page(port: int, host: str = "127.0.0.1") -> CdpPage:
    """Open a fresh blank tab on an already-running Chrome and attach to
    it. This is the default page factory the lane uses."""
    target = new_target(port, "about:blank", host)
    ws = _WebSocket(target["webSocketDebuggerUrl"])
    page = CdpPage(port=port, target_id=str(target.get("id") or ""), _ws=ws, host=host)
    page.enable_domains()
    return page


def default_page_factory(port: Optional[int] = None) -> Callable[[], BrowserPage]:
    port = (
        port
        if port is not None
        else int(os.environ.get("SKCHAT_BROWSER_QA_CDP_PORT", DEFAULT_CDP_PORT))
    )

    def _factory() -> BrowserPage:
        return connect_page(port)

    return _factory


__all__ = [
    "BrowserPage",
    "CdpError",
    "CdpPage",
    "ConsoleEntry",
    "DEFAULT_CDP_PORT",
    "close_target",
    "connect_page",
    "default_page_factory",
    "launch_chrome",
    "new_target",
    "version",
]
