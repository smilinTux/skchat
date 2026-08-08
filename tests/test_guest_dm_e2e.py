"""End-to-end server integration suite for guest-DM (epic 8685ede6).

Merge gate for the server half of direct-DM guest invites. Depends on all
server cards: S1 (d964b5a7) invite mint + alias/contact_ttl sidecar + dm-aware
preview, S2 (a69e7d4e) dm_contacts registry + reusable-link fanout + rate
limit, S3 (a0b8f930) re-entry on a burned single-use jti + revoke/expiry
chokepoint, S4 (35e2f911) operator contact routes, S5 (bd94558f) guest
self-rename, S6 (66eaa6d8) call ring + mute.

Exercises the spec's Testing section (``docs/superpowers/specs/
2026-08-06-direct-dm-guest-invites-design.md``) end to end over a FastAPI
``TestClient``, in the style of the existing guest-group route tests
(``test_guest_group_invite_modes.py`` et al). Each scenario is an
independent test so a failure localizes to exactly the behaviour that broke:

  1. single-use: mint (alias + contact_ttl) -> dm-mode preview (never the
     alias) -> join lands a 2-seat dm group -> text both directions with the
     epoch fence -> file upload/download scoped (foreign transfer_id 403) ->
     call token scoped to the guest's own room -> session expiry -> re-entry
     on the burned jti with the same key succeeds, a different key still
     401s -> rename persists -> revoke locks the guest out with a clear
     reason code and kills re-entry.
  2. reusable: two browser keys fan out into two separate DMs, each returns
     to its own; new admissions past the rate cap get a generic 401;
     disabling the link (revoking its jti) blocks new admissions without
     touching already-admitted contacts.
  3. alias-leak sweep: the alias string is absent from every /guest/* and
     preview response body captured during a run that sets one.
  4. mute: a muted contact's call rings nobody but still gets a token.
"""

from __future__ import annotations

import io
import time
from datetime import datetime, timedelta, timezone

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from skchat import daemon_proxy, livekit_routes
from skchat import daemon_proxy_groupcall as GC
from skchat import daemon_proxy_groups as G
from skchat import guest_group_routes as GGR
from skchat import guest_groups as GG
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
    from skchat import call_observability as CO

    monkeypatch.setattr(CO, "alert_operator", _fake_alert)
    return broadcasts, alerts


# ── shared helpers ────────────────────────────────────────────────────────
def _mint_dm(client, *, alias=None, contact_ttl=None, reusable=False):
    body = {}
    if reusable:
        body["reusable"] = True
    if alias is not None:
        body["alias"] = alias
    if contact_ttl is not None:
        body["contact_ttl"] = contact_ttl
    r = client.post("/api/v1/groups/self/invite?mode=dm", json=body, headers=_OP)
    assert r.status_code == 200, r.text
    return r.json()


def _join(client, invite_token, name="Alice", pubkey="PUBKEY-A"):
    return client.post(
        "/api/v1/guest/join",
        json={"invite_token": invite_token, "display_name": name, "guest_pubkey": pubkey},
    )


def _auth(session):
    return {"Authorization": f"Bearer {session}"}


def _save_operator_msg(hist, group_id, content, *, when=None):
    from skchat.models import ChatMessage

    hist.save(
        ChatMessage(
            sender=daemon_proxy.OPERATOR_ID,
            recipient=f"group:{group_id}",
            content=content,
            thread_id=group_id,
            timestamp=when or datetime.now(timezone.utc),
            metadata={"group_id": group_id},
        )
    )


# ═══════════════════════════════════════════════════════════════════════════
# 1. Single-use: full invite/DM lifecycle
# ═══════════════════════════════════════════════════════════════════════════
def test_e2e_single_use_mint_preview_never_leaks_alias(env, client):
    inv = _mint_dm(client, alias="Secret Nickname", contact_ttl=3600)
    assert "alias" not in inv
    assert "Secret Nickname" not in str(inv)

    preview = client.get(f"/api/v1/guest/invite/{inv['token']}")
    assert preview.status_code == 200, preview.text
    body = preview.json()
    assert body["valid"] is True
    assert body["mode"] == "dm"
    assert "alias" not in body
    assert "Secret Nickname" not in preview.text


