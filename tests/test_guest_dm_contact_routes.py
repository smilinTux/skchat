"""Tests for guest-dm S4 - operator contact routes (list/patch/revoke) + the
operator group-listing guest badge.

Per task 35e2f911 (epic 8685ede6, depends on S2 a69e7d4e, pairs with S3
a0b8f930). Covers:

  * GET /api/v1/guest-dm/contacts, PATCH .../{fp}, POST .../{fp}/revoke are
    operator-gated - 401/403 for guest-authed or anonymous callers.
  * /api/v1/guest-dm/* is gated by the dataplane middleware classifier (it is
    NOT swept into the /api/v1/guest exemption - segment-boundary anchoring).
  * the operator group listing for a mode=dm group carries guest_dm/
    guest_name/guest_alias/guest_status/muted.
  * the alias never leaks into any /guest/* response.
"""

from __future__ import annotations

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from skchat import daemon_proxy
from skchat import daemon_proxy_groups as G
from skchat import guest_group_routes as GGR
from skchat import guest_groups as GG
from skchat.dataplane_paths import is_gated


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


_OPERATOR = "capauth:lumina@skworld.io"
_OP_HEADERS = {"X-Operator-Token": "op-secret"}


def _join(client, invite_token, name="Alice", pubkey="PUBKEY-A"):
    return client.post(
        "/api/v1/guest/join",
        json={"invite_token": invite_token, "display_name": name, "guest_pubkey": pubkey},
    )


def _mint_and_join(client, *, alias=None, contact_ttl=None, name="Alice", pubkey="KEY-A"):
    inv = GG.create_dm_invite(operator_uri=_OPERATOR)
    if alias is not None or contact_ttl is not None:
        GG.store_dm_invite_meta(inv["jti"], alias=alias, contact_ttl=contact_ttl)
    r = _join(client, inv["token"], name=name, pubkey=pubkey)
    assert r.status_code == 200, r.text
    fp = GG.pubkey_fingerprint(pubkey)
    return inv, r.json(), fp


# ── list ──────────────────────────────────────────────────────────────────────
def test_list_contacts_operator_gated(env, client):
    _mint_and_join(client, alias="Bestie")

    r = client.get("/api/v1/guest-dm/contacts", headers=_OP_HEADERS)
    assert r.status_code == 200, r.text
    contacts = r.json()["contacts"]
    assert len(contacts) == 1
    c = contacts[0]
    assert c["alias"] == "Bestie"
    assert c["guest_name"] == "Alice"
    assert c["status"] == "active"
    assert c["muted"] is False
    assert "group_id" in c and "created_at" in c and "last_seen_at" in c


def test_list_contacts_rejects_anonymous(env, client):
    r = client.get("/api/v1/guest-dm/contacts")
    assert r.status_code == 401


def test_list_contacts_rejects_guest_authed_caller(env, client):
    _inv, body, _fp = _mint_and_join(client)
    session = body["session_token"]
    r = client.get(
        "/api/v1/guest-dm/contacts", headers={"Authorization": f"Bearer {session}"}
    )
    assert r.status_code in (401, 403)


# ── patch ─────────────────────────────────────────────────────────────────────
def test_patch_contact_updates_alias_and_mute(env, client):
    _inv, _body, fp = _mint_and_join(client)

    r = client.patch(
        f"/api/v1/guest-dm/contacts/{fp}",
        json={"alias": "Nickname", "muted": True},
        headers=_OP_HEADERS,
    )
    assert r.status_code == 200, r.text
    contact = GG.get_dm_contact(fp)
    assert contact["alias"] == "Nickname"
    assert contact["muted"] == 1


def test_patch_contact_ttl_sets_expiry(env, client):
    _inv, _body, fp = _mint_and_join(client)
    before = GG.get_dm_contact(fp)["contact_expires_at"]
    assert before is None

    r = client.patch(
        f"/api/v1/guest-dm/contacts/{fp}", json={"contact_ttl": 3600}, headers=_OP_HEADERS
    )
    assert r.status_code == 200, r.text
    after = GG.get_dm_contact(fp)["contact_expires_at"]
    assert after is not None and after > time_module().time()


def time_module():
    import time

    return time


def test_patch_contact_unknown_fp_404(env, client):
    r = client.patch(
        "/api/v1/guest-dm/contacts/no-such-fp", json={"alias": "x"}, headers=_OP_HEADERS
    )
    assert r.status_code == 404


def test_patch_contact_rejects_anonymous(env, client):
    _inv, _body, fp = _mint_and_join(client)
    r = client.patch(f"/api/v1/guest-dm/contacts/{fp}", json={"alias": "x"})
    assert r.status_code == 401


