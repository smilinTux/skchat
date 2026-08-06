"""Tests for guest-dm G1: DM → group promotion in place.

Per docs/superpowers/specs (epic 8685ede6, spec section "Group extension"):
minting a ``mode=dm`` invite against an EXISTING dm/gdm group promotes it IN
PLACE (same ``group_id``) to ``mode="gdm"`` instead of minting a fresh 2-seat
DM. Covers the acceptance:

  * mint with mode=dm against an existing dm group flips it to gdm in place,
    sets seat_cap/promoted_* metadata, returns an invite for that SAME group;
    mode=dm with no/unknown path group still mints a fresh 2-seat DM.
  * gdm seat cap enforced in guest_join (cap-full new guest → 403, returning
    guest idempotent); dm cap 2 unchanged.
  * epoch fence applies per guest in gdm: a guest admitted after promotion
    cannot read pre-join messages; original members keep full history.
  * promotion posts a system notice into the thread before the new invite is
    usable, and guest payloads carry mode.
"""

from __future__ import annotations

from datetime import datetime, timezone

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


def _save_group_msg(hist, group_id, content, *, when: datetime):
    from skchat.models import ChatMessage

    hist.save(
        ChatMessage(
            sender=_OPERATOR,
            recipient=f"group:{group_id}",
            content=content,
            thread_id=group_id,
            timestamp=when,
            metadata={"group_id": group_id},
        )
    )


# ── promotion mint: flips dm -> gdm in place ────────────────────────────────
def test_mint_mode_dm_against_existing_dm_group_promotes_in_place(env, client):
    inv = GG.create_dm_invite(operator_uri=_OPERATOR)
    gid = inv["group_id"]
    r1 = _join(client, inv["token"], name="Alice", pubkey="KEY-A")
    assert r1.status_code == 200, r1.text

    r = client.post(f"/api/v1/groups/{gid}/invite?mode=dm", json={}, headers=_OP)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["group_id"] == gid  # SAME group_id, not a fresh mint
    assert body["mode"] == "gdm"

    grp = G.load_group(gid)
    assert grp.metadata.get("mode") == "gdm"
    assert grp.metadata.get("promoted_from") == "dm"
    assert grp.metadata.get("promoted_at") is not None
    assert grp.metadata.get("seat_cap") == GG.gdm_seat_cap_default()

    # The new invite works and adds a guest into the SAME group.
    r3 = _join(client, body["token"], name="Bob", pubkey="KEY-B")
    assert r3.status_code == 200, r3.text
    assert r3.json()["group"]["id"] == gid
    assert G.load_group(gid).member_count == 3


def test_mint_mode_dm_with_seat_cap_override_on_first_promotion(env, client):
    inv = GG.create_dm_invite(operator_uri=_OPERATOR)
    gid = inv["group_id"]

    r = client.post(
        f"/api/v1/groups/{gid}/invite?mode=dm", json={"seat_cap": 4}, headers=_OP
    )
    assert r.status_code == 200, r.text
    grp = G.load_group(gid)
    assert grp.metadata.get("seat_cap") == 4
    assert GG.guest_seat_cap(grp) == 4


def test_mint_mode_dm_against_already_gdm_group_is_noop_promotion(env, client):
    inv = GG.create_dm_invite(operator_uri=_OPERATOR)
    gid = inv["group_id"]
    r0 = client.post(f"/api/v1/groups/{gid}/invite?mode=dm", json={"seat_cap": 5}, headers=_OP)
    assert r0.status_code == 200, r0.text
    grp = G.load_group(gid)
    first_promoted_at = grp.metadata.get("promoted_at")

    # Minting again (already gdm) must not clobber promoted_at/seat_cap.
    r = client.post(f"/api/v1/groups/{gid}/invite?mode=dm", json={"seat_cap": 99}, headers=_OP)
    assert r.status_code == 200, r.text
    assert r.json()["mode"] == "gdm"
    grp2 = G.load_group(gid)
    assert grp2.metadata.get("promoted_at") == first_promoted_at
    assert grp2.metadata.get("seat_cap") == 5


