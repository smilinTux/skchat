"""guest-dm G7 (server half): a gdm ring reaches a ws-less operator, and files.

C5 gave the operator a POLL fallback for guest call rings, so an app with no ws
channel still raises an incoming-ring banner. It only ever stamped the ring onto
the ``mode=dm`` badge, though, so on a PROMOTED room (``gdm``) a ws-less
operator got nothing: the guest's call rang into silence. G7 extends the same
fallback to a gdm, where several guests share one room - so unlike the dm badge
(one guest, one flat ring) the payload has to say WHICH guest is ringing, and
that answer must come from the server, never from a name the guest supplies.

The file half is a smoke test of the sharing rules a promoted room implies: a
guest's upload is readable by the room's other guest and by the operator, and a
revoked guest is refused honestly rather than silently served.
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
    GG._guest_ring_ts.clear()
    return tmp_path


@pytest.fixture
def client(env, monkeypatch):
    async def _bc(msg):
        return None

    monkeypatch.setattr(_webui, "_ws_broadcast", _bc)
    monkeypatch.setattr(CO, "alert_operator", lambda **kw: None)
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


@pytest.fixture
def gdm(client):
    """A promoted room holding two guests: Alice (KEY-A) and Bob (KEY-B)."""
    inv = _mint_dm(client)
    gid = inv["group_id"]
    a = _join(client, inv["token"], name="Alice", pubkey="KEY-A")
    promo = _mint_dm(client, gid)
    b = _join(client, promo["token"], name="Bob", pubkey="KEY-B")
    assert G.load_group(gid).metadata.get("mode") == "gdm"
    return gid, a, b


def _ringers(gid):
    return G.group_to_conversation(G.load_group(gid)).get("ringers") or []


# ═══════════════════════════════════════════════════════════════════════════
# Ring poll fallback on a promoted room
# ═══════════════════════════════════════════════════════════════════════════
def test_a_quiet_gdm_reports_no_ring(client, gdm):
    gid, _a, _b = gdm
    conv = G.group_to_conversation(G.load_group(gid))
    assert conv["ringing"] is False
    assert _ringers(gid) == []


def test_a_guest_call_on_a_gdm_reaches_a_ws_less_operator(client, gdm):
    gid, _a, b = gdm
    r = client.post(
        "/api/v1/guest/call", json={"ring": True}, headers=_auth(b["session_token"])
    )
    assert r.status_code == 200, r.text

    conv = G.group_to_conversation(G.load_group(gid))
    assert conv["ringing"] is True
    # Several guests share one room, so the payload must name WHO is ringing.
    ringers = conv["ringers"]
    assert [x["guest_name"] for x in ringers] == ["Bob"]
    assert ringers[0]["ring_ts"] is not None
    assert conv["ring_ts"] == ringers[0]["ring_ts"]


def test_the_ringer_identity_is_server_resolved_alias_wins(client, gdm):
    """The operator's private alias wins over the self-chosen name, exactly as
    it does on the roster - the banner must never render a name the guest
    picked as if the operator had approved it."""
    gid, _a, b = gdm
    fp_b = GG.pubkey_fingerprint("KEY-B")
    assert client.patch(
        f"/api/v1/guest-dm/contacts/{fp_b}", json={"alias": "Work Bob"}, headers=_OP
    ).status_code == 200
    client.post("/api/v1/guest/call", json={"ring": True}, headers=_auth(b["session_token"]))

    ringer = _ringers(gid)[0]
    assert ringer["guest_alias"] == "Work Bob"
    assert ringer["guest_name"] == "Bob"


def test_two_guests_ringing_are_both_named_newest_first(client, gdm):
    gid, a, b = gdm
    client.post("/api/v1/guest/call", json={"ring": True}, headers=_auth(a["session_token"]))
    client.post("/api/v1/guest/call", json={"ring": True}, headers=_auth(b["session_token"]))

    ringers = _ringers(gid)
    assert {x["guest_name"] for x in ringers} == {"Alice", "Bob"}
    # Newest first, so the banner surfaces the most recent caller.
    assert ringers[0]["ring_ts"] >= ringers[1]["ring_ts"]


def test_a_muted_guest_never_stamps_a_ring_on_a_gdm(client, gdm):
    gid, _a, b = gdm
    fp_b = GG.pubkey_fingerprint("KEY-B")
    client.patch(f"/api/v1/guest-dm/contacts/{fp_b}", json={"muted": True}, headers=_OP)

    r = client.post(
        "/api/v1/guest/call", json={"ring": True}, headers=_auth(b["session_token"])
    )
    # The token still mints (the call works); it just rings nobody.
    assert r.status_code == 200
    assert _ringers(gid) == []
    assert G.group_to_conversation(G.load_group(gid))["ringing"] is False


def test_a_revoked_member_is_not_reported_as_ringing(client, gdm):
    gid, _a, b = gdm
    client.post("/api/v1/guest/call", json={"ring": True}, headers=_auth(b["session_token"]))
    assert _ringers(gid)  # ringing before the revoke

    GG.revoke_group_membership(GG.pubkey_fingerprint("KEY-B"), gid)

    # Off the roster means off the ring list: answering would join nobody.
    assert _ringers(gid) == []


# ═══════════════════════════════════════════════════════════════════════════
# File sharing in a promoted room
# ═══════════════════════════════════════════════════════════════════════════
def _upload(client, session, name="hi.txt", payload=b"payload"):
    r = client.post(
        "/api/v1/guest/file",
        files={"file": (name, io.BytesIO(payload), "text/plain")},
        data={"caption": "shared"},
        headers=_auth(session),
    )
    assert r.status_code == 200, r.text
    msg = r.json().get("message") or {}
    tid = (msg.get("attachments") or [{}])[0].get("transfer_id") or r.json().get("transfer_id")
    assert tid
    return tid


def test_a_guest_upload_is_readable_by_the_rooms_other_guest(client, gdm):
    _gid, a, b = gdm
    tid = _upload(client, a["session_token"])
    dl = client.get(f"/api/v1/guest/file/{tid}", headers=_auth(b["session_token"]))
    assert dl.status_code == 200
    assert dl.content == b"payload"


def test_a_revoked_guest_is_refused_the_rooms_files_honestly(client, gdm):
    gid, a, b = gdm
    tid = _upload(client, a["session_token"])
    GG.revoke_group_membership(GG.pubkey_fingerprint("KEY-B"), gid)

    dl = client.get(f"/api/v1/guest/file/{tid}", headers=_auth(b["session_token"]))
    # Refused, and refused as an authorization failure - not a 200 with an empty
    # body, and not a 404 pretending the file never existed.
    assert dl.status_code == 403
