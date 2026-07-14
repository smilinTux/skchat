"""Group support for the daemon API proxy (the WEB HTTP path).

The Flutter app is a **web build** — its ``DaemonService`` CLI methods are
no-ops on web (``kIsWeb`` guard). So every group feature must work over the
same-origin webui HTTP endpoints. This module backs those endpoints with the
real :class:`skchat.group.GroupChat` model and the same on-disk store the MCP
server / CLI use (``~/.skchat/groups/*.json``), so groups created in the app,
the CLI, or by an agent over MCP are all the SAME objects.

Design (matches docs/comms-suite-plan.md §1 "one room is one entity"):

* A group is a ``GroupChat`` persisted at ``~/.skchat/groups/<id>.json``.
* Group **messages** ride :class:`skchat.history.ChatHistory` keyed by the
  group id: each message is saved with ``recipient="group:<id>"`` and
  ``thread_id=<group_id>``. So ``history.get_thread(<group_id>)`` returns the
  whole group thread (same message contract as a 1:1). A copy is also fanned
  out per non-sender member (``recipient=<member_uri>``) so each member's inbox
  shows it — exactly what :meth:`GroupChat.send` already does.
* **Promote 1:1 → group**: adding a member to a 1:1 turns it into a group
  *with the same room id* — we mint a ``GroupChat`` whose id == the 1:1 peer id,
  seed it with the operator + the existing peer + the new member, and migrate
  the existing 1:1 history onto the group thread (``thread_id``/``recipient``
  rewrite). No new object id, history preserved.

Lightweight Room ACL v1 lives in ``GroupChat.metadata["acl"]``:
``{"read_only": bool, "who_can_add": "admin"|"member", "announcement": bool}``
— creator is always admin. ``read_only``/``announcement`` mean only admins may
post (an announcement channel). ``who_can_add`` gates membership changes.
"""

from __future__ import annotations

import logging
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

logger = logging.getLogger("skchat.daemon_proxy.groups")

# The canonical group store — the SAME directory the MCP server + CLI use.
# Kept as a module constant so existing callers/tests can monkeypatch it; the
# resolver below prefers ``SKCHAT_HOME`` when set (so the daemon/CLI/migrate
# engine + tests all agree on one location), else this default.
_GROUPS_DIR = Path("~/.skchat/groups").expanduser()


def _skchat_home() -> Path:
    return Path(os.environ.get("SKCHAT_HOME", str(Path.home() / ".skchat"))).expanduser()

# Default ACL for a new group (creator = admin).
_DEFAULT_ACL: dict[str, Any] = {
    "read_only": False,
    "announcement": False,
    "who_can_add": "admin",
}


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


# --------------------------------------------------------------------------- #
# SEAM 9 — seal group messages on the fan-out wire (flag-gated, fail-closed)
# --------------------------------------------------------------------------- #
# Group messages historically fanned out as CLEARTEXT while 1:1 DMs were sealed
# (transport.py ``send_message`` -> ``DmRatchetManager.seal``). This wires the
# group's OWN crypto (``GroupChat.encrypt_message`` — classical static-key or the
# hybrid epoch-ratchet, per the group's ``kem_suite``) into ``fan_out_send`` so a
# fanned-out copy carries ciphertext, not plaintext. Gated behind
# ``SKCHAT_SEAL_GROUPS`` (default OFF, so current delivery is byte-for-byte
# unchanged) and FAIL-CLOSED: a member that holds no group key is skipped, never
# fanned out cleartext.

#: Wire marker prefix for a sealed group-message body stored in
#: ``ChatMessage.content`` (mirrors the ``pqdm:``/``pqdr1:`` DM markers in
#: ``crypto.py``). ``skgseal1:`` + base64(json envelope).
GROUP_SEAL_SCHEME = "skgseal1:"


def _seal_groups_enabled() -> bool:
    """Whether group fan-out should seal messages (``SKCHAT_SEAL_GROUPS``).

    Default OFF: unset / ``0`` / ``false`` / ``no`` / ``off`` all mean "leave
    delivery unchanged" (same truthiness convention as ``SKCHAT_DM_RATCHET`` in
    ``transport.py``).
    """
    return os.getenv("SKCHAT_SEAL_GROUPS", "").strip().lower() not in (
        "",
        "0",
        "false",
        "no",
        "off",
    )


