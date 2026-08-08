"""guest-dm G7: server-proof that a 3-party gdm (operator + 2 web guests) lands
everyone in the SAME derived LiveKit room, with real requests through a FastAPI
TestClient, not by calling ``derive_group_room``/``derive_room`` in isolation
and trusting the result.

G7's manual browser checklist only confirms audio works once three humans are
already in a room together; it says nothing about whether the SERVER actually
put them in the same room, or whether a guest's token is scoped the way the
epic assumes. This suite drives the real routes and decodes the minted LiveKit
JWTs (same technique as ``tests/test_spaces_tokens.py``) so each assertion
reads the room/grants the server actually handed out, never a recomputation.

Routes exercised:
  * ``POST /api/v1/groups/{gid}/invite?mode=dm``  - mint + promote (operator)
  * ``POST /api/v1/guest/join``                   - guest admits into a group
  * ``POST /api/v1/guest/call``                   - guest call token (re)mint
  * ``POST /api/v1/groups/{gid}/call/join``        - OPERATOR's own join path
    (``src/skchat/daemon_proxy.py::api_group_call_join``, backed by
    ``daemon_proxy_groupcall.group_call_context`` - see module docstring there
    for the "single-token room" derivation philosophy this suite verifies).

FINDING (fixed; the last test is the regression guard): the operator's join
path computed its caller identity from the hardcoded ``daemon_proxy.
OPERATOR_ID`` constant, but the guest-dm invite mint path
(``guest_groups.create_dm_invite``) seeds the group's operator seat from
``identity_bridge.get_sovereign_identity()`` - the RUNNING AGENT's own identity
(lumina/opus/whatever ``SKAGENT`` resolves to), not the literal operator
constant. Those two only agreed when the daemon happened to be running AS
``chef@skworld.io``. Under the documented default (the live daemon runs as the
``lumina`` agent - see this repo's CLAUDE.md, "skchat-daemon.service: main
receive daemon (**lumina**...)"), the two identities diverged and the
operator's own official join route 403'd them out of a room their own guests
were happily sitting in. ``_group_caller_uri`` now resolves the caller against
the seat the group actually carries, restricted to the daemon's own identities.
"""

from __future__ import annotations

import jwt  # PyJWT (already used by guest.py / test_spaces_tokens.py)
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from skchat import call_observability as CO
from skchat import daemon_proxy, identity_bridge, livekit_routes
from skchat import daemon_proxy_groups as G
from skchat import guest_group_routes as GGR
from skchat import guest_groups as GG
from skchat import webui as _webui

_KEY, _SECRET = "test-key", "test-secret-0123456789"
_OP = {"X-Operator-Token": "op-secret"}


def _claims(token):
    return jwt.decode(token, _SECRET, algorithms=["HS256"], options={"verify_aud": False})


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

    # The happy-path default for this whole suite: pin the running agent's
    # sovereign identity to the SAME literal constant the operator call/join
    # route uses (``daemon_proxy.OPERATOR_ID``). This is the condition under
    # which the epic's claim ("operator resolves to the same room") is even
    # possible to test cleanly - see the module docstring FINDING and the
    # xfail at the bottom for what happens when this alignment does NOT hold
    # (which is the documented default in production).
    monkeypatch.setattr(
        identity_bridge, "get_sovereign_identity", lambda: daemon_proxy.OPERATOR_ID
    )
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


def _guest_call(client, session, **body):
    r = client.post("/api/v1/guest/call", json=body, headers=_auth(session))
    assert r.status_code == 200, r.text
    return r.json()


@pytest.fixture
def gdm(client):
    """A promoted room holding two web guests: Alice (KEY-A) and Bob (KEY-B).

    Mirrors the fixture in ``tests/test_guest_dm_g7_gdm_ring_and_files.py`` -
    same admission sequence (mint dm -> Alice joins -> promote -> Bob joins).
    """
    inv = _mint_dm(client)
    gid = inv["group_id"]
    a = _join(client, inv["token"], name="Alice", pubkey="KEY-A")
    promo = _mint_dm(client, gid)
    b = _join(client, promo["token"], name="Bob", pubkey="KEY-B")
    assert G.load_group(gid).metadata.get("mode") == "gdm"
    return gid, a, b


