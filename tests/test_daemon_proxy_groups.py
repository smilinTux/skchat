"""Tests for GROUP support in ``skchat.daemon_proxy`` (the WEB HTTP path).

Phase 2 of the Sovereign Comms Suite. Everything must work over the same-origin
webui HTTP endpoints (the Flutter app is a web build — CLI is a no-op on web).

Contract the Flutter app depends on:
  * POST /api/v1/groups  → create, persists + lists (CreateGroupResult shape)
  * GET  /api/v1/groups  → list (is_group:true, member_count)
  * GET  /api/v1/groups/{id}/members → member shape (identity_uri/role/...)
  * POST /api/v1/groups/{id}/members → add (or PROMOTE a 1:1)
  * DELETE /api/v1/groups/{id}/members/{identity} → remove (rotates key)
  * DELETE /api/v1/groups/{id}/members/self → leave
  * POST /api/v1/send {group_id|recipient} → fan out, persist on group thread
  * GET  /api/v1/conversations/{group_id} → group thread (same message contract)
  * GET  /api/v1/conversations → includes groups with is_group:true
"""

from __future__ import annotations

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from skchat import daemon_proxy
from skchat import daemon_proxy_groups as G


@pytest.fixture
def client(tmp_path, monkeypatch):
    from skchat.history import ChatHistory

    hist = ChatHistory(store=None, history_dir=tmp_path / "history")
    monkeypatch.setattr(daemon_proxy, "_HISTORY", hist)

    # Isolate the group store to a tmp dir.
    groups_dir = tmp_path / "groups"
    monkeypatch.setattr(G, "_GROUPS_DIR", groups_dir)

    # Don't enrich with the operator's real ~/.skcapstone/peers.
    monkeypatch.setattr(daemon_proxy, "_other_peers", lambda: [])
    # resolve_identity: keep short names as-is so assertions are predictable.
    monkeypatch.setattr(G, "resolve_identity", lambda raw: (raw or "").strip())

    app = FastAPI()
    app.include_router(daemon_proxy.router)
    return TestClient(app)


def _create(client, name="Penguins", members=None, description="", acl=None):
    body = {"name": name}
    if members is not None:
        body["members"] = [{"identity": m} for m in members]
    if description:
        body["description"] = description
    if acl:
        body["acl"] = acl
    r = client.post("/api/v1/groups", json=body)
    assert r.status_code == 200, r.text
    return r.json()


# --------------------------------------------------------------------------- #
# Create + list + persist
# --------------------------------------------------------------------------- #
def test_create_group_persists_and_lists(client):
    res = _create(client, name="Penguins", members=["lumina", "jarvis"],
                  description="the kingdom")
    gid = res["group_id"]
    assert gid
    assert res["name"] == "Penguins"
    assert res["description"] == "the kingdom"
    assert res["key_algorithm"] == "AES-256-GCM"
    assert res["is_group"] is True
    # creator (operator) + 2 members = 3
    assert res["member_count"] == 3

    # GET /groups lists it with is_group:true + member_count.
    groups = client.get("/api/v1/groups").json()
    assert any(g["peer_id"] == gid and g["is_group"] and g["member_count"] == 3
               for g in groups)

    # It also shows up in the unified conversations list.
    convos = client.get("/api/v1/conversations").json()
    assert any(c["peer_id"] == gid and c["is_group"] for c in convos)


def test_create_group_requires_name(client):
    r = client.post("/api/v1/groups", json={"members": [{"identity": "lumina"}]})
    assert r.status_code == 400


def test_members_endpoint_shape(client):
    res = _create(client, members=["lumina"])
    gid = res["group_id"]
    members = client.get(f"/api/v1/groups/{gid}/members").json()
    by_uri = {m["identity_uri"]: m for m in members}
    assert daemon_proxy.OPERATOR_ID in by_uri
    assert "lumina" in by_uri
    # Contract fields the Flutter GroupMemberInfo.fromJson reads.
    for m in members:
        assert set(["identity_uri", "display_name", "role", "participant_type",
                    "is_online"]).issubset(m.keys())
    # Creator is admin; lumina is an agent participant.
    assert by_uri[daemon_proxy.OPERATOR_ID]["role"] == "admin"
    assert by_uri["lumina"]["participant_type"] == "agent"


