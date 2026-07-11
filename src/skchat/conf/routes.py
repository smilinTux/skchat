"""FastAPI routes for Sovereign Conf Calls (multi-party VIDEO conferences).

Mirrors ``skchat.spaces.routes`` in structure — host-gating via ``_require_host``,
the ``_have_creds`` dummy-cred-testable pattern, and the lazy ``LiveKitAPI`` /
``_service`` roster client borrowed from ``spaces.moderation.Moderator``.

A ``Conf`` is the video sibling of an audio ``Space``: everyone may publish
camera + mic + screenshare (up to ``participant_cap``). Token minting flows
through :func:`skchat.spaces.tokens.mint_conf_token`, which routes every grant
through the single ``conf_grant_for`` factory so a guest can never be
over-granted admin.

No SFU call happens at create/token time — LiveKit auto-creates the room when the
first participant connects — so create → token → end is fully testable with a
dummy key/secret. Only the live roster (``/conf/{room}/participants``) and the
optional room teardown on ``/conf/{room}/end`` touch the SFU, and both degrade
gracefully (empty / best-effort) when creds are absent or the SFU is unreachable.
"""

from __future__ import annotations

import logging
import os
import re
import shutil
import subprocess
import time
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse

from skchat.conf.room import Conf, ConfRegistry, PendingGuest
from skchat.spaces.roles import ConfRole
from skchat.spaces.tokens import mint_conf_token

logger = logging.getLogger("skchat.conf.routes")

_DEFAULT_TTL = int(os.getenv("SKCHAT_LIVEKIT_TOKEN_TTL", "21600"))

_MAX_FEDERATION_AGE = 300

_NONCE_CACHE: dict = {}



def _check_fed_nonce(fqid: str, nonce: str) -> bool:
    key = f"{fqid}::{nonce}"
    now = time.time()
    old = [k for k, v in _NONCE_CACHE.items() if now - v > _MAX_FEDERATION_AGE]
    for k in old:
        _NONCE_CACHE.pop(k, None)
    if key in _NONCE_CACHE:
        return False
    _NONCE_CACHE[key] = now
    return True

# --- On-demand Lumina conf-agent (pull the AI into THIS room) ----------------
# The resident single-room agent is ``skchat-lumina-call.service``; this is the
# transient "join THIS conf room" path. We spawn the SAME agent script
# (~/lumina-creative/scripts/lumina-call.py — a DIFFERENT repo, never modified
# here) into the requested room via ``systemd-run --user --scope``, so it is a
# supervised, resource-scoped, individually-stoppable unit. The agent self-mints
# its LiveKit token over the loopback ``WEBUI_URL/livekit/token`` gate.

_DEFAULT_LUMINA_CALL = str(
    Path.home() / "clawd" / "skcapstone-repos" / "lumina-creative" / "scripts" / "lumina-call.py"
)


def _lumina_call_script() -> str:
    """Absolute path to lumina-call.py (override via ``SKCHAT_LUMINA_CALL_SCRIPT``)."""
    return os.getenv("SKCHAT_LUMINA_CALL_SCRIPT", _DEFAULT_LUMINA_CALL)


def _agent_python() -> str:
    """Python interpreter used to run the agent (override via ``SKCHAT_AGENT_PYTHON``)."""
    return os.getenv("SKCHAT_AGENT_PYTHON", str(Path.home() / ".skenv" / "bin" / "python"))


def _sanitize_unit(room: str) -> str:
    """Sanitize ``room`` into the alnum/dash slug used in the systemd unit name.

    Injection guard: the room string lands inside a ``--unit=`` argument, so we
    strip everything but ``[A-Za-z0-9-]`` (collapsing runs to a single dash).
    """
    slug = re.sub(r"[^A-Za-z0-9-]+", "-", room).strip("-")
    return slug or "room"


def _agent_unit(room: str) -> str:
    """The systemd ``--scope`` unit name for the conf-agent of ``room``."""
    return f"lumina-conf-{_sanitize_unit(room)}"


def _default_runner(cmd: list[str]) -> subprocess.CompletedProcess:
    """Default command runner — actually invokes the process.
    
    Tests inject their own runner (no real spawn). Raises ``FileNotFoundError``
    if ``cmd[0]`` is missing, which callers translate into a graceful 503.
    """
    return subprocess.run(cmd, capture_output=True, text=True, timeout=20, check=False)


