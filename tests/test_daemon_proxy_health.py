"""Route-wiring test for ``GET /api/v1/health`` (card f2e6c451).

The probe/payload logic itself (states, honesty rules, concurrency) is
covered in ``tests/test_health.py`` against ``skchat.health`` directly with a
fake httpx transport. This file only checks the route delegates to it and
returns exactly what it produced, with no real network involved.
"""

from __future__ import annotations

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from skchat import daemon_proxy


@pytest.fixture
def client():
    app = FastAPI()
    app.include_router(daemon_proxy.router)
    return TestClient(app)


def test_health_route_returns_the_built_payload(client, monkeypatch):
    canned = {
        "generated_at": "2026-08-16T12:00:00Z",
        "services": [
            {
                "id": "stt",
                "label": "Speech to text",
                "state": "up",
                "detail": "200 in 34ms",
                "latency_ms": 34,
                "checked_at": "2026-08-16T12:00:00Z",
            }
        ],
    }

    async def fake_build_health_payload():
        return canned

    import skchat.health as health_module

    monkeypatch.setattr(health_module, "build_health_payload", fake_build_health_payload)

    r = client.get("/api/v1/health")
    assert r.status_code == 200, r.text
    assert r.json() == canned


def test_health_route_is_gated_like_status():
    """Same capability class as GET /api/v1/status -- leaks infra topology."""
    from skchat.dataplane_auth import route_capability
    from skchat.dataplane_paths import is_gated

    assert is_gated("GET", "/api/v1/health") is True
    assert route_capability("GET", "/api/v1/health") == "skchat.status"
