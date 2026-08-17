"""Tests for skchat.health — the GET /api/v1/health probe logic (card f2e6c451).

No real network is hit: every probe is faked at the httpx transport seam via
``httpx.MockTransport``. These tests are also the mutation-verification bar
from the card:

  1. A failed probe reporting "up" instead of "down" must redden
     ``test_probe_url_reports_down_on_connection_failure``.
  2. An unconfigured service reporting "down" instead of "unknown" must
     redden ``test_probe_url_unconfigured_is_unknown_not_down`` (and its
     sibling ``test_probe_url_blank_url_is_unknown``).
  3. Building ``detail`` from ``str(exc)`` instead of the exception type must
     redden ``test_detail_survives_an_exception_with_empty_str`` — the exact
     bug (empty ``str()`` on an httpx connect timeout) that produced five
     useless "STT failed: " log lines during the 2026-08-13 incident.
  4. A 500 response classifying as "up" must redden
     ``test_probe_url_5xx_is_down_not_up`` (generic probe) and
     ``test_probe_backend_5xx_on_health_path_is_down_not_up`` (health-path
     probe) — a broken-but-listening service must not show green.
  5. A 404 response classifying as "up" must redden
     ``test_probe_url_404_is_unknown_not_up`` — reached-but-unconfirmed is
     not proof of life.
  6. A 401 response classifying as anything other than "up" must redden
     ``test_probe_url_401_is_up_not_down_or_unknown`` — an auth challenge IS
     proof the right service is alive; getting this backwards reads every
     gated service as broken.
"""

from __future__ import annotations

import asyncio

import httpx
import pytest

from skchat.health import (
    SERVICE_CATALOG,
    STATE_DOWN,
    STATE_UNKNOWN,
    STATE_UP,
    ServiceHealth,
    _as_http,
    _classify_response_status,
    _derive_health_url,
    _failure_detail,
    build_health_payload,
    probe_backend_with_health_path,
    probe_url,
    resolve_service_urls,
)


def _client(handler) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


# --------------------------------------------------------------------------- #
# probe_url — generic base-URL probe (used for the SFU, which has no known
# dedicated health path).
# --------------------------------------------------------------------------- #


async def _run_probe(handler, url="http://backend.test:9999/health"):
    async with _client(handler) as client:
        return await probe_url(client, "sfu", "Call server", url)


def test_probe_url_up_on_200():
    async def handler(request):
        return httpx.Response(200, text="ok")

    result = asyncio.run(_run_probe(handler))
    assert result.state == STATE_UP
    assert result.id == "sfu"
    assert result.label == "Call server"
    assert "200" in result.detail
    assert "ms" in result.detail
    assert isinstance(result.latency_ms, int)
    assert result.latency_ms >= 0
    assert result.checked_at.endswith("Z")


# --------------------------------------------------------------------------- #
# probe_url — mutation targets #4-6: status classification must not invent
# green (5xx/other-4xx) or misread a real proof-of-life signal (401/403).
# --------------------------------------------------------------------------- #


def test_probe_url_5xx_is_down_not_up():
    async def handler(request):
        return httpx.Response(500, text="internal error")

    result = asyncio.run(_run_probe(handler))
    assert result.state == STATE_DOWN
    assert result.state != STATE_UP
    assert "500" in result.detail


def test_probe_url_404_is_unknown_not_up():
    """Reached-but-unconfirmed (wrong path, nothing there) is "unknown"."""

    async def handler(request):
        return httpx.Response(404, text="not found")

    result = asyncio.run(_run_probe(handler))
    assert result.state == STATE_UNKNOWN
    assert result.state != STATE_UP
    assert "404" in result.detail


def test_probe_url_405_is_unknown_not_up():
    async def handler(request):
        return httpx.Response(405, text="method not allowed")

    result = asyncio.run(_run_probe(handler))
    assert result.state == STATE_UNKNOWN
    assert "405" in result.detail


def test_probe_url_401_is_up_not_down_or_unknown():
    """An auth challenge proves the RIGHT service is alive -- it is "up"."""

    async def handler(request):
        return httpx.Response(401, text="unauthorized")

    result = asyncio.run(_run_probe(handler))
    assert result.state == STATE_UP
    assert result.state != STATE_DOWN
    assert result.state != STATE_UNKNOWN
    assert "401" in result.detail


def test_probe_url_403_is_up():
    async def handler(request):
        return httpx.Response(403, text="forbidden")

    result = asyncio.run(_run_probe(handler))
    assert result.state == STATE_UP


# --------------------------------------------------------------------------- #
# probe_url — mutation target #1: a failed probe must never report "up".
# --------------------------------------------------------------------------- #


