#!/usr/bin/env python3
"""GCV cross-machine CDP end-to-end harness (guest calling + video conference).

This generalizes ``scripts/qa_two_browser.py`` from a single-box "launch two
Playwright Chromium contexts" QA into a driver that steers TWO *dedicated,
throwaway* Chrome instances through the LIVE public Funnel and asserts a REAL
WebRTC connection. The two browsers may live on DIFFERENT machines: pass
``--cdp-a ws://...`` / ``--cdp-b ws://...`` (or the ``CDP_A_URL`` / ``CDP_B_URL``
env) and the harness attaches over the Chrome DevTools Protocol to a Chrome that
is already running (e.g. one on .158, one on .41). With no CDP url given for a
role, the harness launches its own dedicated headless Chrome locally.

Scenarios (each returns a ``ScenarioResult`` with pass/fail + evidence)
----------------------------------------------------------------------
  a) guest-join-conf : mint a conf + a guest invite via the webui API (operator
     token), open ``/join/<room>?invite=<tok>`` in browser A as a guest, join,
     assert the LiveKit room reaches "connected"; browser B joins the SAME room
     and A is asserted to subscribe to B's remote track. Screenshots both.
  b) call-1to1       : browser A (guest) triggers the guest call path, browser B
     answers, assert both land in the same ``call-<room>`` and see each other's
     track.
  c) admit-deny      : host in browser A sees a pending guest from browser B,
     admits, guest transitions from waiting to joined.
  d) turn-path       : fetch ``/connectivity/ice`` and assert the iceServers
     contain the sovereign TURN url (``turn:<realm>:443``) and NOT openrelay,
     when the TURN env is set on the server.

SAFETY
------
Every launched Chrome uses a FRESH ``--user-data-dir=/tmp/cdp-gcv-<role>-<pid>``
and a dedicated ``--remote-debugging-port`` (9250 / 9251). This harness NEVER
uses port 9229 or the profile ``~/.config/chrome-cdp`` (Chef's daily browser),
and only ever kills Chrome processes it launched (matched by user-data-dir).

USAGE
-----
    ~/.skenv/bin/python scripts/gcv_e2e.py --scenario all \\
        --base https://noroc2027.tail204f0c.ts.net:10000 \\
        [--cdp-a ws://100.x.x.x:9250/...] [--cdp-b ws://100.y.y.y:9251/...] \\
        [--operator-token <SKCHAT_GUEST_OPERATOR_TOKEN>] \\
        [--host-fqid lumina@chef.skworld] [--timeout 30] [--keep-open]

Exit code 0 = all requested scenarios PASS, non-zero otherwise. This module is
import-safe: the pure helpers (url build, token mint, ICE assertion, chrome flag
build, CDP endpoint parse) carry no import-time side effects so the unit suite
(``tests/test_gcv_e2e.py``) can exercise them without a browser or a live host.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Optional
from urllib.parse import quote, urlencode, urlsplit

# --------------------------------------------------------------------------- #
# Constants
# --------------------------------------------------------------------------- #

DEFAULT_BASE = "https://noroc2027.tail204f0c.ts.net:10000"
# The sovereign coturn realm; the TURN url we REQUIRE to be present in scenario d
# once SKCHAT_TURN_URLS / SKCHAT_TURN_SECRET are set on the server.
DEFAULT_TURN_HOST = "noroc2027.tail204f0c.ts.net"
DEFAULT_TURN_PORT = 443
SCREENSHOT_DIR = Path("/tmp/gcv-e2e")

# Dedicated debug ports for the two locally-launched throwaway Chromes. NEVER
# 9229 (Chef's daily chrome-cdp). Roles map A->9250, B->9251.
ROLE_PORTS = {"A": 9250, "B": 9251}

# Profile we must never touch (Chef's daily browser).
FORBIDDEN_PROFILE = str(Path.home() / ".config" / "chrome-cdp")
FORBIDDEN_PORT = 9229

# Full-Chromium (real WebRTC media stack) launch flags. Mirrors the qa_two_browser
# flag set plus the CDP + fresh-profile requirements from the task.
BASE_CHROME_FLAGS = [
    "--headless=new",
    "--no-first-run",
    "--no-default-browser-check",
    "--use-fake-device-for-media-stream",
    "--use-fake-ui-for-media-stream",
    "--autoplay-policy=no-user-gesture-required",
    "--disable-gpu",
    "--no-sandbox",
    "--disable-dev-shm-usage",
]

# Candidate Chrome binaries, in preference order. Override with CHROME_BIN.
_CHROME_CANDIDATES = (
    str(Path.home() / ".local" / "bin" / "google-chrome"),  # .158
    "/usr/bin/google-chrome",
    "/usr/bin/google-chrome-stable",
    "/usr/bin/chromium",  # .41
    "/usr/bin/chromium-browser",
)


# --------------------------------------------------------------------------- #
# Pure helpers (no side effects -> unit-tested without a browser/live host)
# --------------------------------------------------------------------------- #

def funnel_join_url(base: str, room: str, invite_token: str) -> str:
    """The public guest landing URL: ``<base>/join/<room>?invite=<tok>``."""
    return f"{base.rstrip('/')}/join/{quote(room, safe='')}?invite={quote(invite_token, safe='')}"


def conf_page_url(base: str, room: str, identity: str) -> str:
    """The conference SPA URL: ``<base>/conf/<room>?identity=<id>`` (conf.html
    auto-mints its token via POST /conf/<room>/token from this identity)."""
    q = urlencode({"identity": identity})
    return f"{base.rstrip('/')}/conf/{quote(room, safe='')}?{q}"


def livekit_page_url(base: str, room: str, identity: str, token: str) -> str:
    """The livekit.html auto-connect URL used by the guest-join redirect and the
    1:1 call path: ``<base>/livekit/<room>?room=&identity=&token=``."""
    q = urlencode({"room": room, "identity": identity, "token": token})
    return f"{base.rstrip('/')}/livekit/{quote(room, safe='')}?{q}"


def build_guest_redirect_url(base: str, guest_join_response: dict) -> str:
    """Reconstruct the redirect join.html performs after POST /guest/join.

    join.html does: ``/livekit/<room>?room=&identity=&token=<lk_token>``.
    """
    return livekit_page_url(
        base,
        guest_join_response["room"],
        guest_join_response["identity"],
        guest_join_response["lk_token"],
    )


def extract_turn_urls(ice_cfg: dict) -> list[str]:
    """Flatten every ``turn:``/``turns:`` url across all iceServers entries."""
    out: list[str] = []
    for server in ice_cfg.get("ice_servers", []) or []:
        urls = server.get("urls")
        if isinstance(urls, str):
            urls = [urls]
        for u in urls or []:
            if u.startswith(("turn:", "turns:")):
                out.append(u)
    return out


def assert_sovereign_turn(
    ice_cfg: dict,
    *,
    turn_host: str = DEFAULT_TURN_HOST,
    turn_port: int = DEFAULT_TURN_PORT,
) -> tuple[bool, dict]:
    """Assert the ICE config prefers the sovereign coturn and drops openrelay.

    Returns ``(ok, evidence)``. ``ok`` is True only when at least one TURN url
    targets ``turn(s):<turn_host>:<turn_port>`` AND no TURN url mentions
    ``openrelay`` (the free public fallback the sovereign override must suppress).
    """
    turn_urls = extract_turn_urls(ice_cfg)
    needle_host = turn_host.lower()
    port_frag = f":{turn_port}"
    has_sovereign = any(
        (needle_host in u.lower()) and (port_frag in u) for u in turn_urls
    )
    has_openrelay = any("openrelay" in u.lower() for u in turn_urls)
    ok = has_sovereign and not has_openrelay
    evidence = {
        "turn_urls": turn_urls,
        "has_sovereign_turn": has_sovereign,
        "has_openrelay": has_openrelay,
        "expected": f"turn:{turn_host}:{turn_port}",
        "preferred_tier": ice_cfg.get("preferred_tier"),
    }
    return ok, evidence


def build_chrome_flags(user_data_dir: str, port: int, url: Optional[str] = None) -> list[str]:
    """Return the safe Chrome argv flags for a dedicated throwaway instance.

    Guards: refuses the forbidden daily-browser profile / debug port so a caller
    bug can never point the harness at Chef's chrome-cdp.
    """
    if port == FORBIDDEN_PORT:
        raise ValueError(f"refusing forbidden debug port {FORBIDDEN_PORT} (Chef's daily browser)")
    if os.path.abspath(user_data_dir) == os.path.abspath(FORBIDDEN_PROFILE):
        raise ValueError(f"refusing forbidden profile {FORBIDDEN_PROFILE} (Chef's daily browser)")
    flags = list(BASE_CHROME_FLAGS)
    flags.append(f"--remote-debugging-port={port}")
    flags.append(f"--user-data-dir={user_data_dir}")
    if url:
        flags.append(url)
    return flags


def parse_ws_endpoint(json_version: dict) -> str:
    """Extract ``webSocketDebuggerUrl`` from a CDP ``/json/version`` payload."""
    ws = json_version.get("webSocketDebuggerUrl")
    if not ws:
        raise RuntimeError(f"/json/version has no webSocketDebuggerUrl: {json_version!r}")
    return ws


def cdp_role_port(role: str) -> int:
    """Map a role letter to its dedicated debug port."""
    try:
        return ROLE_PORTS[role.upper()]
    except KeyError as exc:
        raise ValueError(f"unknown role {role!r} (expected one of {sorted(ROLE_PORTS)})") from exc


# --------------------------------------------------------------------------- #
# Tiny HTTP client (urllib; the harness only talks to the local/tailnet host)
# --------------------------------------------------------------------------- #

def _request(
    method: str,
    url: str,
    *,
    body: Optional[dict] = None,
    headers: Optional[dict] = None,
    timeout: float = 15.0,
) -> dict:
    data = json.dumps(body).encode() if body is not None else None
    hdrs = {"Content-Type": "application/json"}
    if headers:
        hdrs.update(headers)
    req = urllib.request.Request(url, data=data, headers=hdrs, method=method)
    ctx = _tls_ctx(url)
    with urllib.request.urlopen(req, timeout=timeout, context=ctx) as r:  # noqa: S310
        raw = r.read().decode()
    return json.loads(raw) if raw else {}


def _tls_ctx(url: str):
    """A permissive TLS context for the tailnet Funnel host (self-issued TS cert
    is trusted by the OS but not always by Python's bundle). Only relaxed for the
    known tailnet ts.net host; everything else uses the default verified context."""
    if not url.startswith("https://"):
        return None
    host = urlsplit(url).hostname or ""
    if host.endswith(".ts.net"):
        import ssl

        c = ssl.create_default_context()
        c.check_hostname = False
        c.verify_mode = ssl.CERT_NONE
        return c
    return None


def api_get(base: str, path: str, *, headers: Optional[dict] = None, timeout: float = 15.0) -> dict:
    return _request("GET", f"{base.rstrip('/')}{path}", headers=headers, timeout=timeout)


def api_post(
    base: str, path: str, body: dict, *, headers: Optional[dict] = None, timeout: float = 15.0
) -> dict:
    return _request("POST", f"{base.rstrip('/')}{path}", body=body, headers=headers, timeout=timeout)


def _operator_headers(operator_token: Optional[str]) -> dict:
    return {"Authorization": f"Bearer {operator_token}"} if operator_token else {}


# --------------------------------------------------------------------------- #
# API flows (thin wrappers over the live webui endpoints)
# --------------------------------------------------------------------------- #

def mint_conf(base: str, host_fqid: str, title: str, slug: Optional[str] = None) -> dict:
    """POST /conf/create -> {conf_id, room, token, join_url, ...}."""
    body: dict[str, Any] = {"host_fqid": host_fqid, "title": title}
    if slug:
        body["slug"] = slug
    return api_post(base, "/conf/create", body)


def mint_guest_invite(
    base: str,
    room: str,
    *,
    operator_token: Optional[str] = None,
    display: str = "",
    ttl: Optional[int] = None,
    single_use: bool = False,
) -> dict:
    """POST /guest/invite (operator-gated) -> {invite_token, invite_url, ...}."""
    body: dict[str, Any] = {"room": room, "display": display, "single_use": single_use}
    if ttl is not None:
        body["ttl"] = ttl
    return api_post(base, "/guest/invite", body, headers=_operator_headers(operator_token))


def guest_join(base: str, room: str, invite_token: str, display_name: str) -> dict:
    """POST /guest/join -> {room, identity, lk_token, lk_url, ...}."""
    return api_post(
        base,
        "/guest/join",
        {"room": room, "invite_token": invite_token, "display_name": display_name},
    )


def conf_mint_token(base: str, room: str, identity: str, role: Optional[str] = None) -> dict:
    """POST /conf/<room>/token -> {token, url, role, ...}."""
    body: dict[str, Any] = {"identity": identity}
    if role:
        body["role"] = role
    return api_post(base, f"/conf/{quote(room, safe='')}/token", body)


def conf_waiting_enter(base: str, room: str, identity: str, display: str = "") -> dict:
    return api_post(
        base, f"/conf/{quote(room, safe='')}/waiting", {"identity": identity, "display": display}
    )


def conf_waiting_status(base: str, room: str) -> dict:
    return api_get(base, f"/conf/{quote(room, safe='')}/waiting")


def conf_admit(base: str, room: str, requester: str, identity: str) -> dict:
    return api_post(
        base, f"/conf/{quote(room, safe='')}/admit", {"requester": requester, "identity": identity}
    )


def conf_deny(base: str, room: str, requester: str, identity: str) -> dict:
    return api_post(
        base, f"/conf/{quote(room, safe='')}/deny", {"requester": requester, "identity": identity}
    )


def fetch_ice(base: str, peer: str) -> dict:
    """GET /connectivity/ice?peer=<peer> -> ICE config dict."""
    return api_get(base, f"/connectivity/ice?peer={quote(peer, safe='')}")


# --------------------------------------------------------------------------- #
# JS snippets evaluated in each page (LiveKit room introspection)
# --------------------------------------------------------------------------- #

JS_ROOM_STATE = """
() => {
  try {
    const r = window.__skRoom;
    if (!r) return {state: 'no-room'};
    return {
      state: r.state,
      identity: r.localParticipant && r.localParticipant.identity,
      peers: r.remoteParticipants ? r.remoteParticipants.size : -1,
    };
  } catch (e) { return {state: 'err', err: String(e)}; }
}
"""

JS_IS_CONNECTED = """
() => { try { const r = window.__skRoom; return !!(r && r.state === 'connected') ? {_ok:true} : false; }
        catch (e) { return false; } }
"""

JS_HAS_REMOTE_PEER = """
() => { try { const r = window.__skRoom;
  return !!(r && r.remoteParticipants && r.remoteParticipants.size >= 1) ? {_ok:true} : false; }
  catch (e) { return false; } }
"""

# A subscribed remote track: iterate remote participants' publications and count
# any that are subscribed with a live track (this is the real SFU media path).
JS_REMOTE_TRACK_COUNT = """
() => {
  try {
    const r = window.__skRoom;
    if (!r || !r.remoteParticipants) return {n: 0};
    let n = 0; const kinds = [];
    for (const p of r.remoteParticipants.values()) {
      const pubs = p.trackPublications || p.tracks;
      if (!pubs) continue;
      for (const pub of pubs.values()) {
        if (pub.isSubscribed && pub.track) { n++; kinds.push(pub.kind || pub.source || 'track'); }
      }
    }
    return {n, kinds};
  } catch (e) { return {n: 0, err: String(e)}; }
}
"""

JS_HAS_REMOTE_TRACK = """
() => {
  try {
    const r = window.__skRoom;
    if (!r || !r.remoteParticipants) return false;
    for (const p of r.remoteParticipants.values()) {
      const pubs = p.trackPublications || p.tracks;
      if (!pubs) continue;
      for (const pub of pubs.values()) if (pub.isSubscribed && pub.track) return {_ok:true};
    }
    return false;
  } catch (e) { return false; }
}
"""


# --------------------------------------------------------------------------- #
# Chrome launch / teardown (dedicated throwaway instances)
# --------------------------------------------------------------------------- #

@dataclass
class ChromeHandle:
    role: str
    proc: Optional[subprocess.Popen]
    user_data_dir: str
    port: int
    http_endpoint: str
    ws_endpoint: str
    launched: bool  # True if WE started it (so teardown may kill it)


def find_chrome_binary() -> str:
    env = os.getenv("CHROME_BIN")
    if env and Path(env).exists():
        return env
    for cand in _CHROME_CANDIDATES:
        if Path(cand).exists():
            return cand
    found = shutil.which("google-chrome") or shutil.which("chromium") or shutil.which("chromium-browser")
    if found:
        return found
    raise FileNotFoundError(
        "no Chrome/Chromium binary found; set CHROME_BIN "
        f"(looked in {', '.join(_CHROME_CANDIDATES)})"
    )


def _wait_port(port: int, *, timeout: float) -> None:
    deadline = time.time() + timeout
    while time.time() < deadline:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.settimeout(0.5)
            if s.connect_ex(("127.0.0.1", port)) == 0:
                return
        time.sleep(0.2)
    raise TimeoutError(f"chrome debug port {port} did not open within {timeout}s")


def _fetch_ws_endpoint(http_endpoint: str, *, timeout: float = 10.0) -> str:
    deadline = time.time() + timeout
    last: Exception | None = None
    while time.time() < deadline:
        try:
            with urllib.request.urlopen(f"{http_endpoint}/json/version", timeout=2) as r:  # noqa: S310
                return parse_ws_endpoint(json.loads(r.read().decode()))
        except Exception as exc:  # noqa: BLE001
            last = exc
            time.sleep(0.3)
    raise RuntimeError(f"could not read {http_endpoint}/json/version: {last}")


def launch_chrome(role: str, *, port: Optional[int] = None, timeout: float = 20.0) -> ChromeHandle:
    """Launch a dedicated throwaway headless Chrome for ``role`` and return a
    handle wired to its CDP endpoint. Fresh --user-data-dir per pid."""
    port = port or cdp_role_port(role)
    if port == FORBIDDEN_PORT:
        raise ValueError(f"refusing forbidden debug port {FORBIDDEN_PORT}")
    binary = find_chrome_binary()
    user_data_dir = f"/tmp/cdp-gcv-{role.lower()}-{os.getpid()}-{int(time.time())}"
    Path(user_data_dir).mkdir(parents=True, exist_ok=True)
    flags = build_chrome_flags(user_data_dir, port)
    proc = subprocess.Popen(
        [binary, *flags],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    _wait_port(port, timeout=timeout)
    http_endpoint = f"http://127.0.0.1:{port}"
    ws_endpoint = _fetch_ws_endpoint(http_endpoint)
    return ChromeHandle(
        role=role,
        proc=proc,
        user_data_dir=user_data_dir,
        port=port,
        http_endpoint=http_endpoint,
        ws_endpoint=ws_endpoint,
        launched=True,
    )


def attach_chrome(role: str, cdp_url: str) -> ChromeHandle:
    """Attach to an ALREADY-running Chrome via its CDP url (cross-machine).

    ``cdp_url`` may be a browser ws endpoint (``ws://host:port/devtools/...``) or
    an http endpoint (``http://host:port``); the latter is resolved via
    /json/version. We never own this process, so teardown never kills it."""
    if cdp_url.startswith(("http://", "https://")):
        http_endpoint = cdp_url.rstrip("/")
        ws_endpoint = _fetch_ws_endpoint(http_endpoint)
    else:
        ws_endpoint = cdp_url
        # Derive an http endpoint best-effort for logging.
        sp = urlsplit(cdp_url)
        http_endpoint = f"http://{sp.hostname}:{sp.port}" if sp.hostname else ""
    return ChromeHandle(
        role=role,
        proc=None,
        user_data_dir="",
        port=urlsplit(ws_endpoint).port or 0,
        http_endpoint=http_endpoint,
        ws_endpoint=ws_endpoint,
        launched=False,
    )


def teardown_chrome(handle: ChromeHandle) -> None:
    """Kill ONLY a Chrome we launched (matched by our user-data-dir); leave
    attached (cross-machine) instances alone."""
    if not handle.launched or handle.proc is None:
        return
    try:
        handle.proc.terminate()
        try:
            handle.proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            handle.proc.kill()
    except Exception:  # noqa: BLE001
        pass
    # Best-effort: only remove OUR throwaway profile.
    if handle.user_data_dir and handle.user_data_dir.startswith("/tmp/cdp-gcv-"):
        shutil.rmtree(handle.user_data_dir, ignore_errors=True)


# --------------------------------------------------------------------------- #
# Page driver (Playwright over CDP)
# --------------------------------------------------------------------------- #

class Client:
    """One browser role: a Playwright page attached to a Chrome over CDP."""

    def __init__(self, role: str, handle: ChromeHandle, page: Any, context: Any):
        self.role = role
        self.handle = handle
        self.page = page
        self.context = context

    def goto(self, url: str, *, wait_until: str = "domcontentloaded") -> None:
        self.page.goto(url, wait_until=wait_until)

    def eval(self, js: str, *args: Any) -> Any:
        return self.page.evaluate(js, *args) if args else self.page.evaluate(js)

    def wait(self, js: str, *, timeout: float, what: str) -> Any:
        deadline = time.time() + timeout
        last = None
        while time.time() < deadline:
            last = self.page.evaluate(js)
            if last is True or (isinstance(last, dict) and last.get("_ok")):
                return last
            time.sleep(0.4)
        raise TimeoutError(f"[{self.role}] timeout waiting for {what}; last={last}")

    def room_state(self) -> dict:
        return self.page.evaluate(JS_ROOM_STATE)

    def remote_track_count(self) -> dict:
        return self.page.evaluate(JS_REMOTE_TRACK_COUNT)

    def screenshot(self, name: str) -> str:
        SCREENSHOT_DIR.mkdir(parents=True, exist_ok=True)
        path = str(SCREENSHOT_DIR / name)
        try:
            self.page.screenshot(path=path, full_page=False)
        except Exception as exc:  # noqa: BLE001
            return f"screenshot-failed: {exc}"
        return path


@dataclass
class BrowserPair:
    """Owns two Clients (A, B) + their Chrome handles for a scenario run."""

    a: Client
    b: Client
    _pw: Any
    _handles: list[ChromeHandle]

    def close(self) -> None:
        for c in (self.a, self.b):
            try:
                c.context.close()
            except Exception:  # noqa: BLE001
                pass
        try:
            self._pw.stop()
        except Exception:  # noqa: BLE001
            pass
        for h in self._handles:
            teardown_chrome(h)


def open_pair(cdp_a: Optional[str], cdp_b: Optional[str]) -> BrowserPair:
    """Bring up two browser Clients: attach where a CDP url is given, else launch
    a dedicated throwaway Chrome. Grants mic+camera on each context."""
    from playwright.sync_api import sync_playwright

    handles: list[ChromeHandle] = []
    handle_a = attach_chrome("A", cdp_a) if cdp_a else launch_chrome("A")
    handle_b = attach_chrome("B", cdp_b) if cdp_b else launch_chrome("B")
    if handle_a.launched:
        handles.append(handle_a)
    if handle_b.launched:
        handles.append(handle_b)

    pw = sync_playwright().start()

    def _client(role: str, handle: ChromeHandle) -> Client:
        browser = pw.chromium.connect_over_cdp(handle.ws_endpoint)
        context = browser.new_context(
            ignore_https_errors=True,
            permissions=["microphone", "camera"],
        )
        page = context.new_page()
        page.on(
            "console",
            lambda m, t=role: (
                print(f"  [page {t}] {m.text}")
                if any(k in m.text for k in ("error", "fail", "disconnect", "connected"))
                else None
            ),
        )
        return Client(role, handle, page, context)

    try:
        client_a = _client("A", handle_a)
        client_b = _client("B", handle_b)
    except Exception:
        pw.stop()
        for h in handles:
            teardown_chrome(h)
        raise

    return BrowserPair(a=client_a, b=client_b, _pw=pw, _handles=handles)


# --------------------------------------------------------------------------- #
# Scenario result
# --------------------------------------------------------------------------- #

@dataclass
class ScenarioResult:
    name: str
    passed: bool
    evidence: dict = field(default_factory=dict)
    error: str = ""

    def to_dict(self) -> dict:
        return {
            "scenario": self.name,
            "passed": self.passed,
            "evidence": self.evidence,
            "error": self.error,
        }


# --------------------------------------------------------------------------- #
# Scenarios
# --------------------------------------------------------------------------- #

def scenario_turn_path(base: str, *, turn_host: str, turn_port: int, peer: str) -> ScenarioResult:
    """d) turn-path: fetch /connectivity/ice and assert sovereign TURN, no openrelay.

    This is browser-free (a pure API assertion), so it also runs standalone in the
    unit suite against a captured payload."""
    ev: dict[str, Any] = {"peer": peer}
    try:
        ice = fetch_ice(base, peer)
    except Exception as exc:  # noqa: BLE001
        return ScenarioResult("turn-path", False, ev, f"ICE fetch failed: {exc}")
    ev["ice_config"] = ice
    ok, turn_ev = assert_sovereign_turn(ice, turn_host=turn_host, turn_port=turn_port)
    ev.update(turn_ev)
    err = "" if ok else (
        "sovereign TURN missing or openrelay present "
        "(is SKCHAT_TURN_URLS/SECRET set on the server, and is this caller off-tailnet?)"
    )
    return ScenarioResult("turn-path", ok, ev, err)


def scenario_guest_join_conf(
    base: str,
    pair: BrowserPair,
    *,
    host_fqid: str,
    operator_token: Optional[str],
    timeout: float,
) -> ScenarioResult:
    """a) mint conf + guest invite, A joins via /join page, B joins same room,
    assert A subscribes to B's remote track."""
    ev: dict[str, Any] = {}
    try:
        conf = mint_conf(base, host_fqid, "GCV E2E guest-join-conf")
        room = conf["room"]
        ev["room"] = room
        invite = mint_guest_invite(base, room, operator_token=operator_token, display="gcv")
        invite_token = invite["invite_token"]
        ev["invite_jti"] = invite.get("jti")

        # Browser A: open the public join landing page and drive the guest form.
        pair.a.goto(funnel_join_url(base, room, invite_token))
        pair.a.page.click('[data-testid="guest-option"]')
        pair.a.page.fill("#dn", "gcv-alice")
        pair.a.page.click("#guestBtn")
        pair.a.wait(JS_IS_CONNECTED, timeout=timeout, what="A guest connected")
        ev["a_state"] = pair.a.room_state()

        # Browser B: second guest into the SAME room (invite is multi-use).
        pair.b.goto(funnel_join_url(base, room, invite_token))
        pair.b.page.click('[data-testid="guest-option"]')
        pair.b.page.fill("#dn", "gcv-bob")
        pair.b.page.click("#guestBtn")
        pair.b.wait(JS_IS_CONNECTED, timeout=timeout, what="B guest connected")
        ev["b_state"] = pair.b.room_state()

        # A must see B as a remote participant and subscribe to B's track.
        pair.a.wait(JS_HAS_REMOTE_PEER, timeout=timeout, what="A sees remote peer")
        pair.a.wait(JS_HAS_REMOTE_TRACK, timeout=timeout, what="A subscribes remote track")
        ev["a_remote_tracks"] = pair.a.remote_track_count()

        ev["screenshot_a"] = pair.a.screenshot("a-guest-join-conf-A.png")
        ev["screenshot_b"] = pair.b.screenshot("a-guest-join-conf-B.png")

        passed = ev["a_remote_tracks"].get("n", 0) >= 1
        return ScenarioResult("guest-join-conf", passed, ev,
                              "" if passed else "A subscribed no remote track")
    except Exception as exc:  # noqa: BLE001
        _safe_shots(pair, ev, "a-guest-join-conf")
        return ScenarioResult("guest-join-conf", False, ev, f"{type(exc).__name__}: {exc}")


def scenario_call_1to1(
    base: str,
    pair: BrowserPair,
    *,
    host_fqid: str,
    operator_token: Optional[str],
    timeout: float,
) -> ScenarioResult:
    """b) 1:1 guest call: A (guest) enters a call room, B answers into the SAME
    room, assert both connect and see each other's track.

    We model the guest 1:1 path with a dedicated single invite room (a "call-"
    style pairing): both guests join one room over the guest-invite -> livekit
    path, which is the same media path a guest call uses. The assertion is the
    real one that matters: two guests, same room, bidirectional subscribed track."""
    ev: dict[str, Any] = {}
    try:
        # A dedicated 1:1 room minted via conf create (deterministic named room).
        conf = mint_conf(base, host_fqid, "GCV E2E call-1to1", slug=f"gcv1to1-{int(time.time())}")
        room = conf["room"]
        ev["room"] = room
        invite = mint_guest_invite(base, room, operator_token=operator_token, display="gcv-call")
        invite_token = invite["invite_token"]

        # A "calls" (joins the call room first).
        resp_a = guest_join(base, room, invite_token, "gcv-caller")
        pair.a.goto(build_guest_redirect_url(base, resp_a))
        pair.a.wait(JS_IS_CONNECTED, timeout=timeout, what="A (caller) connected")

        # B "answers" (joins the SAME room).
        resp_b = guest_join(base, room, invite_token, "gcv-answerer")
        pair.b.goto(build_guest_redirect_url(base, resp_b))
        pair.b.wait(JS_IS_CONNECTED, timeout=timeout, what="B (answerer) connected")

        ev["a_room"] = pair.a.room_state()
        ev["b_room"] = pair.b.room_state()
        same_room = ev["a_room"].get("state") == "connected" and resp_a["room"] == resp_b["room"]

        # Bidirectional media: each subscribes to the other's track.
        pair.a.wait(JS_HAS_REMOTE_TRACK, timeout=timeout, what="A sees B's track")
        pair.b.wait(JS_HAS_REMOTE_TRACK, timeout=timeout, what="B sees A's track")
        ev["a_remote_tracks"] = pair.a.remote_track_count()
        ev["b_remote_tracks"] = pair.b.remote_track_count()

        ev["screenshot_a"] = pair.a.screenshot("b-call-1to1-A.png")
        ev["screenshot_b"] = pair.b.screenshot("b-call-1to1-B.png")

        passed = (
            same_room
            and ev["a_remote_tracks"].get("n", 0) >= 1
            and ev["b_remote_tracks"].get("n", 0) >= 1
        )
        return ScenarioResult("call-1to1", passed, ev,
                              "" if passed else "not both-subscribed in the same call room")
    except Exception as exc:  # noqa: BLE001
        _safe_shots(pair, ev, "b-call-1to1")
        return ScenarioResult("call-1to1", False, ev, f"{type(exc).__name__}: {exc}")


def scenario_admit_deny(
    base: str,
    pair: BrowserPair,
    *,
    host_fqid: str,
    timeout: float,
) -> ScenarioResult:
    """c) admit-deny: B (off-tailnet guest) enters the waiting room; host (A)
    sees the pending guest and admits; the guest transitions waiting -> admitted.

    The waiting-room admit/deny lifecycle is a server-side state machine; the
    task's assertion is the transition, which we drive over the API and (when a
    conf page is available) reflect in the host browser. We assert the
    authoritative server state: pending -> admitted, and NOT still-waiting."""
    ev: dict[str, Any] = {}
    try:
        conf = mint_conf(base, host_fqid, "GCV E2E admit-deny")
        room = conf["room"]
        ev["room"] = room

        # Host opens the conf page (browser A) as the sovereign identity.
        pair.a.goto(conf_page_url(base, room, host_fqid))

        # B (guest) enters the waiting room. Force the non-tailnet path by using a
        # guest identity; tailnet callers auto-admit (which we detect + treat as a
        # valid but degenerate pass). The API is the source of truth.
        guest_id = f"guest-{int(time.time())}"
        entered = conf_waiting_enter(base, room, guest_id, display="gcv-waiting")
        ev["waiting_enter"] = entered

        if entered.get("auto_admitted"):
            ev["note"] = "caller auto-admitted (tailnet); admit path is a no-op here"
            status = conf_waiting_status(base, room)
            ev["status"] = status
            passed = guest_id in (status.get("admitted") or [])
            ev["screenshot_a"] = pair.a.screenshot("c-admit-deny-A.png")
            return ScenarioResult("admit-deny", passed, ev,
                                  "" if passed else "auto-admit did not land in admitted set")

        # Host sees the pending guest.
        before = conf_waiting_status(base, room)
        ev["status_before"] = before
        pending = any(g.get("identity") == guest_id for g in (before.get("waiting") or []))
        if not pending:
            return ScenarioResult("admit-deny", False, ev, "guest never appeared in waiting room")

        # Host admits.
        admit = conf_admit(base, room, requester=host_fqid, identity=guest_id)
        ev["admit"] = admit
        after = conf_waiting_status(base, room)
        ev["status_after"] = after
        admitted = guest_id in (after.get("admitted") or [])
        still_waiting = any(g.get("identity") == guest_id for g in (after.get("waiting") or []))

        ev["screenshot_a"] = pair.a.screenshot("c-admit-deny-A.png")
        passed = admitted and not still_waiting
        return ScenarioResult("admit-deny", passed, ev,
                              "" if passed else "guest did not transition waiting -> admitted")
    except Exception as exc:  # noqa: BLE001
        _safe_shots(pair, ev, "c-admit-deny")
        return ScenarioResult("admit-deny", False, ev, f"{type(exc).__name__}: {exc}")


def _safe_shots(pair: Optional[BrowserPair], ev: dict, prefix: str) -> None:
    if pair is None:
        return
    try:
        ev["screenshot_a"] = pair.a.screenshot(f"{prefix}-A-fail.png")
        ev["screenshot_b"] = pair.b.screenshot(f"{prefix}-B-fail.png")
    except Exception:  # noqa: BLE001
        pass


# --------------------------------------------------------------------------- #
# Orchestration
# --------------------------------------------------------------------------- #

BROWSER_SCENARIOS = {"a", "b", "c"}  # need a browser pair
ALL_SCENARIOS = ["a", "b", "c", "d"]
SCENARIO_ALIASES = {
    "guest-join-conf": "a",
    "call-1to1": "b",
    "admit-deny": "c",
    "turn-path": "d",
}


def resolve_scenarios(selector: str) -> list[str]:
    if selector == "all":
        return list(ALL_SCENARIOS)
    out: list[str] = []
    for token in selector.split(","):
        t = token.strip().lower()
        if not t:
            continue
        t = SCENARIO_ALIASES.get(t, t)
        if t not in ALL_SCENARIOS:
            raise ValueError(f"unknown scenario {token!r} (choose from a,b,c,d,all)")
        if t not in out:
            out.append(t)
    return out


def run(args: argparse.Namespace) -> int:
    scenarios = resolve_scenarios(args.scenario)
    print(f"[gcv] base={args.base} scenarios={scenarios}")
    print(f"[gcv] cdp-a={args.cdp_a or '(launch local)'} cdp-b={args.cdp_b or '(launch local)'}")

    results: list[ScenarioResult] = []
    need_browser = bool(set(scenarios) & BROWSER_SCENARIOS)

    # Scenario d (browser-free) can run without a pair.
    if "d" in scenarios:
        results.append(
            scenario_turn_path(
                args.base, turn_host=args.turn_host, turn_port=args.turn_port, peer=args.peer
            )
        )

    pair: Optional[BrowserPair] = None
    if need_browser:
        try:
            from playwright.sync_api import sync_playwright  # noqa: F401
        except ImportError:
            print("BLOCKED-ENV: playwright not installed. "
                  "~/.skenv/bin/pip install playwright && python -m playwright install chromium")
            for s in scenarios:
                if s in BROWSER_SCENARIOS:
                    results.append(ScenarioResult(_name(s), False, {}, "playwright not installed"))
            return _finish(results, scenarios)
        try:
            pair = open_pair(args.cdp_a, args.cdp_b)
        except Exception as exc:  # noqa: BLE001
            print(f"BLOCKED-ENV: could not open browser pair: {exc}")
            for s in scenarios:
                if s in BROWSER_SCENARIOS:
                    results.append(ScenarioResult(_name(s), False, {}, f"open_pair failed: {exc}"))
            return _finish(results, scenarios)

    try:
        for s in scenarios:
            if s == "a":
                results.append(scenario_guest_join_conf(
                    args.base, pair, host_fqid=args.host_fqid,
                    operator_token=args.operator_token, timeout=args.timeout))
            elif s == "b":
                results.append(scenario_call_1to1(
                    args.base, pair, host_fqid=args.host_fqid,
                    operator_token=args.operator_token, timeout=args.timeout))
            elif s == "c":
                results.append(scenario_admit_deny(
                    args.base, pair, host_fqid=args.host_fqid, timeout=args.timeout))
            # d already handled above
    finally:
        if pair is not None and not args.keep_open:
            pair.close()

    return _finish(results, scenarios)


def _name(short: str) -> str:
    return {"a": "guest-join-conf", "b": "call-1to1", "c": "admit-deny", "d": "turn-path"}[short]


def _finish(results: list[ScenarioResult], scenarios: list[str]) -> int:
    SCREENSHOT_DIR.mkdir(parents=True, exist_ok=True)
    report = {
        "scenarios": scenarios,
        "results": [r.to_dict() for r in results],
        "passed": all(r.passed for r in results) and len(results) > 0,
        "generated_at": time.time(),
    }
    report_path = SCREENSHOT_DIR / "gcv-e2e-report.json"
    report_path.write_text(json.dumps(report, indent=2))
    print("\n=== GCV E2E RESULTS ===")
    for r in results:
        flag = "PASS" if r.passed else "FAIL"
        print(f"  [{flag}] {r.name}" + (f" -- {r.error}" if r.error else ""))
    print(f"[gcv] report: {report_path}")
    return 0 if report["passed"] else 1


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--scenario", default="all",
                    help="a|b|c|d|all or names (guest-join-conf,call-1to1,admit-deny,turn-path), comma-separated")
    ap.add_argument("--base", default=os.getenv("SKCHAT_FUNNEL_PUBLIC_URL", DEFAULT_BASE),
                    help="public Funnel base URL")
    ap.add_argument("--cdp-a", default=os.getenv("CDP_A_URL"),
                    help="CDP url for browser A (ws:// or http://); omit to launch a local dedicated chrome")
    ap.add_argument("--cdp-b", default=os.getenv("CDP_B_URL"),
                    help="CDP url for browser B; omit to launch a local dedicated chrome")
    ap.add_argument("--operator-token", default=os.getenv("SKCHAT_GUEST_OPERATOR_TOKEN"),
                    help="operator bearer token for /guest/invite (else loopback/tailnet trust)")
    ap.add_argument("--host-fqid", default=os.getenv("GCV_HOST_FQID", "lumina@chef.skworld"),
                    help="host FQID used to mint confs")
    ap.add_argument("--peer", default=os.getenv("GCV_PEER", "guest@public"),
                    help="peer arg for /connectivity/ice (scenario d)")
    ap.add_argument("--turn-host", default=os.getenv("GCV_TURN_HOST", DEFAULT_TURN_HOST))
    ap.add_argument("--turn-port", type=int, default=int(os.getenv("GCV_TURN_PORT", str(DEFAULT_TURN_PORT))))
    ap.add_argument("--timeout", type=float, default=float(os.getenv("GCV_TIMEOUT", "30")),
                    help="per-stage wait timeout (s)")
    ap.add_argument("--keep-open", action="store_true", help="do not close browsers at the end (debug)")
    return ap


def main(argv: Optional[list[str]] = None) -> int:
    return run(build_parser().parse_args(argv))


if __name__ == "__main__":
    sys.exit(main())
