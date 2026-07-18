"""FastAPI routes for SK Spaces (S1: create/join/guest-join/list/end).

No SFU call at create time — LiveKit auto-creates the room when the host first
connects — so these routes are fully testable with a dummy key/secret.
"""

from __future__ import annotations

import hashlib
import logging
import os
import time
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse

from skchat.spaces.lanes import KNOWN_LANES, LaneDispatcher, LaneStore
from skchat.spaces.registry import SpaceRegistry
from skchat.spaces.roles import Role
from skchat.spaces.space import Space, derive_space_id
from skchat.spaces.tokens import mint_space_token

logger = logging.getLogger("skchat.spaces.routes")

_DEFAULT_TTL = int(os.getenv("SKCHAT_LIVEKIT_TOKEN_TTL", "21600"))

# VER: build-stamp placeholder in space.html, substituted with a short stable
# hash of the file's ORIGINAL bytes so an already-open tab can notice a newer
# deploy landed (no amount of server no-cache headers helps a tab that never
# reloads) and self-heal instead of silently running stale JS.
_SPACE_HTML_PLACEHOLDER = "__SPACE_BUILD__"


def _space_html_path() -> Path:
    """Path to the Space page HTML shell, resolved relative to this module."""
    return Path(__file__).resolve().parent.parent / "static" / "space.html"


def _compute_build_hash(raw: bytes) -> str:
    """Short stable build stamp: first 12 hex chars of a sha1 of the file bytes."""
    return hashlib.sha1(raw).hexdigest()[:12]


def render_space_html() -> tuple[str, str]:
    """Read space.html, hash the ORIGINAL bytes (stable per deploy, computed
    before any substitution), and replace the __SPACE_BUILD__ placeholder with
    that hash.

    Returns ``(html_text, build_hash)``. If the placeholder is not present the
    file is served unchanged (never crash), while the hash is still computed
    and returned so GET /spaces/build always agrees with what was served.
    """
    raw = _space_html_path().read_bytes()
    build_hash = _compute_build_hash(raw)
    text = raw.decode("utf-8")
    if _SPACE_HTML_PLACEHOLDER in text:
        text = text.replace(_SPACE_HTML_PLACEHOLDER, build_hash)
    return text, build_hash


def _url() -> str:
    return os.getenv("SKCHAT_LIVEKIT_URL", "ws://skworld-100:7880")


def _api_url() -> str:
    """Server-side RoomService/Twirp base URL for the Moderator and Recorder.

    SKCHAT_LIVEKIT_URL is browser-facing and, behind a path-based Funnel
    (e.g. wss://host/livekit-ws), makes the LiveKit SDK's Twirp client emit a
    malformed double-slash URL that a reverse proxy 404s. SKCHAT_LIVEKIT_API_URL
    is a dedicated plain host:port Twirp endpoint for server-side calls; when
    unset, fall back to SKCHAT_LIVEKIT_URL for backward compat."""
    return os.getenv("SKCHAT_LIVEKIT_API_URL", "").strip() or _url()


def _public_url(request: Request) -> str:
    """Public-aware SFU URL for a browser/guest: an off-tailnet caller (Tailscale
    Funnel on cellular) gets the public wss URL, a tailnet caller keeps the
    tailnet URL. Falls back to the tailnet default if the helper is unavailable.
    See livekit_routes.public_aware_livekit_url."""
    try:
        from skchat.livekit_routes import public_aware_livekit_url

        return public_aware_livekit_url(request)
    except Exception:  # pragma: no cover - defensive fallback
        return _url()


def _have_creds() -> bool:
    return bool(os.getenv("SKCHAT_LIVEKIT_API_KEY") and os.getenv("SKCHAT_LIVEKIT_API_SECRET"))


