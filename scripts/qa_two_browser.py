#!/usr/bin/env python3
"""Headless two-browser-client QA: real-time LiveKit DATA-CHANNEL lane sync.

This is the one leg HTTP tests cannot cover. The HTTP lane store
(``/spaces/{id}/lanes/event`` -> ``LaneStore``) only proves *persistence +
late-joiner replay*. This test proves *real-time peer delivery*: a chat-lane
envelope published by client A over the LiveKit WebRTC data channel arrives at
client B's page in real time, routed through the page's own
``publishLane`` -> ``RoomEvent.DataReceived`` -> ``routeDataMessage`` ->
``onChatReceived`` path.

WHAT IT DOES
------------
1. Mints TWO LiveKit participant tokens for the SAME Space, via the real Space
   token flow: ``POST /spaces/{id}/join`` (returns ``{url, token, room, ...}``).
   If no live Space is given, it creates one (needs ``host_fqid``+creds) or
   falls back to the live "Town Hall" default.
2. Launches two headless Chromium contexts (fake audio device so the audio
   Space has a mic to publish) and opens the lane-capable page
   ``/livekit?room=<space_id>&identity=<id>&token=<space-token>`` in each.
   NB: the *Space* page (``/space/{id}``, ``space.html``) is audio-only and has
   NO data-channel lane JS. The data lanes (``publishLane`` /
   ``routeDataMessage`` / ``onChatReceived``) live in ``livekit.html``, which
   joins the SAME room (room name == space_id), so it exercises the exact lane
   wiring in the Space's real room.
3. Waits for BOTH pages to reach the LiveKit room "connected" state and to see
   each other (remoteParticipants count == 1).
4. Client A calls ``publishLane({lane:'chat', from:'A', text:'dc-hello-<n>',
   ts})`` over the data channel via ``page.evaluate``.
5. Client B is asserted to receive it in the chat DOM (``#chat-messages``)
   within the timeout -> proves real-time data-channel delivery.

ENVIRONMENT REQUIREMENTS (why it might be BLOCKED-ENV)
------------------------------------------------------
* webui reachable (default ``http://localhost:8765``).
* LiveKit SFU reachable from THIS box at the ``url`` the webui reports
  (e.g. ``wss://noroc2027.tail204f0c.ts.net:8443`` over the tailnet) with a
  TLS cert the browser trusts. If the SFU is down / cert untrusted / ICE can't
  complete from a headless container, the connect step times out -> the script
  reports the exact failure rather than faking a pass.
* Playwright + a FULL Chromium build (not just headless-shell — WebRTC needs
  the real media stack):
      ~/.skenv/bin/pip install playwright
      ~/.skenv/bin/python -m playwright install chromium
  Launched with ``--use-fake-device-for-media-stream`` +
  ``--use-fake-ui-for-media-stream`` so the audio Space gets a synthetic mic
  with no hardware / permission prompt.

USAGE
-----
    ~/.skenv/bin/python scripts/qa_two_browser.py \
        [--base http://localhost:8765] \
        [--space space-zvteyh73i6b6czb6] \
        [--create-host lumina@chef.skworld] \
        [--timeout 25]

Exit code 0 = PASS (B received A's data-channel message), non-zero = FAIL/BLOCKED.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
import urllib.request

# Full chromium (WebRTC) launch flags: synthetic audio device, auto-grant media.
CHROMIUM_ARGS = [
    "--use-fake-device-for-media-stream",
    "--use-fake-ui-for-media-stream",
    "--autoplay-policy=no-user-gesture-required",
    # headless WebRTC stability in containers/no-X
    "--disable-gpu",
    "--no-sandbox",
]


def _post(url: str, body: dict, timeout: int = 10) -> dict:
    data = json.dumps(body).encode()
    req = urllib.request.Request(
        url, data=data, headers={"Content-Type": "application/json"}, method="POST"
    )
    with urllib.request.urlopen(req, timeout=timeout) as r:  # noqa: S310 (local tailnet)
        return json.loads(r.read().decode())


def _get(url: str, timeout: int = 10) -> dict:
    with urllib.request.urlopen(url, timeout=timeout) as r:  # noqa: S310
        return json.loads(r.read().decode())


def resolve_space(base: str, space: str | None, create_host: str | None) -> str:
    """Return a live space_id, creating one if requested, else first live, else
    the well-known Town Hall default."""
    if space:
        return space
    try:
        spaces = _get(f"{base}/spaces").get("spaces", [])
    except Exception as exc:  # noqa: BLE001
        spaces = []
        print(f"[warn] could not list spaces: {exc}")
    live = [s for s in spaces if s.get("status") != "ended"]
    if create_host:
        slug = f"qa-dc-{int(time.time())}"
        out = _post(
            f"{base}/spaces/create",
            {"host_fqid": create_host, "title": "QA Data-Channel Test", "slug": slug},
        )
        print(f"[info] created space {out['space_id']}")
        return out["space_id"]
    if live:
        print(f"[info] using live space {live[0]['space_id']} ({live[0].get('title')})")
        return live[0]["space_id"]
    return "space-zvteyh73i6b6czb6"  # Town Hall default


def mint_token(base: str, space_id: str, identity: str) -> dict:
    """Real Space token flow: POST /spaces/{id}/join -> {url, token, room, ...}."""
    out = _post(
        f"{base}/spaces/{space_id}/join",
        {"identity": identity, "name": identity},
    )
    if "token" not in out or "url" not in out:
        raise RuntimeError(f"join did not return a token/url: {out}")
    return out


# JS evaluated in each page to report LiveKit room state to the test driver.
JS_ROOM_STATE = """
() => {
  try {
    const room = window.__skRoom;
    if (!room) return {state: 'no-room'};
    return {
      state: room.state,                                  // 'connected' etc.
      identity: room.localParticipant && room.localParticipant.identity,
      peers: room.remoteParticipants ? room.remoteParticipants.size : -1,
    };
  } catch (e) { return {state: 'err', err: String(e)}; }
}
"""


def wait_for(page, predicate_js, *, timeout: float, what: str):
    deadline = time.time() + timeout
    last = None
    while time.time() < deadline:
        last = page.evaluate(predicate_js)
        if last is True or (isinstance(last, dict) and last.get("_ok")):
            return last
        time.sleep(0.4)
    raise TimeoutError(f"timeout waiting for {what}; last={last}")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--base", default="http://localhost:8765")
    ap.add_argument("--space", default=None, help="space_id (else create/first-live/default)")
    ap.add_argument("--create-host", default=None, help="host_fqid to create a fresh Space")
    ap.add_argument("--timeout", type=float, default=25.0, help="per-stage timeout (s)")
    ap.add_argument("--headed", action="store_true", help="run with a visible browser (debug)")
    args = ap.parse_args()

    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        print("BLOCKED-ENV: playwright not installed. "
              "Run: ~/.skenv/bin/pip install playwright && "
              "~/.skenv/bin/python -m playwright install chromium")
        return 3

    space_id = resolve_space(args.base, args.space, args.create_host)
    print(f"[info] space_id = {space_id}")
    tok_a = mint_token(args.base, space_id, "qa-alice")
    tok_b = mint_token(args.base, space_id, "qa-bob")
    print(f"[info] SFU url = {tok_a['url']}")
    print(f"[info] minted tokens: alice({len(tok_a['token'])}B) bob({len(tok_b['token'])}B) "
          f"room={tok_a.get('room')}")

    def page_url(tok: dict, identity: str) -> str:
        from urllib.parse import quote
        return (f"{args.base}/livekit?room={quote(space_id)}"
                f"&identity={quote(identity)}&token={quote(tok['token'])}")

    marker = f"dc-hello-{int(time.time())}"
    result = {"connected": False, "saw_each_other": False, "delivered": False}

    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=not args.headed, args=CHROMIUM_ARGS)
        try:
            ctx_a = browser.new_context(permissions=["microphone"])
            ctx_b = browser.new_context(permissions=["microphone"])
            page_a = ctx_a.new_page()
            page_b = ctx_b.new_page()
            for tag, pg in (("A", page_a), ("B", page_b)):
                pg.on("console", lambda m, t=tag: (
                    print(f"  [page {t}] {m.text}") if any(
                        k in m.text for k in ("error", "fail", "disconnect", "connected"))
                    else None))

            page_a.goto(page_url(tok_a, "qa-alice"), wait_until="domcontentloaded")
            page_b.goto(page_url(tok_b, "qa-bob"), wait_until="domcontentloaded")

            # 1) both pages reach LiveKit 'connected'
            for tag, pg in (("A", page_a), ("B", page_b)):
                st = wait_for(
                    pg,
                    "() => { try { const room=window.__skRoom; return (room && "
                    "room.state==='connected') ? {_ok:true} : false; } "
                    "catch(e){ return false; } }",
                    timeout=args.timeout, what=f"page {tag} room connected")
                print(f"[ok] page {tag} connected: {pg.evaluate(JS_ROOM_STATE)}")
            result["connected"] = True

            # 2) both see each other (remoteParticipants == 1)
            for tag, pg in (("A", page_a), ("B", page_b)):
                wait_for(
                    pg,
                    "() => { try { const room=window.__skRoom; return (room && "
                    "room.remoteParticipants && room.remoteParticipants.size>=1) "
                    "? {_ok:true} : false; } catch(e){ return false; } }",
                    timeout=args.timeout, what=f"page {tag} sees peer")
            print("[ok] both contexts see each other (2 participants in room)")
            result["saw_each_other"] = True

            # 3) A publishes a chat-lane envelope over the DATA CHANNEL
            published = page_a.evaluate(
                "(text) => { try { window.__skPublishLane({lane:'chat', "
                "from:'qa-alice', text, ts: Date.now()}); return true; } "
                "catch(e){ return String(e); } }",
                marker)
            if published is not True:
                print(f"FAIL: publishLane threw: {published}")
                return 1
            print(f"[ok] A published over data channel: {marker!r}")

            # 4) B receives it in the chat DOM in real time
            recv = (
                "(needle) => { try {"
                " const els = document.querySelectorAll('#chat-messages .chat-msg');"
                " for (const e of els) if (e.textContent.includes(needle)) return {_ok:true};"
                " return false; } catch(e){ return false; } }"
            )
            got = None
            deadline = time.time() + args.timeout
            while time.time() < deadline:
                got = page_b.evaluate(recv, marker)
                if isinstance(got, dict) and got.get("_ok"):
                    break
                time.sleep(0.4)
            if isinstance(got, dict) and got.get("_ok"):
                result["delivered"] = True
                print(f"[PASS] B received A's data-channel chat message {marker!r} in real time")
            else:
                print(f"FAIL: B never received {marker!r} over the data channel")
        finally:
            browser.close()

    print("\n=== RESULT ===")
    print(json.dumps(result, indent=2))
    if result["delivered"]:
        print("STATUS: PASS — real-time data-channel lane sync verified between two clients.")
        return 0
    if result["connected"]:
        print("STATUS: FAIL — clients connected but data-channel message did not round-trip.")
        return 1
    print("STATUS: BLOCKED-ENV — clients could not connect to the SFU (see errors above).")
    return 2


if __name__ == "__main__":
    sys.exit(main())