def _member_has_group_key(group, member) -> bool:
    """Whether *member* can obtain the group key (so a sealed body is readable).

    Mirrors EXACTLY the condition under which
    :meth:`GroupKeyDistributor.distribute_key` yields a payload (not ``None``): a
    hybrid member needs a hybrid-KEM public key, a classical member needs a PGP
    public key. A member failing this holds NO group key — under
    ``SKCHAT_SEAL_GROUPS`` it is SKIPPED (fail closed), never fanned out cleartext.
    """
    if getattr(group, "is_hybrid", False):
        return bool(getattr(member, "hybrid_kem_public_hex", ""))
    return bool(getattr(member, "public_key_armor", ""))


def _seal_group_content(group, content: str) -> str:
    """Seal *content* under the group's existing crypto → an opaque wire token.

    Reuses :meth:`GroupChat.encrypt_message` (no rebuilt crypto): a single
    ciphertext under the sender/group key (classical) or the current epoch key
    (hybrid), packed as ``skgseal1:`` + base64(json envelope) so every keyed
    member decrypts the same body. Called ONCE per group message.
    """
    import base64
    import json

    envelope = group.encrypt_message(content)
    blob = base64.b64encode(json.dumps(envelope).encode("utf-8")).decode("ascii")
    return GROUP_SEAL_SCHEME + blob


def is_sealed_group_content(content: Any) -> bool:
    """True if *content* is a ``skgseal1:`` sealed group body."""
    return isinstance(content, str) and content.startswith(GROUP_SEAL_SCHEME)


def unseal_group_content(group, content: str) -> str:
    """Open a ``skgseal1:`` sealed group body via :meth:`GroupChat.decrypt_message`.

    Returns *content* unchanged if it is not a sealed token (so a mixed history of
    cleartext + sealed rows reads back uniformly).
    """
    import base64
    import json

    if not is_sealed_group_content(content):
        return content
    envelope = json.loads(base64.b64decode(content[len(GROUP_SEAL_SCHEME):]))
    return group.decrypt_message(envelope)


# --------------------------------------------------------------------------- #
# Persistence (shared store)
# --------------------------------------------------------------------------- #
def _groups_dir() -> Path:
    # SKCHAT_HOME wins when explicitly set; else honour a monkeypatched/overridden
    # module-level _GROUPS_DIR; else the default under SKCHAT_HOME.
    if os.environ.get("SKCHAT_HOME"):
        d = _skchat_home() / "groups"
    else:
        d = _GROUPS_DIR
    d.mkdir(parents=True, exist_ok=True)
    return d


def load_group(group_id: str):
    """Load a ``GroupChat`` by id from ``~/.skchat/groups/<id>.json`` (or None)."""
    from .group import GroupChat

    path = _groups_dir() / f"{group_id}.json"
    if not path.exists():
        return None
    try:
        return GroupChat.model_validate_json(path.read_text(encoding="utf-8"))
    except Exception as exc:
        logger.warning("daemon_proxy_groups: failed to load %s: %s", group_id, exc)
        return None


def save_group(group) -> None:
    """Persist a ``GroupChat`` to ``~/.skchat/groups/<id>.json``."""
    try:
        path = _groups_dir() / f"{group.id}.json"
        path.write_text(group.model_dump_json(indent=2), encoding="utf-8")
    except Exception as exc:
        logger.warning("daemon_proxy_groups: failed to save %s: %s", group.id, exc)


def list_groups() -> list:
    """Return every persisted ``GroupChat``, newest-updated first."""
    from .group import GroupChat

    out = []
    for f in _groups_dir().glob("*.json"):
        # Skip delete tombstones (``<id>.deleted.json``) — not real groups.
        if f.name.endswith(".deleted.json"):
            continue
        try:
            out.append(GroupChat.model_validate_json(f.read_text(encoding="utf-8")))
        except Exception as exc:
            logger.warning("daemon_proxy_groups: skipping %s: %s", f.name, exc)
    out.sort(key=lambda g: g.updated_at, reverse=True)
    return out


