"""Guest-DM E2E: server integration suite — full invite/DM lifecycle.

Per task f33eb91c (epic 8685ede6), the merge gate for the server half of the
direct-DM guest invites feature. Depends on all server cards: S1 (d964b5a7,
mint/preview), S2 (a69e7d4e, dm_contacts registry + reusable fanout), S3
(a0b8f930, re-entry + revoke/expiry chokepoint), S4 (35e2f911, operator
contact routes), S5 (bd94558f, guest rename), S6 (66eaa6d8, call ring).

Exercises the spec's Testing section end to end, over a real FastAPI
TestClient (``daemon_proxy`` + ``guest_group_routes`` routers), against
tmp-path/env-isolated stores (mirrors the style of the existing guest-group
route tests). Each scenario is its own test so a failure localizes to the
exact lifecycle step that broke, but the module's contract is the FULL
sequence: mint -> preview -> join -> text -> files -> calls -> rename ->
revoke, for both single-use and reusable invites, plus a whole-run alias-leak
sweep.
"""

from __future__ import annotations

import io
import time
from datetime import datetime, timedelta, timezone

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from skchat import call_observability as CO
from skchat import daemon_proxy
from skchat import daemon_proxy_groups as G
from skchat import daemon_proxy_groupcall as GC
from skchat import guest_group_routes as GGR
from skchat import guest_groups as GG
from skchat import livekit_routes
from skchat import webui as _webui

_OPERATOR = "capauth:lumina@skworld.io"
_OP_HEADERS = {"X-Operator-Token": "op-secret"}
_LK_KEY, _LK_SECRET = "test-key", "test-secret-0123456789"


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


@pytest.fixture
def call_env(env, monkeypatch):
    """``env`` plus working LiveKit creds so ``/guest/call`` actually mints."""
    monkeypatch.setattr(livekit_routes, "LIVEKIT_API_KEY", _LK_KEY)
    monkeypatch.setattr(livekit_routes, "LIVEKIT_API_SECRET", _LK_SECRET)
    monkeypatch.setenv("SKCHAT_LIVEKIT_API_KEY", _LK_KEY)
    monkeypatch.setenv("SKCHAT_LIVEKIT_API_SECRET", _LK_SECRET)
    return env


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


# ── shared helpers ────────────────────────────────────────────────────────────
def _mint_dm(client, **body):
    r = client.post("/api/v1/groups/self/invite?mode=dm", json=body, headers=_OP_HEADERS)
    assert r.status_code == 200, r.text
    return r.json()


def _preview(client, token):
    return client.get(f"/api/v1/guest/invite/{token}")


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
            sender=_OPERATOR,
            recipient=f"group:{group_id}",
            content=content,
            thread_id=group_id,
            timestamp=when or datetime.now(timezone.utc),
            metadata={"group_id": group_id},
        )
    )


# ═══════════════════════════════════════════════════════════════════════════
# (1) Single-use lifecycle
# ═══════════════════════════════════════════════════════════════════════════
def test_single_use_mint_preview_shows_dm_never_alias(env, client):
    minted = _mint_dm(client, alias="Secret Nickname", contact_ttl=3600)
    assert minted["single_use"] is True
    assert "alias" not in minted
    assert "Secret Nickname" not in client.get(
        f"/api/v1/guest/invite/{minted['token']}"
    ).text

    preview = _preview(client, minted["token"])
    assert preview.status_code == 200, preview.text
    body = preview.json()
    assert body["valid"] is True
    assert body["mode"] == "dm"
    assert body["operator_name"]
    assert "alias" not in body


def test_single_use_join_lands_2seat_dm_not_group_join_seat_cap_holds(env, client):
    minted = _mint_dm(client)
    r = _join(client, minted["token"], name="Alice", pubkey="KEY-A")
    assert r.status_code == 200, r.text
    body = r.json()
    gid = body["group"]["id"]
    assert body["group"]["mode"] == "dm"

    grp = G.load_group(gid)
    assert grp.metadata.get("mode") == "dm"
    assert grp.member_count == 2  # operator + Alice, NOT a bare group-join
    assert grp.get_member(_OPERATOR) is not None

    # Single-use: a second distinct occupant is refused outright (seat cap
    # holds even before considering the reusable-fanout path).
    r2 = _join(client, minted["token"], name="Mallory", pubkey="KEY-M")
    assert r2.status_code in (401, 403)
    assert G.load_group(gid).member_count == 2


