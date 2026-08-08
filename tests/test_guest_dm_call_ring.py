"""Tests for guest-dm S6: a guest call rings the operator (shipped default).

``POST /api/v1/guest/call`` on a ``metadata.mode="dm"`` group with body
``{ring: true}`` broadcasts a ``guest_call`` ws event to operator clients and
fires a best-effort ``call_observability.alert_operator`` alert. Covers the
acceptance:
  * ``ring: true`` on a dm group emits the ws event + operator alert;
    ``ring: false``/absent is a silent remint.
  * a muted ``dm_contacts`` row never rings/alerts but still gets a token.
  * non-dm guest groups are unchanged; the alias is operator-facing only.
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
    # _have_creds() reads the livekit_routes module globals, but
    # guest_group_routes._mint_guest_call_token re-reads the env vars directly
    # for the actual signing key/secret - both must be set or the mint raises
    # inside build_livekit_token and /guest/call degrades to a 503.
    monkeypatch.setattr(livekit_routes, "LIVEKIT_API_KEY", _KEY)
    monkeypatch.setattr(livekit_routes, "LIVEKIT_API_SECRET", _SECRET)
    monkeypatch.setenv("SKCHAT_LIVEKIT_API_KEY", _KEY)
    monkeypatch.setenv("SKCHAT_LIVEKIT_API_SECRET", _SECRET)
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


@pytest.fixture
def spies(monkeypatch):
    broadcasts = []
    alerts = []

    async def _fake_broadcast(msg_dict):
        broadcasts.append(msg_dict)

    def _fake_alert(**kw):
        alerts.append(kw)

    monkeypatch.setattr(_webui, "_ws_broadcast", _fake_broadcast)
    monkeypatch.setattr(CO, "alert_operator", _fake_alert)
    return broadcasts, alerts


def _auth(session):
    return {"Authorization": f"Bearer {session}"}


def _promote_to_gdm(client, gid):
    return client.post(f"/api/v1/groups/{gid}/invite?mode=dm", json={}, headers=_OP).json()


def _join_second_guest(client, invite_token, name="Bob", pubkey="KEY-B"):
    r = client.post(
        "/api/v1/guest/join",
        json={"invite_token": invite_token, "display_name": name, "guest_pubkey": pubkey},
    )
    assert r.status_code == 200, r.text
    return r.json()


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


def _make_group(client, name="Town Hall", members=("lumina",)):
    return G.create_group(name=name, creator_uri=daemon_proxy.OPERATOR_ID, members=list(members))


def _join_group(client, group_id, name="Bob", pubkey="PUBKEY-B"):
    inv = client.post(f"/api/v1/groups/{group_id}/invite", json={}, headers=_OP).json()
    r = client.post(
        "/api/v1/guest/join",
        json={"invite_token": inv["token"], "display_name": name, "guest_pubkey": pubkey},
    )
    assert r.status_code == 200, r.text
    return r.json()


# ── ring:true on a dm group ───────────────────────────────────────────────────
def test_ring_true_on_dm_group_emits_ws_and_alert(client, spies):
    broadcasts, alerts = spies
    j = _join_dm(client, alias="Bestie")
    session = j["session_token"]

    r = client.post("/api/v1/guest/call", json={"ring": True}, headers=_auth(session))
    assert r.status_code == 200, r.text
    assert "alias" not in r.json()  # never leaks to the guest response

    guest_calls = [b for b in broadcasts if b.get("type") == "guest_call"]
    assert len(guest_calls) == 1
    evt = guest_calls[0]
    assert evt["group_id"] == j["group"]["id"]
    assert evt["guest_id"] == j["guest_id"]
    assert evt["display"] == "Alice"
    assert evt["alias"] == "Bestie"  # operator-facing payload only

    assert len(alerts) == 1


def test_ring_true_without_alias_omits_alias_key(client, spies):
    broadcasts, alerts = spies
    j = _join_dm(client)  # no alias pre-set on the invite
    session = j["session_token"]

    r = client.post("/api/v1/guest/call", json={"ring": True}, headers=_auth(session))
    assert r.status_code == 200, r.text

    guest_calls = [b for b in broadcasts if b.get("type") == "guest_call"]
    assert len(guest_calls) == 1
    assert "alias" not in guest_calls[0]
    assert len(alerts) == 1


def test_ring_false_or_absent_is_silent(client, spies):
    broadcasts, alerts = spies
    j = _join_dm(client)
    session = j["session_token"]

    r1 = client.post("/api/v1/guest/call", json={"ring": False}, headers=_auth(session))
    assert r1.status_code == 200, r1.text
    r2 = client.post("/api/v1/guest/call", json={}, headers=_auth(session))
    assert r2.status_code == 200, r2.text

    assert [b for b in broadcasts if b.get("type") == "guest_call"] == []
    assert alerts == []


# ── muted contact ──────────────────────────────────────────────────────────────
def test_muted_contact_never_rings_but_gets_token(client, spies):
    broadcasts, alerts = spies
    j = _join_dm(client)
    session = j["session_token"]

    contact = GG.get_dm_contact(j["fingerprint"])
    assert GG.update_dm_contact(contact["fp"], muted=True)

    r = client.post("/api/v1/guest/call", json={"ring": True}, headers=_auth(session))
    assert r.status_code == 200, r.text
    assert r.json()["available"] is True  # still a working room token

    assert [b for b in broadcasts if b.get("type") == "guest_call"] == []
    assert alerts == []


# ── ring:true on a gdm group (guest-dm G4: dm-family, not just dm) ──────────
def test_ring_true_on_gdm_group_emits_ws_and_alert_to_operator_only(client, spies):
    broadcasts, alerts = spies
    j_alice = _join_dm(client, name="Alice", pubkey="KEY-A")
    gid = j_alice["group"]["id"]
    promo = _promote_to_gdm(client, gid)
    j_bob = _join_second_guest(client, promo["token"])

    r = client.post(
        "/api/v1/guest/call", json={"ring": True}, headers=_auth(j_bob["session_token"])
    )
    assert r.status_code == 200, r.text
    assert "alias" not in r.json()  # never leaks to the guest response

    guest_calls = [b for b in broadcasts if b.get("type") == "guest_call"]
    assert len(guest_calls) == 1
    evt = guest_calls[0]
    assert evt["group_id"] == gid
    assert evt["guest_id"] == j_bob["guest_id"]
    assert evt["display"] == "Bob"
    assert len(alerts) == 1


def test_muted_contact_never_rings_on_gdm_group(client, spies):
    broadcasts, alerts = spies
    j_alice = _join_dm(client, name="Alice", pubkey="KEY-A")
    gid = j_alice["group"]["id"]
    promo = _promote_to_gdm(client, gid)
    j_bob = _join_second_guest(client, promo["token"])
    fp_bob = GG.pubkey_fingerprint("KEY-B")
    assert GG.update_dm_contact(fp_bob, muted=True)

    r = client.post(
        "/api/v1/guest/call", json={"ring": True}, headers=_auth(j_bob["session_token"])
    )
    assert r.status_code == 200, r.text
    assert r.json()["available"] is True  # still a working room token

    assert [b for b in broadcasts if b.get("type") == "guest_call"] == []
    assert alerts == []


def test_gdm_ring_from_one_guest_does_not_fan_to_other_guests(client, spies):
    broadcasts, alerts = spies
    j_alice = _join_dm(client, name="Alice", pubkey="KEY-A")
    gid = j_alice["group"]["id"]
    promo = _promote_to_gdm(client, gid)
    _join_second_guest(client, promo["token"])  # Bob, a second guest in the room

    r = client.post(
        "/api/v1/guest/call", json={"ring": True}, headers=_auth(j_alice["session_token"])
    )
    assert r.status_code == 200, r.text

    guest_calls = [b for b in broadcasts if b.get("type") == "guest_call"]
    # Exactly one event for the one ringing guest - guests have no ring surface
    # of their own, so this never fans out per other guest in the room.
    assert len(guest_calls) == 1
    assert guest_calls[0]["guest_id"] == j_alice["guest_id"]
    assert len(alerts) == 1


# ── non-dm groups unchanged ────────────────────────────────────────────────────
def test_non_dm_group_ring_true_unchanged(client, spies):
    broadcasts, alerts = spies
    grp = _make_group(client)
    j = _join_group(client, grp.id)
    session = j["session_token"]

    r = client.post("/api/v1/guest/call", json={"ring": True}, headers=_auth(session))
    assert r.status_code == 200, r.text

    assert [b for b in broadcasts if b.get("type") == "guest_call"] == []
    assert alerts == []