def _maybe_start_writeup(space_id: str, title: str) -> bool:
    """Opt-in completion hook: when a recording stops, kick off the
    transcript → write-up → chat-lane pipeline in a background thread.

    Disabled by default; enable with ``SKCHAT_SPACES_AUTO_WRITEUP=1``. Runs in a
    daemon thread (Whisper + LLM are slow) and never raises into the request
    path — a failure to start is logged and reported as ``False``.
    """
    if os.getenv("SKCHAT_SPACES_AUTO_WRITEUP", "").lower() not in ("1", "true", "yes"):
        return False
    try:
        import threading
        from pathlib import Path as _P

        from skchat.spaces.recording_writeup import RecordingWriteup

        audio = _P.home() / ".skchat" / "spaces-recordings" / f"{space_id}.ogg"

        def _run() -> None:
            try:
                RecordingWriteup().process(space_id, str(audio), title=title)
            except Exception as exc:  # noqa: BLE001
                logger.warning("auto write-up failed for %s: %s", space_id, exc)

        threading.Thread(target=_run, name=f"writeup-{space_id}", daemon=True).start()
        return True
    except Exception as exc:  # noqa: BLE001
        logger.warning("could not start auto write-up for %s: %s", space_id, exc)
        return False


def register_spaces_routes(
    app: FastAPI,
    *,
    registry: SpaceRegistry | None = None,
    moderator=None,
    consent=None,
    recorder=None,
    lane_store: "LaneStore | None" = None,
    skreach_executor=None,
    advertiser=None,
) -> None:
    reg = registry or SpaceRegistry()

    def _advertise_space(*, host_fqid: str, space_id: str, title: str) -> None:
        """C2 best-effort focus-advertise on space create — never fails create."""
        try:
            if advertiser is not None:
                advertiser(host_fqid=host_fqid, space_id=space_id, title=title)
                return
            from skchat.spaces.federation.advertise import advertise_space

            advertise_space(host_fqid=host_fqid, space_id=space_id, title=title)
        except Exception as exc:  # noqa: BLE001 - advertise must never fail create
            logger.warning("spaces: focus-advertise failed for %s: %s", space_id, exc)
    _mod_holder = {"mod": moderator}
    from skchat.spaces.consent import ConsentLedger

    led = consent or ConsentLedger()
    _rec_holder = {"rec": recorder}
    _lane_store = lane_store or LaneStore(db_path=Path.home() / ".skchat" / "lanes.db")
    _lane_dispatch = LaneDispatcher(store=_lane_store)
    # Injectable executor for the term-lane run route (tests pass a fake-runner
    # executor here; None → a default SkreachExecutor built per-request).
    _skreach_holder = {"ex": skreach_executor}

    def _moderator():
        if _mod_holder["mod"] is None:
            from skchat.spaces.moderation import Moderator

            _mod_holder["mod"] = Moderator(
                _api_url(),
                os.getenv("SKCHAT_LIVEKIT_API_KEY", ""),
                os.getenv("SKCHAT_LIVEKIT_API_SECRET", ""),
            )
        return _mod_holder["mod"]

    def _recorder():
        if _rec_holder["rec"] is None:
            from skchat.spaces.recording import Recorder

            _rec_holder["rec"] = Recorder(
                _api_url(),
                os.getenv("SKCHAT_LIVEKIT_API_KEY", ""),
                os.getenv("SKCHAT_LIVEKIT_API_SECRET", ""),
            )
        return _rec_holder["rec"]

    def _require_host(space, requester: str) -> None:
        if not space.host_fqid.strip() or requester != space.host_fqid:
            raise HTTPException(403, "host-only action")

    def _token_response(
        identity: str, name: str, role: Role, space: Space, request: Request
    ) -> dict:
        token = mint_space_token(identity, name, role, space.space_id, _DEFAULT_TTL)
        return {
            "space_id": space.space_id,
            "room": space.room,
            "url": _public_url(request),
            "identity": identity,
            "name": name,
            "role": role.value,
            "token": token,
            "title": space.title,
        }

    @app.post("/spaces/create")
    async def create_space(request: Request) -> JSONResponse:
        # SECURITY: S1/S2 trust the tailnet — host_fqid is asserted, not proven, so
        # this endpoint mints a roomAdmin token for whoever asks. Tailnet-only until
        # S5 sk-lk-authd verifies a capauth-signed operator assertion. Do NOT expose
        # this route publicly before that hardening lands.
        if not _have_creds():
            raise HTTPException(503, "livekit not configured")
        body = await request.json()
        host = (body.get("host_fqid") or "").strip()
        title = (body.get("title") or "").strip()
        slug = (body.get("slug") or "").strip()
        if not (host and title and slug):
            raise HTTPException(400, "host_fqid, title, slug required")
        # C1 defense-in-depth: cap title length to blunt a stored-XSS payload.
        if len(title) > 120:
            raise HTTPException(400, "title too long (max 120 chars)")
        sid = derive_space_id(host, slug)
        space = Space(space_id=sid, host_fqid=host, title=title, slug=slug, created_at=time.time())
        reg.add(space)
        # C2: advertise this instance as the SFU focus for the new Space.
        _advertise_space(host_fqid=host, space_id=space.space_id, title=space.title)
        return JSONResponse(_token_response(host, host.split("@")[0], Role.HOST, space, request))

    @app.post("/spaces/{space_id}/join")
    async def join_space(space_id: str, request: Request) -> JSONResponse:
        space = reg.get(space_id)
        if space is None or space.status.value == "ended":
            raise HTTPException(404, "space not found or ended")
        body = await request.json()
        identity = (body.get("identity") or "").strip()
        if not identity:
            raise HTTPException(400, "identity required")
        name = body.get("name") or identity.split("@")[0]
        return JSONResponse(_token_response(identity, name, Role.LISTENER, space, request))

    @app.post("/spaces/{space_id}/join-host")
    async def join_space_host(space_id: str, request: Request) -> JSONResponse:
        """Mint a HOST token (publish + roomAdmin) — only for the Space's host."""
        space = reg.get(space_id)
        if space is None or space.status.value == "ended":
            raise HTTPException(404, "space not found or ended")
        body = await request.json()
        requester = (body.get("requester") or "").strip()
        _require_host(space, requester)
        return JSONResponse(
            _token_response(requester, requester.split("@")[0], Role.HOST, space, request)
        )

    @app.post("/spaces/{space_id}/join-guest")
    async def join_space_guest(space_id: str, request: Request) -> JSONResponse:
        """Guest-link listener: verify a guest.py invite, then mint a LISTENER token."""
        space = reg.get(space_id)
        if space is None or space.status.value == "ended":
            raise HTTPException(404, "space not found or ended")
        body = await request.json()
        invite = (body.get("invite_token") or "").strip()
        display = (body.get("display") or "Guest").strip()
        if not invite:
            raise HTTPException(400, "invite_token required")
        from skchat.guest import GuestJoinError, InviteVerifier

        try:
            guest = InviteVerifier().verify(invite, expected_room=space_id, display_name=display)
        except GuestJoinError as exc:
            raise HTTPException(403, f"invalid invite: {exc}") from exc
        return JSONResponse(
            _token_response(guest.identity, guest.display or display, Role.LISTENER, space, request)
        )

    @app.get("/spaces")
    async def list_spaces() -> JSONResponse:
        return JSONResponse(
            {
                "spaces": [
                    {
                        "space_id": s.space_id,
                        "title": s.title,
                        "host_fqid": s.host_fqid,
                        "status": s.status.value,
                        "speakers": s.speakers,
                        "recording": s.recording,
                    }
                    for s in reg.live()
                ]
            }
        )

    @app.post("/spaces/{space_id}/end")
    async def end_space(space_id: str, request: Request) -> JSONResponse:
        space = reg.get(space_id)
        if space is None:
            raise HTTPException(404, "space not found")
        body = await request.json()
        _require_host(space, (body.get("requester") or "").strip())
        reg.end(space_id)
        return JSONResponse({"ok": True, "space_id": space_id})

    async def _on_promoted(space: Space, space_id: str, identity: str) -> None:
        """After a stage_action reports on_stage/can_publish, make the on-stage set
        server-authoritative. I3: if recording is active and the speaker has NOT
        consented, revert the promotion and 409 rather than capture them silently."""
        if space.recording and not led.has(space_id, identity):
            await _moderator().stage_action(space.room, identity, "remove")
            raise HTTPException(409, "consent required to speak while recording is active")
        reg.add_speaker(space_id, identity)

    @app.post("/spaces/{space_id}/raise-hand")
    async def raise_hand(space_id: str, request: Request) -> JSONResponse:
        space = reg.get(space_id)
        if space is None:
            raise HTTPException(404, "space not found")
        body = await request.json()
        identity = (body.get("identity") or "").strip()
        if not identity:
            raise HTTPException(400, "identity required")
        on_stage = await _moderator().stage_action(space.room, identity, "raise_hand")
        if on_stage:
            await _on_promoted(space, space_id, identity)
        return JSONResponse({"ok": True, "on_stage": on_stage})

    @app.post("/spaces/{space_id}/invite")
    async def invite(space_id: str, request: Request) -> JSONResponse:
        space = reg.get(space_id)
        if space is None:
            raise HTTPException(404, "space not found")
        body = await request.json()
        _require_host(space, (body.get("requester") or "").strip())
        identity = (body.get("identity") or "").strip()
        if not identity:
            raise HTTPException(400, "identity required")
        on_stage = await _moderator().stage_action(space.room, identity, "invite")
        if on_stage:
            await _on_promoted(space, space_id, identity)
        return JSONResponse({"ok": True, "on_stage": on_stage})

    @app.post("/spaces/{space_id}/remove-from-stage")
    async def remove_from_stage(space_id: str, request: Request) -> JSONResponse:
        space = reg.get(space_id)
        if space is None:
            raise HTTPException(404, "space not found")
        body = await request.json()
        requester = (body.get("requester") or "").strip()
        identity = (body.get("identity") or "").strip()
        # host OR self may remove from stage
        if requester != space.host_fqid and requester != identity:
            raise HTTPException(403, "host-or-self only")
        await _moderator().stage_action(space.room, identity, "remove")
        reg.remove_speaker(space_id, identity)
        return JSONResponse({"ok": True})

    @app.post("/spaces/{space_id}/mute")
    async def mute(space_id: str, request: Request) -> JSONResponse:
        space = reg.get(space_id)
        if space is None:
            raise HTTPException(404, "space not found")
        body = await request.json()
        _require_host(space, (body.get("requester") or "").strip())
        await _moderator().mute(
            space.room, (body.get("identity") or "").strip(), (body.get("track_sid") or "").strip()
        )
        return JSONResponse({"ok": True})

    @app.post("/spaces/{space_id}/kick")
    async def kick(space_id: str, request: Request) -> JSONResponse:
        space = reg.get(space_id)
        if space is None:
            raise HTTPException(404, "space not found")
        body = await request.json()
        _require_host(space, (body.get("requester") or "").strip())
        await _moderator().kick(space.room, (body.get("identity") or "").strip())
        return JSONResponse({"ok": True})

    @app.post("/spaces/{space_id}/consent")
    async def record_consent(space_id: str, request: Request) -> JSONResponse:
        if reg.get(space_id) is None:
            raise HTTPException(404, "space not found")
        body = await request.json()
        identity = (body.get("identity") or "").strip()
        if not identity:
            raise HTTPException(400, "identity required")
        led.add(space_id, identity)
        return JSONResponse({"ok": True})

    @app.post("/spaces/{space_id}/lanes/event")
    async def lanes_event(space_id: str, request: Request) -> JSONResponse:
        body = await request.json()
        try:
            _lane_dispatch.dispatch(space_id, body)
        except ValueError as exc:
            return JSONResponse({"error": str(exc)}, status_code=400)
        return JSONResponse({"ok": True})

    @app.get("/spaces/{space_id}/lanes/{lane}/state")
    async def lanes_state(space_id: str, lane: str) -> JSONResponse:
        if lane not in KNOWN_LANES:
            return JSONResponse({"error": f"unknown lane {lane!r}"}, status_code=400)
        return JSONResponse({"events": _lane_store.replay(space_id, lane)})

    @app.post("/spaces/{space_id}/lanes/term/run")
    async def lanes_term_run(space_id: str, request: Request) -> JSONResponse:
        """Run a term-lane command through the sandboxed SkreachExecutor.

        Gated by default: returns an ``exec_disabled`` event unless
        ``SKREACHD_ENABLED`` is set. The existing ``lanes/event`` route does NOT
        auto-execute — execution only happens through this explicit route.
        """
        from skchat.spaces.skreachd import SkreachExecutor

        body = await request.json()
        if not isinstance(body, dict):
            return JSONResponse({"error": "body must be an object"}, status_code=400)
        cmd = body.get("cmd")
        if not isinstance(cmd, str):
            return JSONResponse({"error": "missing 'cmd' string"}, status_code=400)
        cmd_id = body.get("id", "")
        identity = body.get("from", "")

        executor = _skreach_holder.get("ex") or SkreachExecutor()
        events = executor.run(cmd, identity=identity, cmd_id=cmd_id)
        return JSONResponse({"events": events})

    @app.post("/spaces/{space_id}/record/start")
    async def record_start(space_id: str, request: Request) -> JSONResponse:
        from pathlib import Path as _P

        from skchat.spaces.consent import can_record

        space = reg.get(space_id)
        if space is None:
            raise HTTPException(404, "space not found")
        body = await request.json()
        _require_host(space, (body.get("requester") or "").strip())
        # I1/I2: gate on the server-authoritative on-stage set, NOT the request
        # body (a client could omit/forge `speakers` to bypass consent).
        ok, missing = can_record(space.speakers, space_id, led)
        if not ok:
            return JSONResponse({"ok": False, "missing_consent": missing}, status_code=409)
        rec_dir = _P.home() / ".skchat" / "spaces-recordings"
        rec_dir.mkdir(parents=True, exist_ok=True)
        filepath = str(rec_dir / f"{space_id}.ogg")
        egress_id = await _recorder().start(space.room, filepath)
        reg.set_recording(space_id, True, egress_id)
        return JSONResponse({"ok": True, "egress_id": egress_id, "path": filepath})

    @app.post("/spaces/{space_id}/record/stop")
    async def record_stop(space_id: str, request: Request) -> JSONResponse:
        space = reg.get(space_id)
        if space is None:
            raise HTTPException(404, "space not found")
        body = await request.json()
        _require_host(space, (body.get("requester") or "").strip())
        if space.egress_id:
            await _recorder().stop(space.egress_id)
        reg.set_recording(space_id, False, "")
        writeup_started = _maybe_start_writeup(space_id, space.title)
        return JSONResponse({"ok": True, "writeup_started": writeup_started})

    @app.post("/sfu/get")
    async def sfu_get(request: Request) -> JSONResponse:
        """sk-lk-authd: verify a capauth-signed FQID assertion (the {claim, sig}
        body), apply the local trust policy, and mint a LiveKit token for THIS
        host's SFU. The authorize() logic + crypto seams are unit-tested in
        test_fed_authd.py; this route is thin (parse + 400/403 mapping)."""
        from skchat.spaces.federation.assertion import (
            AssertionError as FedAssertionError,
        )
        from skchat.spaces.federation.authd import AuthDenied, authorize

        try:
            signed = await request.json()
        except Exception as exc:  # malformed / non-JSON body
            raise HTTPException(400, "malformed body: expected JSON") from exc
        if not isinstance(signed, dict) or "claim" not in signed or "sig" not in signed:
            raise HTTPException(400, "body must be {claim, sig}")

        def _space_live(sid: str) -> bool:
            s = reg.get(sid)
            return s is not None and s.status.value != "ended"

        try:
            out = authorize(signed, sfu_ws_url=_public_url(request), _space_live=_space_live)
        except AuthDenied as exc:
            raise HTTPException(403, str(exc)) from exc
        except FedAssertionError as exc:
            raise HTTPException(403, f"assertion rejected: {exc}") from exc
        return JSONResponse(out)

    @app.get("/sfu/candidates")
    async def sfu_candidates() -> JSONResponse:
        """Federation discovery: list advertised SFU focus hosts for this realm.

        Returns ``{hosts: [{fqid, auth_url, sfu_ws_url}]}`` built from the focus
        descriptors advertised on the configured Nostr relays. This is a
        best-effort, never-fatal endpoint: any relay/parse failure yields an
        EMPTY list rather than a 500, so a client can always poll it safely.
        """
        hosts: list[dict] = []
        try:
            from skchat.spaces.federation.events import FOCUS_KIND, parse_focus_descriptor
            from skchat.spaces.federation.nostr_io import FederationNostr

            relays = [r for r in os.getenv("SKCHAT_NOSTR_RELAYS", "").split(",") if r.strip()]
            if relays:
                nostr = FederationNostr(relays=relays)
                seen: set[str] = set()
                for ev in nostr._query({"kinds": [FOCUS_KIND]}):
                    try:
                        d = parse_focus_descriptor(ev)
                    except Exception:  # noqa: BLE001 - hostile/malformed relay event
                        continue
                    fqid = (d.get("host_fqid") or "").strip()
                    auth_url = (d.get("auth_url") or "").strip()
                    sfu_ws_url = (d.get("sfu_ws_url") or "").strip()
                    if not (fqid and auth_url and sfu_ws_url) or fqid in seen:
                        continue
                    seen.add(fqid)
                    hosts.append({"fqid": fqid, "auth_url": auth_url, "sfu_ws_url": sfu_ws_url})
        except Exception as exc:  # noqa: BLE001 - discovery is best-effort, never 500
            logger.warning("sfu/candidates discovery failed: %s", exc)
            hosts = []
        return JSONResponse({"hosts": hosts})

    # These HTML shells carry the live client JS. They must never be cached by a
    # browser, or a phone that loaded a Space before a deploy keeps running stale
    # JS (this is what hid the promotion unmute button from a guest after the
    # invited-banner fix shipped). Assets are versioned via query strings; the
    # shell itself is always revalidated.
    _no_cache_headers = {"Cache-Control": "no-cache, no-store, must-revalidate"}

    @app.get("/spaces/live", response_class=HTMLResponse)
    async def spaces_directory() -> HTMLResponse:
        static = Path(__file__).resolve().parent.parent / "static" / "spaces.html"
        if static.exists():
            return FileResponse(static, media_type="text/html", headers=_no_cache_headers)
        return HTMLResponse("spaces.html missing", status_code=500)

    @app.get("/space/{space_id}", response_class=HTMLResponse)
    async def space_page(space_id: str) -> HTMLResponse:  # noqa: ARG001
        static = _space_html_path()
        if not static.exists():
            return HTMLResponse("space.html missing", status_code=500)
        html, _build_hash = render_space_html()
        return HTMLResponse(html, headers=_no_cache_headers)

    @app.get("/spaces/build")
    async def spaces_build() -> JSONResponse:
        """VER: cheap version endpoint the open Space tab polls to notice a
        newer build deployed while it was sitting open. Same hash the shell
        was (or would be) served with, computed from the current file bytes."""
        static = _space_html_path()
        if not static.exists():
            return JSONResponse({"build": ""})
        _html, build_hash = render_space_html()
        return JSONResponse({"build": build_hash})