def test_single_use_text_both_directions_epoch_fence(env, client):
    minted = _mint_dm(client)
    gid = minted["group_id"]
    hist = daemon_proxy._get_history()

    pre_join = datetime.now(timezone.utc) - timedelta(hours=1)
    _save_operator_msg(hist, gid, "before you arrived", when=pre_join)

    joined = _join(client, minted["token"], pubkey="KEY-A").json()
    session = joined["session_token"]
    # Epoch fence: the join bootstrap never surfaces pre-join operator text.
    assert all(m["body"] != "before you arrived" for m in joined["messages"])

    # Guest -> operator direction.
    r = client.post(
        "/api/v1/guest/send", json={"body": "hi from guest"}, headers=_auth(session)
    )
    assert r.status_code == 200, r.text

    # Operator -> guest direction, sent AFTER the epoch.
    _save_operator_msg(hist, gid, "welcome!", when=datetime.now(timezone.utc))

    conv = client.get("/api/v1/guest/conversation", headers=_auth(session)).json()
    bodies = [m["body"] for m in conv["messages"]]
    assert "hi from guest" in bodies
    assert "welcome!" in bodies
    assert "before you arrived" not in bodies

    # The guest's message is durably in the canonical thread the operator reads.
    thread = G.group_thread_messages(hist, gid)
    assert any(m.content == "hi from guest" for m in thread)


def test_single_use_file_upload_download_scoped_foreign_403(env, client):
    minted_a = _mint_dm(client)
    session_a = _join(client, minted_a["token"], name="Alice", pubkey="KEY-A").json()[
        "session_token"
    ]
    minted_b = _mint_dm(client)
    session_b = _join(client, minted_b["token"], name="Bob", pubkey="KEY-B").json()[
        "session_token"
    ]

    files = {"file": ("note.txt", io.BytesIO(b"alice's secret"), "text/plain")}
    up = client.post(
        "/api/v1/guest/file", files=files, data={"caption": "mine"}, headers=_auth(session_a)
    )
    assert up.status_code == 200, up.text
    tid = up.json()["transfer_id"]

    own = client.get(f"/api/v1/guest/file/{tid}", headers=_auth(session_a))
    assert own.status_code == 200
    assert own.content == b"alice's secret"

    foreign = client.get(f"/api/v1/guest/file/{tid}", headers=_auth(session_b))
    assert foreign.status_code == 403


def test_single_use_call_token_scoped_to_own_room(call_env, client):
    minted_a = _mint_dm(client)
    joined_a = _join(client, minted_a["token"], name="Alice", pubkey="KEY-A").json()
    minted_b = _mint_dm(client)
    joined_b = _join(client, minted_b["token"], name="Bob", pubkey="KEY-B").json()

    call_a = client.post(
        "/api/v1/guest/call", json={}, headers=_auth(joined_a["session_token"])
    )
    call_b = client.post(
        "/api/v1/guest/call", json={}, headers=_auth(joined_b["session_token"])
    )
    assert call_a.status_code == 200, call_a.text
    assert call_b.status_code == 200, call_b.text

    room_a, room_b = call_a.json()["room"], call_b.json()["room"]
    assert room_a == GC.derive_group_room(joined_a["group"]["id"])
    assert room_b == GC.derive_group_room(joined_b["group"]["id"])
    assert room_a != room_b


def test_single_use_session_expiry_403(env, client):
    minted = _mint_dm(client)
    joined = _join(client, minted["token"], pubkey="KEY-A").json()

    expired = GG.mint_guest_session(
        group_id=joined["group"]["id"],
        guest_id=joined["guest_id"],
        name="Alice",
        fp=joined["fingerprint"],
        ttl=1,
        now_fn=lambda: time.time() - 1000,
    )
    r = client.get("/api/v1/guest/conversation", headers=_auth(expired))
    assert r.status_code == 403

    # The freshly-minted session from join still works.
    r2 = client.get(
        "/api/v1/guest/conversation", headers=_auth(joined["session_token"])
    )
    assert r2.status_code == 200


def test_single_use_reentry_same_key_succeeds_different_key_401(env, client):
    minted = _mint_dm(client)
    r1 = _join(client, minted["token"], name="Alice", pubkey="KEY-A")
    assert r1.status_code == 200, r1.text
    gid = r1.json()["group"]["id"]
    session_1 = r1.json()["session_token"]

    # Burned single-use jti: the SAME key is re-admitted with a fresh session.
    r2 = _join(client, minted["token"], name="Alice", pubkey="KEY-A")
    assert r2.status_code == 200, r2.text
    assert r2.json()["group"]["id"] == gid
    assert r2.json()["session_token"] != session_1
    assert G.load_group(gid).member_count == 2

    # A DIFFERENT key presenting the same burned jti gets the generic 401 -
    # no oracle distinguishing "burned" from "never valid".
    r3 = _join(client, minted["token"], name="Mallory", pubkey="KEY-M")
    assert r3.status_code == 401
    assert r3.json() == {"detail": "invalid or expired invite"}
    assert G.load_group(gid).member_count == 2


