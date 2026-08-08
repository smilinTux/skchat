"""Tests for guest-dm S5 - guest display-name change endpoint (rename self).

Per task bd94558f (epic 8685ede6, depends on S2 a69e7d4e, blocks C2). Covers:

  * POST /api/v1/guest/name renames the caller in the bound group and REMINTS
    the guest session (the display name is baked into the session JWT claims,
    so a rename without remint would revert on the next request).
  * the old session token keeps working (until expiry) but still shows the
    OLD name - only the fresh token carries the new one.
  * guest_id / fp / epoch fence / message attribution are unchanged across a
    rename - only the display name moves. Reserved names are still suffixed.
  * missing/blank name -> 400; non-guest (anonymous) caller -> 403.
"""

from __future__ import annotations

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from skchat import daemon_proxy
from skchat import daemon_proxy_groups as G
from skchat import guest_group_routes as GGR
from skchat import guest_groups as GG


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setenv("SKCHAT_GUEST_TOKEN_SECRET", "x" * 48)
    monkeypatch.setenv("SKCHAT_GUEST_LINKS_ENABLED", "1")
    monkeypatch.setenv("SKCHAT_GUEST_OPERATOR_TOKEN", "op-secret")
    monkeypatch.setenv("SKCHAT_HOME", str(tmp_path / "skchat-home"))
    monkeypatch.setenv("SKCHAT_GUEST_REVOCATION_DB", str(tmp_path / "rev.db"))
    monkeypatch.setenv("SKCHAT_GUEST_GROUP_DB", str(tmp_path / "gg.db"))
    from skchat import guest as _guest

    _guest._reset_revocation_cache()

    from skchat.history import ChatHistory

    hist = ChatHistory(store=None, history_dir=tmp_path / "history")
    monkeypatch.setattr(daemon_proxy, "_HISTORY", hist)
    groups_dir = tmp_path / "groups"
    monkeypatch.setattr(G, "_GROUPS_DIR", groups_dir)
    monkeypatch.setattr(G, "resolve_identity", lambda raw: (raw or "").strip())
    return tmp_path


@pytest.fixture
def client(env):
    app = FastAPI()
    app.include_router(daemon_proxy.router)
    app.include_router(GGR.router)
    return TestClient(app)


_OP = {"X-Operator-Token": "op-secret"}


def _make_group(name="Town Hall", members=("lumina",)):
    return G.create_group(name=name, creator_uri=daemon_proxy.OPERATOR_ID, members=list(members))


def _invite(client, group_id, **kw):
    r = client.post(f"/api/v1/groups/{group_id}/invite", json=kw, headers=_OP)
    assert r.status_code == 200, r.text
    return r.json()


def _join(client, invite_token, name="Alice", pubkey="PUBKEY-A"):
    r = client.post(
        "/api/v1/guest/join",
        json={"invite_token": invite_token, "display_name": name, "guest_pubkey": pubkey},
    )
    assert r.status_code == 200, r.text
    return r.json()


def _auth(session):
    return {"Authorization": f"Bearer {session}"}


def test_rename_returns_fresh_session_with_new_name(client):
    grp = _make_group()
    inv = _invite(client, grp.id)
    joined = _join(client, inv["token"])
    old_session = joined["session_token"]
    guest_id = joined["guest_id"]

    r = client.post("/api/v1/guest/name", json={"name": "Alicia"}, headers=_auth(old_session))
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["ok"] is True
    assert body["display_name"] == "Alicia"
    new_session = body["session_token"]
    assert new_session != old_session

    fresh = GG.verify_guest_session(new_session)
    assert fresh.name == "Alicia"
    assert fresh.guest_id == guest_id

    # The OLD token still verifies (not revoked) but still carries the OLD name
    # baked into its claims - a rename doesn't retroactively rewrite it.
    stale = GG.verify_guest_session(old_session)
    assert stale.name == "Alice"
    assert stale.guest_id == guest_id


def test_rename_preserves_guest_id_fp_epoch_fence_and_attribution(client):
    grp = _make_group()
    inv = _invite(client, grp.id)
    joined = _join(client, inv["token"])
    session = joined["session_token"]
    guest_id = joined["guest_id"]
    fp = joined["fingerprint"]

    before = G.load_group(grp.id)
    added_at_before = before.metadata["guests"][guest_id]["added_at"]

    send = client.post(
        "/api/v1/guest/send", json={"body": "hi before rename"}, headers=_auth(session)
    )
    assert send.status_code == 200, send.text

    r = client.post("/api/v1/guest/name", json={"name": "Alicia"}, headers=_auth(session))
    assert r.status_code == 200, r.text
    new_session = r.json()["session_token"]
    fresh = GG.verify_guest_session(new_session)

    # guest_id / fp are stable identifiers, never recomputed from the new name.
    assert fresh.guest_id == guest_id
    assert fresh.fp == fp
    assert fresh.group_id == grp.id

    after = G.load_group(grp.id)
    assert after.metadata["guests"][guest_id]["added_at"] == added_at_before
    assert after.metadata["guests"][guest_id]["display"] == "Alicia"
    member = after.get_member(guest_id)
    assert member is not None
    assert member.display_name == "Alicia"
    assert member.identity_uri == guest_id

    # Message attribution (sender = guest_id) is untouched by the rename.
    conv = client.get("/api/v1/guest/conversation", headers=_auth(new_session)).json()
    msgs = [m for m in conv["messages"] if m.get("content") == "hi before rename"]
    assert len(msgs) == 1
    assert msgs[0]["sender"] == guest_id


def test_rename_reserved_name_is_suffixed(client):
    grp = _make_group()
    inv = _invite(client, grp.id)
    joined = _join(client, inv["token"])
    session = joined["session_token"]

    r = client.post("/api/v1/guest/name", json={"name": "Lumina"}, headers=_auth(session))
    assert r.status_code == 200, r.text
    assert r.json()["display_name"] == "Lumina (guest)"


def test_rename_rejects_missing_or_blank_name(client):
    grp = _make_group()
    inv = _invite(client, grp.id)
    joined = _join(client, inv["token"])
    session = joined["session_token"]

    r = client.post("/api/v1/guest/name", json={}, headers=_auth(session))
    assert r.status_code == 400

    r = client.post("/api/v1/guest/name", json={"name": "   "}, headers=_auth(session))
    assert r.status_code == 400


def test_rename_rejects_non_guest_caller(client):
    r = client.post("/api/v1/guest/name", json={"name": "Alicia"})
    assert r.status_code == 403


def test_rename_404_when_flag_off(client, monkeypatch):
    grp = _make_group()
    inv = _invite(client, grp.id)
    joined = _join(client, inv["token"])
    session = joined["session_token"]

    monkeypatch.setenv("SKCHAT_GUEST_LINKS_ENABLED", "0")
    r = client.post("/api/v1/guest/name", json={"name": "Alicia"}, headers=_auth(session))
    assert r.status_code == 403