# --------------------------------------------------------------------------- #
# Add / remove member
# --------------------------------------------------------------------------- #
def test_add_member(client):
    gid = _create(client, members=["lumina"])["group_id"]
    r = client.post(f"/api/v1/groups/{gid}/members", json={"identity": "jarvis"})
    assert r.status_code == 200, r.text
    uris = {m["identity_uri"] for m in client.get(f"/api/v1/groups/{gid}/members").json()}
    assert "jarvis" in uris


def test_remove_member_rotates_key(client):
    gid = _create(client, members=["lumina", "jarvis"])["group_id"]
    before = G.load_group(gid).key_version
    r = client.delete(f"/api/v1/groups/{gid}/members/jarvis")
    assert r.status_code == 200, r.text
    uris = {m["identity_uri"] for m in client.get(f"/api/v1/groups/{gid}/members").json()}
    assert "jarvis" not in uris
    # Forward secrecy: removing a member rotates the group key.
    assert G.load_group(gid).key_version > before


def test_leave_group(client):
    gid = _create(client, members=["lumina"])["group_id"]
    r = client.delete(f"/api/v1/groups/{gid}/members/self")
    assert r.status_code == 200
    uris = {m["identity_uri"] for m in client.get(f"/api/v1/groups/{gid}/members").json()}
    assert daemon_proxy.OPERATOR_ID not in uris


# --------------------------------------------------------------------------- #
# Group send fan-out + history
# --------------------------------------------------------------------------- #
def test_group_send_fans_out_and_history_returns_contract(client):
    gid = _create(client, members=["lumina", "jarvis"])["group_id"]
    r = client.post("/api/v1/send", json={"group_id": gid, "message": "standup time"})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["ok"] is True
    assert body["group_id"] == gid
    msg = body["message"]
    assert msg["content"] == "standup time"
    assert msg["conversation_id"] == gid
    assert msg["is_outbound"] is True  # operator authored it

    # Group history returns the contract, oldest-first, one row per message.
    hist = client.get(f"/api/v1/conversations/{gid}").json()
    assert [m["content"] for m in hist] == ["standup time"]
    assert hist[0]["conversation_id"] == gid
    for key in ("id", "sender", "content_type", "body", "ts", "is_outbound",
                "is_agent", "sender_name"):
        assert key in hist[0]

    # Sending via `recipient` (no group_id) also routes to the group.
    client.post("/api/v1/send", json={"recipient": gid, "message": "second"})
    hist = client.get(f"/api/v1/conversations/{gid}").json()
    assert [m["content"] for m in hist] == ["standup time", "second"]

    # Conversation preview reflects the latest group message.
    convos = client.get("/api/v1/conversations").json()
    grp = next(c for c in convos if c["peer_id"] == gid)
    assert grp["last_message"] == "second"


def test_agent_sender_flagged_in_group_history(client):
    """A message authored by an agent member reads back is_agent:true."""
    gid = _create(client, members=["lumina"])["group_id"]
    hist = daemon_proxy._get_history()
    group = G.load_group(gid)
    G.fan_out_send(group, hist, "lumina", "hello team")
    rows = client.get(f"/api/v1/conversations/{gid}").json()
    msg = next(m for m in rows if m["content"] == "hello team")
    assert msg["is_agent"] is True
    assert msg["is_outbound"] is False


# --------------------------------------------------------------------------- #
# Promote 1:1 → group (same room id, history preserved)
# --------------------------------------------------------------------------- #
def test_promote_one_to_one_keeps_room_id_and_history(client):
    # Seed a 1:1 history between the operator and "jarvis".
    hist = daemon_proxy._get_history()
    from skchat.models import ChatMessage

    hist.save(ChatMessage(sender=daemon_proxy.OPERATOR_ID, recipient="jarvis",
                          content="hey jarvis"))
    hist.save(ChatMessage(sender="jarvis", recipient=daemon_proxy.OPERATOR_ID,
                          content="hey chef"))

    # Add a member to the 1:1 — promotes it to a group of the SAME id.
    r = client.post("/api/v1/groups/jarvis/members", json={"identity": "lumina"})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["promoted"] is True
    # Same room id.
    assert body["group"]["peer_id"] == "jarvis"
    assert body["group"]["is_group"] is True

    # Members = operator (admin) + jarvis + lumina.
    uris = {m["identity_uri"] for m in client.get("/api/v1/groups/jarvis/members").json()}
    assert {daemon_proxy.OPERATOR_ID, "jarvis", "lumina"}.issubset(uris)

    # The prior 1:1 history is preserved on the group thread.
    hist_rows = client.get("/api/v1/conversations/jarvis").json()
    contents = [m["content"] for m in hist_rows]
    assert "hey jarvis" in contents and "hey chef" in contents

    # A new group send appends to the SAME room.
    client.post("/api/v1/send", json={"group_id": "jarvis", "message": "now a group"})
    contents = [m["content"] for m in client.get("/api/v1/conversations/jarvis").json()]
    assert contents[-1] == "now a group"


