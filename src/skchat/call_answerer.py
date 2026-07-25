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

import logging
import os
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
    # Loop until the room closes (caller hangs up) or the optional hold expires.
    frame_ms = 20
    samples = sample_rate * frame_ms // 1000
    silence = rtc.AudioFrame.create(sample_rate, 1, samples)
    deadline = (time.monotonic() + hold_s) if hold_s > 0 else None
    try:
        while room.connection_state == rtc.ConnectionState.CONN_CONNECTED:
            await source.capture_frame(silence)
            await asyncio.sleep(frame_ms / 1000)
            if deadline is not None and time.monotonic() >= deadline:
                break
    finally:
        await room.disconnect()
        logger.info("answerer left room=%s", room.name)


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
    import asyncio

    base_url = base_url or _resolve_webui_url()
    operator_token = operator_token or os.getenv("SKCHAT_GUEST_OPERATOR_TOKEN", "")
    interval = poll_interval_s if poll_interval_s is not None else POLL_INTERVAL_S
    if not operator_token:
        logger.error("no SKCHAT_GUEST_OPERATOR_TOKEN; the auth gate will 401 every call")
        return 2

    api = AnswererApi(base_url, operator_token)
    seen: set = set()
    logger.info("call answerer polling %s every %.1fs", base_url, interval)
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
            try:
                asyncio.run(_join_and_publish(joinable))
            except Exception as e:
                logger.error("join/publish failed for room=%s: %s", joinable["room"], e)
        time.sleep(interval)


def main() -> int:
    logging.basicConfig(
        level=os.getenv("SKCHAT_ANSWERER_LOGLEVEL", "INFO"),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    return run_answerer()


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
