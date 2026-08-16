"""Per-service health probe backing ``GET /api/v1/health``.

Built after the 2026-08-13 incident where the ``.100`` node died and the only
symptom in the Flutter app was Lumina silently not answering — diagnosing it
needed shell access. This module is the source of truth for the operator
screen that shows WHY the assistant went quiet: it probes each backend skchat
depends on (STT, TTS, LLM, the LiveKit SFU) plus reports on skchat's own app
server, and returns a fixed-shape payload the client renders as a status list.

Honesty contract (card f2e6c451 — do not relax any of these):

  * "up" is asserted ONLY after a probe actually got an HTTP response back.
    A probe that did not run, timed out, or targets an unconfigured URL is
    NEVER reported as "up".
  * "down" and "unknown" are different facts and are never collapsed:
    "down" means the probe reached the network layer and it errored (refused,
    reset, timed out, TLS failure, ...). "unknown" means we could not even
    attempt it — a missing/blank config URL, or a config-resolution failure.
  * A missing or blank config URL is ALWAYS "unknown" with a "not configured"
    detail, never "down", and the service is never omitted from the response.
  * ``detail`` is built from the exception's TYPE (+ target host:port), never
    from ``str(exc)`` alone: a connect timeout's ``str()`` is frequently
    empty, which is exactly what produced five useless "STT failed: " log
    lines during the incident this endpoint exists to prevent.
  * This module never raises. Every probe is independently wrapped so one
    dead backend cannot take down the response for the other services.
"""

from __future__ import annotations

import asyncio
import os
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Optional
from urllib.parse import urlsplit

import httpx

#: Per-probe timeout (connect+read+write+pool all bound by this). Keeps the
#: whole endpoint fast even when a backend is a black hole (connect hangs).
PROBE_TIMEOUT_SECONDS = 2.0

STATE_UP = "up"
STATE_DOWN = "down"
STATE_UNKNOWN = "unknown"

#: (id, label) pairs in the fixed order the response emits them. ``id`` and
#: ``label`` are part of the wire contract the Flutter client is built
#: against — do not rename or reorder without updating the client in lockstep.
SERVICE_CATALOG: tuple[tuple[str, str], ...] = (
    ("stt", "Speech to text"),
    ("tts", "Voice"),
    ("llm", "Language model"),
    ("sfu", "Call server"),
    ("webui", "App server"),
)

#: env var + default matching ``livekit_routes.LIVEKIT_URL`` — the SFU URL
#: the call routes use to reach LiveKit from the server side. Read live (not
#: imported as a module-level constant) so an operator env edit takes effect
#: on the next probe without a reimport, matching how ``VoiceConfig.from_env``
#: resolves the STT/TTS/LLM URLs below.
_LIVEKIT_URL_ENV = "SKCHAT_LIVEKIT_URL"
_LIVEKIT_URL_DEFAULT = "ws://skworld-100:7880"


def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


@dataclass(frozen=True)
class ServiceHealth:
    id: str
    label: str
    state: str
    detail: str
    latency_ms: Optional[int]
    checked_at: str

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "label": self.label,
            "state": self.state,
            "detail": self.detail,
            "latency_ms": self.latency_ms,
            "checked_at": self.checked_at,
        }


def _host_port(url: str) -> str:
    """Best-effort ``host:port`` for a detail string. Never raises."""
    try:
        parts = urlsplit(url)
        host = parts.hostname or "?"
        port = parts.port
        if port is None:
            port = 443 if parts.scheme in ("https", "wss") else 80
        return f"{host}:{port}"
    except Exception:
        return "?"


def _failure_detail(exc: BaseException, url: str) -> str:
    """Build a detail string from the exception's TYPE, never ``str(exc)``.

    ``str()`` on an httpx connect-timeout/connect-error is frequently EMPTY,
    so the exception class name plus the target host:port is the only thing
    guaranteed to carry information (the exact bug this endpoint exists to
    stop repeating — see the incident note in the module docstring).
    """
    return f"{type(exc).__name__} connecting to {_host_port(url)}"


def _as_http(url: str) -> str:
    """``ws``/``wss`` -> ``http``/``https`` so httpx can dial an SFU URL.

    LiveKit's control port answers plain HTTP (any status code counts as
    reachable, see :func:`probe_url`); httpx has no ``ws://`` transport, so a
    websocket URL is translated before it is dialed. Any other scheme passes
    through unchanged.
    """
    if url.startswith("wss://"):
        return "https://" + url[len("wss://") :]
    if url.startswith("ws://"):
        return "http://" + url[len("ws://") :]
    return url