# ═══════════════════════════════════════════════════════════════════════════
# 1 + 5. Operator + two guests all resolve to the SAME server-derived room
# ═══════════════════════════════════════════════════════════════════════════
def test_operator_and_two_guests_land_in_the_same_room(client, gdm):
    """Drive all three real join paths and compare the room the SERVER handed
    back to each party - never a recomputation of ``derive_group_room``."""
    gid, a, b = gdm

    a_call = _guest_call(client, a["session_token"])
    b_call = _guest_call(client, b["session_token"])
    # Property 5: the operator's own group-call route, driven for real.
    op = client.post(f"/api/v1/groups/{gid}/call/join", json={})
    assert op.status_code == 200, op.text
    op_call = op.json()

    assert a_call["room"] == b_call["room"] == op_call["room"]

    # Cross-check against the JWT payload itself, not just the JSON envelope -
    # a server could echo the "right" room in the envelope while minting a
    # token scoped to something else, and the envelope-only check would miss it.
    assert _claims(a_call["token"])["video"]["room"] == a_call["room"]
    assert _claims(b_call["token"])["video"]["room"] == b_call["room"]
    assert _claims(op_call["token"])["video"]["room"] == op_call["room"]


# ═══════════════════════════════════════════════════════════════════════════
# 2. Every token is scoped to the room AND to its own party's identity, never
#    admin/recorder for a guest
# ═══════════════════════════════════════════════════════════════════════════
def test_guest_tokens_are_room_and_identity_scoped_never_admin_or_recorder(client, gdm):
    """Same assertion style as
    ``test_spaces_tokens.py::test_conf_guest_token_is_never_admin_or_recorder``,
    applied to the guest-dm call mint (``guest_group_routes._mint_guest_call_
    token`` -> ``guest.build_livekit_token``, route ``POST /api/v1/guest/
    call``)."""
    _gid, a, b = gdm
    a_call = _guest_call(client, a["session_token"])
    b_call = _guest_call(client, b["session_token"])

    a_claims = _claims(a_call["token"])["video"]
    b_claims = _claims(b_call["token"])["video"]

    # Scoped to the SAME room...
    assert a_claims["room"] == b_claims["room"] == a_call["room"]
    # ...but each guest's token identity (sub) is their OWN guest_id, never
    # the other guest's - a guest cannot pose as anyone else in the room.
    assert _claims(a_call["token"])["sub"] == a["guest_id"]
    assert _claims(b_call["token"])["sub"] == b["guest_id"]
    assert a["guest_id"] != b["guest_id"]

    # Never admin/recorder/room-create for either guest.
    for claims in (a_claims, b_claims):
        assert claims.get("roomAdmin", False) is False
        assert claims.get("roomRecord", False) is False
        assert claims.get("roomCreate", False) is False
        # Can publish (screenshare included per _mint_guest_call_token's
        # allow_screenshare=True) and subscribe, but nothing administrative.
        assert claims["canPublish"] is True
        assert claims["canSubscribe"] is True


# ═══════════════════════════════════════════════════════════════════════════
# 3. Per-room derivation: a guest from a DIFFERENT gdm never lands here
# ═══════════════════════════════════════════════════════════════════════════
def test_a_guest_from_a_different_room_never_lands_in_this_room(client, gdm):
    gid, a, _b = gdm
    a_call = _guest_call(client, a["session_token"])

    # A second, wholly independent guest DM - different mint, different guest.
    inv2 = _mint_dm(client)
    gid2 = inv2["group_id"]
    assert gid2 != gid
    c = _join(client, inv2["token"], name="Carol", pubkey="KEY-C")
    c_call = _guest_call(client, c["session_token"])

    assert c_call["room"] != a_call["room"]
    assert _claims(c_call["token"])["video"]["room"] != a_call["room"]

    # The isolation chokepoint (``_assert_same_group``) also refuses a request
    # that explicitly names the OTHER room - a guest cannot even ask their way
    # in by supplying a foreign group_id in the body.
    cross = client.post(
        "/api/v1/guest/call", json={"group_id": gid}, headers=_auth(c["session_token"])
    )
    assert cross.status_code == 403


# ═══════════════════════════════════════════════════════════════════════════
# 4. A revoked guest cannot obtain a call token for the room at all
# ═══════════════════════════════════════════════════════════════════════════
def test_a_revoked_guest_cannot_obtain_a_call_token(client, gdm):
    gid, _a, b = gdm
    fp_b = GG.pubkey_fingerprint("KEY-B")
    assert client.post(f"/api/v1/guest-dm/contacts/{fp_b}/revoke", headers=_OP).status_code == 200

    r = client.post("/api/v1/guest/call", json={}, headers=_auth(b["session_token"]))
    assert r.status_code == 403
    assert r.json()["detail"]["reason"] == "contact_revoked"
    assert "token" not in r.json()