def test_e2e_single_use_join_lands_two_seat_dm_not_group(env, client):
    inv = _mint_dm(client, alias="Secret Nickname")
    r = _join(client, inv["token"], name="Alice", pubkey="KEY-A")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["group"]["id"] == inv["group_id"]
    assert body["group"]["mode"] == "dm"
    assert "alias" not in body

    grp = G.load_group(inv["group_id"])
    assert grp.metadata.get("mode") == "dm"
    assert grp.member_count == 2  # operator + the one guest, seat cap holds


def test_e2e_single_use_text_both_directions_epoch_fence(env, client):
    inv = _mint_dm(client)
    gid = inv["group_id"]
    hist = daemon_proxy._get_history()

    # A message that predates the guest's arrival must never surface to them.
    before_join = datetime.now(timezone.utc) - timedelta(hours=1)
    _save_operator_msg(hist, gid, "before you arrived", when=before_join)

    j = _join(client, inv["token"], name="Alice", pubkey="KEY-A").json()
    session = j["session_token"]
    assert all(m["body"] != "before you arrived" for m in j["messages"])

    guest_send = client.post(
        "/api/v1/guest/send", json={"body": "hi from guest"}, headers=_auth(session)
    )
    assert guest_send.status_code == 200, guest_send.text

    _save_operator_msg(hist, gid, "hi from operator")

    conv = client.get("/api/v1/guest/conversation", headers=_auth(session))
    assert conv.status_code == 200, conv.text
    bodies = [m["body"] for m in conv.json()["messages"]]
    assert "hi from guest" in bodies
    assert "hi from operator" in bodies
    assert "before you arrived" not in bodies


def test_e2e_single_use_file_upload_download_scoped_foreign_403(env, client):
    inv_a = _mint_dm(client)
    inv_b = _mint_dm(client)
    session_a = _join(client, inv_a["token"], name="Alice", pubkey="KEY-A").json()["session_token"]
    session_b = _join(client, inv_b["token"], name="Bob", pubkey="KEY-B").json()["session_token"]

    files = {"file": ("secret.txt", io.BytesIO(b"only for my dm"), "text/plain")}
    up = client.post(
        "/api/v1/guest/file", files=files, data={"caption": "mine"}, headers=_auth(session_a)
    )
    assert up.status_code == 200, up.text
    tid = up.json()["transfer_id"]

    own = client.get(f"/api/v1/guest/file/{tid}", headers=_auth(session_a))
    assert own.status_code == 200
    assert own.content == b"only for my dm"

    foreign = client.get(f"/api/v1/guest/file/{tid}", headers=_auth(session_b))
    assert foreign.status_code == 403


def test_e2e_single_use_call_token_scoped_to_own_room(env, client):
    inv_a = _mint_dm(client)
    inv_b = _mint_dm(client)
    j_a = _join(client, inv_a["token"], name="Alice", pubkey="KEY-A").json()
    j_b = _join(client, inv_b["token"], name="Bob", pubkey="KEY-B").json()

    r = client.post("/api/v1/guest/call", json={}, headers=_auth(j_a["session_token"]))
    assert r.status_code == 200, r.text
    call = r.json()
    assert call["available"] is True
    assert call["room"] == GC.derive_group_room(inv_a["group_id"])
    assert call["room"] != GC.derive_group_room(inv_b["group_id"])

    # A guest cannot mint a call token for a group other than their own.
    leak = client.post(
        "/api/v1/guest/call",
        json={"group_id": inv_b["group_id"]},
        headers=_auth(j_a["session_token"]),
    )
    assert leak.status_code == 403
    assert j_b  # sanity: the other guest's own join succeeded independently


def test_e2e_single_use_session_expiry_then_reentry_succeeds(env, client):
    inv = _mint_dm(client)
    j = _join(client, inv["token"], name="Alice", pubkey="KEY-A").json()
    guest_id, fp = j["guest_id"], j["fingerprint"]

    # Simulate the guest's session JWT having expired (it can outlive the
    # invite's own already-burned single-use link, per S3).
    expired = GG.mint_guest_session(
        group_id=inv["group_id"],
        guest_id=guest_id,
        name="Alice",
        fp=fp,
        ttl=1,
        now_fn=lambda: time.time() - 1000,
    )
    r = client.get("/api/v1/guest/conversation", headers=_auth(expired))
    assert r.status_code == 403

    # Re-entry: same key on the burned jti mints a fresh, working session.
    r2 = _join(client, inv["token"], name="Alice", pubkey="KEY-A")
    assert r2.status_code == 200, r2.text
    body2 = r2.json()
    assert body2["guest_id"] == guest_id
    assert body2["session_token"] != expired
    assert body2["session_token"] != j["session_token"]

    r3 = client.get("/api/v1/guest/conversation", headers=_auth(body2["session_token"]))
    assert r3.status_code == 200, r3.text

    assert G.load_group(inv["group_id"]).member_count == 2  # no new seat