def test_single_use_rename_persists(env, client):
    minted = _mint_dm(client)
    joined = _join(client, minted["token"], name="Alice", pubkey="KEY-A").json()
    session = joined["session_token"]
    gid = joined["group"]["id"]
    guest_id = joined["guest_id"]

    r = client.post("/api/v1/guest/name", json={"name": "Alicia"}, headers=_auth(session))
    assert r.status_code == 200, r.text
    new_session = r.json()["session_token"]
    assert r.json()["display_name"] == "Alicia"

    grp = G.load_group(gid)
    assert grp.metadata["guests"][guest_id]["display"] == "Alicia"
    assert grp.get_member(guest_id).display_name == "Alicia"

    # Persists across a fresh read with the new session.
    conv = client.get("/api/v1/guest/conversation", headers=_auth(new_session))
    assert conv.status_code == 200


def test_single_use_revoke_locks_out_with_reason_and_reentry_dead(env, client):
    minted = _mint_dm(client)
    joined = _join(client, minted["token"], pubkey="KEY-A").json()
    session = joined["session_token"]
    fp = joined["fingerprint"]

    assert GG.revoke_dm_contact(fp) is True

    r = client.get("/api/v1/guest/conversation", headers=_auth(session))
    assert r.status_code == 403
    assert r.json()["detail"]["reason"] == "contact_revoked"

    r_send = client.post(
        "/api/v1/guest/send", json={"body": "still here?"}, headers=_auth(session)
    )
    assert r_send.status_code == 403
    assert r_send.json()["detail"]["reason"] == "contact_revoked"

    # Re-entry via the (now-locked-out) invite is dead too.
    r_reentry = _join(client, minted["token"], pubkey="KEY-A")
    assert r_reentry.status_code == 401
    assert r_reentry.json() == {"detail": "invalid or expired invite"}


# ═══════════════════════════════════════════════════════════════════════════
# (2) Reusable my-DM-link
# ═══════════════════════════════════════════════════════════════════════════
def test_reusable_two_keys_two_separate_dms_each_returns_to_own(env, client):
    minted = _mint_dm(client, reusable=True)
    assert minted["single_use"] is False

    r_a1 = _join(client, minted["token"], name="Alice", pubkey="KEY-A")
    r_m1 = _join(client, minted["token"], name="Mallory", pubkey="KEY-M")
    assert r_a1.status_code == r_m1.status_code == 200
    gid_a, gid_m = r_a1.json()["group"]["id"], r_m1.json()["group"]["id"]
    assert gid_a != gid_m

    session_a = r_a1.json()["session_token"]
    client.post(
        "/api/v1/guest/send", json={"body": "it's alice"}, headers=_auth(session_a)
    )

    # Alice returns via the SAME standing link -> back in HER OWN dm, not a
    # third fresh one, with her history intact.
    r_a2 = _join(client, minted["token"], name="Alice", pubkey="KEY-A")
    assert r_a2.status_code == 200, r_a2.text
    assert r_a2.json()["group"]["id"] == gid_a
    assert any(m["body"] == "it's alice" for m in r_a2.json()["messages"])
    assert G.load_group(gid_a).member_count == 2
    assert G.load_group(gid_m).member_count == 2


def test_reusable_rate_limit_past_cap_generic_401(env, client, monkeypatch):
    monkeypatch.setenv("SKCHAT_DM_CONTACT_RATE_LIMIT", "2")
    minted = _mint_dm(client, reusable=True)

    assert _join(client, minted["token"], name="A", pubkey="KEY-1").status_code == 200
    assert _join(client, minted["token"], name="B", pubkey="KEY-2").status_code == 200

    r3 = _join(client, minted["token"], name="C", pubkey="KEY-3")
    assert r3.status_code == 401
    assert r3.json() == {"detail": "invalid or expired invite"}

    # An already-admitted key is unaffected by the new-contact cap.
    r4 = _join(client, minted["token"], name="A", pubkey="KEY-1")
    assert r4.status_code == 200