def _agent_runner(cmd: list[str]) -> subprocess.CompletedProcess:
    """Runner for long-lived agent processes — fire-and-forget, check start only."""
    proc = subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        proc.wait(timeout=5)
    except subprocess.TimeoutExpired:
        pass
    return proc


def _url() -> str:
    return os.getenv("SKCHAT_LIVEKIT_URL", "ws://skworld-100:7880")


def _public_url(request: Request) -> str:
    try:
        from skchat.livekit_routes import public_aware_livekit_url
        return public_aware_livekit_url(request)
    except Exception:
        return _url()


def _http_url(ws_url: str) -> str:
    return ws_url.replace("ws://", "http://").replace("wss://", "https://")


def _have_creds() -> bool:
    return bool(os.getenv("SKCHAT_LIVEKIT_API_KEY") and os.getenv("SKCHAT_LIVEKIT_API_SECRET"))


_PRIVATE_PREFIXES = (
    "127.", "10.", "192.168.", "100.", "::1", "fd",
)


def _client_ip(request: Request) -> str:
    forwarded = request.headers.get("x-forwarded-for", "").strip()
    if forwarded:
        return forwarded.split(",")[0].strip()
    client = getattr(request, "client", None)
    return getattr(client, "host", "") if client else ""


def _is_tailnet(request: Request) -> bool:
    ip = _client_ip(request)
    return any(ip.startswith(p) for p in _PRIVATE_PREFIXES) if ip else False