def test_e2e_single_use_reentry_different_key_401_no_oracle(env, client):
    inv = _mint_dm(client)
    r1 = _join(client, inv["token"], name="Alice", pubkey="KEY-A")
    assert r1.status_code == 200

    r2 = _join(client, inv["token"], name="Mallory", pubkey="KEY-M")
    assert r2.status_code == 401
    assert r2.json() == {"detail": "invalid or expired invite"}
    assert G.load_group(inv["group_id"]).member_count == 2


def test_e2e_single_use_rename_persists(env, client):
    inv = _mint_dm(client)
    j = _join(client, inv["token"], name="Alice", pubkey="KEY-A").json()
    session = j["session_token"]

    r = client.post("/api/v1/guest/name", json={"name": "Alicia"}, headers=_auth(session))
    assert r.status_code == 200, r.text
    new_session = r.json()["session_token"]
    assert r.json()["display_name"] == "Alicia"

    member = G.load_group(inv["group_id"]).get_member(j["guest_id"])
    assert member.display_name == "Alicia"

    # A rejoin (S3 re-entry path) sees the persisted new name, not the old one.
    r2 = _join(client, inv["token"], name="Alice", pubkey="KEY-A")
    assert r2.status_code == 200, r2.text
    assert r2.json()["display_name"] == "Alicia"
    assert new_session  # fresh token from the rename actually usable
    conv = client.get("/api/v1/guest/conversation", headers=_auth(new_session))
    assert conv.status_code == 200


def test_e2e_single_use_revoke_locks_out_with_reason_and_kills_reentry(env, client):
    inv = _mint_dm(client)
    j = _join(client, inv["token"], name="Alice", pubkey="KEY-A").json()
    session, fp = j["session_token"], j["fingerprint"]

    revoke = client.post(f"/api/v1/guest-dm/contacts/{fp}/revoke", headers=_OP)
    assert revoke.status_code == 200, revoke.text

    r = client.get("/api/v1/guest/conversation", headers=_auth(session))
    assert r.status_code == 403
    assert r.json()["detail"]["reason"] == "contact_revoked"

    r_send = client.post("/api/v1/guest/send", json={"body": "hi"}, headers=_auth(session))
    assert r_send.status_code == 403
    assert r_send.json()["detail"]["reason"] == "contact_revoked"

    # Re-entry is dead too - a revoked contact can never walk back in.
    r2 = _join(client, inv["token"], name="Alice", pubkey="KEY-A")
    assert r2.status_code == 401
    assert r2.json() == {"detail": "invalid or expired invite"}


# ═══════════════════════════════════════════════════════════════════════════
# 2. Reusable: fan-out, dedupe, rate limit, link-disable survival
# ═══════════════════════════════════════════════════════════════════════════
def test_e2e_reusable_two_keys_fan_out_to_separate_dms_and_dedupe(env, client):
    inv = _mint_dm(client, reusable=True)

    r_a = _join(client, inv["token"], name="Alice", pubkey="KEY-A")
    r_m = _join(client, inv["token"], name="Mallory", pubkey="KEY-M")
    assert r_a.status_code == 200, r_a.text
    assert r_m.status_code == 200, r_m.text
    gid_a, gid_m = r_a.json()["group"]["id"], r_m.json()["group"]["id"]
    assert gid_a != gid_m
    for gid in (gid_a, gid_m):
        grp = G.load_group(gid)
        assert grp.metadata.get("mode") == "dm"
        assert grp.member_count == 2

    # Alice returns via the SAME standing link -> back to her own DM, no 3rd seat.
    r_a_again = _join(client, inv["token"], name="Alice", pubkey="KEY-A")
    assert r_a_again.status_code == 200, r_a_again.text
    assert r_a_again.json()["group"]["id"] == gid_a
    assert G.load_group(gid_a).member_count == 2