# --------------------------------------------------------------------------- #
# ACL helpers (lightweight v1)
# --------------------------------------------------------------------------- #
def _acl(group) -> dict[str, Any]:
    acl = dict(_DEFAULT_ACL)
    acl.update(group.metadata.get("acl") or {})
    return acl


def can_post(group, sender_uri: str) -> bool:
    """True if *sender_uri* may post to this group under the ACL.

    A read-only / announcement group accepts posts from admins only. Otherwise
    any active (non-observer) member may post.
    """
    from .group import MemberRole

    member = group.get_member(sender_uri)
    if member is None:
        return False
    if member.role == MemberRole.OBSERVER:
        return False
    acl = _acl(group)
    if acl.get("read_only") or acl.get("announcement"):
        return member.role == MemberRole.ADMIN
    return True


def can_add_members(group, by_uri: str) -> bool:
    """True if *by_uri* may add/remove members under the ACL ``who_can_add``."""
    from .group import MemberRole

    member = group.get_member(by_uri)
    if member is None:
        return False
    if _acl(group).get("who_can_add") == "member":
        return member.role != MemberRole.OBSERVER
    return member.role == MemberRole.ADMIN


# --------------------------------------------------------------------------- #
# Serialization (Flutter contract)
# --------------------------------------------------------------------------- #
def group_to_conversation(group, *, online_uris: Optional[set[str]] = None) -> dict:
    """Map a ``GroupChat`` to the app conversation shape (``is_group:true``).

    Matches ``Conversation.fromJson`` + ``GroupsNotifier`` expectations:
    ``peer_id`` (== group id), ``display_name``, ``is_group``, ``member_count``,
    ``last_message``, ``last_message_time``.
    """
    return {
        "peer_id": group.id,
        "display_name": group.name,
        "last_message": (group.metadata.get("last_message") or ""),
        "last_message_time": (group.metadata.get("last_message_time") or group.updated_at.isoformat()),
        "soul_fingerprint": group.id,
        "is_online": False,
        "is_agent": False,
        "unread_count": 0,
        "last_delivery_status": "delivered",
        "is_group": True,
        "member_count": group.member_count,
        "avatar_url": "",
        "description": group.description,
        "acl": _acl(group),
    }


def member_to_app(member, *, online_uris: Optional[set[str]] = None) -> dict:
    """Map a ``GroupMember`` to the app member shape (``GroupMemberInfo.fromJson``).

    The Flutter parser reads ``identity_uri``, ``display_name``, ``role``,
    ``participant_type``, ``is_online``.
    """
    online = bool(online_uris and member.identity_uri in online_uris)
    display = member.display_name or member.identity_uri.split(":")[-1].split("@")[0]
    return {
        "identity_uri": member.identity_uri,
        "display_name": display,
        "role": member.role.value,
        "participant_type": member.participant_type.value,
        "is_online": online,
    }


def create_result(group) -> dict:
    """The ``POST /api/v1/groups`` response (``CreateGroupResult.fromJson``)."""
    return {
        "group_id": group.id,
        "name": group.name,
        "description": group.description,
        "member_count": group.member_count,
        "key_id": f"v{group.key_version}",
        "key_algorithm": "AES-256-GCM",
        "members": [{"identity": m.identity_uri} for m in group.members],
        "is_group": True,
        "acl": _acl(group),
    }


# --------------------------------------------------------------------------- #
# Member resolution
# --------------------------------------------------------------------------- #
def resolve_identity(raw: str) -> str:
    """Best-effort canonicalisation of a member handle to a URI.

    Short names (``lumina``, ``jarvis``) resolve via the identity bridge; an
    already-qualified URI / fqid passes through.
    """
    raw = (raw or "").strip()
    if not raw:
        return raw
    if raw.startswith("capauth:") or "@" in raw:
        return raw
    try:
        from .identity_bridge import resolve_peer_name

        return resolve_peer_name(raw)
    except Exception:
        return raw


def _participant_type_for(uri: str):
    """Classify a member URI as agent/human for display (informational only)."""
    from .group import ParticipantType

    short = uri.split(":")[-1].split("@")[0].lower()
    if short in {"lumina", "jarvis", "opus", "ava", "ara", "artisan", "herald",
                 "sentinel", "architect", "scholar", "steward", "coder"}:
        return ParticipantType.AGENT
    return ParticipantType.HUMAN


