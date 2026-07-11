"""Tests for the Sovereign Conf Calls REST API (conf/routes.py).

Mirrors tests/test_spaces_routes.py: create -> token -> end with dummy creds (no
live SFU), JWT shape assertions on the minted tokens, graceful roster degradation
without a reachable SFU, and route registration on the app.
"""

import jwt
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from skchat.conf.room import ConfRegistry
from skchat.conf.routes import register_conf_routes

_KEY, _SECRET = "test-key", "test-secret-0123456789"


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("SKCHAT_LIVEKIT_API_KEY", _KEY)
    monkeypatch.setenv("SKCHAT_LIVEKIT_API_SECRET", _SECRET)
    monkeypatch.setenv("SKCHAT_LIVEKIT_URL", "ws://test-sfu:7880")
    app = FastAPI()
    # inject a tmp-path registry so tests don't touch ~/.skchat
    register_conf_routes(app, registry=ConfRegistry(path=tmp_path / "confs.json"))
    return TestClient(app)


def _video(token):
    return jwt.decode(token, _SECRET, algorithms=["HS256"], options={"verify_aud": False})["video"]


def _create(client, **over):
    body = {"host_fqid": "lumina@chef.skworld", "title": "Standup"}
    body.update(over)
    return client.post("/conf/create", json=body)


def test_create_returns_sovereign_token_and_registers(client):
    r = _create(client, slug="standup")
    assert r.status_code == 200
    body = r.json()
    assert body["conf_id"].startswith("conf-")
    assert body["room"] == body["conf_id"]
    assert body["role"] == "sovereign"
    assert body["join_url"] == f"/conf/{body['room']}"
    # SOVEREIGN host with sovereign_admin=True carries roomAdmin + full publish
    v = _video(body["token"])
    assert v["roomAdmin"] is True
    assert v["canPublish"] is True

    live = client.get("/conf").json()["confs"]
    assert any(c["conf_id"] == body["conf_id"] for c in live)


def test_create_named_slug_is_deterministic(client):
    a = _create(client, slug="weekly").json()["conf_id"]
    b = _create(client, slug="weekly").json()["conf_id"]
    assert a == b  # same (host, slug) -> same room


def test_create_adhoc_without_slug_is_random(client):
    a = _create(client).json()["conf_id"]
    b = _create(client).json()["conf_id"]
    assert a != b  # no slug -> fresh random room each time


def test_create_requires_host_and_title(client):
    assert _create(client, host_fqid="", slug="x").status_code == 400
    assert _create(client, title="", slug="x").status_code == 400


def test_create_rejects_overlong_title(client):
    assert _create(client, title="x" * 121, slug="long").status_code == 400
    assert _create(client, title="x" * 120, slug="ok").status_code == 200


def test_create_503_without_creds(client, monkeypatch):
    monkeypatch.delenv("SKCHAT_LIVEKIT_API_KEY", raising=False)
    monkeypatch.delenv("SKCHAT_LIVEKIT_API_SECRET", raising=False)
    assert _create(client, slug="nocreds").status_code == 503


def test_token_defaults_to_participant_and_is_a_jwt(client):
    room = _create(client, slug="s").json()["room"]
    r = client.post(f"/conf/{room}/token", json={"identity": "opus@chef.skworld", "name": "Opus"})
    assert r.status_code == 200
    body = r.json()
    assert body["role"] == "participant"
    v = _video(body["token"])  # decodes -> it is a valid signed JWT
    assert v["room"] == room
    assert v["canPublish"] is True
    # PARTICIPANT must NOT be a room admin
    assert v.get("roomAdmin", False) is False


def test_token_honors_explicit_role(client):
    room = _create(client, slug="s").json()["room"]
    r = client.post(
        f"/conf/{room}/token",
        json={"identity": "guest@x.y", "role": "guest_conf"},
    )
    assert r.status_code == 200
    assert r.json()["role"] == "guest_conf"
    # guest can never carry room_admin (factory-enforced)
    assert _video(r.json()["token"]).get("roomAdmin", False) is False


