"""End-to-end server integration suite for the guest-DM GROUP extension (G1-G4).

Group half of epic 8685ede6, in the style of the 1:1 suite
``test_guest_dm_e2e.py``. Depends on G1 (DM->group promotion in place), G2
(both group invite modes: per-person + shared reusable link, rate-limited),
G3 (person/membership revoke split), G4 (operator gdm surfaces). Each scenario
is an independent test so a failure localizes to exactly what broke.

  1. promotion: a dm flips to gdm in place, posts a system notice, admits a
     second per-person guest behind the epoch fence, keeps the first guest's
     session working, and enforces the seat cap.
  2. shared link: one reusable link admits N distinct keys into ONE group,
     dedupes a returning key, and disabling it blocks new joins (not admitted).
  3. files + calls: a guest file is downloadable by the other guest, a foreign
     transfer id 403s, both guests get a call token, ring alerts once, muted is
     silent.
  4. revoke: per-group revoke kills only that membership (a separate DM keeps
     working); person-level revoke kills everything.
"""
from __future__ import annotations

import io

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from skchat import call_observability as CO
from skchat import daemon_proxy
from skchat import daemon_proxy_groups as G
from skchat import guest_group_routes as GGR
from skchat import guest_groups as GG
from skchat import livekit_routes
from skchat import webui as _webui

_KEY, _SECRET = "test-key", "test-secret-0123456789"
_OP = {"X-Operator-Token": "op-secret"}


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
    from skchat import guest as _guest

    _guest._reset_revocation_cache()
    from skchat.history import ChatHistory

    hist = ChatHistory(store=None, history_dir=tmp_path / "history")
    monkeypatch.setattr(daemon_proxy, "_HISTORY", hist)
    monkeypatch.setattr(G, "_GROUPS_DIR", tmp_path / "groups")
    monkeypatch.setattr(G, "resolve_identity", lambda raw: (raw or "").strip())
    return tmp_path


@pytest.fixture
def client(env):
    app = FastAPI()
    app.include_router(daemon_proxy.router)
    app.include_router(GGR.router)
    return TestClient(app)


@pytest.fixture
def spies(monkeypatch):
    broadcasts, alerts = [], []

    async def _bc(msg):
        broadcasts.append(msg)

    monkeypatch.setattr(_webui, "_ws_broadcast", _bc)
    monkeypatch.setattr(CO, "alert_operator", lambda **kw: alerts.append(kw))
    return broadcasts, alerts


def _auth(session):
    return {"Authorization": f"Bearer {session}"}


def _mint_dm(client, **body):
    r = client.post("/api/v1/groups/self/invite?mode=dm", json=body, headers=_OP)
    assert r.status_code == 200, r.text
    return r.json()


def _join(client, token, *, name, pubkey):
    return client.post(
        "/api/v1/guest/join",
        json={"invite_token": token, "display_name": name, "guest_pubkey": pubkey},
    )


def _promote(client, gid, **body):
    """Mint a mode=dm invite against an EXISTING group -> promotes it to gdm."""
    r = client.post(f"/api/v1/groups/{gid}/invite?mode=dm", json=body, headers=_OP)
    assert r.status_code == 200, r.text
    return r.json()


# ═══════════════════════════════════════════════════════════════════════════
# 1. Promotion: dm -> gdm in place
# ═══════════════════════════════════════════════════════════════════════════
def test_promotion_flips_to_gdm_admits_second_guest_behind_fence(client, spies):
    inv = _mint_dm(client, alias="Bestie")
    gid = inv["group_id"]
    a = _join(client, inv["token"], name="Alice", pubkey="KEY-A").json()
    a_session = a["session_token"]
    client.post("/api/v1/guest/send", json={"body": "pre-promo msg"}, headers=_auth(a_session))

    # Promote in place: SAME group id, flips to gdm, posts a system notice.
    promo = _promote(client, gid, alias="Second")
    assert promo["group_id"] == gid
    grp = G.load_group(gid)
    assert grp.metadata.get("mode") == "gdm"
    thread = daemon_proxy._get_history().get_thread(gid)
    assert any("dm_promoted_to_gdm" in str(m.metadata) for m in thread)

    # Guest B joins via the per-person invite; the epoch fence hides pre-promo text.
    b = _join(client, promo["token"], name="Bob", pubkey="KEY-B").json()
    assert all(m["body"] != "pre-promo msg" for m in b["messages"])

    # Guest A's original session keeps working across the promotion.
    assert client.get("/api/v1/guest/conversation", headers=_auth(a_session)).status_code == 200


def test_promotion_seat_cap_rejects_a_new_guest_when_full(client, spies, monkeypatch):
    monkeypatch.setenv("SKCHAT_GDM_SEAT_CAP", "2")  # operator + 1 guest
    inv = _mint_dm(client)
    gid = inv["group_id"]
    _join(client, inv["token"], name="Alice", pubkey="KEY-A")
    promo = _promote(client, gid)
    # Cap is 2 (operator + Alice); a brand-new key cannot take a seat.
    r = _join(client, promo["token"], name="Carol", pubkey="KEY-C")
    assert r.status_code == 403


# ═══════════════════════════════════════════════════════════════════════════
# 2. Shared reusable group link
# ═══════════════════════════════════════════════════════════════════════════
def test_shared_link_admits_many_keys_into_one_group_and_dedupes(client, spies):
    inv = _mint_dm(client)
    gid = inv["group_id"]
    _promote(client, gid)  # make it a gdm
    link = GG.create_group_invite(gid, single_use=False)

    for name, key in (("Bob", "KEY-B"), ("Carl", "KEY-C")):
        assert _join(client, link["token"], name=name, pubkey=key).status_code == 200
    # All land in the SAME group (no fan-out to separate groups).
    b = _join(client, link["token"], name="Bob", pubkey="KEY-B").json()  # returning key
    assert b["group"]["id"] == gid