# --------------------------------------------------------------------------- #
# Group lifecycle
# --------------------------------------------------------------------------- #
def create_group(
    name: str,
    creator_uri: str,
    members: list[str],
    description: str = "",
    acl: Optional[dict[str, Any]] = None,
):
    """Create + persist a new ``GroupChat`` with the creator as admin.

    *members* are raw handles/URIs (the creator is added automatically).
    Returns the persisted group.
    """
    from .group import GroupChat, MemberRole

    # PQC cut-over: resolve every member URI up-front so we can collect their
    # hybrid prekeys and create the group hybrid-from-epoch-1 by DEFAULT (members
    # without a prekey fall back classically and are flagged, not locked out).
    member_uris = []
    for raw in members or []:
        uri = resolve_identity(raw)
        if uri and uri != creator_uri and uri not in member_uris:
            member_uris.append(uri)

    creator_hybrid = ""
    member_hybrid_keys: dict[str, str] = {}
    try:
        from . import pq_prekeys as _PQ

        creator_hybrid = _PQ.hybrid_pub_hex_for(creator_uri)
        member_hybrid_keys = _PQ.collect_member_hybrid_keys(member_uris)
    except Exception as exc:  # pragma: no cover - prekey store optional
        logger.debug("hybrid prekey collection skipped: %s", exc)

    group = GroupChat.create(
        name=name,
        creator_uri=creator_uri,
        description=description,
        creator_hybrid_kem_public_hex=creator_hybrid,
        member_hybrid_keys=member_hybrid_keys,
    )
    merged_acl = dict(_DEFAULT_ACL)
    if acl:
        merged_acl.update({k: v for k, v in acl.items() if k in _DEFAULT_ACL})
    group.metadata["acl"] = merged_acl

    for uri in member_uris:
        group.add_member(
            identity_uri=uri,
            role=MemberRole.MEMBER,
            participant_type=_participant_type_for(uri),
            hybrid_kem_public_hex=member_hybrid_keys.get(uri, ""),
        )
    # If a hybrid key arrived only with the members (creator classical), make
    # sure epoch 1 is seeded now that all members are attached.
    group.ensure_epoch()
    save_group(group)
    logger.info(
        "Created group %s (%s) with %d members [suite=%s epoch=%d]",
        group.id[:8],
        name,
        group.member_count,
        group.kem_suite,
        group.epoch,
    )
    return group


def add_member(group, identity: str, role: str = "member"):
    """Add (or re-role) a member and persist. Returns the resolved URI added."""
    from .group import MemberRole

    uri = resolve_identity(identity)
    try:
        role_enum = MemberRole(role)
    except ValueError:
        role_enum = MemberRole.MEMBER
    existing = group.get_member(uri)
    if existing is not None:
        existing.role = role_enum
        group.updated_at = datetime.now(timezone.utc)
    else:
        group.add_member(
            identity_uri=uri,
            role=role_enum,
            participant_type=_participant_type_for(uri),
        )
    save_group(group)
    return uri


def remove_member(group, identity: str) -> bool:
    """Remove a member (rotates the group key) and persist. True if removed."""
    uri = resolve_identity(identity)
    removed = group.remove_member(uri)
    if removed:
        save_group(group)
    return removed


def is_admin(group, identity: str) -> bool:
    """True if *identity* (resolved) is an admin of *group*.

    Used to gate destructive actions (group delete). The group ``created_by``
    is also accepted so the original creator always counts as admin even if a
    legacy persisted group never stamped the ADMIN role on the membership row.
    """
    uri = resolve_identity(identity)
    if group.is_admin(uri):
        return True
    return bool(getattr(group, "created_by", "") and group.created_by == uri)


