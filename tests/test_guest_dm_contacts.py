"""Tests for guest-dm S2 — dm_contacts registry + reusable-link per-guest fanout.

Per task a69e7d4e (epic 8685ede6, depends on S1 d964b5a7): a reusable "my-DM-
link" invite now fans out — each DISTINCT browser key (fp) gets its OWN 2-seat
DM group, and a returning key finds its way back to the same group + history
via a small ``dm_contacts`` registry (``fp`` PRIMARY KEY). Covers:

  * two different guest keys through the SAME reusable invite land in two
    separate 2-seat dm groups; the same key returns to its own group/history.
  * every dm admission (reusable or single-use) upserts ``dm_contacts`` with
    fp/group_id/jti/alias-from-sidecar/expiry; ``last_seen_at`` bumps on rejoin.
  * new-contact creation is rate-limited per invite jti with a generic 401;
    single-use dm flow and classic (non-dm) group joins are unchanged.
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
_OPERATOR = "capauth:lumina@skworld.io"


def _join(client, invite_token, name="Alice", pubkey="PUBKEY-A"):
    return client.post(
        "/api/v1/guest/join",
        json={"invite_token": invite_token, "display_name": name, "guest_pubkey": pubkey},
    )


def _auth(session):
    return {"Authorization": f"Bearer {session}"}


# ── reusable-link fanout ─────────────────────────────────────────────────────
def test_reusable_dm_fanout_two_keys_two_groups(env, client):
    inv = GG.create_dm_invite(operator_uri=_OPERATOR, reusable=True)

    r1 = _join(client, inv["token"], name="Alice", pubkey="KEY-A")
    assert r1.status_code == 200, r1.text
    gid_a = r1.json()["group"]["id"]
    assert gid_a == inv["group_id"]  # first arrival lands in the anchor group

    r2 = _join(client, inv["token"], name="Mallory", pubkey="KEY-M")
    assert r2.status_code == 200, r2.text
    gid_m = r2.json()["group"]["id"]

    assert gid_m != gid_a  # a second distinct key gets a SEPARATE dm group
    for gid in (gid_a, gid_m):
        grp = G.load_group(gid)
        assert grp is not None
        assert grp.metadata.get("mode") == "dm"
        assert grp.member_count == 2  # DM_SEAT_CAP still holds per fanned group


def test_reusable_dm_fanout_same_key_returns_to_own_group(env, client):
    inv = GG.create_dm_invite(operator_uri=_OPERATOR, reusable=True)

    r1 = _join(client, inv["token"], name="Alice", pubkey="KEY-A")
    gid_a = r1.json()["group"]["id"]
    _join(client, inv["token"], name="Mallory", pubkey="KEY-M")

    # Alice returns via the SAME standing link — she lands back in her own
    # group (not a third fresh group), and sees her own history.
    hist = daemon_proxy._get_history()
    session_a = r1.json()["session_token"]
    client.post("/api/v1/guest/send", json={"body": "hi it's me"}, headers=_auth(session_a))

    r3 = _join(client, inv["token"], name="Alice", pubkey="KEY-A")
    assert r3.status_code == 200, r3.text
    assert r3.json()["group"]["id"] == gid_a
    bodies = [m["body"] for m in r3.json()["messages"]]
    assert "hi it's me" in bodies
    assert G.load_group(gid_a).member_count == 2  # idempotent, no 3rd seat


def test_reusable_dm_fanout_third_distinct_key_gets_third_group(env, client):
    inv = GG.create_dm_invite(operator_uri=_OPERATOR, reusable=True)
    r1 = _join(client, inv["token"], name="Alice", pubkey="KEY-A")
    r2 = _join(client, inv["token"], name="Mallory", pubkey="KEY-M")
    r3 = _join(client, inv["token"], name="Carl", pubkey="KEY-C")
    assert r3.status_code == 200, r3.text
    gids = {r1.json()["group"]["id"], r2.json()["group"]["id"], r3.json()["group"]["id"]}
    assert len(gids) == 3
    for gid in gids:
        assert G.load_group(gid).member_count == 2


# ── dm_contacts registry upsert ──────────────────────────────────────────────
def test_dm_admission_upserts_contact_row(env, client):
    inv = GG.create_dm_invite(operator_uri=_OPERATOR, reusable=True)
    GG.store_dm_invite_meta(inv["jti"], alias="Bestie", contact_ttl=3600)

    r = _join(client, inv["token"], name="Alice", pubkey="KEY-A")
    assert r.status_code == 200, r.text
    fp = GG.pubkey_fingerprint("KEY-A")

    contact = GG.get_dm_contact(fp)
    assert contact is not None
    assert contact["fp"] == fp
    assert contact["group_id"] == r.json()["group"]["id"]
    assert contact["invite_jti"] == inv["jti"]
    assert contact["alias"] == "Bestie"
    assert contact["contact_expires_at"] is not None
    first_seen = contact["last_seen_at"]

    # Alias is server-side only — never surfaced in the join response.
    assert "alias" not in r.json()
    assert "Bestie" not in r.text

    # Rejoin bumps last_seen_at.
    r2 = _join(client, inv["token"], name="Alice", pubkey="KEY-A")
    assert r2.status_code == 200
    contact2 = GG.get_dm_contact(fp)
    assert contact2["last_seen_at"] >= first_seen


def test_single_use_dm_admission_also_upserts_contact(env, client):
    inv = GG.create_dm_invite(operator_uri=_OPERATOR)  # single-use, not reusable
    r = _join(client, inv["token"], name="Alice", pubkey="KEY-A")
    assert r.status_code == 200, r.text
    fp = GG.pubkey_fingerprint("KEY-A")
    contact = GG.get_dm_contact(fp)
    assert contact is not None
    assert contact["group_id"] == inv["group_id"]
    assert contact["invite_jti"] == inv["jti"]


def test_classic_group_join_does_not_touch_dm_contacts(env, client):
    grp = G.create_group(name="Town Hall", creator_uri=daemon_proxy.OPERATOR_ID, members=[])
    inv = GG.create_group_invite(grp.id)
    r = _join(client, inv["token"], name="Alice", pubkey="KEY-A")
    assert r.status_code == 200, r.text
    fp = GG.pubkey_fingerprint("KEY-A")
    assert GG.get_dm_contact(fp) is None


# ── rate limiting ─────────────────────────────────────────────────────────────
def test_new_contact_rate_limit_enforced(env, client, monkeypatch):
    monkeypatch.setenv("SKCHAT_DM_CONTACT_RATE_LIMIT", "2")
    inv = GG.create_dm_invite(operator_uri=_OPERATOR, reusable=True)

    r1 = _join(client, inv["token"], name="A", pubkey="KEY-1")
    r2 = _join(client, inv["token"], name="B", pubkey="KEY-2")
    assert r1.status_code == 200
    assert r2.status_code == 200

    # Third DISTINCT key exceeds the cap of 2 new contacts for this jti.
    r3 = _join(client, inv["token"], name="C", pubkey="KEY-3")
    assert r3.status_code == 401
    assert r3.json() == {"detail": "invalid or expired invite"}  # generic, no oracle

    # A key that ALREADY has a contact is unaffected by the cap (not "new").
    r4 = _join(client, inv["token"], name="A", pubkey="KEY-1")
    assert r4.status_code == 200


def test_rate_limit_helper_function(env):
    import os

    os.environ["SKCHAT_DM_CONTACT_RATE_LIMIT"] = "1"
    try:
        assert GG.check_new_contact_allowed("jti-x") is True
        GG.upsert_dm_contact("fp1", guest_id="guest:x#fp1", group_id="g1", invite_jti="jti-x")
        assert GG.check_new_contact_allowed("jti-x") is False
        # A different jti has its own independent budget.
        assert GG.check_new_contact_allowed("jti-y") is True
    finally:
        os.environ.pop("SKCHAT_DM_CONTACT_RATE_LIMIT", None)


# ── unchanged behaviour ───────────────────────────────────────────────────────
def test_single_use_dm_flow_unchanged(env, client):
    inv = GG.create_dm_invite(operator_uri=_OPERATOR, single_use=False)
    r1 = _join(client, inv["token"], name="Alice", pubkey="KEY-A")
    assert r1.status_code == 200
    assert G.load_group(inv["group_id"]).member_count == 2
    r2 = _join(client, inv["token"], name="Mallory", pubkey="KEY-M")
    assert r2.status_code == 403  # non-reusable: still a hard 2-seat cap, no fanout


def test_classic_group_invite_flow_unchanged(env, client):
    grp = G.create_group(name="Ops", creator_uri=daemon_proxy.OPERATOR_ID, members=[])
    inv = GG.create_group_invite(grp.id)
    r = _join(client, inv["token"], name="Alice", pubkey="KEY-A")
    assert r.status_code == 200
    assert r.json()["group"]["id"] == grp.id