def test_patch_contact_rejects_guest_authed_caller(env, client):
    _inv, body, fp = _mint_and_join(client)
    session = body["session_token"]
    r = client.patch(
        f"/api/v1/guest-dm/contacts/{fp}",
        json={"alias": "x"},
        headers={"Authorization": f"Bearer {session}"},
    )
    assert r.status_code in (401, 403)


# ── revoke ────────────────────────────────────────────────────────────────────
def test_revoke_contact_via_route(env, client):
    _inv, _body, fp = _mint_and_join(client)

    r = client.post(f"/api/v1/guest-dm/contacts/{fp}/revoke", headers=_OP_HEADERS)
    assert r.status_code == 200, r.text
    assert GG.get_dm_contact(fp)["status"] == "revoked"


def test_revoke_contact_unknown_fp_404(env, client):
    r = client.post("/api/v1/guest-dm/contacts/no-such-fp/revoke", headers=_OP_HEADERS)
    assert r.status_code == 404


def test_revoke_contact_rejects_anonymous(env, client):
    _inv, _body, fp = _mint_and_join(client)
    r = client.post(f"/api/v1/guest-dm/contacts/{fp}/revoke")
    assert r.status_code == 401


def test_revoke_contact_rejects_guest_authed_caller(env, client):
    _inv, body, fp = _mint_and_join(client)
    session = body["session_token"]
    r = client.post(
        f"/api/v1/guest-dm/contacts/{fp}/revoke",
        headers={"Authorization": f"Bearer {session}"},
    )
    assert r.status_code in (401, 403)


# ── feature flag off -> 404, matching the sibling operator routes ────────────
def test_routes_404_when_flag_off(env, client, monkeypatch):
    monkeypatch.setenv("SKCHAT_GUEST_LINKS_ENABLED", "0")
    assert client.get("/api/v1/guest-dm/contacts", headers=_OP_HEADERS).status_code == 404
    assert (
        client.patch(
            "/api/v1/guest-dm/contacts/x", json={}, headers=_OP_HEADERS
        ).status_code
        == 404
    )
    assert (
        client.post(
            "/api/v1/guest-dm/contacts/x/revoke", headers=_OP_HEADERS
        ).status_code
        == 404
    )


# ── dataplane gating ──────────────────────────────────────────────────────────
def test_guest_dm_paths_are_gated_not_swept_into_guest_exemption():
    # /api/v1/guest-dm/* must NOT match the /api/v1/guest exempt prefix (segment
    # boundary: "guest-dm" != "guest" + "/"), so it stays gated by BOTH the
    # in-route operator check above AND the dataplane middleware.
    assert is_gated("GET", "/api/v1/guest-dm/contacts") is True
    assert is_gated("PATCH", "/api/v1/guest-dm/contacts/abc123") is True
    assert is_gated("POST", "/api/v1/guest-dm/contacts/abc123/revoke") is True
    # Regression: the real guest exemption is untouched.
    assert is_gated("GET", "/api/v1/guest/conversation") is False
    assert is_gated("POST", "/api/v1/guest/join") is False


# ── operator group listing badge ─────────────────────────────────────────────
def test_group_listing_carries_guest_dm_badge(env, client):
    _inv, body, fp = _mint_and_join(client, alias="Bestie", name="Alice")
    GG.update_dm_contact(fp, muted=True)

    group = G.load_group(body["group"]["id"])
    conv = G.group_to_conversation(group)
    assert conv["guest_dm"] is True
    assert conv["guest_name"] == "Alice"
    assert conv["guest_alias"] == "Bestie"
    assert conv["guest_status"] == "active"
    assert conv["muted"] is True


def test_non_dm_group_listing_has_no_guest_dm_badge(env, client):
    grp = G.create_group(name="Ops", creator_uri=daemon_proxy.OPERATOR_ID, members=[])
    conv = G.group_to_conversation(grp)
    assert "guest_dm" not in conv


def test_alias_never_leaks_into_guest_response(env, client):
    _inv, body, _fp = _mint_and_join(client, alias="SecretNickname")
    session = body["session_token"]

    assert "SecretNickname" not in body.__repr__()

    r_conv = client.get("/api/v1/guest/conversation", headers={"Authorization": f"Bearer {session}"})
    assert r_conv.status_code == 200
    assert "SecretNickname" not in r_conv.text

    r_send = client.post(
        "/api/v1/guest/send",
        json={"body": "hi"},
        headers={"Authorization": f"Bearer {session}"},
    )
    assert r_send.status_code == 200
    assert "SecretNickname" not in r_send.text