async def probe_url(
    client: httpx.AsyncClient, service_id: str, label: str, url: Optional[str]
) -> ServiceHealth:
    """Probe one backend. Never raises — always returns a :class:`ServiceHealth`.

    A blank/missing ``url`` is "unknown" (never attempted, never "down").
    Any HTTP response — even a 404 or 405 — proves the backend is reachable
    and is reported as "up"; the probe hits the configured URL directly
    rather than a dedicated health path, since none of these backends
    (whisper/TTS/LLM/LiveKit) is guaranteed to expose one, and reachability
    is the fact this endpoint needs, not application-level correctness.
    """
    if not url or not url.strip():
        return ServiceHealth(
            id=service_id,
            label=label,
            state=STATE_UNKNOWN,
            detail="not configured",
            latency_ms=None,
            checked_at=_now_iso(),
        )

    target = _as_http(url.strip())
    start = time.monotonic()
    try:
        resp = await client.get(target, timeout=PROBE_TIMEOUT_SECONDS)
    except Exception as exc:  # noqa: BLE001 — a probe failure is a fact to report, never a raise
        return ServiceHealth(
            id=service_id,
            label=label,
            state=STATE_DOWN,
            detail=_failure_detail(exc, target),
            latency_ms=None,
            checked_at=_now_iso(),
        )
    latency_ms = round((time.monotonic() - start) * 1000)
    return ServiceHealth(
        id=service_id,
        label=label,
        state=STATE_UP,
        detail=f"{resp.status_code} in {latency_ms}ms",
        latency_ms=latency_ms,
        checked_at=_now_iso(),
    )


def resolve_service_urls() -> dict[str, Optional[str]]:
    """Resolve every probed service's URL exactly as the live code does.

    STT/TTS/LLM come from ``VoiceConfig.from_env()`` — the same env schema
    the running voice path reads (e.g. ``daemon_proxy.api_transcribe``'s
    ``STTClient(VoiceConfig.from_env())``). SFU comes from the same env var
    and default as ``livekit_routes.LIVEKIT_URL``. If resolution itself fails
    (e.g. the voice engine module cannot be imported), the affected entries
    come back ``None`` rather than a guessed value — :func:`probe_url` turns
    that into "unknown", never "down".
    """
    urls: dict[str, Optional[str]] = {"stt": None, "tts": None, "llm": None, "sfu": None}
    try:
        from .voice_engine.config import VoiceConfig

        cfg = VoiceConfig.from_env()
        urls["stt"] = cfg.stt_url or None
        urls["tts"] = cfg.tts_url or None
        urls["llm"] = cfg.llm_url or None
    except Exception:
        pass  # left as None -> "unknown" for all three, never a guess.

    sfu = os.getenv(_LIVEKIT_URL_ENV, _LIVEKIT_URL_DEFAULT)
    urls["sfu"] = sfu.strip() or None if sfu is not None else None
    return urls


async def build_health_payload(*, client: Optional[httpx.AsyncClient] = None) -> dict:
    """Build the full ``GET /api/v1/health`` response body. Never raises.

    Probes run CONCURRENTLY (``asyncio.gather``) so one slow/hung backend
    does not serialize behind the others; each individual probe already
    self-times-out at :data:`PROBE_TIMEOUT_SECONDS` and never raises, so
    ``gather`` here needs no ``return_exceptions`` safety net.

    Args:
        client: inject an ``httpx.AsyncClient`` (e.g. with a
            ``httpx.MockTransport``) for tests. Defaults to a real client
            that this function opens and closes itself.
    """
    urls = resolve_service_urls()
    labels = dict(SERVICE_CATALOG)

    owns_client = client is None
    if client is None:
        client = httpx.AsyncClient()
    try:
        probed = [sid for sid, _label in SERVICE_CATALOG if sid != "webui"]
        results = await asyncio.gather(
            *(probe_url(client, sid, labels[sid], urls.get(sid)) for sid in probed)
        )
    finally:
        if owns_client:
            await client.aclose()

    by_id = {r.id: r for r in results}
    # "webui" reports on skchat's OWN app server: if this handler is running
    # to build this very response, the app server serving it is up by
    # construction — not a guess, and not a network probe (hence no latency).
    by_id["webui"] = ServiceHealth(
        id="webui",
        label=labels["webui"],
        state=STATE_UP,
        detail="serving this request",
        latency_ms=None,
        checked_at=_now_iso(),
    )

    services = [by_id[sid].to_dict() for sid, _label in SERVICE_CATALOG]
    return {"generated_at": _now_iso(), "services": services}
