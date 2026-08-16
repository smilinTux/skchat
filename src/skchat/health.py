"""Per-service health probe backing ``GET /api/v1/health``.

Built after the 2026-08-13 incident where the ``.100`` node died and the only
symptom in the Flutter app was Lumina silently not answering — diagnosing it
needed shell access. This module is the source of truth for the operator
screen that shows WHY the assistant went quiet: it probes each backend skchat
depends on (STT, TTS, LLM, the LiveKit SFU) plus reports on skchat's own app
server, and returns a fixed-shape payload the client renders as a status list.

STT/TTS/LLM each expose a dedicated ``/health`` path on their own host:port
(``skworld-100:18794/health``, ``:18796/health``, ``:8082/health`` as of this
writing) — this module derives that path from the configured base URL (never
a hardcoded host) and probes it first. A genuine ``/health`` 200 is real proof
of life; a bare "something answered on the configured port" is NOT — a 404
from the wrong path, or a 500 from a broken-but-listening process, would both
have looked identical to "up" under a base-URL-only probe, which is exactly
the kind of invented green this endpoint exists to stop. The LiveKit SFU has
no known dedicated health path, so it is probed directly at its base URL.

Honesty contract (card f2e6c451 — do not relax any of these):

  * "up" is asserted ONLY for a response that is real evidence of life: 2xx,
    or 401/403 (an auth challenge proves the RIGHT service answered — only a
    live, correctly-routed backend can challenge a request). A probe that
    did not run, timed out, or targets an unconfigured URL is NEVER "up".
  * "down" means the probe reached the network layer and it errored:
    connection refused/reset/timed out/TLS failure, OR a 5xx (the service
    answered and is failing). "unknown" means we could not confirm health at
    all: a missing/blank config URL, a config-resolution failure, or a 4xx
    (other than 401/403) that proves something is listening but gives no
    signal it is the labelled service. "down" and "unknown" are never
    collapsed into each other.
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
#: A backend probed via /health + base-URL fallback pays this TWICE at most
#: (still well under a second even in the worst case).
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

#: Backends known to expose a dedicated ``/health`` path (derived from their
#: configured base URL — see :func:`_derive_health_url`). SFU is deliberately
#: excluded: LiveKit has no known dedicated health path, so it is probed
#: directly at its base URL via the generic :func:`probe_url`.
_HEALTH_PATH_SERVICE_IDS = frozenset({"stt", "tts", "llm"})

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

    LiveKit's control port answers plain HTTP; httpx has no ``ws://``
    transport, so a websocket URL is translated before it is dialed. Any
    other scheme passes through unchanged.
    """
    if url.startswith("wss://"):
        return "https://" + url[len("wss://") :]
    if url.startswith("ws://"):
        return "http://" + url[len("ws://") :]
    return url


def _derive_health_url(base_url: str) -> str:
    """``<scheme>://<host[:port]>/health`` derived from a configured base URL.

    Never hardcodes a host: the same backend a base URL points at (e.g.
    ``http://skworld-100:18794/v1/audio/transcriptions``) exposes ``/health``
    on that SAME host:port, just a different path
    (``http://skworld-100:18794/health``). Falls back to the literal string
    ``"/health"`` if the base URL cannot be parsed at all (never raises).
    """
    try:
        parts = urlsplit(_as_http(base_url))
        if parts.scheme and parts.netloc:
            return f"{parts.scheme}://{parts.netloc}/health"
    except Exception:
        pass
    return "/health"


def _classify_response_status(status_code: int) -> tuple[str, str]:
    """Classify an HTTP status code into ``(state, reason)``.

    2xx and 401/403 are treated as proof of life: an auth challenge can only
    come from the right service actually running, so it counts as "up" the
    same as a clean 200 (getting this backwards would read every gated
    service as broken). 5xx means the service answered AND is failing --
    "down", not "up": a broken-but-listening process must not show green.
    Any other 4xx (404, 405, ...) proves something is listening on the port
    but gives no signal that confirms it is the labelled service -- exactly
    what "unknown" exists for; it is never upgraded to "up" on a guess.
    """
    if 200 <= status_code < 300 or status_code in (401, 403):
        return STATE_UP, ""
    if 500 <= status_code < 600:
        return STATE_DOWN, "server error"
    return STATE_UNKNOWN, "no health signal"