def test_mint_mode_dm_with_unknown_path_group_mints_fresh_dm(env, client):
    r = client.post("/api/v1/groups/does-not-exist/invite?mode=dm", json={}, headers=_OP)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["group_id"] != "does-not-exist"
    grp = G.load_group(body["group_id"])
    assert grp.metadata.get("mode") == "dm"
    assert grp.member_count == 1


def test_mint_mode_dm_with_no_path_group_mints_fresh_dm(env, client):
    # "self" is the existing convention for "no meaningful path id" in this route.
    r = client.post("/api/v1/groups/self/invite?mode=dm", json={}, headers=_OP)
    assert r.status_code == 200, r.text
    body = r.json()
    grp = G.load_group(body["group_id"])
    assert grp.metadata.get("mode") == "dm"


def test_mint_mode_dm_against_plain_group_mints_fresh_dm(env, client):
    # A classic (non-dm) group at the path id is not dm-like, so it's ignored.
    grp = G.create_group(name="Town Hall", creator_uri=daemon_proxy.OPERATOR_ID, members=[])
    r = client.post(f"/api/v1/groups/{grp.id}/invite?mode=dm", json={}, headers=_OP)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["group_id"] != grp.id
    assert G.load_group(body["group_id"]).metadata.get("mode") == "dm"
    assert G.load_group(grp.id).metadata.get("mode") is None


# ── gdm seat cap enforcement ─────────────────────────────────────────────────
def test_gdm_seat_cap_enforced_in_guest_join(env, client, monkeypatch):
    monkeypatch.setenv("SKCHAT_GDM_SEAT_CAP", "3")
    inv = GG.create_dm_invite(operator_uri=_OPERATOR)
    gid = inv["group_id"]
    _join(client, inv["token"], name="Alice", pubkey="KEY-A")

    promo = client.post(f"/api/v1/groups/{gid}/invite?mode=dm", json={}, headers=_OP).json()
    assert promo["mode"] == "gdm"

    # Operator + Alice = 2 seats; cap is 3, so one more guest fits exactly.
    r_bob = _join(client, promo["token"], name="Bob", pubkey="KEY-B")
    assert r_bob.status_code == 200, r_bob.text
    assert G.load_group(gid).member_count == 3

    # A 4th distinct guest exceeds the cap → 403.
    invite2 = GG.create_group_invite(gid, single_use=False)
    r_mallory = _join(client, invite2["token"], name="Mallory", pubkey="KEY-M")
    assert r_mallory.status_code == 403
    assert G.load_group(gid).member_count == 3

    # Bob returning (same key) is idempotent, still allowed.
    r_bob_again = _join(client, invite2["token"], name="Bob", pubkey="KEY-B")
    assert r_bob_again.status_code == 200, r_bob_again.text
    assert G.load_group(gid).member_count == 3


def test_dm_cap_still_two_after_gdm_helpers_added(env, client):
    inv = GG.create_dm_invite(operator_uri=_OPERATOR, single_use=False)
    r1 = _join(client, inv["token"], name="Alice", pubkey="KEY-A")
    assert r1.status_code == 200, r1.text
    assert G.load_group(inv["group_id"]).member_count == 2

    r2 = _join(client, inv["token"], name="Mallory", pubkey="KEY-M")
    assert r2.status_code == 403


# ── epoch fence generalized to gdm, per-guest ───────────────────────────────
def _set_guest_added_at(gid, guest_id, added_at: float) -> None:
    """Pin a member's epoch-fence cutoff to an exact value (deterministic tests).

    ``added_at`` is real wall-clock time at join, which makes ordering between
    join events and hand-stamped message timestamps race-prone within a fast
    test. Pinning it directly (same field ``add_untrusted_guest_member``
    writes/preserves) removes that flakiness without touching fence logic.
    """
    grp = G.load_group(gid)
    guests = dict(grp.metadata.get("guests") or {})
    guests[guest_id]["added_at"] = added_at
    grp.metadata["guests"] = guests
    G.save_group(grp)