def test_token_rejects_unknown_role(client):
    room = _create(client, slug="s").json()["room"]
    r = client.post(f"/conf/{room}/token", json={"identity": "x@y.z", "role": "emperor"})
    assert r.status_code == 400


def test_token_requires_identity(client):
    room = _create(client, slug="s").json()["room"]
    assert client.post(f"/conf/{room}/token", json={}).status_code == 400


def test_token_unknown_room_404(client):
    r = client.post("/conf/conf-doesnotexist0/token", json={"identity": "x@y.z"})
    assert r.status_code == 404


def test_participants_degrades_gracefully(client):
    """No reachable SFU -> empty roster + live=false, never a 5xx."""
    room = _create(client, slug="s").json()["room"]
    r = client.get(f"/conf/{room}/participants")
    assert r.status_code == 200
    body = r.json()
    assert body["participants"] == []
    assert body["live"] is False


def test_participants_unknown_room_404(client):
    assert client.get("/conf/conf-nope000000000/participants").status_code == 404


def test_end_marks_not_live_host_gated(client):
    room = _create(client, slug="s").json()["room"]
    # non-host cannot end
    assert client.post(f"/conf/{room}/end", json={"requester": "rando@x.y"}).status_code == 403
    # host can end
    r = client.post(f"/conf/{room}/end", json={"requester": "lumina@chef.skworld"})
    assert r.status_code == 200
    assert r.json()["ok"] is True
    live = client.get("/conf").json()["confs"]
    assert all(c["conf_id"] != room for c in live)


def test_token_on_ended_conf_404(client):
    room = _create(client, slug="s").json()["room"]
    client.post(f"/conf/{room}/end", json={"requester": "lumina@chef.skworld"})
    r = client.post(f"/conf/{room}/token", json={"identity": "x@y.z"})
    assert r.status_code == 404


def test_end_unknown_room_404(client):
    assert (
        client.post("/conf/conf-nope000000000/end", json={"requester": "x@y.z"}).status_code == 404
    )


def test_routes_registered_on_app():
    app = FastAPI()
    register_conf_routes(app)
    paths = {r.path for r in app.routes}
    assert "/conf/create" in paths
    assert "/conf/{room}/token" in paths
    assert "/conf/{room}/participants" in paths
    assert "/conf/{room}/end" in paths
    assert "/conf" in paths
    assert "/conf/health" in paths
    assert "/conf/{room}/waiting" in paths
    assert "/conf/{room}/admit" in paths
    assert "/conf/{room}/deny" in paths
    assert "/conf/{room}/federated-token" in paths


def test_conf_health_returns_ok(client):
    r = client.get("/conf/health")
    assert r.status_code == 200
    body = r.json()
    assert body["service"] == "skchat-conf"
    assert body["status"] == "ok"
    assert "live_confs" in body
    assert "livekit_configured" in body
    assert body["livekit_configured"] is True


def test_enter_waiting_room_and_admit(client):
    conf = _create(client, slug="wait-test").json()
    room = conf["room"]

    # Enter waiting room
    r = client.post(f"/conf/{room}/waiting", json={"identity": "guest:abc123", "display": "Alice"})
    assert r.status_code == 200
    body = r.json()
    assert body["admitted"] is False
    assert body["identity"] == "guest:abc123"

    # Check waiting room status
    r = client.get(f"/conf/{room}/waiting")
    assert r.status_code == 200
    body = r.json()
    assert len(body["waiting"]) == 1
    assert body["waiting"][0]["identity"] == "guest:abc123"

    # Host admits
    r = client.post(f"/conf/{room}/admit", json={
        "requester": "lumina@chef.skworld",
        "identity": "guest:abc123",
    })
    assert r.status_code == 200
    assert r.json()["ok"] is True

    # Waiting room should now be empty, guest admitted
    r = client.get(f"/conf/{room}/waiting")
    assert r.status_code == 200
    assert len(r.json()["waiting"]) == 0
    assert "guest:abc123" in r.json()["admitted"]