def _status_detail(status_code: int, reason: str, latency_ms: int) -> str:
    suffix = f" ({reason})" if reason else ""
    return f"{status_code}{suffix} in {latency_ms}ms"


async def probe_url(
    client: httpx.AsyncClient, service_id: str, label: str, url: Optional[str]
) -> ServiceHealth:
    """Probe one backend directly at its base URL. Never raises.

    Used for backends with no known dedicated health path (the SFU). A blank
    /missing ``url`` is "unknown" (never attempted, never "down"). The
    response status is classified per :func:`_classify_response_status` --
    NOT every response is "up" (a 5xx or an unrelated 4xx is not proof of
    life; see the module docstring).
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
    state, reason = _classify_response_status(resp.status_code)
    return ServiceHealth(
        id=service_id,
        label=label,
        state=state,
        detail=_status_detail(resp.status_code, reason, latency_ms),
        latency_ms=latency_ms,
        checked_at=_now_iso(),
    )


async def probe_backend_with_health_path(
    client: httpx.AsyncClient, service_id: str, label: str, base_url: Optional[str]
) -> ServiceHealth:
    """Probe a backend's dedicated ``/health`` path, derived from ``base_url``.

    Used for STT/TTS/LLM, which each expose a real ``/health`` endpoint. A
    genuine health-path response is classified per
    :func:`_classify_response_status` (2xx/401/403 up, 5xx down, other 4xx
    unknown).

    If ``/health`` itself 404s (no such path deployed), falls back to probing
    the base URL as a pure REACHABILITY check -- and caps the result at
    "unknown" regardless of what the base URL returns, even a 200: reaching
    *some* HTTP server on the configured port is not the same claim as a
    dedicated health check confirming it, and this endpoint does not invent
    "up" out of that ambiguity (honesty rule 1).
    """
    if not base_url or not base_url.strip():
        return ServiceHealth(
            id=service_id,
            label=label,
            state=STATE_UNKNOWN,
            detail="not configured",
            latency_ms=None,
            checked_at=_now_iso(),
        )

    base = base_url.strip()
    health_target = _derive_health_url(base)
    start = time.monotonic()
    try:
        resp = await client.get(health_target, timeout=PROBE_TIMEOUT_SECONDS)
    except Exception as exc:  # noqa: BLE001 — a probe failure is a fact to report, never a raise
        return ServiceHealth(
            id=service_id,
            label=label,
            state=STATE_DOWN,
            detail=_failure_detail(exc, health_target),
            latency_ms=None,
            checked_at=_now_iso(),
        )
    latency_ms = round((time.monotonic() - start) * 1000)

    if resp.status_code == 404:
        # No dedicated /health path deployed. Fall back to the base URL for
        # a bare reachability signal ONLY -- never re-labelled "up" from it.
        base_target = _as_http(base)
        start2 = time.monotonic()
        try:
            resp2 = await client.get(base_target, timeout=PROBE_TIMEOUT_SECONDS)
        except Exception as exc:  # noqa: BLE001
            return ServiceHealth(
                id=service_id,
                label=label,
                state=STATE_DOWN,
                detail=_failure_detail(exc, base_target),
                latency_ms=None,
                checked_at=_now_iso(),
            )
        latency2 = round((time.monotonic() - start2) * 1000)
        return ServiceHealth(
            id=service_id,
            label=label,
            state=STATE_UNKNOWN,
            detail=(
                f"no /health endpoint (404); base URL responded "
                f"{resp2.status_code} in {latency2}ms"
            ),
            latency_ms=latency2,
            checked_at=_now_iso(),
        )

    state, reason = _classify_response_status(resp.status_code)
    return ServiceHealth(
        id=service_id,
        label=label,
        state=state,
        detail=_status_detail(resp.status_code, reason, latency_ms),
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
    come back ``None`` rather than a guessed value — the probe functions turn
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
        tasks = []
        for sid in probed:
            if sid in _HEALTH_PATH_SERVICE_IDS:
                tasks.append(
                    probe_backend_with_health_path(client, sid, labels[sid], urls.get(sid))
                )
            else:
                tasks.append(probe_url(client, sid, labels[sid], urls.get(sid)))
        results = await asyncio.gather(*tasks)
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