def register_conf_routes(
    app: FastAPI,
    *,
    registry: ConfRegistry | None = None,
    room_service=None,
    runner=None,
    advertiser=None,
) -> None:
    """Wire the conference REST API onto ``app``.

    ``registry`` and ``room_service`` are injectable for tests; in production a
    default ``ConfRegistry`` (``~/.skchat/confs.json``) is used and the LiveKit
    ``RoomService`` is built lazily from the env creds on first roster/teardown.

    ``runner`` is the systemd-run/systemctl command runner for the on-demand
    conf-agent routes; it defaults to :func:`_default_runner` (a real
    ``subprocess.run``) and is injected by tests so no process is ever spawned.

    ``advertiser`` is the C2 federation focus-advertise callable invoked on
    ``/conf/create`` to publish this room's focus descriptor + membership to the
    Nostr relay(s). It defaults to :func:`skchat.spaces.federation.advertise.
    advertise_conf` and is injected by tests (a fake recorder) so create-time
    advertising is verified without touching a relay.
    """
    reg = registry or ConfRegistry()
    run_cmd = runner or _default_runner
    # The long-lived agent spawn honors the SAME injected runner (tests must
    # never spawn a real systemd scope); only the production default differs
    # (fire-and-forget Popen instead of a blocking subprocess.run).
    agent_run = runner or _agent_runner

    def _advertise_conf(*, host_fqid: str, room: str, title: str) -> None:
        """Best-effort focus-advertise on conf create — never fails the create."""
        try:
            if advertiser is not None:
                advertiser(host_fqid=host_fqid, room=room, title=title)
                return
            from skchat.spaces.federation.advertise import advertise_conf

            advertise_conf(host_fqid=host_fqid, room=room, title=title)
        except Exception as exc:  # noqa: BLE001 - advertise must never fail create
            logger.warning("conf: focus-advertise failed for %s: %s", room, exc)
    # Lazy LiveKit room-service client (mirrors Moderator._service): only built
    # when a route actually needs the live SFU, and only if creds are present.
    _svc_holder = {"svc": room_service}

    def _service():
        """Return a LiveKit RoomService, or None if creds are missing / livekit
        is not installed. Never raises — callers degrade gracefully on None."""
        if _svc_holder["svc"] is not None:
            return _svc_holder["svc"]
        if not _have_creds():
            return None
        try:
            from livekit import api

            client = api.LiveKitAPI(
                _http_url(_url()),
                os.getenv("SKCHAT_LIVEKIT_API_KEY", ""),
                os.getenv("SKCHAT_LIVEKIT_API_SECRET", ""),
            )
            _svc_holder["svc"] = client.room
        except Exception as exc:  # noqa: BLE001 - missing livekit / bad creds → degrade
            logger.warning("conf: could not build LiveKit room service: %s", exc)
            return None
        return _svc_holder["svc"]

    def _require_host(conf: Conf, requester: str) -> None:
        if not conf.host_fqid.strip() or requester != conf.host_fqid:
            raise HTTPException(403, "host-only action")

    def _join_url(room: str) -> str:
        return f"/conf/{room}"

    def _stop_agent_unit(room: str) -> bool:
        """Best-effort stop of BOTH the local + federated conf-agent units.

        ``systemctl --user stop <unit>.scope`` for ``lumina-conf-<room>`` (local
        invite) and ``lumina-fedconf-<room>`` (federated invite). Idempotent and
        never raises — stopping an absent unit is a no-op.
        """
        from skchat.conf.fed_agent import fed_agent_unit

        units = [_agent_unit(room), fed_agent_unit(room)]
        ok = False
        for unit in units:
            try:
                run_cmd(["systemctl", "--user", "stop", f"{unit}.scope"])
                ok = True
            except Exception as exc:  # noqa: BLE001 - teardown is best-effort
                logger.warning("conf: could not stop agent unit %s.scope: %s", unit, exc)
        return ok

    @app.post("/conf/create")
    async def create_conf(request: Request) -> JSONResponse:
        # SECURITY: like /spaces/create, this trusts the tailnet — host_fqid is
        # asserted, not proven, so a SOVEREIGN (optionally room_admin) token is
        # minted for whoever asks. Tailnet-only until the identity epic verifies a
        # capauth-signed operator assertion through the /conf/{room}/token seam.
        # Do NOT expose this route publicly before that hardening lands.
        if not _have_creds():
            raise HTTPException(503, "livekit not configured")
        body = await request.json()
        host = (body.get("host_fqid") or "").strip()
        title = (body.get("title") or "").strip()
        # slug is optional: omitted → ad-hoc "new meeting" room (random suffix);
        # given → deterministic, re-derivable named room.
        raw_slug = body.get("slug")
        slug = raw_slug.strip() if isinstance(raw_slug, str) else None
        if not (host and title):
            raise HTTPException(400, "host_fqid and title required")
        # C1 defense-in-depth: cap title length to blunt a stored-XSS payload.
        if len(title) > 120:
            raise HTTPException(400, "title too long (max 120 chars)")
        conf = reg.create(host, title, slug=slug or None)
        # C2: advertise this instance as the SFU focus for the new room so a
        # federated peer can discover + elect it (best-effort, never fatal).
        _advertise_conf(host_fqid=host, room=conf.room, title=conf.title)
        # Host is SOVEREIGN with room_admin so it can moderate / tear down.
        token = mint_conf_token(
            host,
            host.split("@")[0],
            ConfRole.SOVEREIGN,
            conf.room,
            _DEFAULT_TTL,
            sovereign_admin=True,
        )
        return JSONResponse(
            {
                "conf_id": conf.conf_id,
                "room": conf.room,
                "url": _public_url(request),
                "identity": host,
                "name": host.split("@")[0],
                "role": ConfRole.SOVEREIGN.value,
                "token": token,
                "title": conf.title,
                "join_url": _join_url(conf.room),
            }
        )

    @app.post("/conf/{room}/token")
    async def conf_token(room: str, request: Request) -> JSONResponse:
        """Mint a role-scoped conference token for ``room`` (default PARTICIPANT).

        This is the SINGLE join contract: the identity epic plugs sovereign-vs-
        guest decisioning in here (validate a signed assertion → choose role /
        sovereign_admin) without touching create/end. Keep the seam clean —
        every conf join, human or agent, flows through this one route.
        """
        conf = reg.get(room)
        if conf is None or conf.status.value == "ended":
            raise HTTPException(404, "conf not found or ended")
        body = await request.json()
        identity = (body.get("identity") or "").strip()
        if not identity:
            raise HTTPException(400, "identity required")
        name = (body.get("name") or "").strip() or identity.split("@")[0]
        role_raw = (body.get("role") or ConfRole.PARTICIPANT.value).strip()
        try:
            role = ConfRole(role_raw)
        except ValueError as exc:
            raise HTTPException(400, f"unknown conf role: {role_raw!r}") from exc
        token = mint_conf_token(identity, name, role, conf.room, _DEFAULT_TTL)
        return JSONResponse(
            {
                "conf_id": conf.conf_id,
                "room": conf.room,
                "url": _public_url(request),
                "identity": identity,
                "name": name,
                "role": role.value,
                "token": token,
                "title": conf.title,
            }
        )

    @app.post("/conf/{room}/invite-agent")
    async def invite_agent(room: str, request: Request) -> JSONResponse:
        """Pull the Lumina AI agent into ``room``.

        Launches ``lumina-call.py --room <room> --greet "<greeting>"`` as a
        transient, supervised, resource-scoped systemd ``--scope`` unit
        (``lumina-conf-<sanitized-room>``). The room name is sanitized into the
        unit name (alnum/dash only) to prevent argument injection. Degrades
        gracefully (503 + clear error) if ``systemd-run`` is unavailable.

        Two room kinds are served through the SAME contract so the app can pull
        Lumina into whatever call it is already in:

        * A REGISTERED conference (``reg.get(room)`` live): host-gated, exactly
          as before (only the conf host may invite).
        * A PLAIN call room (a 1:1 / group ``sk-room-...`` the app connected to
          via ``/call/*`` or ``/livekit/token``) that is NOT in the conf
          registry: there is no conf host to gate on, so this path is restricted
          to tailnet callers (the same trust model ``call_routes`` uses for
          ``/call/start`` and ``/connectivity/ice``). Off-tailnet callers still
          get the original clean 404 so nothing new is exposed over the Funnel.
        """
        body = await request.json()
        conf = reg.get(room)
        if conf is not None and conf.status.value != "ended":
            # Registered conf: host-gated (unchanged contract).
            _require_host(conf, (body.get("requester") or "").strip())
            target_room = conf.room
            default_greet = "Lumina here, joining the conference."
        else:
            # Not a registered conf: allow pulling Lumina into an ad-hoc CALL
            # room, but only for a genuine tailnet caller (see docstring).
            if not _is_tailnet(request):
                raise HTTPException(404, "conf not found or ended")
            target_room = room
            default_greet = "Lumina here, joining the call."
        greeting = (body.get("greet") or default_greet).strip()
        # C-defense: cap greeting length (it is an arg, sanitized, but keep it sane).
        if len(greeting) > 500:
            raise HTTPException(400, "greeting too long (max 500 chars)")

        # Fail clearly (not 5xx-crash) if the spawning mechanism is unavailable.
        if shutil.which("systemd-run") is None:
            raise HTTPException(503, "systemd-run unavailable; cannot launch conf agent")

        unit = _agent_unit(target_room)
        cmd = [
            "systemd-run",
            "--user",
            "--scope",
            f"--unit={unit}",
            "--property=MemoryMax=2G",
            "--property=CPUQuota=200%",
            "-E", f"SKCHAT_WEBUI_URL=http://127.0.0.1:{os.getenv('SKCHAT_PORT', '8765')}",
            "-E", f"SKCHAT_LIVEKIT_API_KEY={os.getenv('SKCHAT_LIVEKIT_API_KEY', '')}",
            "-E", f"SKCHAT_LIVEKIT_API_SECRET={os.getenv('SKCHAT_LIVEKIT_API_SECRET', '')}",
            _agent_python(),
            _lumina_call_script(),
            "--room",
            target_room,
            "--greet",
            greeting,
        ]
        try:
            proc = agent_run(cmd)
        except FileNotFoundError as exc:
            raise HTTPException(503, "systemd-run unavailable; cannot launch conf agent") from exc
        except Exception as exc:  # noqa: BLE001 - any spawn failure -> graceful error
            logger.warning("conf: invite-agent spawn failed for %s: %s", target_room, exc)
            raise HTTPException(500, f"failed to launch conf agent: {exc}") from exc

        rc = getattr(proc, "returncode", 0)
        if rc not in (0, None):
            stderr = (getattr(proc, "stderr", "") or "").strip()
            logger.warning("conf: systemd-run rc=%s for %s: %s", rc, target_room, stderr)
            raise HTTPException(502, f"systemd-run failed (rc={rc}): {stderr}")
        return JSONResponse({"ok": True, "unit": unit, "room": target_room})

    @app.post("/conf/{room}/invite-agent-federated")
    async def invite_agent_federated(room: str, request: Request) -> JSONResponse:
        """Pull the AI agent into a REMOTE-hosted conf ``room`` (federated join).

        Unlike ``/conf/{room}/invite-agent`` (which spawns the agent into a
        room hosted on THIS instance with a local token), this discovers the
        elected SFU focus for ``room`` (or uses the supplied ``host`` auth_url),
        mints a cross-realm token, and spawns the SAME agent media stack against
        the REMOTE SFU. The room need NOT exist in this instance's registry.

        Body: ``{requester, host?, fqid?, greet?}``. ``requester`` is required
        and (when the room is local) host-gated; for a purely remote room there
        is no local host to gate against, so any tailnet caller may invite —
        the cross-realm trust gate is enforced at the REMOTE authd.

        Degrades gracefully: missing ``systemd-run`` → 503; discovery/trust
        failure → 502 with a clear reason (never an unhandled 5xx crash).
        """
        body = await request.json()
        requester = (body.get("requester") or "").strip()
        if not requester:
            raise HTTPException(400, "requester required")
        host = (body.get("host") or "").strip() or None
        fqid = (body.get("fqid") or "").strip() or None
        greeting = (
            body.get("greet") or "Lumina here — joining the federated conference."
        ).strip()
        if len(greeting) > 500:
            raise HTTPException(400, "greeting too long (max 500 chars)")

        # If the room IS local, keep the existing host-gate (only its host may
        # invite). If it is purely remote (not in our registry), there is no
        # local host to gate on — the remote authd is the trust boundary.
        local = reg.get(room)
        if local is not None and local.status.value != "ended":
            _require_host(local, requester)

        if shutil.which("systemd-run") is None:
            raise HTTPException(503, "systemd-run unavailable; cannot launch conf agent")

        from skchat.conf.fed_agent import FederatedAgentJoinError, federated_agent_join

        try:
            result = federated_agent_join(room, host=host, fqid=fqid, greet=greeting)
        except FederatedAgentJoinError as exc:
            raise HTTPException(502, f"federated join failed: {exc}") from exc
        except Exception as exc:  # noqa: BLE001 - mint/auth/spawn failure → graceful
            logger.warning("conf: invite-agent-federated failed for %s: %s", room, exc)
            raise HTTPException(502, f"federated join failed: {exc}") from exc
        return JSONResponse(result)

    @app.post("/conf/{room}/remove-agent")
    async def remove_agent(room: str, request: Request) -> JSONResponse:
        """Stop the Lumina conf-agent for ``room`` (host-gated). Idempotent."""
        conf = reg.get(room)
        if conf is None:
            raise HTTPException(404, "conf not found")
        body = await request.json()
        _require_host(conf, (body.get("requester") or "").strip())
        unit = _agent_unit(conf.room)
        stopped = _stop_agent_unit(conf.room)
        return JSONResponse({"ok": True, "unit": unit, "room": conf.room, "stopped": stopped})

    @app.get("/conf/{room}/participants")
    async def conf_participants(room: str) -> JSONResponse:
        """LIVE roster from the SFU. Degrades gracefully: no creds / no livekit /
        unreachable SFU → an empty list + ``live=false`` rather than a 5xx."""
        conf = reg.get(room)
        if conf is None:
            raise HTTPException(404, "conf not found")
        svc = _service()
        if svc is None:
            return JSONResponse({"room": room, "participants": [], "live": False})
        try:
            from livekit import api

            resp = await svc.list_participants(api.ListParticipantsRequest(room=conf.room))
            participants = [
                {
                    "identity": p.identity,
                    "name": getattr(p, "name", "") or "",
                    "state": getattr(
                        getattr(p, "state", None), "name", str(getattr(p, "state", ""))
                    ),
                    "joined_at": getattr(p, "joined_at", 0),
                }
                for p in resp.participants
            ]
        except Exception as exc:  # noqa: BLE001 - roster is best-effort, never 500
            logger.warning("conf: list_participants failed for %s: %s", conf.room, exc)
            return JSONResponse({"room": room, "participants": [], "live": False})
        return JSONResponse({"room": room, "participants": participants, "live": True})

    @app.post("/conf/{room}/end")
    async def end_conf(room: str, request: Request) -> JSONResponse:
        """Mark the Conf ended (host-gated) and best-effort delete the SFU room."""
        conf = reg.get(room)
        if conf is None:
            raise HTTPException(404, "conf not found")
        body = await request.json()
        _require_host(conf, (body.get("requester") or "").strip())
        reg.end(room)
        # Best-effort stop any on-demand Lumina conf-agent for this room.
        agent_stopped = _stop_agent_unit(conf.room)
        room_deleted = False
        svc = _service()
        if svc is not None:
            try:
                from livekit import api

                await svc.delete_room(api.DeleteRoomRequest(room=conf.room))
                room_deleted = True
            except Exception as exc:  # noqa: BLE001 - teardown is best-effort
                logger.warning("conf: delete_room failed for %s: %s", conf.room, exc)
        return JSONResponse(
            {
                "ok": True,
                "conf_id": conf.conf_id,
                "room_deleted": room_deleted,
                "agent_stopped": agent_stopped,
            }
        )

    @app.get("/conf/candidates")
    async def conf_candidates() -> JSONResponse:
        """Federation discovery for CONFS: list discoverable federated conf rooms.

        Cross-host sibling of ``/conf`` (which lists only LOCAL confs). Reads the
        focus descriptors + Space-state events advertised on the configured Nostr
        relays and joins them into ``{confs: [{room, title, status, host_fqid,
        auth_url, sfu_ws_url}]}``. A conf is discoverable only if its focus host
        has a complete descriptor (so a peer can actually redeem a token).

        Best-effort + never-fatal: any relay/parse/import failure yields an EMPTY
        list rather than a 500, so a client can always poll it safely.
        """
        confs: list[dict] = []
        try:
            from skchat.spaces.federation.events import (
                FOCUS_KIND,
                SPACE_KIND,
                parse_focus_descriptor,
            )
            from skchat.spaces.federation.nostr_io import FederationNostr

            relays = [r for r in os.getenv("SKCHAT_NOSTR_RELAYS", "").split(",") if r.strip()]
            if relays:
                nostr = FederationNostr(relays=relays)
                # host_fqid -> {auth_url, sfu_ws_url}
                foci: dict[str, dict] = {}
                for ev in nostr._query({"kinds": [FOCUS_KIND]}):
                    try:
                        d = parse_focus_descriptor(ev)
                    except Exception:  # noqa: BLE001 - hostile/malformed relay event
                        continue
                    fqid = (d.get("host_fqid") or "").strip()
                    auth_url = (d.get("auth_url") or "").strip()
                    sfu_ws_url = (d.get("sfu_ws_url") or "").strip()
                    if fqid and auth_url and sfu_ws_url:
                        foci[fqid] = {"auth_url": auth_url, "sfu_ws_url": sfu_ws_url}
                # join Space-state events to their focus host, conf rooms only.
                seen: set[str] = set()
                for ev in nostr._query({"kinds": [SPACE_KIND]}):
                    tags = {
                        t[0]: t[1]
                        for t in (ev.get("tags") or [])
                        if isinstance(t, list) and len(t) >= 2
                    }
                    room = (tags.get("d") or "").strip()
                    host = (tags.get("host") or "").strip()
                    # conf rooms are the only ones whose auth_url mints conf tokens;
                    # we surface every advertised room with a complete focus, but
                    # mark which are conf rooms (id prefix) so the UI can filter.
                    if not room or room in seen or host not in foci:
                        continue
                    seen.add(room)
                    confs.append(
                        {
                            "room": room,
                            "title": (tags.get("title") or "").strip(),
                            "status": (tags.get("status") or "").strip() or "live",
                            "host_fqid": host,
                            "auth_url": foci[host]["auth_url"],
                            "sfu_ws_url": foci[host]["sfu_ws_url"],
                            "is_conf": room.startswith("conf-"),
                        }
                    )
        except Exception as exc:  # noqa: BLE001 - discovery best-effort, never 500
            logger.warning("conf/candidates discovery failed: %s", exc)
            confs = []
        return JSONResponse({"confs": confs})

    @app.get("/conf")
    async def list_confs() -> JSONResponse:
        return JSONResponse(
            {
                "confs": [
                    {
                        "conf_id": c.conf_id,
                        "title": c.title,
                        "host_fqid": c.host_fqid,
                        "status": c.status.value,
                        "participants": c.participants,
                        "participant_cap": c.participant_cap,
                        "recording": c.recording,
                    }
                    for c in reg.list_live()
                ]
            }
        )

    @app.get("/conf/health")
    async def conf_ops_health() -> JSONResponse:
        """Standalone ops health endpoint for the conf subsystem.
        
        Reports conf registry stats, LiveKit credential status, and
        any running agent-worker units (best-effort).
        """
        live = reg.list_live()
        agent_units: list[str] = []
        try:
            proc = run_cmd(["systemctl", "--user", "list-units", "--type=scope", "--no-pager"])
            out = getattr(proc, "stdout", "") or ""
            for line in out.splitlines():
                if "lumina-conf-" in line:
                    parts = line.split()
                    if len(parts) > 1:
                        agent_units.append(parts[0])
        except Exception:
            pass
        return JSONResponse({
            "service": "skchat-conf",
            "status": "ok",
            "live_confs": len(live),
            "total_participants": sum(len(c.participants) for c in live),
            "total_waiting": sum(len(c.waiting_room) for c in live),
            "livekit_configured": _have_creds(),
            "agent_workers": agent_units,
        })

    @app.post("/conf/{room}/waiting")
    async def enter_waiting_room(room: str, request: Request) -> JSONResponse:
        """A guest enters the conf waiting room. Tailnet callers auto-admitted."""
        conf = reg.get(room)
        if conf is None or conf.status.value == "ended":
            raise HTTPException(404, "conf not found or ended")
        body = await request.json()
        identity = (body.get("identity") or "").strip()
        display = (body.get("display") or "").strip()[:40]
        if not identity:
            raise HTTPException(400, "identity required")
        if reg.is_denied(room, identity):
            raise HTTPException(403, "guest was denied entry to this conf")
        if reg.is_admitted(room, identity):
            return JSONResponse({"admitted": True, "identity": identity})
        client_ip = _client_ip(request)
        tailnet = _is_tailnet(request)
        guest = PendingGuest(
            identity=identity,
            display=display or identity.split("@")[0],
            ip=client_ip,
            is_tailnet=tailnet,
            timestamp=time.time(),
        )
        reg.add_waiting_guest(room, guest)
        if tailnet:
            reg.admit_guest(room, identity)
            return JSONResponse({"admitted": True, "identity": identity, "auto_admitted": True})
        return JSONResponse({
            "admitted": False,
            "identity": identity,
            "position": len(conf.waiting_room),
            "message": "Waiting for host to admit you",
        })

    @app.get("/conf/{room}/waiting")
    async def waiting_room_status(room: str) -> JSONResponse:
        """Host views the waiting room (callers, not admitted/denied)."""
        conf = reg.get(room)
        if conf is None:
            raise HTTPException(404, "conf not found")
        return JSONResponse({
            "room": room,
            "waiting": conf.waiting_room,
            "admitted": conf.admitted,
            "denied": conf.denied,
        })

    @app.post("/conf/{room}/admit")
    async def admit_guest(room: str, request: Request) -> JSONResponse:
        """Host admits a waiting guest by identity."""
        conf = reg.get(room)
        if conf is None:
            raise HTTPException(404, "conf not found")
        body = await request.json()
        _require_host(conf, (body.get("requester") or "").strip())
        identity = (body.get("identity") or "").strip()
        if not identity:
            raise HTTPException(400, "identity required")
        ok = reg.admit_guest(room, identity)
        if not ok:
            raise HTTPException(400, "could not admit guest")
        return JSONResponse({"ok": True, "identity": identity})

    @app.post("/conf/{room}/deny")
    async def deny_guest(room: str, request: Request) -> JSONResponse:
        """Host denies a waiting guest by identity."""
        conf = reg.get(room)
        if conf is None:
            raise HTTPException(404, "conf not found")
        body = await request.json()
        _require_host(conf, (body.get("requester") or "").strip())
        identity = (body.get("identity") or "").strip()
        if not identity:
            raise HTTPException(400, "identity required")
        ok = reg.deny_guest(room, identity)
        if not ok:
            raise HTTPException(400, "could not deny guest")
        return JSONResponse({"ok": True, "identity": identity})

    @app.post("/conf/{room}/federated-token")
    async def conf_federated_token(room: str, request: Request) -> JSONResponse:
        """sk-lk-authd for conferences: cross-instance token minting.
        
        Accepts a capauth-signed FQID assertion ``{claim, sig}``, verifies
        the assertion signature and freshness, checks trust policy, and mints
        a conf PARTICIPANT token for this host's LiveKit SFU.
        
        This is the conf parallel of ``POST /sfu/get`` (which mints audio Space
        tokens). The assertion uses the same capauth-signing format, so a remote
        agent can present the same identity credential to either endpoint.
        """
        try:
            from skchat.spaces.federation.assertion import (
                AssertionError as FedAssertionError,
                verify_signed,
            )
            from skchat.spaces.federation.trust import AccessLevel, TrustPolicy
        except ImportError:
            raise HTTPException(503, "federation module not available")

        body = await request.json()
        if not isinstance(body, dict) or "claim" not in body or "sig" not in body:
            raise HTTPException(400, "body must be {claim, sig}")

        conf = reg.get(room)
        if conf is None or conf.status.value == "ended":
            raise HTTPException(404, "conf not found or ended")

        try:
            assertion = verify_signed(body)
        except FedAssertionError as exc:
            raise HTTPException(403, f"assertion rejected: {exc}") from exc

        if not _check_fed_nonce(assertion.fqid, assertion.nonce):
            raise HTTPException(403, "replay detected")

        access = TrustPolicy().access_for(assertion.fqid)
        if access == AccessLevel.DENY:
            raise HTTPException(403, f"fqid {assertion.fqid!r} not permitted")

        role = ConfRole.PARTICIPANT
        rmr = TrustPolicy().remote_max_role
        if rmr == "listener":
            role = ConfRole.GUEST_CONF

        token = mint_conf_token(
            assertion.fqid,
            assertion.fqid.split("@")[0],
            role,
            conf.room,
            _DEFAULT_TTL,
        )
        # Federation observability: a remote peer just redeemed a cross-realm
        # token against this instance's SFU (best-effort counter, never fatal).
        try:
            from skchat.federation_status import incr

            incr("fed_tokens_redeemed")
        except Exception:  # noqa: BLE001 - observability must never break the mint
            pass
        return JSONResponse({
            "token": token,
            "url": _public_url(request),
            "role": role.value,
            "identity": assertion.fqid,
            "conf_id": conf.conf_id,
            "room": conf.room,
        })

    @app.get("/app/{rest:path}", response_class=FileResponse)
    async def flutter_app(rest: str) -> FileResponse:
        """Serve the Flutter web app (skchat-app build)."""
        base = Path(__file__).resolve().parent.parent / "static" / "app"

        def _resp(p: Path) -> FileResponse:
            # index.html + the JS entrypoints change on every redeploy. Without
            # this, browsers (esp. iOS Safari) HTTP-cache main.dart.js and keep
            # serving a STALE build after a deploy. Force revalidation on the
            # files that change; hashed assets (canvaskit/fonts) cache normally.
            volatile = {
                "index.html",
                "main.dart.js",
                "flutter_bootstrap.js",
                "flutter.js",
                "flutter_service_worker.js",
                "version.json",
            }
            headers = (
                {"Cache-Control": "no-cache, must-revalidate"}
                if p.name in volatile
                else None
            )
            return FileResponse(p, headers=headers)

        if not rest:
            return _resp(base / "index.html")
        path = base / rest
        if path.exists() and path.is_file():
            return _resp(path)
        # SPA fallback: return index.html for any unrecognized path
        return _resp(base / "index.html")

    @app.get("/conf/{room}", response_class=HTMLResponse)
    async def conf_page(room: str, request: Request) -> HTMLResponse:
        """Hand a shared conference link to the NATIVE Flutter app by default.

        A ``/conf/{room}`` link now lands in the app's native conf experience
        (``/app/#/conf?room=...``), where the app mints a role-scoped token with
        the signed-in identity and joins the room (with its panels). The legacy
        web client (``livekit.html``) stays reachable as a fallback via
        ``?web=1`` so nothing is lost if the app is unavailable. The room name is
        HTML-escaped / URL-encoded before it lands in the markup + redirect URL.
        """
        import html as _html
        from urllib.parse import quote

        from skchat.app_link import conf_app_link, wants_web_fallback

        if wants_web_fallback(request):
            target = f"/livekit/{quote(room)}?room={quote(room)}"
        else:
            target = conf_app_link(room)
        safe_room = _html.escape(room)
        safe_target = _html.escape(target)
        return HTMLResponse(f"""<!doctype html>
<html><head>
<meta charset="utf-8"/>
<meta http-equiv="refresh" content="0;url={safe_target}" />
<title>Conference: {safe_room}</title>
</head><body>
<p>Joining conference room <strong>{safe_room}</strong>...</p>
</body></html>""")