# ═══════════════════════════════════════════════════════════════════════════
# 5b. REGRESSION: the operator's join path and the guest-dm mint path used to
#     derive the operator identity from two sources that only agreed by luck
# ═══════════════════════════════════════════════════════════════════════════
def test_operator_can_join_their_own_gdm_under_the_documented_default(client, gdm, monkeypatch):
    """Re-run property 5 WITHOUT the alignment the other tests rely on.

    Every other test in this file pins ``identity_bridge.get_sovereign_
    identity`` to the literal ``daemon_proxy.OPERATOR_ID`` in the ``env``
    fixture. That pin is NOT how the real daemon runs: this repo's CLAUDE.md
    documents the live daemon as ``skchat-daemon.service`` running the
    ``lumina`` agent, and ``guest_groups.create_dm_invite`` seeds a fresh
    dm/gdm group's operator seat from ``identity_bridge.get_sovereign_
    identity()`` (the RUNNING AGENT's own identity).

    This is the regression test for that divergence: ``_group_caller_uri`` used
    to return the hardcoded ``OPERATOR_ID`` constant from both of its branches,
    so under the documented default the operator's own official join route 403'd
    them out of a room their own guests were sitting in. The caller is now
    resolved against the seat the group actually carries, restricted to the
    daemon's own identities (``_local_call_identities``).
    """
    gid, _a, _b = gdm  # confirms the "aligned" fixture DID work above (property 1/5)

    # Simulate the documented live default: the daemon runs as an AI agent,
    # not literally as "chef@skworld.io".
    monkeypatch.setattr(
        identity_bridge, "get_sovereign_identity", lambda: "capauth:lumina@skworld.io"
    )
    inv = _mint_dm(client)  # fresh gdm, seeded under the "lumina" identity
    gid2 = inv["group_id"]
    guest = _join(client, inv["token"], name="Dave", pubkey="KEY-D")
    guest_call = _guest_call(client, guest["session_token"])
    assert guest_call["room"]  # the guest's own call token mints fine

    op = client.post(f"/api/v1/groups/{gid2}/call/join", json={})
    assert op.status_code == 200, op.text
    assert op.json()["room"] == guest_call["room"]

    # Sanity: the OTHER (aligned) gdm from the fixture is unaffected by this
    # test's identity swap having happened after it was minted.
    assert G.load_group(gid) is not None


# ═══════════════════════════════════════════════════════════════════════════
# 6. The caller resolution above is scoped to OUR identities, never a peer's
# ═══════════════════════════════════════════════════════════════════════════
def test_daemon_never_mints_a_call_token_as_a_remote_peers_identity(client, monkeypatch):
    """The fix for the divergence above must not turn into an impersonation seam.

    ``_group_caller_uri`` resolves the caller against the seat the group really
    carries, but only across the daemon's OWN identities
    (``daemon_proxy._local_call_identities``: the operator constant plus the
    running agent). A group whose admin seat belongs to a REMOTE peer - one this
    daemon merely knows about, e.g. a group created on somebody else's node -
    must still be refused, and must never yield a token whose LiveKit identity
    is that peer. Had the caller been resolved as "whatever holds the admin
    seat", this would mint a publish-capable token under the peer's name.
    """
    remote = "capauth:jarvis@skworld.io"
    grp = G.create_group(name="Someone else's room", creator_uri=remote, members=[])
    G.save_group(grp)
    assert grp.get_member(remote) is not None  # the remote peer holds the seat

    # Neither of our identities is on that roster...
    monkeypatch.setattr(
        identity_bridge, "get_sovereign_identity", lambda: "capauth:lumina@skworld.io"
    )
    assert grp.get_member(daemon_proxy.OPERATOR_ID) is None
    assert grp.get_member("capauth:lumina@skworld.io") is None

    # ...so the gate fails closed rather than adopting the peer's seat.
    r = client.post(f"/api/v1/groups/{grp.id}/call/join", json={})
    assert r.status_code == 403
    assert "token" not in r.json()

    # And the caller helper itself never hands back the remote identity.
    assert daemon_proxy._group_caller_uri(grp) != remote