def test_promote_is_idempotent_adds_member(client):
    """Adding to an already-promoted group just adds the member (no re-promote)."""
    client.post("/api/v1/groups/jarvis/members", json={"identity": "lumina"})
    r = client.post("/api/v1/groups/jarvis/members", json={"identity": "opus"})
    assert r.status_code == 200
    uris = {m["identity_uri"] for m in client.get("/api/v1/groups/jarvis/members").json()}
    assert "opus" in uris and "lumina" in uris


# --------------------------------------------------------------------------- #
# Room ACL (lightweight v1)
# --------------------------------------------------------------------------- #
def test_read_only_announcement_group_blocks_member_posts(client):
    """A read-only (announcement) group rejects sends from non-admins."""
    gid = _create(client, members=["lumina"], acl={"read_only": True})["group_id"]
    # Operator (admin) can still post.
    r = client.post("/api/v1/send", json={"group_id": gid, "message": "ann"})
    assert r.status_code == 200
    # A member (lumina) cannot — simulate by checking the ACL gate directly.
    group = G.load_group(gid)
    assert G.can_post(group, "lumina") is False
    assert G.can_post(group, daemon_proxy.OPERATOR_ID) is True


def test_update_group_name_and_acl(client):
    gid = _create(client, members=["lumina"])["group_id"]
    r = client.put(f"/api/v1/groups/{gid}",
                   json={"name": "Renamed", "acl": {"announcement": True}})
    assert r.status_code == 200
    grp = G.load_group(gid)
    assert grp.name == "Renamed"
    assert G._acl(grp)["announcement"] is True


def test_missing_group_404s(client):
    assert client.get("/api/v1/groups/nope/members").status_code == 404
    assert client.put("/api/v1/groups/nope", json={"name": "x"}).status_code == 404
    assert client.delete("/api/v1/groups/nope/members/self").status_code == 404
    assert client.delete("/api/v1/groups/nope").status_code == 404


# --------------------------------------------------------------------------- #
# Delete group (admin-only)
# --------------------------------------------------------------------------- #
def test_delete_group_admin_removes_and_tombstones(client):
    """The creator (operator = admin) can delete: gone from list + store, tombstoned."""
    res = _create(client, name="Doomed", members=["lumina"])
    gid = res["group_id"]
    # Present before.
    assert any(g["peer_id"] == gid for g in client.get("/api/v1/groups").json())

    r = client.delete(f"/api/v1/groups/{gid}")
    assert r.status_code == 200, r.text
    assert r.json()["deleted"] == gid

    # Gone from the list + the unified conversations + the store (404 on members).
    assert not any(g["peer_id"] == gid for g in client.get("/api/v1/groups").json())
    assert not any(c["peer_id"] == gid for c in client.get("/api/v1/conversations").json())
    assert client.get(f"/api/v1/groups/{gid}/members").status_code == 404
    assert G.load_group(gid) is None

    # A tombstone was written so a re-list / re-sync doesn't resurrect it.
    tomb = G._groups_dir() / f"{gid}.deleted.json"
    assert tomb.exists()


def test_delete_group_non_admin_forbidden(client, monkeypatch):
    """A non-admin operator cannot delete — 403, group untouched."""
    gid = _create(client, name="NotYours", members=["lumina"])["group_id"]

    # Simulate the caller NOT being an admin of this group.
    monkeypatch.setattr(G, "is_admin", lambda group, identity: False)
    r = client.delete(f"/api/v1/groups/{gid}")
    assert r.status_code == 403, r.text

    # Still present + nothing tombstoned.
    assert G.load_group(gid) is not None
    assert not (G._groups_dir() / f"{gid}.deleted.json").exists()


def test_deleted_group_not_relisted(client):
    """After delete, list_groups ignores the tombstone file (no phantom group)."""
    gid = _create(client, name="Ghost")["group_id"]
    client.delete(f"/api/v1/groups/{gid}")
    groups = client.get("/api/v1/groups").json()
    assert all(g["peer_id"] != gid for g in groups)
    # Tombstone file exists but is not parsed as a group.
    assert (G._groups_dir() / f"{gid}.deleted.json").exists()
