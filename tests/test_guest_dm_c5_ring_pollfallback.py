"""guest-dm C5: the guest-call ring POLL FALLBACK for clients without a ws channel.

S6 rings the operator over a ws broadcast, but skworld-app polls (no ws). So a
guest call also stamps a transient ring the operator group/conversation payload
surfaces (``ringing``/``ring_ts``), which the app polls. Muted guests never ring.
"""

from __future__ import annotations

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from skchat import call_observability as CO
from skchat import daemon_proxy, livekit_routes
from skchat import daemon_proxy_groups as G
from skchat import guest_group_routes as GGR
from skchat import guest_groups as GG
from skchat import webui as _webui

_KEY, _SECRET = "test-key", "test-secret-0123456789"
_OP = {"X-Operator-Token": "op-secret"}
_OPERATOR = "capauth:lumina@skworld.io"


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setenv("SKCHAT_GUEST_TOKEN_SECRET", "x" * 48)
    monkeypatch.setenv("SKCHAT_GUEST_LINKS_ENABLED", "1")
    monkeypatch.setenv("SKCHAT_GUEST_OPERATOR_TOKEN", "op-secret")
    monkeypatch.setenv("SKCHAT_HOME", str(tmp_path / "skchat-home"))
    monkeypatch.setenv("SKCHAT_GUEST_REVOCATION_DB", str(tmp_path / "rev.db"))
    monkeypatch.setenv("SKCHAT_GUEST_GROUP_DB", str(tmp_path / "gg.db"))
    monkeypatch.setattr(livekit_routes, "LIVEKIT_API_KEY", _KEY)
    monkeypatch.setattr(livekit_routes, "LIVEKIT_API_SECRET", _SECRET)
    monkeypatch.setenv("SKCHAT_LIVEKIT_API_KEY", _KEY)
    monkeypatch.setenv("SKCHAT_LIVEKIT_API_SECRET", _SECRET)
    from skchat.history import ChatHistory

    hist = ChatHistory(store=None, history_dir=tmp_path / "history")
    monkeypatch.setattr(daemon_proxy, "_HISTORY", hist)
    groups_dir = tmp_path / "groups"
    monkeypatch.setattr(G, "_GROUPS_DIR", groups_dir)
    monkeypatch.setattr(G, "resolve_identity", lambda raw: (raw or "").strip())
    # The ring registry is module-global in-memory state; isolate each test.
    GG._guest_ring_ts.clear()
    return tmp_path


@pytest.fixture
def client(env):
    app = FastAPI()
    app.include_router(daemon_proxy.router)
    app.include_router(GGR.router)
    return TestClient(app)


@pytest.fixture(autouse=True)
def _spies(monkeypatch):
    async def _fake_broadcast(msg_dict):
        pass

    monkeypatch.setattr(_webui, "_ws_broadcast", _fake_broadcast)
    monkeypatch.setattr(CO, "alert_operator", lambda **kw: None)


def _auth(session):
    return {"Authorization": f"Bearer {session}"}


def _join_dm(client, name="Alice", pubkey="PUBKEY-A", alias=None):
    inv = GG.create_dm_invite(operator_uri=_OPERATOR)
    if alias is not None:
        GG.store_dm_invite_meta(inv["jti"], alias=alias)
    r = client.post(
        "/api/v1/guest/join",
        json={"invite_token": inv["token"], "display_name": name, "guest_pubkey": pubkey},
    )
    assert r.status_code == 200, r.text
    return r.json()


# ── ring registry unit behaviour ─────────────────────────────────────────────
def test_ring_registry_marks_reads_expires_clears():
    GG.mark_guest_ringing("fp-x", now=1000.0)
    assert GG.guest_ring_ts("fp-x", now=1005.0) == 1000.0
    # Past the TTL it is gone (and self-pruned).
    assert GG.guest_ring_ts("fp-x", now=1000.0 + GG._GUEST_RING_TTL_SEC + 1) is None
    assert "fp-x" not in GG._guest_ring_ts

    GG.mark_guest_ringing("fp-y")
    assert GG.guest_ring_ts("fp-y") is not None
    GG.clear_guest_ring("fp-y")
    assert GG.guest_ring_ts("fp-y") is None


# ── the operator poll payload reflects a live ring ───────────────────────────
def test_guest_call_stamps_ring_visible_in_operator_payload(client):
    j = _join_dm(client, alias="Bestie")
    gid = j["group"]["id"]
    fp = GG.get_dm_contact_by_group(gid)["fp"]

    # Nothing ringing before the call.
    assert GG.guest_ring_ts(fp) is None
    assert G._dm_guest_badge(G.load_group(gid))["ringing"] is False

    r = client.post("/api/v1/guest/call", json={"ring": True}, headers=_auth(j["session_token"]))
    assert r.status_code == 200, r.text

    # The poll-only app sees ringing:true + a ring_ts on the operator payload.
    assert GG.guest_ring_ts(fp) is not None
    badge = G._dm_guest_badge(G.load_group(gid))
    assert badge["ringing"] is True
    assert isinstance(badge["ring_ts"], (int, float))


def test_muted_guest_call_never_stamps_a_ring(client):
    j = _join_dm(client, name="Carol", pubkey="PUBKEY-C")
    gid = j["group"]["id"]
    fp = GG.get_dm_contact_by_group(gid)["fp"]
    GG.update_dm_contact(fp, muted=True)

    r = client.post("/api/v1/guest/call", json={"ring": True}, headers=_auth(j["session_token"]))
    assert r.status_code == 200, r.text

    # Muted -> no ring stamped, operator payload stays quiet.
    assert GG.guest_ring_ts(fp) is None
    assert G._dm_guest_badge(G.load_group(gid))["ringing"] is False