def test_probe_url_reports_down_on_connection_failure():
    async def handler(request):
        raise httpx.ConnectError("connection refused")

    result = asyncio.run(_run_probe(handler))
    assert result.state == STATE_DOWN
    assert result.state != STATE_UP


def test_probe_url_reports_down_on_timeout():
    async def handler(request):
        raise httpx.ConnectTimeout("timed out")

    result = asyncio.run(_run_probe(handler))
    assert result.state == STATE_DOWN


# --------------------------------------------------------------------------- #
# probe_url — mutation target #2: unconfigured must be "unknown", not "down".
# --------------------------------------------------------------------------- #


def test_probe_url_unconfigured_is_unknown_not_down():
    async def handler(request):
        raise AssertionError("must not probe an unconfigured URL")

    result = asyncio.run(_run_probe(handler, url=None))
    assert result.state == STATE_UNKNOWN
    assert result.state != STATE_DOWN
    assert "not configured" in result.detail
    assert result.latency_ms is None


def test_probe_url_blank_url_is_unknown():
    async def handler(request):
        raise AssertionError("must not probe a blank URL")

    result = asyncio.run(_run_probe(handler, url="   "))
    assert result.state == STATE_UNKNOWN
    assert result.state != STATE_DOWN


# --------------------------------------------------------------------------- #
# _classify_response_status — the pure classifier, tested directly.
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("code", [200, 201, 204, 299])
def test_classify_2xx_is_up(code):
    state, _reason = _classify_response_status(code)
    assert state == STATE_UP


@pytest.mark.parametrize("code", [401, 403])
def test_classify_auth_challenge_is_up(code):
    state, _reason = _classify_response_status(code)
    assert state == STATE_UP


@pytest.mark.parametrize("code", [500, 502, 503, 504])
def test_classify_5xx_is_down(code):
    state, _reason = _classify_response_status(code)
    assert state == STATE_DOWN


@pytest.mark.parametrize("code", [400, 404, 405, 429])
def test_classify_other_4xx_is_unknown(code):
    state, _reason = _classify_response_status(code)
    assert state == STATE_UNKNOWN


# --------------------------------------------------------------------------- #
# _failure_detail — mutation target #3: detail must survive str(exc) == "".
# --------------------------------------------------------------------------- #


def test_detail_survives_an_exception_with_empty_str():
    exc = httpx.ConnectTimeout("")
    assert str(exc) == ""  # sanity: this is the exact incident condition

    detail = _failure_detail(exc, "http://skworld-100:18794/v1/audio/transcriptions")
    assert detail  # must not be empty / falsy
    assert "ConnectTimeout" in detail
    assert "skworld-100:18794" in detail


def test_probe_url_detail_survives_empty_str_end_to_end():
    async def handler(request):
        raise httpx.ConnectTimeout("")

    result = asyncio.run(
        _run_probe(handler, url="http://skworld-100:18794/v1/audio/transcriptions")
    )
    assert result.state == STATE_DOWN
    assert result.detail  # never empty even though str(exc) is empty
    assert "ConnectTimeout" in result.detail
    assert "skworld-100:18794" in result.detail


def test_failure_detail_uses_type_for_a_bare_exception():
    """Even a plain Exception("") with no informative str() yields a real detail."""
    exc = Exception("")
    detail = _failure_detail(exc, "http://host:1234/x")
    assert detail
    assert "Exception" in detail
    assert "host:1234" in detail


# --------------------------------------------------------------------------- #
# probe_url — never raises.
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    "exc",
    [
        httpx.ConnectError(""),
        httpx.ReadTimeout(""),
        httpx.RemoteProtocolError(""),
        OSError(""),
        ValueError("unexpected"),
    ],
)
def test_probe_url_never_raises(exc):
    async def handler(request):
        raise exc

    result = asyncio.run(_run_probe(handler))
    assert result.state == STATE_DOWN
    assert result.detail


# --------------------------------------------------------------------------- #
# _as_http / _derive_health_url
# --------------------------------------------------------------------------- #


def test_as_http_converts_ws_scheme():
    assert _as_http("ws://skworld-100:7880") == "http://skworld-100:7880"


def test_as_http_converts_wss_scheme():
    assert _as_http("wss://noroc2027.example/livekit-ws") == "https://noroc2027.example/livekit-ws"


def test_as_http_passes_through_http():
    assert _as_http("http://localhost:18783/v1/chat/completions") == (
        "http://localhost:18783/v1/chat/completions"
    )


