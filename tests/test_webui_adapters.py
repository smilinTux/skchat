"""Tests for the webui GET /adapters health endpoint.

Documented shape per adapter: {name, channel_type, connected, latency_ms, error}.
The endpoint reads from a live AdapterRegistry if present, else returns [] with
a 200 (must never 500 when the registry isn't wired up yet).
"""

from __future__ import annotations

from types import SimpleNamespace

from fastapi.testclient import TestClient

from skchat import webui

_FIELDS = {"name", "channel_type", "connected", "latency_ms", "error"}


def _client() -> TestClient:
    return TestClient(webui.app)


class _StubAdapter:
    def __init__(self, name, channel_type, connected, latency_ms=None, error=None):
        self.name = name
        self.channel_type = channel_type
        self.connected = connected
        self.latency_ms = latency_ms
        self.error = error


class _StubRegistry:
    """Minimal AdapterRegistry stand-in exposing an ``adapters`` attribute."""

    def __init__(self, adapters):
        self.adapters = list(adapters)


def test_adapters_no_registry_returns_empty_list(monkeypatch):
    monkeypatch.setattr(webui, "_get_adapter_registry", lambda: None)
    r = _client().get("/adapters")
    assert r.status_code == 200
    assert r.json() == []


def test_adapters_no_registry_does_not_500(monkeypatch):
    # Default resolution path (no integration.adapter_registry set) must be graceful.
    import skchat.integration as integ

    monkeypatch.delattr(integ, "adapter_registry", raising=False)
    r = _client().get("/adapters")
    assert r.status_code == 200
    assert r.json() == []


def test_adapters_with_stub_registry_reports_shape(monkeypatch):
    reg = _StubRegistry(
        [
            _StubAdapter("matrix", "matrix", True, latency_ms=12),
            _StubAdapter("telegram", "telegram", False, error="auth expired"),
        ]
    )
    monkeypatch.setattr(webui, "_get_adapter_registry", lambda: reg)

    r = _client().get("/adapters")
    assert r.status_code == 200
    body = r.json()
    assert isinstance(body, list)
    assert len(body) == 2

    for entry in body:
        assert _FIELDS.issubset(entry.keys())

    by_name = {e["name"]: e for e in body}
    assert by_name["matrix"]["connected"] is True
    assert by_name["matrix"]["channel_type"] == "matrix"
    assert by_name["matrix"]["latency_ms"] == 12
    assert by_name["matrix"]["error"] is None

    assert by_name["telegram"]["connected"] is False
    assert by_name["telegram"]["error"] == "auth expired"


def test_adapters_fake_adapter_shows_connected_true(monkeypatch):
    reg = _StubRegistry([_StubAdapter("p2p", "p2p", True, latency_ms=3)])
    monkeypatch.setattr(webui, "_get_adapter_registry", lambda: reg)

    r = _client().get("/adapters")
    assert r.status_code == 200
    body = r.json()
    assert len(body) == 1
    assert body[0]["connected"] is True
    assert body[0]["name"] == "p2p"


def test_adapters_registry_via_callable_and_dict(monkeypatch):
    # Registry exposing ``adapters`` as a callable returning a dict.
    fake = _StubAdapter("matrix", "matrix", True)
    reg = SimpleNamespace(adapters=lambda: {"matrix": fake})
    monkeypatch.setattr(webui, "_get_adapter_registry", lambda: reg)

    r = _client().get("/adapters")
    assert r.status_code == 200
    body = r.json()
    assert len(body) == 1
    assert body[0]["name"] == "matrix"
    assert body[0]["connected"] is True


def test_adapters_broken_registry_does_not_500(monkeypatch):
    class _Boom:
        @property
        def adapters(self):
            raise RuntimeError("registry exploded")

    monkeypatch.setattr(webui, "_get_adapter_registry", lambda: _Boom())
    r = _client().get("/adapters")
    assert r.status_code == 200
    assert r.json() == []
