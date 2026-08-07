"""guest-dm G6 (server half): the roster payloads both sides render from.

Two payloads, two audiences, one hard rule between them:

* ``GET /api/v1/guest/conversation`` (guest-facing) grows a ``members`` list so
  a guest can see who else is in the room after a dm->gdm promotion. It carries
  ONLY what a guest may know: display name, a ``guest`` flag, and ``self``.
  Operator-private facts (the alias, the membership/person status, capauth
  fingerprints) must never appear here.
* the operator group listing (``daemon_proxy_groups.group_to_conversation``)
  keeps its per-member guest fields from G4 and additionally carries the
  person-level ``guest_status`` so a person-revoked guest - who stays on the
  roster, unlike a per-group revoke which removes them - can be dimmed.
"""
from __future__ import annotations

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from skchat import daemon_proxy
from skchat import daemon_proxy_groups as G
from skchat import guest_group_routes as GGR
from skchat import guest_groups as GG
from skchat import webui as _webui

_OP = {"X-Operator-Token": "op-secret"}


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
    monkeypatch.setattr(G, "_GROUPS_DIR", tmp_path / "groups")
    monkeypatch.setattr(G, "resolve_identity", lambda raw: (raw or "").strip())
    return tmp_path


@pytest.fixture
def client(env, monkeypatch):
    async def _bc(msg):
        return None

    monkeypatch.setattr(_webui, "_ws_broadcast", _bc)
    app = FastAPI()
    app.include_router(daemon_proxy.router)
    app.include_router(GGR.router)
    return TestClient(app)


def _auth(session):
    return {"Authorization": f"Bearer {session}"}


def _mint_dm(client, gid="self", **body):
    r = client.post(f"/api/v1/groups/{gid}/invite?mode=dm", json=body, headers=_OP)
    assert r.status_code == 200, r.text
    return r.json()


def _join(client, token, *, name, pubkey):
    r = client.post(
        "/api/v1/guest/join",
        json={"invite_token": token, "display_name": name, "guest_pubkey": pubkey},
    )
    assert r.status_code == 200, r.text
    return r.json()


def _conversation(client, session):
    r = client.get("/api/v1/guest/conversation", headers=_auth(session))
    assert r.status_code == 200, r.text
    return r.json()


# ═══════════════════════════════════════════════════════════════════════════
# Guest-facing roster
# ═══════════════════════════════════════════════════════════════════════════
def test_guest_conversation_carries_mode_and_members_for_a_dm(client):
    inv = _mint_dm(client)
    a = _join(client, inv["token"], name="Alice", pubkey="KEY-A")

    conv = _conversation(client, a["session_token"])
    assert conv["mode"] == "dm"
    members = conv["members"]
    # Operator seat + the guest themself.
    by_name = {m["display_name"]: m for m in members}
    assert "Alice" in by_name
    assert by_name["Alice"]["guest"] is True
    assert by_name["Alice"]["self"] is True
    assert any(m["guest"] is False for m in members)  # the operator seat


def test_guest_sees_the_other_guests_after_promotion(client):
    inv = _mint_dm(client)
    gid = inv["group_id"]
    a = _join(client, inv["token"], name="Alice", pubkey="KEY-A")
    promo = _mint_dm(client, gid)  # dm -> gdm in place
    _join(client, promo["token"], name="Bob", pubkey="KEY-B")

    conv = _conversation(client, a["session_token"])
    assert conv["mode"] == "gdm"
    by_name = {m["display_name"]: m for m in conv["members"]}
    assert by_name["Bob"]["guest"] is True
    assert by_name["Bob"]["self"] is False
    assert by_name["Alice"]["self"] is True


def test_guest_roster_never_leaks_operator_private_fields(client):
    """The alias is operator-only. A guest must not learn it, nor any status
    or fingerprint, no matter what the operator set."""
    inv = _mint_dm(client, alias="Bestie")
    gid = inv["group_id"]
    a = _join(client, inv["token"], name="Alice", pubkey="KEY-A")
    promo = _mint_dm(client, gid)
    _join(client, promo["token"], name="Bob", pubkey="KEY-B")
    # Operator aliases Bob privately.
    fp_b = GG.pubkey_fingerprint("KEY-B")
    assert client.patch(
        f"/api/v1/guest-dm/contacts/{fp_b}", json={"alias": "Work Bob"}, headers=_OP
    ).status_code == 200

    conv = _conversation(client, a["session_token"])
    blob = repr(conv)
    assert "Bestie" not in blob and "Work Bob" not in blob
    for member in conv["members"]:
        assert set(member) == {"identity_uri", "display_name", "guest", "self"}


def test_revoked_guest_drops_off_the_guest_roster(client):
    inv = _mint_dm(client)
    gid = inv["group_id"]
    a = _join(client, inv["token"], name="Alice", pubkey="KEY-A")
    promo = _mint_dm(client, gid)
    _join(client, promo["token"], name="Bob", pubkey="KEY-B")

    GG.revoke_group_membership(GG.pubkey_fingerprint("KEY-B"), gid)

    conv = _conversation(client, a["session_token"])
    assert "Bob" not in {m["display_name"] for m in conv["members"]}


# ═══════════════════════════════════════════════════════════════════════════
# Operator-facing roster
# ═══════════════════════════════════════════════════════════════════════════
def test_operator_gdm_roster_marks_a_person_revoked_guest(client):
    """A person-level revoke leaves the guest ON the roster (unlike a per-group
    revoke, which removes them), so the operator payload must say so or the
    app has nothing to dim."""
    inv = _mint_dm(client)
    gid = inv["group_id"]
    _join(client, inv["token"], name="Alice", pubkey="KEY-A")
    promo = _mint_dm(client, gid)
    _join(client, promo["token"], name="Bob", pubkey="KEY-B")

    GG.revoke_dm_contact(GG.pubkey_fingerprint("KEY-B"))

    conv = G.group_to_conversation(G.load_group(gid))
    assert conv["mode"] == "gdm"
    by_name = {p["display_name"]: p for p in conv["participants"]}
    assert by_name["Bob"]["guest"] is True
    assert by_name["Bob"]["guest_status"] == "revoked"
    assert by_name["Alice"]["guest_status"] == "active"