def test_reusable_operator_disable_link_no_new_admissions_existing_survive(env, client):
    minted = _mint_dm(client, reusable=True)
    joined_a = _join(client, minted["token"], name="Alice", pubkey="KEY-A").json()
    session_a = joined_a["session_token"]
    fp_a = joined_a["fingerprint"]

    revoke = client.delete(
        f"/api/v1/groups/{minted['group_id']}/invite/{minted['token']}",
        headers=_OP_HEADERS,
    )
    assert revoke.status_code == 200, revoke.text

    # No NEW admission via the disabled link.
    r_new = _join(client, minted["token"], name="Newcomer", pubkey="KEY-NEW")
    assert r_new.status_code == 401
    assert r_new.json() == {"detail": "invalid or expired invite"}

    # Alice's existing DM keeps working — her session never re-presents the
    # invite token.
    r_send = client.post(
        "/api/v1/guest/send", json={"body": "still chatting"}, headers=_auth(session_a)
    )
    assert r_send.status_code == 200, r_send.text
    r_conv = client.get("/api/v1/guest/conversation", headers=_auth(session_a))
    assert r_conv.status_code == 200

    # Individually revoking Alice's contact THEN locks her out too.
    assert GG.revoke_dm_contact(fp_a) is True
    r_after = client.get("/api/v1/guest/conversation", headers=_auth(session_a))
    assert r_after.status_code == 403
    assert r_after.json()["detail"]["reason"] == "contact_revoked"


# ═══════════════════════════════════════════════════════════════════════════
# (3) Alias-leak sweep
# ═══════════════════════════════════════════════════════════════════════════
def test_alias_never_leaks_in_any_guest_or_preview_response(call_env, client):
    ALIAS = "Sweep Secret Nickname 42"
    captured = []

    def _cap(resp):
        captured.append(resp)
        return resp

    mint_resp = _cap(
        client.post(
            "/api/v1/groups/self/invite?mode=dm",
            json={"alias": ALIAS, "contact_ttl": 3600},
            headers=_OP_HEADERS,
        )
    )
    assert mint_resp.status_code == 200, mint_resp.text
    minted = mint_resp.json()
    _cap(_preview(client, minted["token"]))
    joined_resp = _cap(_join(client, minted["token"], name="Alice", pubkey="KEY-A"))
    joined = joined_resp.json()
    session = joined["session_token"]

    _cap(client.get("/api/v1/guest/conversation", headers=_auth(session)))
    _cap(
        client.post(
            "/api/v1/guest/send", json={"body": "hi"}, headers=_auth(session)
        )
    )
    _cap(
        client.post(
            "/api/v1/guest/name", json={"name": "Alicia"}, headers=_auth(session)
        )
    )
    _cap(client.post("/api/v1/guest/call", json={"ring": True}, headers=_auth(session)))

    files = {"file": ("note.txt", io.BytesIO(b"payload"), "text/plain")}
    up = _cap(client.post("/api/v1/guest/file", files=files, headers=_auth(session)))
    tid = up.json()["transfer_id"]
    _cap(client.get(f"/api/v1/guest/file/{tid}", headers=_auth(session)))

    # Also sweep the second, re-minted (post-rename) session's own reads.
    new_session = client.post(
        "/api/v1/guest/name", json={"name": "Alicia2"}, headers=_auth(session)
    ).json()["session_token"]
    _cap(client.get("/api/v1/guest/conversation", headers=_auth(new_session)))

    for resp in captured:
        assert ALIAS not in resp.text, f"alias leaked in {resp.request.url}: {resp.text}"
        try:
            body = resp.json()
        except Exception:
            continue
        if isinstance(body, dict):
            assert "alias" not in body, f"'alias' key leaked in {resp.request.url}: {body}"


# ═══════════════════════════════════════════════════════════════════════════
# (4) Mute: ring skipped, token still minted
# ═══════════════════════════════════════════════════════════════════════════
def test_muted_contact_ring_skipped_but_call_token_still_minted(call_env, client, spies):
    broadcasts, alerts = spies
    minted = _mint_dm(client)
    joined = _join(client, minted["token"], name="Alice", pubkey="KEY-A").json()
    session = joined["session_token"]
    fp = joined["fingerprint"]

    assert GG.update_dm_contact(fp, muted=True)

    r = client.post("/api/v1/guest/call", json={"ring": True}, headers=_auth(session))
    assert r.status_code == 200, r.text
    assert r.json()["available"] is True
    assert r.json()["room"] == GC.derive_group_room(joined["group"]["id"])

    assert [b for b in broadcasts if b.get("type") == "guest_call"] == []
    assert alerts == []
