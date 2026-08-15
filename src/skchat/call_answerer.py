"""Minimal always-on 1:1 call answerer.

Polls ``GET /call/incoming`` (presenting the ``X-Operator-Token``), and on a
fresh, server-verified invite calls ``POST /call/answer`` to get the LiveKit
room + token, then joins that DYNAMIC room and publishes an audio track. The
LiveKit connect + publish is the same ``livekit.rtc`` primitive sequence
``lumina-creative/scripts/lumina-call.py`` uses (rtc.Room -> connect ->
AudioSource -> LocalAudioTrack -> publish_track); we call the SDK directly
rather than import that file's heavyweight conversational agent (whisper / TTS /
avatar deps), which is coupled to a hardcoded room.

Security: consumes ONLY invites the server has already anti-spoof-verified and
addressed to self (``/call/incoming`` is signature-gated). It never re-parses
unverified bodies, and it PRESENTS the operator token on every call, never a
network-position bypass.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import threading
import time
from typing import Optional
from urllib import error as urlerror
from urllib import request as urlrequest

try:  # optional at import time; the pure core does not need it
    import json as _json
except Exception:  # pragma: no cover
    _json = None

logger = logging.getLogger(__name__)

POLL_INTERVAL_S = float(os.getenv("SKCHAT_ANSWERER_POLL_S", "3"))


def _resolve_webui_url() -> str:
    """The callee webui the answerer polls.

    Explicit ``SKCHAT_WEBUI_URL`` wins. Otherwise derive from ``SKCHAT_PORT``
    (each agent's ``webui-<agent>.env`` sets its own port, e.g. opus is 8766), so
    the systemd template stays genuinely per-agent instead of hardcoding a port.
    Falls back to the lumina default only when neither is set.
    """
    explicit = os.getenv("SKCHAT_WEBUI_URL", "").strip()
    if explicit:
        return explicit
    port = os.getenv("SKCHAT_PORT", "").strip()
    if port:
        return f"http://localhost:{port}"
    return "http://localhost:8765"


# Leave a room this long after joining if no remote party ever appears (a stale
# invite, or a caller that rang but never connected). Prevents the answerer from
# holding an empty room forever and never returning to poll.
ALONE_TIMEOUT_S = float(os.getenv("SKCHAT_ANSWERER_ALONE_TIMEOUT_S", "45"))
# Hard cap on a single call so a wedged room can never pin the service.
MAX_CALL_S = float(os.getenv("SKCHAT_ANSWERER_MAX_CALL_S", "3600"))


def _should_leave(
    remote_count: int,
    ever_saw_peer: bool,
    alone_elapsed_s: float,
    call_elapsed_s: float,
    *,
    alone_timeout_s: float = ALONE_TIMEOUT_S,
    max_call_s: float = MAX_CALL_S,
) -> bool:
    """Whether the answerer should leave the room and return to polling.

    Pure decision so the exit policy is unit-tested without LiveKit. Leave when:
    the call exceeded the hard cap; the caller hung up (a remote party was seen
    and then the room emptied); or no remote party ever joined within the alone
    timeout (a stale/unanswered ring). Otherwise stay (a party is present, or we
    are still inside the alone grace window).
    """
    if call_elapsed_s >= max_call_s:
        return True
    if remote_count > 0:
        return False
    if ever_saw_peer:
        return True
    return alone_elapsed_s >= alone_timeout_s


def poll_and_answer(api, seen: set) -> Optional[dict]:
    """One poll cycle: answer the newest un-seen invite.

    Returns the ``{room, token, livekit_url}`` to join, or ``None`` when there is
    nothing fresh to answer. Pure over the ``api`` seam (no HTTP, no LiveKit).

    All fresh invites this cycle are marked ``seen`` (not just the answered one),
    so an older invite left pending alongside a newer one is not picked up as a
    stale second call on a later cycle. Invites without a ``nonce`` are ignored
    (the signature-gated endpoint always stamps one; a body without it is not a
    verified invite).
    """
    invites = api.poll_incoming() or []
    fresh = [i for i in invites if i.get("nonce") and i["nonce"] not in seen]
    if not fresh:
        return None
    fresh.sort(key=lambda i: i.get("ts", 0), reverse=True)
    for i in fresh:
        seen.add(i["nonce"])
    invite = fresh[0]
    joinable = api.answer(invite["from_fqid"])
    return {
        "room": joinable["room"],
        "token": joinable["token"],
        "livekit_url": joinable["livekit_url"],
        # Carry the caller through. /call/incoming has already cross-checked this
        # against the signed envelope sender, so it is the trustworthy identity
        # for choosing the conversational mode. Without it a derived room name
        # (call-<hash>) silently demotes Chef to the group register.
        "peer_fqid": invite["from_fqid"],
    }


class AnswererApi:
    """Thin HTTP seam to the webui ``/call/*`` endpoints.

    Every request carries the operator token; the auth gate requires it (network
    position alone is not enough). Uses stdlib ``urllib`` so the answerer has no
    extra runtime dependency beyond LiveKit.
    """

    def __init__(self, base_url: str, operator_token: str, timeout: float = 8.0):
        self.base_url = base_url.rstrip("/")
        self.operator_token = operator_token
        self.timeout = timeout

    def _headers(self) -> dict:
        return {
            "Content-Type": "application/json",
            "X-Operator-Token": self.operator_token,
        }

    def mint_token(self, room: str, identity: str, name: str = "") -> dict:
        """Mint a LiveKit JWT for an arbitrary room.

        Needed for a presence rejoin, where there is no invite to carry a token.
        /livekit/token is gated to loopback/tailnet OR the operator token, and
        the answerer satisfies both.
        """
        body = json.dumps({"room": room, "identity": identity, "name": name or identity})
        req = urlrequest.Request(
            f"{self.base_url}/livekit/token",
            data=body.encode("utf-8"),
            headers=self._headers(),
            method="POST",
        )
        with urlrequest.urlopen(req, timeout=self.timeout) as resp:
            return json.loads(resp.read().decode("utf-8"))

    def poll_incoming(self) -> list:
        req = urlrequest.Request(
            f"{self.base_url}/call/incoming",
            headers=self._headers(),
            method="GET",
        )
        try:
            with urlrequest.urlopen(req, timeout=self.timeout) as r:
                body = _json.loads(r.read() or b"{}")
        except urlerror.HTTPError as e:
            logger.warning("poll /call/incoming failed: HTTP %s", e.code)
            return []
        except (urlerror.URLError, OSError) as e:
            logger.warning("poll /call/incoming transport error: %s", e)
            return []
        return body.get("invites", []) if isinstance(body, dict) else []

    def answer(self, peer: str) -> dict:
        data = _json.dumps({"peer": peer}).encode()
        req = urlrequest.Request(
            f"{self.base_url}/call/answer",
            data=data,
            headers=self._headers(),
            method="POST",
        )
        with urlrequest.urlopen(req, timeout=self.timeout) as r:
            return _json.loads(r.read())


async def _join_and_publish(joinable: dict, hold_s: float = 0.0) -> None:
    """Connect to the DYNAMIC LiveKit room and publish an audio track.

    Same primitive sequence as ``lumina-call.py``'s ``Speaker`` (AudioSource ->
    LocalAudioTrack -> publish_track), driven by the room/token/url from
    ``/call/answer`` instead of a hardcoded room. Pushes silence to keep the
    published track alive; audible content (TTS greeting) is a follow-on. The
    LiveKit import is guarded here so the pure core imports without it.
    """
    import asyncio

    from livekit import rtc

    room = rtc.Room()
    await room.connect(joinable["livekit_url"], joinable["token"])
    sid = await room.sid
    logger.info(
        "answerer connected: room=%s sid=%s peers=%s",
        room.name,
        sid,
        [p.identity for p in room.remote_participants.values()] or "(none)",
    )

    sample_rate = 48000
    source = rtc.AudioSource(sample_rate=sample_rate, num_channels=1, queue_size_ms=300)
    track = rtc.LocalAudioTrack.create_audio_track("answerer-voice", source)
    opts = rtc.TrackPublishOptions(source=rtc.TrackSource.SOURCE_MICROPHONE)
    await room.local_participant.publish_track(track, opts)
    logger.info("answerer audio track published in room=%s", room.name)

    # Keepalive: push 20ms silence frames so the track stays live for the caller.
    # Leave (return to polling) when the caller hangs up, when no one ever joins
    # within the alone timeout, or at the hard call cap. `hold_s` (>0) forces a
    # fixed hold instead, for tests.
    frame_ms = 20
    samples = sample_rate * frame_ms // 1000
    silence = rtc.AudioFrame.create(sample_rate, 1, samples)
    start = time.monotonic()
    fixed_deadline = (start + hold_s) if hold_s > 0 else None
    ever_saw_peer = False
    alone_since: Optional[float] = start
    try:
        while room.connection_state == rtc.ConnectionState.CONN_CONNECTED:
            await source.capture_frame(silence)
            await asyncio.sleep(frame_ms / 1000)
            now = time.monotonic()
            if fixed_deadline is not None:
                if now >= fixed_deadline:
                    break
                continue
            remote_count = len(room.remote_participants)
            if remote_count > 0:
                if not ever_saw_peer:
                    logger.info(
                        "answerer: peer joined room=%s (%s)",
                        room.name,
                        [p.identity for p in room.remote_participants.values()],
                    )
                ever_saw_peer = True
                alone_since = None
            elif alone_since is None:
                alone_since = now
            alone_elapsed = (now - alone_since) if alone_since is not None else 0.0
            if _should_leave(remote_count, ever_saw_peer, alone_elapsed, now - start):
                reason = (
                    "peer hung up" if ever_saw_peer else f"no peer within {ALONE_TIMEOUT_S:.0f}s"
                )
                logger.info("answerer leaving room=%s (%s)", room.name, reason)
                break
    finally:
        await room.disconnect()
        logger.info("answerer left room=%s", room.name)


async def _run_call(joinable: dict) -> None:
    """Run one answered call: an engine-backed session, or silence as fallback.

    The answerer has always joined the RIGHT room (the one the signed invite
    names) but published only silence, while the one process that could actually
    converse sat in a different, fixed room. This closes that gap by handing the
    answered room straight to the VoiceEngine LiveKit transport.

    Gated on SKCHAT_ANSWERER_ENGINE so the silence loop remains the fallback: if
    the engine or the RTC extras are missing on this host, a call still connects
    and holds rather than dropping. Losing the voice is bad; losing the call is
    worse.

    The peer identity comes from the invite, which /call/incoming has already
    signature-verified, and decides the conversational mode. Passing it matters:
    a derived room name alone would silently demote Chef to the group register.
    """
    if os.getenv("SKCHAT_ANSWERER_ENGINE", "").strip().lower() not in ("1", "true", "yes", "on"):
        await _join_and_publish(joinable)
        return
    try:
        from skchat.transports.livekit import build_room_session  # noqa: PLC0415
    except Exception as exc:  # pragma: no cover - env dependent
        logger.warning("engine transport unavailable (%s); holding the call with silence", exc)
        await _join_and_publish(joinable)
        return
    try:
        await build_room_session(
            joinable["room"],
            url=joinable["livekit_url"],
            token=joinable["token"],
            agent_name=os.getenv("SKAGENT", "lumina"),
            peer_fqid=_conversational_peer(joinable),
        )
    except Exception:
        logger.exception(
            "engine session failed for room=%s; falling back to silence", joinable["room"]
        )
        await _join_and_publish(joinable)


#: Suffix the agent appends to its own LiveKit identity when it joins.
AGENT_IDENTITY_SUFFIX = "#agent"

#: How often to reconcile presence against the rooms she is wanted in.
RECONCILE_INTERVAL_S = float(os.getenv("SKCHAT_ANSWERER_RECONCILE_S", "10"))

#: Rooms she should be in whenever a human is waiting there. Seeded with her
#: own derived 1:1 room and grown by every invite seen, so "pull her into a
#: room" works for group/space rooms too without her ever joining a room nobody
#: asked her to.
_WATCHED_ROOMS: set = set()


def should_join(identities: list, agent_suffix: str = AGENT_IDENTITY_SUFFIX) -> bool:
    """Should the agent join a room currently holding *identities*?

    Yes only when a human is waiting and no agent is already there.

    This exists because answering was purely INVITE-driven, and an invite is a
    momentary thing: it has a 120s TTL and is consumed on answer. On 2026-08-13
    Chef's phone left (ending her session correctly), he rejoined later from
    another device without placing a NEW call, and nothing on the server had any
    reason to put her back. He sat alone in the room while she was healthy and
    idle. Presence is the durable signal; an invite is only the first hint of
    it.

    Both halves matter. Requiring a human stops her joining and holding empty
    rooms; requiring no agent stops a second session colliding with a live one,
    which is how identity collisions and doubled audio pumps happened before.
    """
    humans = [i for i in identities if not str(i).endswith(agent_suffix)]
    agents = [i for i in identities if str(i).endswith(agent_suffix)]
    return bool(humans) and not agents


#: Rooms with a call in flight, so a re-delivered invite does not double-join.
_ACTIVE_ROOMS: set[str] = set()
_ACTIVE_LOCK = threading.Lock()


def _conversational_peer(joinable: dict) -> str | None:
    """Who is Lumina actually talking to, for choosing the conversational mode.

    Normally the invite's from_fqid. But a SELF-addressed call (from == to) means
    the browser is authenticated as the agent's own webui rather than as chef, so
    the literal peer is Lumina herself and the mode would fall to 'group',
    leaving her waiting to be addressed by name in what is really a 1:1.

    /call/incoming and /call/answer are both operator-gated, so a self-addressed
    invite can only have come from the operator. Treat it as chef.
    """
    peer = joinable.get("peer_fqid") or joinable.get("from_fqid")
    me = os.getenv("SKAGENT", "lumina")
    if peer and str(peer).split("@")[0] == me:
        logger.info("self-addressed call; treating the caller as the operator")
        return "chef"
    return peer


def _spawn_call(joinable: dict) -> None:
    """Run one answered call on a worker thread, keeping the poll loop alive.

    ``run_answerer`` used to ``asyncio.run(...)`` inline, which blocks polling
    for the entire duration of the call: a second caller rings into nothing
    until the first one hangs up. Each call now gets its own thread and its own
    event loop, and the room is tracked so a re-delivered invite for a call
    already in progress is ignored rather than joined twice.
    """
    room = joinable.get("room") or ""
    with _ACTIVE_LOCK:
        if room in _ACTIVE_ROOMS:
            # INFO, not DEBUG. The caller logs "answering call -> room=..."
            # BEFORE this guard runs, so a re-delivered invite prints that line
            # again and reads exactly like a genuine second join. On 2026-08-13
            # that cost a debugging session: rooms are derived per-pair and so
            # are stable across calls, which made the repeat look like the
            # known double-pump bug. If the join is skipped, say so at the same
            # level as the line that claimed it happened.
            logger.info("already in room=%s; not joining twice", room)
            return
        _ACTIVE_ROOMS.add(room)

    def _runner() -> None:
        try:
            asyncio.run(_run_call(joinable))
        except Exception as e:
            logger.error("join/publish failed for room=%s: %s", room, e)
        finally:
            with _ACTIVE_LOCK:
                _ACTIVE_ROOMS.discard(room)

    threading.Thread(target=_runner, name=f"call:{room[:16]}", daemon=True).start()


def _livekit_admin():
    """(url, key, secret) for the local SFU, or None.

    Read from the SFU's own config on this node. Presence needs to ask "who is
    in that room", which the call routes do not expose. Fail SOFT: without it
    the answerer keeps working exactly as before, invite-driven.
    """
    import yaml  # noqa: PLC0415 - only needed when reconciling

    try:
        cfg = yaml.safe_load(open(os.path.expanduser("~/.config/livekit/livekit.yaml")))
        keys = cfg.get("keys") or {}
        agent = os.getenv("SKAGENT", "lumina")
        name = f"skchat-{agent}" if f"skchat-{agent}" in keys else next(iter(keys), None)
        if not name:
            return None
        url = os.getenv("SKCHAT_ANSWERER_LIVEKIT_HTTP", "http://100.108.59.57:7880")
        return url, name, keys[name]
    except Exception as exc:  # noqa: BLE001
        logger.info("presence rejoin disabled (no LiveKit admin config: %r)", exc)
        return None


async def _room_identities(admin, room: str) -> list:
    """Identities currently in *room*, or [] if it does not exist."""
    from livekit import api  # noqa: PLC0415

    url, key, secret = admin
    lk = api.LiveKitAPI(url, key, secret)
    try:
        res = await lk.room.list_participants(api.ListParticipantsRequest(room=room))
        return [p.identity for p in res.participants]
    except Exception:  # noqa: BLE001 - a missing room is not an error
        return []
    finally:
        await lk.aclose()


def _reconcile_presence(api_client, admin) -> None:
    """Put her in any WATCHED room where a human is waiting and she is not.

    One pass. Deliberately narrow: it only ever considers rooms already in
    _WATCHED_ROOMS (her own derived 1:1 room, plus any room an invite named), so
    she can be pulled into a group or space room but never wanders into a call
    that did not ask for her.
    """
    agent = os.getenv("SKAGENT", "lumina")
    fqid = _self_fqid()
    for room in sorted(_WATCHED_ROOMS):
        with _ACTIVE_LOCK:
            if room in _ACTIVE_ROOMS:
                continue
        try:
            identities = asyncio.run(_room_identities(admin, room))
        except Exception as exc:  # noqa: BLE001
            logger.debug("presence check failed for %s: %r", room, exc)
            continue
        if not should_join(identities):
            continue
        logger.info(
            "presence: %s has %s waiting and no agent; joining", room, ",".join(identities)
        )
        try:
            minted = api_client.mint_token(
                room, identity=f"{fqid}{AGENT_IDENTITY_SUFFIX}", name=agent
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning("presence: could not mint a token for %s: %r", room, exc)
            continue
        direct = os.getenv("SKCHAT_ANSWERER_LIVEKIT_URL", "").strip()
        _spawn_call(
            {
                "room": room,
                "token": minted.get("token"),
                "livekit_url": direct or minted.get("url") or minted.get("livekit_url"),
                "peer_fqid": fqid,
                "from_fqid": fqid,
            }
        )


def _self_fqid() -> str:
    """This agent's FQID, as the browser addresses it."""
    agent = os.getenv("SKAGENT", "lumina")
    return os.getenv("SKCHAT_AGENT_FQID", "") or f"{agent}@chef.skworld.io"


def run_answerer(
    base_url: Optional[str] = None,
    operator_token: Optional[str] = None,
    poll_interval_s: Optional[float] = None,
) -> int:
    """Runnable loop: poll -> answer -> join the dynamic room and publish audio.

    Reads config from the environment when not passed:
    ``SKCHAT_WEBUI_URL`` or ``SKCHAT_PORT`` (the callee webui, see
    ``_resolve_webui_url``), ``SKCHAT_GUEST_OPERATOR_TOKEN`` (required),
    ``SKCHAT_ANSWERER_POLL_S`` (default 3s).
    """

    base_url = base_url or _resolve_webui_url()
    operator_token = operator_token or os.getenv("SKCHAT_GUEST_OPERATOR_TOKEN", "")
    interval = poll_interval_s if poll_interval_s is not None else POLL_INTERVAL_S
    if not operator_token:
        logger.error("no SKCHAT_GUEST_OPERATOR_TOKEN; the auth gate will 401 every call")
        return 2

    api = AnswererApi(base_url, operator_token)
    seen: set = set()
    logger.info("call answerer polling %s every %.1fs", base_url, interval)

    # Presence rejoin. An invite is momentary (120s TTL, consumed on answer) but
    # a person sitting in a room is not, and answering ONLY on invites meant she
    # never came back when Chef rejoined from another device without placing a
    # new call. Seed the watch with her own derived 1:1 room; every invite adds
    # the room it names, so she can be pulled into group and space rooms too.
    admin = _livekit_admin()
    try:
        from skchat.call_session import derive_room  # noqa: PLC0415

        me = _self_fqid()
        _WATCHED_ROOMS.add(derive_room(me, me))
        logger.info("presence: watching %s", ",".join(sorted(_WATCHED_ROOMS)))
    except Exception as exc:  # noqa: BLE001
        logger.info("presence: could not derive the 1:1 room (%r)", exc)
    last_reconcile = 0.0

    while True:
        try:
            joinable = poll_and_answer(api, seen)
        except Exception as e:  # a bad cycle must not kill the loop
            logger.warning("poll cycle error: %s", e)
            joinable = None
        if joinable:
            # A co-located answerer joins the LOCAL SFU directly. The advertised
            # livekit_url is a public/funnel URL for off-box browsers; from the
            # same host that path is a wasteful round-trip (and the opus webui's
            # advertised URL is a misconfigured :8443 TLS port that is not the
            # signaling ws). SKCHAT_ANSWERER_LIVEKIT_URL (e.g. ws://<tailnet-ip>:7880)
            # overrides it. The token is URL-independent (signed by a key the
            # server knows), so overriding the host is safe.
            direct = os.getenv("SKCHAT_ANSWERER_LIVEKIT_URL", "").strip()
            if direct:
                joinable = {**joinable, "livekit_url": direct}
            logger.info(
                "answering call -> room=%s via %s", joinable["room"], joinable["livekit_url"]
            )
            # Run the call on its own thread so polling continues. asyncio.run()
            # inline blocked the loop for the WHOLE call, so a second caller rang
            # into nothing until the first hung up.
            _WATCHED_ROOMS.add(joinable["room"])
            _spawn_call(joinable)
        # Presence is checked on its own cadence so a slow SFU query cannot
        # stall invite answering, which is still the fast path when ringing.
        if admin and time.monotonic() - last_reconcile >= RECONCILE_INTERVAL_S:
            last_reconcile = time.monotonic()
            try:
                _reconcile_presence(api, admin)
            except Exception as exc:  # noqa: BLE001 - never kill the poll loop
                logger.warning("presence reconcile error: %r", exc)
        time.sleep(interval)


def main() -> int:
    logging.basicConfig(
        level=os.getenv("SKCHAT_ANSWERER_LOGLEVEL", "INFO"),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    return run_answerer()


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