def test_deny_guest(client):
    conf = _create(client, slug="deny-test").json()
    room = conf["room"]

    client.post(f"/conf/{room}/waiting", json={"identity": "guest:denied1", "display": "Bob"})
    r = client.post(f"/conf/{room}/deny", json={
        "requester": "lumina@chef.skworld",
        "identity": "guest:denied1",
    })
    assert r.status_code == 200

    # Re-entry should be denied
    r = client.post(f"/conf/{room}/waiting", json={"identity": "guest:denied1", "display": "Bob"})
    assert r.status_code == 403


def test_admit_nonexistent_guest_graceful(client):
    conf = _create(client, slug="noop-admit").json()
    room = conf["room"]
    r = client.post(f"/conf/{room}/admit", json={
        "requester": "lumina@chef.skworld",
        "identity": "guest:nobody",
    })
    assert r.status_code == 200  # admit is idempotent


def test_waiting_conf_not_found(client):
    assert client.post("/conf/conf-nonexistent/waiting", json={"identity": "guest:x"}).status_code == 404
    assert client.get("/conf/conf-nonexistent/waiting").status_code == 404
    assert client.post("/conf/conf-nonexistent/admit", json={
        "requester": "lumina@chef.skworld", "identity": "guest:x",
    }).status_code == 404


def test_waiting_requires_identity(client):
    conf = _create(client, slug="req-id").json()
    room = conf["room"]
    assert client.post(f"/conf/{room}/waiting", json={}).status_code == 400


def test_admit_requires_host(client):
    conf = _create(client, slug="host-gate").json()
    room = conf["room"]
    client.post(f"/conf/{room}/waiting", json={"identity": "guest:x", "display": "X"})
    r = client.post(f"/conf/{room}/admit", json={
        "requester": "wrong@host",
        "identity": "guest:x",
    })
    assert r.status_code == 403


def test_federated_token_rejects_missing_body(client):
    conf = _create(client, slug="fed-test").json()
    room = conf["room"]
    assert client.post(f"/conf/{room}/federated-token", json={}).status_code == 400


def test_federated_token_rejects_missing_claim_or_sig(client):
    conf = _create(client, slug="fed-test2").json()
    room = conf["room"]
    assert client.post(f"/conf/{room}/federated-token", json={"claim": "x"}).status_code == 400
    assert client.post(f"/conf/{room}/federated-token", json={"sig": "y"}).status_code == 400


def test_federated_token_404_for_unknown_conf(client):
    assert client.post("/conf/conf-nonexistent/federated-token", json={
        "claim": {"fqid": "x@y", "nonce": "n"},
        "sig": "s",
    }).status_code == 404


def test_federated_token_rejects_bad_assertion(client):
    conf = _create(client, slug="fed-bad").json()
    room = conf["room"]
    r = client.post(f"/conf/{room}/federated-token", json={
        "claim": {"fqid": "x@y", "nonce": "n", "space_id": "s", "issued_at": 1000},
        "sig": "bad",
    })
    assert r.status_code in (400, 403)  # federation module missing or assertion rejected


# ── Native app hand-off (coord 59184ca7) ──────────────────────────────────────
def test_conf_page_hands_off_to_native_app_by_default(client):
    """A shared /conf/{room} link lands in the native Flutter app deep link
    (/app/#/conf?room=...), not the legacy web livekit.html, by default."""
    r = client.get("/conf/conf-standup0001")
    assert r.status_code == 200
    body = r.text
    assert "/app/#/conf?room=conf-standup0001" in body
    # Legacy web client is NOT the default target.
    assert "url=/livekit/" not in body
    # Still a recognizable conference landing page.
    assert "conference" in body.lower()


def test_conf_page_web_fallback_keeps_livekit_html(client):
    """?web=1 preserves the legacy web client so nothing is lost."""
    r = client.get("/conf/conf-standup0002?web=1")
    assert r.status_code == 200
    body = r.text
    assert "/livekit/conf-standup0002?room=conf-standup0002" in body
    assert "/app/#/conf" not in body


def test_conf_page_escapes_room_name(client):
    """A hostile room name must not break out of the markup or redirect URL."""
    r = client.get("/conf/%3Cimg%20src=x%3E")
    assert r.status_code == 200
    assert "<img src=x>" not in r.text