def test_e2e_reusable_rate_limit_past_cap_generic_401(env, client, monkeypatch):
    monkeypatch.setenv("SKCHAT_DM_CONTACT_RATE_LIMIT", "2")
    inv = _mint_dm(client, reusable=True)

    r1 = _join(client, inv["token"], name="A", pubkey="KEY-1")
    r2 = _join(client, inv["token"], name="B", pubkey="KEY-2")
    assert r1.status_code == 200, r1.text
    assert r2.status_code == 200, r2.text

    r3 = _join(client, inv["token"], name="C", pubkey="KEY-3")
    assert r3.status_code == 401
    assert r3.json() == {"detail": "invalid or expired invite"}

    # A returning key is never rate-limited (not a NEW contact).
    r4 = _join(client, inv["token"], name="A", pubkey="KEY-1")
    assert r4.status_code == 200, r4.text


def test_e2e_reusable_link_disable_blocks_new_not_existing_contacts(env, client):
    inv = _mint_dm(client, reusable=True)

    r_a = _join(client, inv["token"], name="Alice", pubkey="KEY-A")
    r_m = _join(client, inv["token"], name="Mallory", pubkey="KEY-M")
    assert r_a.status_code == 200, r_a.text
    assert r_m.status_code == 200, r_m.text
    session_a = r_a.json()["session_token"]
    session_m = r_m.json()["session_token"]

    revoke = client.delete(f"/api/v1/groups/{inv['group_id']}/invite/{inv['token']}", headers=_OP)
    assert revoke.status_code == 200, revoke.text

    # A brand-new key can no longer walk in through the disabled link.
    r_new = _join(client, inv["token"], name="Dana", pubkey="KEY-D")
    assert r_new.status_code == 401

    # Both already-admitted contacts keep working untouched.
    for session in (session_a, session_m):
        conv = client.get("/api/v1/guest/conversation", headers=_auth(session))
        assert conv.status_code == 200, conv.text
        send = client.post(
            "/api/v1/guest/send", json={"body": "still here"}, headers=_auth(session)
        )
        assert send.status_code == 200, send.text


# ═══════════════════════════════════════════════════════════════════════════
# 3. Alias-leak sweep
# ═══════════════════════════════════════════════════════════════════════════
def test_e2e_alias_never_appears_in_any_guest_or_preview_response(env, client):
    ALIAS = "TopSecretAliasXYZ"
    inv = _mint_dm(client, alias=ALIAS, contact_ttl=3600)

    responses = [
        client.get(f"/api/v1/guest/invite/{inv['token']}"),
    ]
    j = _join(client, inv["token"], name="Alice", pubkey="KEY-A")
    responses.append(j)
    session = j.json()["session_token"]

    responses.append(client.get("/api/v1/guest/conversation", headers=_auth(session)))
    responses.append(
        client.post("/api/v1/guest/send", json={"body": "hello"}, headers=_auth(session))
    )
    responses.append(
        client.post("/api/v1/guest/call", json={"ring": True}, headers=_auth(session))
    )
    files = {"file": ("f.txt", io.BytesIO(b"data"), "text/plain")}
    up = client.post("/api/v1/guest/file", files=files, headers=_auth(session))
    responses.append(up)
    tid = up.json()["transfer_id"]
    responses.append(client.get(f"/api/v1/guest/file/{tid}", headers=_auth(session)))
    rename = client.post("/api/v1/guest/name", json={"name": "Alicia"}, headers=_auth(session))
    responses.append(rename)
    responses.append(
        client.get("/api/v1/guest/conversation", headers=_auth(rename.json()["session_token"]))
    )

    for r in responses:
        assert ALIAS not in r.text, f"alias leaked in {r.request.url}: {r.text}"


# ═══════════════════════════════════════════════════════════════════════════
# 4. Mute: ring skipped, token still minted
# ═══════════════════════════════════════════════════════════════════════════
def test_e2e_muted_contact_ring_skipped_token_still_minted(env, client, spies):
    broadcasts, alerts = spies
    inv = _mint_dm(client)
    j = _join(client, inv["token"], name="Alice", pubkey="KEY-A").json()
    fp = j["fingerprint"]

    mute = client.patch(f"/api/v1/guest-dm/contacts/{fp}", json={"muted": True}, headers=_OP)
    assert mute.status_code == 200, mute.text

    r = client.post("/api/v1/guest/call", json={"ring": True}, headers=_auth(j["session_token"]))
    assert r.status_code == 200, r.text
    assert r.json()["available"] is True
    assert r.json().get("token")

    assert [b for b in broadcasts if b.get("type") == "guest_call"] == []
    assert alerts == []