def delete_group(group_id: str) -> bool:
    """Delete a group: remove its store file + drop a tombstone. True if removed.

    Removes ``~/.skchat/groups/<id>.json`` and writes a sibling
    ``<id>.deleted.json`` tombstone (so a re-sync / re-list doesn't resurrect a
    stale copy and any peer learns the room is gone). Idempotent — a missing
    group still leaves a tombstone and returns False (nothing to remove).
    """
    import json

    d = _groups_dir()
    path = d / f"{group_id}.json"
    tomb = d / f"{group_id}.deleted.json"
    existed = path.exists()
    try:
        if existed:
            path.unlink()
    except Exception as exc:
        logger.warning("delete_group: failed to unlink %s: %s", group_id, exc)
    try:
        tomb.write_text(
            json.dumps({"id": group_id, "deleted_at": _now_iso(), "tombstone": True}),
            encoding="utf-8",
        )
    except Exception as exc:
        logger.warning("delete_group: failed to write tombstone for %s: %s", group_id, exc)
    if existed:
        logger.info("Deleted group %s", group_id[:8])
    return existed


def update_group(group, *, name: Optional[str] = None, description: Optional[str] = None,
                 acl: Optional[dict[str, Any]] = None):
    """Update name/description/acl and persist."""
    if name is not None and name.strip():
        group.name = name.strip()
    if description is not None:
        group.description = description
    if acl:
        cur = dict(_acl(group))
        cur.update({k: v for k, v in acl.items() if k in _DEFAULT_ACL})
        group.metadata["acl"] = cur
    group.updated_at = datetime.now(timezone.utc)
    save_group(group)
    return group


# --------------------------------------------------------------------------- #
# Messaging + history
# --------------------------------------------------------------------------- #
def _delivery_transport(identity: str):
    """Build a ChatTransport for network delivery to member daemons, or None.

    Group fan-out must reach each member's *daemon* (a separate process polling
    its own inbox), not just the operator's local history — otherwise agents
    never see the message and never respond. Best-effort: returns None if the
    transport can't be built so persistence still succeeds.
    """
    try:
        from skcomms import SKComms

        from .history import ChatHistory
        from .transport import ChatTransport

        comm = SKComms.from_config()
        if not getattr(comm, "router", None) or not comm.router.transports:
            return None
        return ChatTransport(skcomms=comm, history=ChatHistory(), identity=identity)
    except Exception as exc:  # noqa: BLE001
        logger.debug("group delivery transport unavailable: %s", exc)
        return None


def fan_out_send(group, hist, sender_uri: str, content: str,
                 reply_to_id: Optional[str] = None, thread_id: Optional[str] = None,
                 content_type: Optional[str] = None, rich: Optional[dict] = None):
    """Persist a group message keyed by the group id + per-member copies.

    Returns the canonical group-thread :class:`ChatMessage` (the one whose
    ``recipient == "group:<id>"``), so the caller can echo it back. The group
    thread message AND each member copy carry ``thread_id == group.id`` so the
    whole conversation reads back via ``history.get_thread(group.id)``.

    ``content_type``/``rich`` carry the P1 typed-message payload (e.g. a
    location pin) onto the group thread + every member copy unchanged.
    """
    from .models import ChatMessage

    def _typed(**kw):
        if content_type:
            kw["content_type"] = content_type
        if rich is not None:
            kw["rich"] = rich
        return ChatMessage(**kw)

    # SEAM 9: seal the body ONCE under the group's own crypto before fan-out (a
    # single ciphertext shared by every keyed member — sender/group-key model),
    # flag-gated + fail-closed. The canonical group-thread copy below stays
    # CLEARTEXT: it is the operator's own local record (recipient=group:<id>,
    # never delivered on the wire) so ``group_thread_messages`` still reads back.
    seal = _seal_groups_enabled()
    sealed_content = _seal_group_content(group, content) if seal else None
    skipped: list[str] = []

    group_msg = _typed(
        sender=sender_uri,
        recipient=f"group:{group.id}",
        content=content,
        thread_id=group.id,
        reply_to_id=reply_to_id or None,
        metadata={"group_id": group.id, "group_name": group.name,
                  "key_version": group.key_version, "sealed": bool(seal)},
    )
    hist.save(group_msg)

    # Network transport so each member's DAEMON actually receives the message
    # (hist.save is local-display only). Without this the message shows in the
    # operator's app but no agent ever sees it, so none respond.
    _transport = _delivery_transport(sender_uri)

    # Per-member copy (so each member's 1:1-style inbox/load(peer=uri) sees it)
    # AND deliver it over the network so their daemon responds.
    for member in group.members:
        if member.identity_uri == sender_uri:
            continue
        # Fail closed: never fan out a plaintext body to a member that holds no
        # group key — skip it entirely (and flag) rather than downgrade to clear.
        if seal and not _member_has_group_key(group, member):
            logger.warning(
                "fan_out_send: skipping %s — no group key "
                "(SKCHAT_SEAL_GROUPS, fail closed)",
                member.identity_uri,
            )
            skipped.append(member.identity_uri)
            continue
        member_msg = _typed(
            sender=sender_uri,
            recipient=member.identity_uri,
            content=sealed_content if seal else content,
            thread_id=group.id,
            reply_to_id=reply_to_id or None,
            metadata={"group_id": group.id, "group_name": group.name,
                      "key_version": group.key_version, "sealed": bool(seal)},
        )
        try:
            hist.save(member_msg)
        except Exception as exc:
            logger.warning("fan_out_send: member copy for %s failed: %s",
                           member.identity_uri, exc)
        # Prefer a direct same-box inbox write (reliable); fall back to the
        # network transport only when the recipient isn't a local agent.
        if not local_deliver_to_agent(member_msg) and _transport is not None:
            try:
                _transport.send_message(member_msg)
            except Exception as exc:
                logger.warning("fan_out_send: delivery to %s failed: %s",
                               member.identity_uri, exc)

    if skipped:
        # Surface the fail-closed skips on the returned group-thread message so
        # callers/UI can flag "N member(s) not reachable sealed" without changing
        # the return type.
        group_msg.metadata["sealed_skipped"] = skipped

    group.touch()
    group.metadata["last_message"] = content
    group.metadata["last_message_time"] = group_msg.timestamp.isoformat()
    save_group(group)
    return group_msg