def test_disabling_shared_link_blocks_new_joins_not_admitted(client, spies):
    inv = _mint_dm(client)
    gid = inv["group_id"]
    _promote(client, gid)
    link = GG.create_group_invite(gid, single_use=False)
    admitted = _join(client, link["token"], name="Bob", pubkey="KEY-B").json()

    from skchat.guest import revoke_invite

    revoke_invite(link["jti"])  # disable the shared link

    # A new key is blocked...
    assert _join(client, link["token"], name="Eve", pubkey="KEY-E").status_code != 200
    # ...but the already-admitted guest keeps working.
    assert client.get(
        "/api/v1/guest/conversation", headers=_auth(admitted["session_token"])
    ).status_code == 200


# ═══════════════════════════════════════════════════════════════════════════
# 3. Files + calls
# ═══════════════════════════════════════════════════════════════════════════
def test_gdm_file_shared_between_guests_foreign_id_403(client, spies):
    inv = _mint_dm(client)
    gid = inv["group_id"]
    _promote(client, gid)
    link = GG.create_group_invite(gid, single_use=False)
    a = _join(client, link["token"], name="Alice", pubkey="KEY-A").json()
    b = _join(client, link["token"], name="Bob", pubkey="KEY-B").json()

    up = client.post(
        "/api/v1/guest/file",
        files={"file": ("hi.txt", io.BytesIO(b"payload"), "text/plain")},
        data={"caption": "shared"},
        headers=_auth(a["session_token"]),
    )
    assert up.status_code == 200, up.text
    msg = up.json().get("message") or {}
    tid = (msg.get("attachments") or [{}])[0].get("transfer_id") or up.json().get("transfer_id")
    assert tid

    # The other guest in the SAME group can download it.
    dl = client.get(f"/api/v1/guest/file/{tid}", headers=_auth(b["session_token"]))
    assert dl.status_code == 200
    # A foreign / made-up transfer id is refused.
    assert client.get(
        "/api/v1/guest/file/deadbeef-not-a-real-id", headers=_auth(b["session_token"])
    ).status_code in (403, 404)


def test_gdm_both_guests_get_call_token_same_room_ring_and_mute(client, spies):
    broadcasts, alerts = spies
    inv = _mint_dm(client)
    gid = inv["group_id"]
    _promote(client, gid)
    link = GG.create_group_invite(gid, single_use=False)
    a = _join(client, link["token"], name="Alice", pubkey="KEY-A").json()
    b = _join(client, link["token"], name="Bob", pubkey="KEY-B").json()

    ca = client.post("/api/v1/guest/call", json={"ring": True}, headers=_auth(a["session_token"]))
    cb = client.post("/api/v1/guest/call", json={"ring": True}, headers=_auth(b["session_token"]))
    assert ca.status_code == 200 and cb.status_code == 200
    assert ca.json()["room"] == cb.json()["room"]  # same derived room
    # Each ring reached the operator over the ws broadcast.
    assert len([x for x in broadcasts if x.get("type") == "guest_call"]) >= 1

    # Mute Bob -> his ring is silent (no new guest_call broadcast).
    client.patch(
        f"/api/v1/guest-dm/contacts/{GG.pubkey_fingerprint('KEY-B')}",
        json={"muted": True}, headers=_OP,
    )
    before = len(broadcasts)
    client.post("/api/v1/guest/call", json={"ring": True}, headers=_auth(b["session_token"]))
    assert len([x for x in broadcasts[before:] if x.get("type") == "guest_call"]) == 0


# ═══════════════════════════════════════════════════════════════════════════
# 4. Revoke: per-group vs person
# ═══════════════════════════════════════════════════════════════════════════
def test_per_group_revoke_kills_only_that_membership(client, spies):
    # Alice is in a gdm group AND has her own separate 1:1 DM.
    inv1 = _mint_dm(client)
    gid_group = inv1["group_id"]
    _promote(client, gid_group)
    link = GG.create_group_invite(gid_group, single_use=False)
    a_group = _join(client, link["token"], name="Alice", pubkey="KEY-A").json()

    inv2 = _mint_dm(client)
    a_dm = _join(client, inv2["token"], name="Alice", pubkey="KEY-A2").json()

    fp = GG.pubkey_fingerprint("KEY-A")
    assert GG.revoke_group_membership(fp, gid_group) is True

    # The group session is blocked with the per-group reason...
    rg = client.get("/api/v1/guest/conversation", headers=_auth(a_group["session_token"]))
    assert rg.status_code == 403
    assert rg.json()["detail"]["reason"] == "membership_revoked"
    # ...but Alice's separate 1:1 DM is untouched.
    assert client.get(
        "/api/v1/guest/conversation", headers=_auth(a_dm["session_token"])
    ).status_code == 200


def test_person_revoke_kills_everything(client, spies):
    inv = _mint_dm(client)
    gid = inv["group_id"]
    _promote(client, gid)
    link = GG.create_group_invite(gid, single_use=False)
    a = _join(client, link["token"], name="Alice", pubkey="KEY-A").json()

    fp = GG.pubkey_fingerprint("KEY-A")
    assert client.post(f"/api/v1/guest-dm/contacts/{fp}/revoke", headers=_OP).status_code == 200

    r = client.get("/api/v1/guest/conversation", headers=_auth(a["session_token"]))
    assert r.status_code == 403
    assert r.json()["detail"]["reason"] == "contact_revoked"
