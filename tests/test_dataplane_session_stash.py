"""R3: the verified operator session must reach the route that needs device_fp."""

from __future__ import annotations

import pytest
from fastapi import Depends, FastAPI, Request
from fastapi.testclient import TestClient

from skchat import operator_auth as OA
from skchat.dataplane_auth import require_dataplane_auth


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("SKCHAT_OPERATOR_TOKEN_SECRET", "s" * 48)
    monkeypatch.setenv("SKCHAT_GUEST_REVOCATION_DB", str(tmp_path / "rev.db"))
    # Step 5 wires touch_throttled into the stash, which reads the registry.
    # Without this the suite would touch the real ~/.skchat.
    monkeypatch.setenv("SKCHAT_DEVICE_REGISTRY", str(tmp_path / "registry.json"))
    monkeypatch.setenv("SKCHAT_DATAPLANE_AUTH", "1")
    monkeypatch.setenv("SKCHAT_AUTHZ_PDP", "off")
    from skchat import guest as G

    G._reset_revocation_cache()
    G._reset_device_revocation_cache()

    app = FastAPI()

    @app.get("/probe")
    async def probe(request: Request, _auth: None = Depends(require_dataplane_auth)):
        session = getattr(request.state, "operator_session", None)
        return {"device_fp": getattr(session, "device_fp", None)}

    return TestClient(app)


def test_the_route_sees_the_device_fp_of_the_session_that_authenticated_it(client):
    token = OA.mint_operator_session(device_fp="a1b2c3d4e5f60718")
    r = client.get("/probe", headers={"Authorization": f"Bearer {token}"})
    assert r.status_code == 200
    assert r.json()["device_fp"] == "a1b2c3d4e5f60718"


def test_two_devices_are_told_apart_by_their_own_sessions(client):
    one = OA.mint_operator_session(device_fp="aa" * 8)
    two = OA.mint_operator_session(device_fp="bb" * 8)
    first = client.get("/probe", headers={"Authorization": f"Bearer {one}"})
    second = client.get("/probe", headers={"Authorization": f"Bearer {two}"})
    assert first.json()["device_fp"] == "aa" * 8
    assert second.json()["device_fp"] == "bb" * 8


def test_with_the_gate_off_there_is_no_session_and_that_is_not_an_error(
    client, monkeypatch
):
    # Gate off is the default in dev/tests: no credential is verified, so no
    # session exists. Routes must treat this as "unknown device", not a failure.
    monkeypatch.setenv("SKCHAT_DATAPLANE_AUTH", "")
    r = client.get("/probe")
    assert r.status_code == 200
    assert r.json()["device_fp"] is None