def local_deliver_to_agent(chat_msg) -> bool:
    """Write a ChatMessage straight to a same-box agent's comms inbox.

    Federation delivery (``transport.send_message`` -> https-s2s ->
    skcomms-api) is flaky, so ``@all`` fan-out can intermittently reach only
    one member (or none). For a recipient that lives on THIS box, skip the
    network entirely: drop a :class:`skcomms.models.MessageEnvelope`
    ``.skc.json`` file straight into ``~/.skcapstone/agents/<agent>/comms/
    inbox/`` — exactly what ``skcomms.transports.file.FileTransport.receive``
    reads, so the recipient's own daemon (``ChatTransport.poll_inbox``) picks
    it up on its next poll, reliably.

    Returns False if *chat_msg.recipient* is NOT a local agent (no
    ``comms/inbox`` dir under its agent home) — the caller should then fall
    back to ``transport.send_message`` (the network path).
    """
    recipient = getattr(chat_msg, "recipient", "") or ""
    agent = recipient.split(":")[-1].split("@")[0].lower()
    if not agent:
        return False
    inbox = Path.home() / ".skcapstone" / "agents" / agent / "comms" / "inbox"
    if not inbox.exists():
        return False
    try:
        from skcomms.models import MessageEnvelope, MessagePayload

        env = MessageEnvelope(
            sender=chat_msg.sender,
            recipient=chat_msg.recipient,
            payload=MessagePayload(content=chat_msg.model_dump_json(), content_type="text"),
        )
        target = inbox / f"{env.envelope_id}.skc.json"
        tmp = target.with_suffix(target.suffix + ".tmp")
        tmp.write_text(env.model_dump_json(), encoding="utf-8")
        os.replace(tmp, target)
        return True
    except Exception as exc:
        logger.warning("local_deliver_to_agent: delivery to %s failed: %s", agent, exc)
        return False


def group_thread_messages(hist, group_id: str, limit: int = 500) -> list:
    """Return the group thread (the ``recipient=group:<id>`` copies), oldest-first.

    We read the thread by ``thread_id`` then keep only the canonical group-thread
    rows (``recipient == "group:<id>"``) so each message appears once (not once
    per fanned-out member copy).
    """
    rows = hist.get_thread(group_id, limit=limit * 4)
    marker = f"group:{group_id}"
    seen: set[str] = set()
    out: list = []
    for m in rows:
        if getattr(m, "recipient", "") != marker:
            continue
        mid = getattr(m, "id", None)
        if mid in seen:
            continue
        seen.add(mid)
        out.append(m)
    return out[:limit]