def test_derive_health_url_same_host_port_different_path():
    assert (
        _derive_health_url("http://skworld-100:18794/v1/audio/transcriptions")
        == "http://skworld-100:18794/health"
    )
    assert _derive_health_url("http://localhost:18783/v1/chat/completions") == (
        "http://localhost:18783/health"
    )
    assert _derive_health_url("http://skworld-100:18796/audio/speech") == (
        "http://skworld-100:18796/health"
    )


def test_derive_health_url_never_raises_on_garbage():
    assert _derive_health_url("not a url at all") == "/health" or "/health" in _derive_health_url(
        "not a url at all"
    )


# --------------------------------------------------------------------------- #
# probe_backend_with_health_path — the STT/TTS/LLM path (real /health first).
# --------------------------------------------------------------------------- #


async def _run_backend_probe(handler, base_url="http://backend.test:9999/v1/whatever"):
    async with _client(handler) as client:
        return await probe_backend_with_health_path(client, "stt", "Speech to text", base_url)


def test_probe_backend_up_on_health_200():
    seen = []

    async def handler(request):
        seen.append(str(request.url))
        return httpx.Response(200, text="ok")

    result = asyncio.run(_run_backend_probe(handler))
    assert result.state == STATE_UP
    assert "200" in result.detail
    # the /health path was actually hit, derived from the base URL's host:port
    assert seen == ["http://backend.test:9999/health"]


def test_probe_backend_5xx_on_health_path_is_down_not_up():
    async def handler(request):
        return httpx.Response(503, text="unhealthy")

    result = asyncio.run(_run_backend_probe(handler))
    assert result.state == STATE_DOWN
    assert result.state != STATE_UP
    assert "503" in result.detail


def test_probe_backend_401_on_health_path_is_up():
    async def handler(request):
        return httpx.Response(401, text="unauthorized")

    result = asyncio.run(_run_backend_probe(handler))
    assert result.state == STATE_UP


def test_probe_backend_falls_back_and_reports_unknown_on_health_404():
    """No /health deployed: fall back to the base URL, but NEVER call it "up"."""
    seen = []

    async def handler(request):
        seen.append(str(request.url))
        if str(request.url).endswith("/health"):
            return httpx.Response(404, text="not found")
        return httpx.Response(200, text="looks fine, but unconfirmed")

    result = asyncio.run(_run_backend_probe(handler, base_url="http://backend.test:9999/v1/x"))
    assert result.state == STATE_UNKNOWN
    assert result.state != STATE_UP
    assert seen == ["http://backend.test:9999/health", "http://backend.test:9999/v1/x"]
    assert "404" in result.detail
    assert "200" in result.detail  # base URL's own status is still surfaced


def test_probe_backend_fallback_down_if_base_url_also_fails():
    async def handler(request):
        if str(request.url).endswith("/health"):
            return httpx.Response(404, text="not found")
        raise httpx.ConnectError("refused")

    result = asyncio.run(_run_backend_probe(handler))
    assert result.state == STATE_DOWN
    assert "ConnectError" in result.detail


def test_probe_backend_down_on_health_path_connect_failure():
    async def handler(request):
        raise httpx.ConnectTimeout("")

    result = asyncio.run(_run_backend_probe(handler))
    assert result.state == STATE_DOWN
    assert "ConnectTimeout" in result.detail


def test_probe_backend_unconfigured_is_unknown():
    async def handler(request):
        raise AssertionError("must not probe an unconfigured URL")

    result = asyncio.run(_run_backend_probe(handler, base_url=""))
    assert result.state == STATE_UNKNOWN
    assert "not configured" in result.detail


# --------------------------------------------------------------------------- #
# resolve_service_urls — reads the SAME env schema the voice engine reads.
# --------------------------------------------------------------------------- #


def test_resolve_service_urls_reads_voice_config_env(monkeypatch):
    monkeypatch.setenv("SKVOICE_STT_URL", "http://stt.example:1")
    monkeypatch.setenv("SKVOICE_TTS_URL", "http://tts.example:2")
    monkeypatch.setenv("SKVOICE_LLM_URL", "http://llm.example:3")
    monkeypatch.setenv("SKCHAT_LIVEKIT_URL", "ws://sfu.example:4")

    urls = resolve_service_urls()
    assert urls["stt"] == "http://stt.example:1"
    assert urls["tts"] == "http://tts.example:2"
    assert urls["llm"] == "http://llm.example:3"
    assert urls["sfu"] == "ws://sfu.example:4"