def test_gdm_epoch_fence_per_guest(env, client):
    inv = GG.create_dm_invite(operator_uri=_OPERATOR)
    gid = inv["group_id"]
    hist = daemon_proxy._get_history()

    j_alice = _join(client, inv["token"], name="Alice", pubkey="KEY-A").json()
    alice_session = j_alice["session_token"]
    alice_id = j_alice["guest_id"]
    _set_guest_added_at(gid, alice_id, 1_000.0)  # Alice predates everything below

    # A message posted after Alice joined but before the promotion.
    _save_group_msg(
        hist, gid, "after-alice-before-promo",
        when=datetime.fromtimestamp(1_500.0, tz=timezone.utc),
    )

    promo = client.post(f"/api/v1/groups/{gid}/invite?mode=dm", json={}, headers=_OP).json()
    assert promo["mode"] == "gdm"

    # A message posted after the promotion but before Bob (the 3rd guest) joins.
    _save_group_msg(
        hist, gid, "after-promo-before-bob",
        when=datetime.fromtimestamp(1_600.0, tz=timezone.utc),
    )

    # Bob joins AFTER the promotion - a third guest admitted post-flip.
    j_bob = _join(client, promo["token"], name="Bob", pubkey="KEY-B").json()
    bob_id = j_bob["guest_id"]
    _set_guest_added_at(gid, bob_id, 1_700.0)  # Bob's cutoff is after both messages

    bob_conv = client.get(
        "/api/v1/guest/conversation", headers=_auth(j_bob["session_token"])
    ).json()
    bob_bodies = [m["body"] for m in bob_conv["messages"]]
    assert "after-alice-before-promo" not in bob_bodies
    assert "after-promo-before-bob" not in bob_bodies

    # Original member (Alice) keeps full history, including pre-promotion msgs.
    alice_conv = client.get(
        "/api/v1/guest/conversation", headers=_auth(alice_session)
    ).json()
    alice_bodies = [m["body"] for m in alice_conv["messages"]]
    assert "after-alice-before-promo" in alice_bodies
    assert "after-promo-before-bob" in alice_bodies


# ── promotion notice + mode surfaced in guest payloads ──────────────────────
def test_promotion_posts_system_notice_before_invite_usable(env, client):
    inv = GG.create_dm_invite(operator_uri=_OPERATOR)
    gid = inv["group_id"]
    j_alice = _join(client, inv["token"], name="Alice", pubkey="KEY-A")
    alice_session = j_alice.json()["session_token"]

    r = client.post(f"/api/v1/groups/{gid}/invite?mode=dm", json={}, headers=_OP)
    assert r.status_code == 200, r.text

    conv = client.get("/api/v1/guest/conversation", headers=_auth(alice_session)).json()
    bodies = [m["body"] for m in conv["messages"]]
    assert any("now a group" in b for b in bodies)


def test_guest_join_and_conversation_payloads_surface_mode(env, client):
    inv = GG.create_dm_invite(operator_uri=_OPERATOR)
    gid = inv["group_id"]
    j_alice = _join(client, inv["token"], name="Alice", pubkey="KEY-A").json()
    assert j_alice["group"]["mode"] == "dm"
    alice_session = j_alice["session_token"]

    promo = client.post(f"/api/v1/groups/{gid}/invite?mode=dm", json={}, headers=_OP).json()
    j_bob = _join(client, promo["token"], name="Bob", pubkey="KEY-B").json()
    assert j_bob["group"]["mode"] == "gdm"

    # Alice's own conversation view now reflects the audience change too.
    conv = client.get("/api/v1/guest/conversation", headers=_auth(alice_session)).json()
    assert conv["mode"] == "gdm"


def test_guest_invite_preview_surfaces_gdm_mode(env, client):
    inv = GG.create_dm_invite(operator_uri=_OPERATOR)
    gid = inv["group_id"]
    _join(client, inv["token"], name="Alice", pubkey="KEY-A")
    promo = client.post(f"/api/v1/groups/{gid}/invite?mode=dm", json={}, headers=_OP).json()

    r = client.get(f"/api/v1/guest/invite/{promo['token']}")
    assert r.status_code == 200, r.text
    assert r.json()["mode"] == "gdm"