# --------------------------------------------------------------------------- #
# Promote 1:1 → group (same room id)
# --------------------------------------------------------------------------- #
def promote_one_to_one(
    hist,
    peer_id: str,
    new_member: str,
    operator_uri: str,
    *,
    name: Optional[str] = None,
):
    """Turn a 1:1 conversation with *peer_id* into a group of the SAME id.

    The new ``GroupChat`` keeps ``id == peer_id`` (no new object), seeds members
    = {operator (admin), the existing peer, the new member}, and migrates the
    existing 1:1 history onto the group thread by rewriting the matching rows'
    ``thread_id``/``recipient`` to the group form. Idempotent: if a group with
    this id already exists, the new member is simply added.

    Returns the persisted group.
    """
    from .group import GroupChat, MemberRole

    existing = load_group(peer_id)
    if existing is not None:
        add_member(existing, new_member)
        return existing

    group = GroupChat(
        id=peer_id,
        name=name or _derive_group_name(peer_id, new_member),
        created_by=operator_uri,
    )
    group.metadata["acl"] = dict(_DEFAULT_ACL)
    group.add_member(identity_uri=operator_uri, role=MemberRole.ADMIN,
                     participant_type=_participant_type_for(operator_uri))
    # The existing 1:1 peer.
    peer_uri = resolve_identity(peer_id)
    group.add_member(identity_uri=peer_uri,
                     participant_type=_participant_type_for(peer_uri))
    # The newly-added member.
    nm_uri = resolve_identity(new_member)
    if nm_uri and nm_uri != peer_uri:
        group.add_member(identity_uri=nm_uri,
                         participant_type=_participant_type_for(nm_uri))

    _migrate_history_to_group(hist, peer_id, group.id, peer_uri, operator_uri)
    save_group(group)
    logger.info("Promoted 1:1 %s → group %s (%d members)",
                peer_id, group.id[:8], group.member_count)
    return group


def _derive_group_name(peer_id: str, new_member: str) -> str:
    a = peer_id.split(":")[-1].split("@")[0].title()
    b = new_member.split(":")[-1].split("@")[0].title()
    return f"{a}, {b}"


def _migrate_history_to_group(hist, peer_id: str, group_id: str,
                              peer_uri: str, operator_uri: str) -> int:
    """Rewrite the existing 1:1 rows so they read as the group thread.

    Each message of the old 1:1 (between operator and the peer) is stamped with
    ``thread_id = group_id``; the canonical "group thread" copy is created by
    writing a ``recipient="group:<id>"`` row for each (preserving order). This
    keeps the prior conversation visible after promotion. Returns the count
    migrated.
    """
    from .models import ChatMessage

    # Load the existing 1:1 history (both directions).
    rows = []
    for p in {peer_id, peer_uri}:
        rows += hist.load(peer=p, limit=1000)
    seen: set[str] = set()
    pair = {peer_id, peer_uri, operator_uri}
    migrated = 0
    for m in sorted(rows, key=lambda x: x.timestamp):
        if m.id in seen:
            continue
        seen.add(m.id)
        s, r = getattr(m, "sender", ""), getattr(m, "recipient", "")
        # Only migrate the actual 1:1 turns (skip anything already group-marked).
        if r.startswith("group:"):
            continue
        if not (s in pair and r in pair):
            continue
        # Mark the original row as part of the group thread.
        if m.thread_id != group_id:
            m.thread_id = group_id
            try:
                hist.update_message(m)
            except Exception:
                pass
        # Add a canonical group-thread copy so it shows in group_thread_messages.
        try:
            hist.save(ChatMessage(
                id=f"{m.id}-grp",
                sender=s,
                recipient=f"group:{group_id}",
                content=m.content,
                thread_id=group_id,
                timestamp=m.timestamp,
                metadata={"group_id": group_id, "migrated_from": m.id},
            ))
            migrated += 1
        except Exception as exc:
            logger.warning("_migrate_history_to_group: copy of %s failed: %s", m.id, exc)
    return migrated