def test_resolve_service_urls_blank_env_is_none_not_default(monkeypatch):
    """An operator explicitly blanking a URL must resolve to unconfigured.

    ``VoiceConfig.from_env`` treats a SET-but-blank env var as that blank
    value (it never falls back to its default for a present key), so this
    must come back None -> "unknown", never a stale guessed default.
    """
    monkeypatch.setenv("SKVOICE_STT_URL", "")
    monkeypatch.setenv("SKCHAT_LIVEKIT_URL", "")

    urls = resolve_service_urls()
    assert urls["stt"] is None
    assert urls["sfu"] is None


# --------------------------------------------------------------------------- #
# build_health_payload — full shape + concurrency + never-raises.
# --------------------------------------------------------------------------- #


def test_build_health_payload_shape(monkeypatch):
    monkeypatch.setenv("SKVOICE_STT_URL", "http://stt.example")
    monkeypatch.setenv("SKVOICE_TTS_URL", "http://tts.example")
    monkeypatch.setenv("SKVOICE_LLM_URL", "http://llm.example")
    monkeypatch.setenv("SKCHAT_LIVEKIT_URL", "ws://sfu.example")

    async def handler(request):
        return httpx.Response(200)

    async def run():
        async with _client(handler) as client:
            return await build_health_payload(client=client)

    payload = asyncio.run(run())

    assert set(payload.keys()) == {"generated_at", "services"}
    assert payload["generated_at"].endswith("Z")

    ids = [s["id"] for s in payload["services"]]
    assert ids == [sid for sid, _label in SERVICE_CATALOG]

    for svc in payload["services"]:
        assert set(svc.keys()) == {"id", "label", "state", "detail", "latency_ms", "checked_at"}
        assert svc["state"] in (STATE_UP, STATE_DOWN, STATE_UNKNOWN)

    webui = next(s for s in payload["services"] if s["id"] == "webui")
    assert webui["state"] == STATE_UP
    assert webui["label"] == "App server"

    # stt/tts/llm all got a genuine 200 on /health -> up. sfu (no health
    # path, base-URL probe) also got 200 -> up.
    for sid in ("stt", "tts", "llm", "sfu"):
        assert next(s for s in payload["services"] if s["id"] == sid)["state"] == STATE_UP


def test_build_health_payload_never_raises_on_mixed_failures(monkeypatch):
    """One dead backend cannot prevent the others from being reported."""
    monkeypatch.setenv("SKVOICE_STT_URL", "http://stt.example")
    monkeypatch.setenv("SKVOICE_TTS_URL", "")  # unconfigured
    monkeypatch.setenv("SKVOICE_LLM_URL", "http://llm.example")
    monkeypatch.setenv("SKCHAT_LIVEKIT_URL", "ws://sfu.example")

    async def handler(request):
        if "stt" in str(request.url):
            raise httpx.ConnectTimeout("")
        return httpx.Response(200)

    async def run():
        async with _client(handler) as client:
            return await build_health_payload(client=client)

    payload = asyncio.run(run())
    by_id = {s["id"]: s for s in payload["services"]}
    assert len(by_id) == 5  # every catalog entry present every time
    assert by_id["tts"]["state"] == STATE_UNKNOWN
    assert by_id["webui"]["state"] == STATE_UP
    for sid in ("stt", "tts", "llm", "sfu", "webui"):
        assert by_id[sid]["state"] in (STATE_UP, STATE_DOWN, STATE_UNKNOWN)


def test_build_health_payload_probes_concurrently():
    """Probes must run concurrently, not serialize behind one another.

    Each of 4 probed backends "hangs" until every one of them has started
    (an asyncio.Event released only once N have arrived). If probing were
    sequential, the 2nd probe would never see the 1st already waiting and
    this would deadlock into the test's own timeout below.
    """
    started = 0
    all_started = asyncio.Event()

    async def handler(request):
        nonlocal started
        started += 1
        if started >= 4:
            all_started.set()
        await asyncio.wait_for(all_started.wait(), timeout=2.0)
        return httpx.Response(200)

    async def run():
        async with _client(handler) as client:
            return await asyncio.wait_for(build_health_payload(client=client), timeout=3.0)

    payload = asyncio.run(run())
    assert started == 4
    assert all(s["state"] == STATE_UP for s in payload["services"] if s["id"] != "webui")


def test_service_health_to_dict():
    sh = ServiceHealth(
        id="llm",
        label="Language model",
        state=STATE_UP,
        detail="200 in 5ms",
        latency_ms=5,
        checked_at="2026-08-16T12:00:00Z",
    )
    assert sh.to_dict() == {
        "id": "llm",
        "label": "Language model",
        "state": STATE_UP,
        "detail": "200 in 5ms",
        "latency_ms": 5,
        "checked_at": "2026-08-16T12:00:00Z",
    }
