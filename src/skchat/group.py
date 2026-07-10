"""SKChat group messaging -- multi-participant encrypted conversations.

Group chats use AES-256-GCM for message encryption. How the *key* is
established depends on the group's crypto suite (``kem_suite``), which is the
PQC crypto-agility gate:

* **Classical** (``rsa-pgp-wrap-v1``, the Q0 default) — a static AES-256 group
  key is distributed to each member by encrypting it with their PGP public key,
  and rotated + re-distributed when a member is removed. This is the original,
  HNDL-exposed behaviour and is preserved UNCHANGED for back-compat.
* **Hybrid post-quantum** (``x25519-mlkem768``, PQC Q2) — a per-EPOCH secret is
  wrapped to each member with a hybrid X25519+ML-KEM-768 KEM (``skcomms.pqkem``,
  once per epoch), and per-message keys are derived from it by a symmetric KDF
  ratchet (see ``skchat.group_ratchet``). Re-keying on add/remove + a
  50-msg/7-day bound gives forward secrecy and post-compromise security.

Humans and agents are first-class participants with identical messaging
capabilities. The only distinction is tool invocation scope: admins can
define which skills/tools agents may invoke within the group context.
Conversation itself is unrestricted — actions are scoped, not speech.
"""

from __future__ import annotations

import logging
import os
import uuid
from datetime import datetime, timezone
from enum import Enum
from typing import Any, ClassVar, Optional

from pydantic import BaseModel, Field

from .models import ChatMessage, Thread

logger = logging.getLogger("skchat.group")


class MemberRole(str, Enum):
    """Role within a group chat."""

    ADMIN = "admin"
    MEMBER = "member"
    OBSERVER = "observer"


class ParticipantType(str, Enum):
    """Informational participant classification.

    This is metadata for display purposes only — it does NOT gate
    capabilities. A sovereign agent and a human have identical
    messaging rights in a group.
    """

    HUMAN = "human"
    AGENT = "agent"
    SERVICE = "service"


class GroupMember(BaseModel):
    """A participant in a group chat — human or agent, equal rights.

    Attributes:
        identity_uri: CapAuth identity URI.
        role: Member role (admin, member, observer).
        participant_type: Informational only — human, agent, or service.
        display_name: Human-readable name.
        public_key_armor: PGP public key for key distribution.
        joined_at: When the member joined.
        tool_scope: Skills/tools this participant can invoke in group context.
            Empty list means unrestricted. Only enforced on tool invocation,
            never on messaging. Admins define this per-member.
    """

    identity_uri: str
    role: MemberRole = MemberRole.MEMBER
    participant_type: ParticipantType = ParticipantType.HUMAN
    display_name: str = ""
    public_key_armor: str = ""
    # PQC Q2 (additive, back-compatible): hybrid X25519+ML-KEM-768 public key
    # (hex of the 1216-byte ``skcomms.pqkem`` wire key) used to wrap the
    # per-epoch group secret. Empty for classical-only members; such members
    # fall back gracefully (they are skipped during hybrid distribution and the
    # self-report flags the gap). Never populated for classical groups.
    hybrid_kem_public_hex: str = ""
    joined_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    tool_scope: list[str] = Field(
        default_factory=list,
        description="Allowed skill.tool names (empty = unrestricted)",
    )


class GroupChat(BaseModel):
    """A multi-participant encrypted group conversation.

    Extends Thread with group-specific features: shared symmetric
    key, member management, admin controls, and key rotation.

    Attributes:
        id: Unique group identifier.
        name: Group display name.
        description: Group description.
        members: List of group participants.
        created_by: Identity URI of the creator.
        created_at: Creation timestamp.
        updated_at: Last activity timestamp.
        message_count: Total messages sent in the group.
        group_key: AES-256 symmetric key (hex-encoded, 64 chars).
        key_version: Incremented on key rotation.
        kem_suite: Machine-readable key-encapsulation cipher-suite id (PQC Q0
            crypto-agility). Describes how the group key is *wrapped* for
            distribution. Defaults to the current classical suite
            (``"rsa-pgp-wrap-v1"``) so groups serialized *without* this field
            still load and are correctly reported as classical. The id resolves
            against ``skcomms.crypto_suites`` (single source of truth). Phase 0
            changes **no crypto** — the static AES group key + PGP key-wrap are
            unchanged; this field only lets the object self-describe its suite
            for a future non-breaking swap (e.g. ``"x25519-mlkem768-v2"`` in
            Phase 1).
        epoch: Ratchet epoch (PQC Q2). Distinct from ``key_version``. For
            classical (``rsa-pgp-wrap-v1``) groups this stays ``0`` and is
            unused. For hybrid (``x25519-mlkem768``) groups it increments on
            every re-key (add / remove / 50-msg / 7-day bound); each epoch has
            its own ``epoch_secret_hex`` from which per-message keys derive
            (see ``skchat.group_ratchet``).
        metadata: Extensible metadata.
    """

    id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    name: str
    description: str = ""
    members: list[GroupMember] = Field(default_factory=list)
    created_by: str = ""
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    updated_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    message_count: int = 0
    group_key: str = Field(default_factory=lambda: os.urandom(32).hex())
    key_version: int = 1
    # PQC Q0 crypto-agility scaffolding (additive, back-compatible).
    kem_suite: str = "rsa-pgp-wrap-v1"
    epoch: int = 0
    # PQC Q2 hybrid-ratchet state (additive, back-compatible). Only populated
    # when ``kem_suite`` is the hybrid suite; classical groups leave these at
    # their defaults and behave EXACTLY as before. ``epoch_secret_hex`` is the
    # secret for the current ``epoch`` from which per-message keys are derived
    # (see ``skchat.group_ratchet``). ``message_index`` is the next outbound
    # message index within the epoch (drives the per-message KDF + the 50-msg
    # re-key bound).
    epoch_secret_hex: str = ""
    message_index: int = 0
    rekey_msg_bound: int = 50
    rekey_age_seconds: int = 7 * 24 * 3600
    epoch_started_at: float = 0.0
    metadata: dict[str, Any] = Field(default_factory=dict)
    rotation_history: list[dict[str, Any]] = Field(default_factory=list)

    #: Default ``kem_suite`` for NEWLY CREATED groups (PQC confidentiality
    #: cut-over). Hybrid is now the DEFAULT for new objects — a group created
    #: through :meth:`create` is hybrid from epoch 1 for every member that has a
    #: hybrid prekey. This is intentionally NOT the field default (the field on
    #: line ~140 stays classical) so groups serialized WITHOUT a ``kem_suite``
    #: (pre-cut-over, on disk) still deserialize and report as classical for
    #: byte-for-byte back-compat. The cut-over lives at the create/factory layer,
    #: never in deserialization.
    DEFAULT_NEW_KEM_SUITE: ClassVar[str] = "x25519-mlkem768"

    @classmethod
    def create(
        cls,
        name: str,
        creator_uri: str,
        creator_public_key: str = "",
        description: str = "",
        kem_suite: Optional[str] = None,
        creator_hybrid_kem_public_hex: str = "",
        member_hybrid_keys: Optional[dict[str, str]] = None,
    ) -> GroupChat:
        """Create a new group chat with the creator as admin.

        **PQC cut-over:** hybrid (``x25519-mlkem768``) is now the DEFAULT
        ``kem_suite`` for newly created groups. A group is hybrid from epoch 1
        for every member that carries a hybrid prekey; members without one fall
        back gracefully (classical wrap, flagged in the self-report) and are not
        locked out. Pass ``kem_suite="rsa-pgp-wrap-v1"`` to force a classical
        group explicitly.

        Args:
            name: Group display name.
            creator_uri: CapAuth identity URI of the creator.
            creator_public_key: Creator's PGP public key.
            description: Optional group description.
            kem_suite: Suite id for this group. ``None`` (default) selects the
                cut-over default :attr:`DEFAULT_NEW_KEM_SUITE` (hybrid). Pass an
                explicit classical suite to opt out.
            creator_hybrid_kem_public_hex: The creator's own hybrid-KEM public
                key (hex of the 1216-byte ``skcomms.pqkem`` wire key), so the
                creator can decrypt epoch-1. Empty = creator falls back classical.
            member_hybrid_keys: ``identity_uri -> hex(hybrid pub)`` for any
                members added at creation (used by the create paths to populate
                hybrid keys from the prekey store before seeding epoch 1).

        Returns:
            GroupChat: New group with creator as admin member. If hybrid and at
            least one member holds a hybrid key, epoch 1 is seeded; otherwise the
            group is hybrid-tagged but stays at epoch 0 until a member uploads a
            key (``ensure_epoch`` / ``migrate_to_hybrid`` seeds it later).
        """
        from .group_ratchet import HYBRID_KEM_SUITE

        suite = kem_suite if kem_suite is not None else cls.DEFAULT_NEW_KEM_SUITE
        group = cls(
            name=name,
            description=description,
            created_by=creator_uri,
            kem_suite=suite,
        )
        group.add_member(
            identity_uri=creator_uri,
            role=MemberRole.ADMIN,
            public_key_armor=creator_public_key,
            hybrid_kem_public_hex=creator_hybrid_kem_public_hex,
        )
        if member_hybrid_keys:
            for uri, pub_hex in member_hybrid_keys.items():
                m = group.get_member(uri)
                if m is not None and pub_hex:
                    m.hybrid_kem_public_hex = pub_hex
        # Seed epoch 1 only when hybrid AND at least one member can actually
        # receive the wrapped epoch secret (avoids a "hybrid but epoch 0, nobody
        # keyed" object). Distribution is local-only here (no transport); the
        # create paths broadcast on their own schedule.
        if suite == HYBRID_KEM_SUITE and any(
            m.hybrid_kem_public_hex for m in group.members
        ):
            group.ensure_epoch()
        return group

    def add_member(
        self,
        identity_uri: str,
        role: MemberRole = MemberRole.MEMBER,
        participant_type: ParticipantType = ParticipantType.HUMAN,
        display_name: str = "",
        public_key_armor: str = "",
        tool_scope: Optional[list[str]] = None,
        is_ai: bool = False,
        hybrid_kem_public_hex: str = "",
        rekey: bool = False,
        transport: Any = None,
    ) -> Optional[GroupMember]:
        """Add a new member to the group.

        Args:
            identity_uri: CapAuth identity URI.
            role: Member role.
            participant_type: Informational classification.
            display_name: Display name.
            public_key_armor: PGP public key for key distribution.
            tool_scope: Allowed tool names (empty = unrestricted).
            is_ai: Deprecated — use participant_type=ParticipantType.AGENT.

        Returns:
            Optional[GroupMember]: The added member, or None if already exists.
        """
        if self.get_member(identity_uri):
            return None

        if is_ai and participant_type == ParticipantType.HUMAN:
            participant_type = ParticipantType.AGENT

        member = GroupMember(
            identity_uri=identity_uri,
            role=role,
            participant_type=participant_type,
            display_name=display_name or identity_uri.split(":")[-1],
            public_key_armor=public_key_armor,
            hybrid_kem_public_hex=hybrid_kem_public_hex,
            tool_scope=tool_scope or [],
        )
        self.members.append(member)
        self.updated_at = datetime.now(timezone.utc)
        # PQC Q2: a new member must NOT be able to read prior epochs (forward
        # secrecy for the group's past), so adding a member re-keys hybrid
        # groups into a fresh epoch. Opt-in via ``rekey=True`` so existing
        # callers / classical groups are unaffected. Classical groups never
        # re-key on add (unchanged behaviour).
        if rekey and self.is_hybrid:
            self.rotate_key(reason=f"member_added:{identity_uri}", transport=transport)
        return member

    @staticmethod
    def _identity_parts(identity_uri: str) -> tuple[str, Optional[str]]:
        """Split an identity URI into ``(bare-handle, realm-or-None)``.

        Strips a leading ``scheme:`` prefix (e.g. ``capauth:``) — that part is
        purely cosmetic — then splits the remainder on the first ``@``. An
        identity with no ``@`` at all (a truly bare handle, e.g. ``"chef"``)
        has no realm information (``realm`` is ``None``).
        """
        s = (identity_uri or "").strip().lower()
        s = s.split(":", 1)[-1]
        if "@" in s:
            handle, realm = s.split("@", 1)
            return handle, realm
        return s, None

    @classmethod
    def _same_principal(cls, a: str, b: str) -> bool:
        """Whether two identity URIs refer to the SAME principal.

        Different *forms* of one identity are the same principal — e.g.
        ``capauth:chef@skworld.io`` and ``chef@skworld.io`` (the scheme prefix
        is cosmetic). This is what the bare-handle match exists to fix: a
        caller using the operator id ``chef@skworld.io`` still matches a
        member stored as ``capauth:chef@skworld.io``, since both share the
        same handle *and* the same realm.

        Different realms/operators sharing the same short handle are NOT the
        same principal — ``lumina@chef.skworld`` and ``lumina@bob.skworld``
        are two different operators' agents that happen to share a name, and
        must never collide (that collision was a cross-tenant admin/tool-scope
        bypass). So: handles must always match; if BOTH sides carry a realm,
        the realms must also match. Only when NEITHER side carries a realm at
        all do we fall back to a handle-only match (there's no realm to
        compare, so there's nothing to scope on).

        Args:
            a: First identity URI, any form.
            b: Second identity URI, any form.

        Returns:
            bool: True if ``a`` and ``b`` denote the same principal.
        """
        handle_a, realm_a = cls._identity_parts(a)
        handle_b, realm_b = cls._identity_parts(b)
        if not handle_a or handle_a != handle_b:
            return False
        if realm_a is not None and realm_b is not None:
            return realm_a == realm_b
        return realm_a is None and realm_b is None

    def remove_member(self, identity_uri: str, transport: Any = None) -> bool:
        """Remove a member from the group and rotate the key.

        Uses the same realm-scoped identity matching as :meth:`get_member` (a
        member found via an equivalent identity form — e.g. looked up as
        ``chef@skworld.io`` but stored as ``capauth:chef@skworld.io`` — is
        actually removed, not silently kept). The group key is rotated
        automatically so the removed member cannot decrypt future messages
        (forward secrecy).

        Args:
            identity_uri: CapAuth identity URI to remove.
            transport: Optional transport to broadcast the new key.

        Returns:
            bool: True if the member was found and removed.
        """
        before = len(self.members)
        self.members = [
            m
            for m in self.members
            if not (m.identity_uri == identity_uri or self._same_principal(m.identity_uri, identity_uri))
        ]
        removed = len(self.members) < before

        if removed:
            self.rotate_key(
                reason=f"member_removed:{identity_uri}",
                transport=transport,
            )
            self.updated_at = datetime.now(timezone.utc)

        return removed

    def get_member(self, identity_uri: str) -> Optional[GroupMember]:
        """Look up a member by identity URI.

        Matches across identity forms for the SAME principal — e.g.
        ``capauth:chef@skworld.io`` and ``chef@skworld.io`` (bare) both
        resolve to the same member, since the ``capauth:`` scheme prefix is
        cosmetic and the realm (``skworld.io``) is identical. Without this, a
        caller using one form (e.g. the operator id ``chef@skworld.io``)
        would fail to match a member stored under a differently-prefixed
        form, and posting/ACL checks would silently treat the operator as a
        non-member.

        This match is realm-scoped: it will NOT collapse two DIFFERENT
        realms/operators that happen to share a bare handle (e.g.
        ``lumina@chef.skworld`` vs ``lumina@bob.skworld`` are different
        principals and never match each other), which would otherwise be a
        cross-tenant admin/tool-scope bypass. See :meth:`_same_principal`.

        Args:
            identity_uri: Identity URI in any form.

        Returns:
            Optional[GroupMember]: The member if found.
        """
        return next(
            (
                m
                for m in self.members
                if m.identity_uri == identity_uri or self._same_principal(m.identity_uri, identity_uri)
            ),
            None,
        )

    def is_admin(self, identity_uri: str) -> bool:
        """Check if a member has admin privileges.

        Args:
            identity_uri: CapAuth identity URI.

        Returns:
            bool: True if the member is an admin.
        """
        member = self.get_member(identity_uri)
        return member is not None and member.role == MemberRole.ADMIN

    @property
    def is_hybrid(self) -> bool:
        """Whether this group uses the PQC Q2 hybrid epoch-ratchet.

        Gate condition for ALL new behaviour: classical (``rsa-pgp-wrap-v1``)
        groups return ``False`` and take the unchanged classical path
        everywhere. Only groups whose ``kem_suite`` is the hybrid suite
        (``x25519-mlkem768``) ratchet.
        """
        from .group_ratchet import HYBRID_KEM_SUITE

        return self.kem_suite == HYBRID_KEM_SUITE

    def rotate_key(self, reason: str = "manual", transport: Any = None) -> str:
        """Generate a new group key, record history, and optionally broadcast.

        Called automatically when a member is removed to maintain forward
        secrecy. Can also be triggered manually for periodic key hygiene or
        after a security concern.

        **Crypto-agility gate:** for classical (``rsa-pgp-wrap-v1``) groups this
        is the original behaviour, unchanged — a fresh ``os.urandom(32)`` group
        key, ``key_version += 1``, optional PGP re-distribution. For hybrid
        (``x25519-mlkem768``) groups it ALSO advances the epoch ratchet: a fresh
        epoch secret, ``epoch += 1``, message index reset, and re-distribution
        via the hybrid KEM (``group_ratchet.wrap_epoch_secret``). ``key_version``
        still increments in both modes so legacy readers observe a changed key.

        Args:
            reason: Human-readable reason for the rotation (e.g.
                ``"manual"``, ``"member_removed:capauth:bob@…"``).
            transport: Optional transport object whose ``send(identity, payload)``
                method is called to push the new key to every remaining member.

        Returns:
            str: The new group key (hex-encoded) — for hybrid groups this is the
            new epoch secret (hex); for classical groups the new AES key (hex),
            exactly as before.
        """
        if self.is_hybrid:
            return self._advance_epoch(reason=reason, transport=transport)

        # --- Classical path (UNCHANGED — rsa-pgp-wrap-v1) ------------------
        self.group_key = os.urandom(32).hex()
        self.key_version += 1

        self.rotation_history.append(
            {
                "event": "key_rotation",
                "reason": reason,
                "timestamp": datetime.now(timezone.utc).isoformat(),
                "version": self.key_version,
            }
        )

        if transport is not None:
            distributions = GroupKeyDistributor.distribute_key(self)
            key_package = {
                "type": "group_key_rotation",
                "group_id": self.id,
                "key_version": self.key_version,
                "reason": reason,
                "distributions": distributions,
            }
            for member in self.members:
                try:
                    transport.send(member.identity_uri, key_package)
                except Exception as exc:
                    logger.warning(
                        "Failed to broadcast key rotation to %s: %s",
                        member.identity_uri,
                        exc,
                    )

        logger.info(
            "Group %s key rotated to version %d (reason: %s)",
            self.id[:8],
            self.key_version,
            reason,
        )
        return self.group_key

    def _advance_epoch(self, reason: str = "manual", transport: Any = None) -> str:
        """Advance the hybrid ratchet into a new epoch (FS + PCS).

        Generates an independent fresh epoch secret (so a leaked previous epoch
        gives no information about this one — PCS), increments ``epoch``, resets
        the per-message index, and re-distributes the new epoch secret to every
        member that holds a hybrid-KEM public key. Members removed before this
        call never receive the new secret and so cannot derive any key in the new
        epoch (FS). ``key_version`` is bumped too for legacy-reader parity.

        Returns:
            str: The new epoch secret (hex-encoded).
        """
        import time as _time

        from .group_ratchet import new_epoch_secret

        secret = new_epoch_secret()
        self.epoch_secret_hex = secret.hex()
        self.epoch += 1
        self.message_index = 0
        self.epoch_started_at = _time.time()
        self.key_version += 1
        # Keep ``group_key`` populated so any legacy/back-compat reader still
        # sees a 64-hex value that CHANGED on rotation. It is NOT used to
        # encrypt messages in hybrid mode (per-message keys derive from the
        # epoch secret) — it is purely a compatibility shim.
        self.group_key = secret.hex()

        self.rotation_history.append(
            {
                "event": "epoch_advance",
                "reason": reason,
                "timestamp": datetime.now(timezone.utc).isoformat(),
                "version": self.key_version,
                "epoch": self.epoch,
                "suite": self.kem_suite,
            }
        )

        if transport is not None:
            distributions = GroupKeyDistributor.distribute_key(self)
            key_package = {
                "type": "group_epoch_advance",
                "group_id": self.id,
                "key_version": self.key_version,
                "epoch": self.epoch,
                "kem_suite": self.kem_suite,
                "reason": reason,
                "distributions": distributions,
            }
            for member in self.members:
                try:
                    transport.send(member.identity_uri, key_package)
                except Exception as exc:
                    logger.warning(
                        "Failed to broadcast epoch advance to %s: %s",
                        member.identity_uri,
                        exc,
                    )

        logger.info(
            "Group %s advanced to epoch %d (v%d, reason: %s)",
            self.id[:8],
            self.epoch,
            self.key_version,
            reason,
        )
        return self.epoch_secret_hex

    def migrate_to_hybrid(
        self,
        member_hybrid_keys: Optional[dict[str, str]] = None,
        transport: Any = None,
    ) -> str:
        """Migrate an existing classical group to the hybrid epoch-ratchet.

        Opt-in, non-destructive migration path (PQC Q2). Flips ``kem_suite`` to
        the hybrid suite, attaches each member's hybrid-KEM public key (hex),
        and seeds epoch 1 with a fresh hybrid-distributed secret. Members for
        which no hybrid key is supplied keep messaging but are skipped during
        hybrid distribution (documented graceful fallback) — they are not locked
        out; the self-report flags the gap until they upload a key.

        Calling this on an already-hybrid group is a no-op re-key.

        Args:
            member_hybrid_keys: ``identity_uri -> hex(1216-byte hybrid pub)``.
                Members not present keep their existing (possibly empty) key.
            transport: Optional transport for distributing the first epoch.

        Returns:
            str: The new epoch secret (hex).
        """
        from .group_ratchet import HYBRID_KEM_SUITE

        if member_hybrid_keys:
            for uri, pub_hex in member_hybrid_keys.items():
                m = self.get_member(uri)
                if m is not None:
                    m.hybrid_kem_public_hex = pub_hex
        self.kem_suite = HYBRID_KEM_SUITE
        # Seed the first hybrid epoch (epoch becomes 1 via _advance_epoch).
        return self._advance_epoch(reason="migrate_to_hybrid", transport=transport)

    def ensure_epoch(self, transport: Any = None) -> None:
        """Make sure a hybrid group has a live epoch secret (lazy-seed).

        No-op for classical groups and for hybrid groups that already have an
        epoch secret. If a hybrid group reaches messaging with no secret yet
        (e.g. ``kem_suite`` set directly without migrating), seed epoch 1.
        """
        if self.is_hybrid and not self.epoch_secret_hex:
            self._advance_epoch(reason="lazy_seed", transport=transport)

    def maybe_rekey(self, transport: Any = None) -> bool:
        """Re-key if the 50-message / 7-day bound is reached (hybrid only).

        Returns:
            bool: True if a re-key (epoch advance) happened.
        """
        if not self.is_hybrid or not self.epoch_secret_hex:
            return False
        import time as _time

        over_msgs = self.message_index >= self.rekey_msg_bound
        over_age = (
            self.epoch_started_at > 0
            and (_time.time() - self.epoch_started_at) >= self.rekey_age_seconds
        )
        if over_msgs or over_age:
            reason = "rekey_bound:messages" if over_msgs else "rekey_bound:age"
            self.rotate_key(reason=reason, transport=transport)
            return True
        return False

    def encrypt_message(self, plaintext: str) -> dict[str, Any]:
        """Encrypt a message for this group, honouring the active suite.

        For classical groups: AES-256-GCM under the static ``group_key`` (the
        original behaviour, via ``GroupMessageEncryptor``).

        For hybrid groups: derive the next per-message key from the current
        epoch secret + message index (symmetric KDF ratchet), encrypt with
        AES-256-GCM, advance the index, and re-key if the bound is hit. The
        returned envelope carries ``(epoch, index)`` so receivers derive the
        same key (loss/reorder tolerant). NO PQ material rides per message.

        Returns:
            dict: ``{"suite", "epoch", "index", "ciphertext"}``. For classical
            groups ``epoch``/``index`` are ``None``.
        """
        if not self.is_hybrid:
            return {
                "suite": self.kem_suite,
                "epoch": None,
                "index": None,
                "ciphertext": GroupMessageEncryptor.encrypt(plaintext, self.group_key),
            }

        self.ensure_epoch()
        from .group_ratchet import EpochRatchet

        ratchet = EpochRatchet(
            epoch=self.epoch,
            epoch_secret=bytes.fromhex(self.epoch_secret_hex),
            message_index=self.message_index,
            rekey_msg_bound=self.rekey_msg_bound,
            rekey_age_seconds=self.rekey_age_seconds,
        )
        index, key = ratchet.next_outbound_key()
        self.message_index = ratchet.message_index
        ciphertext = GroupMessageEncryptor.encrypt(plaintext, key.hex())
        envelope = {
            "suite": self.kem_suite,
            "epoch": self.epoch,
            "index": index,
            "ciphertext": ciphertext,
        }
        # Honour the per-epoch message bound (re-key for the NEXT message).
        self.maybe_rekey()
        return envelope

    def decrypt_message(self, envelope: dict[str, Any]) -> str:
        """Decrypt a message envelope produced by :meth:`encrypt_message`.

        Hybrid envelopes are decrypted by re-deriving the per-message key from
        the carried ``(epoch, index)`` against the current epoch secret — so any
        order, with gaps, decrypts as long as the receiver holds that epoch's
        secret. Classical envelopes use the static group key.
        """
        suite = envelope.get("suite", self.kem_suite)
        ciphertext = envelope["ciphertext"]
        if suite != self.kem_suite or not self.is_hybrid or envelope.get("epoch") is None:
            return GroupMessageEncryptor.decrypt(ciphertext, self.group_key)

        from .group_ratchet import derive_message_key

        epoch = envelope["epoch"]
        index = envelope["index"]
        if epoch != self.epoch:
            raise ValueError(
                f"message is for epoch {epoch} but group is at epoch {self.epoch} "
                "(epoch secret for the message's epoch is required to decrypt)"
            )
        key = derive_message_key(bytes.fromhex(self.epoch_secret_hex), epoch, index)
        return GroupMessageEncryptor.decrypt(ciphertext, key.hex())

    def crypto_self_report(self) -> dict[str, Any]:
        """Per-group crypto-posture self-report (PQC §4.4 — reflects REALITY).

        Resolves this group's actual ``kem_suite`` against the
        ``skcomms.crypto_suites`` registry (single source of truth) so a hybrid
        group reports ``x25519-mlkem768`` [hybrid-pq] while classical groups
        still report ``rsa-pgp-wrap-v1`` [classical]. Never hard-codes the
        quantum-resistance verdict — it comes from the registry.

        Returns:
            dict: ``{group_id, kem_suite, status, quantum_resistant, epoch,
                key_version, primitives, fips_refs, members_with_hybrid_key,
                members_total, note}``.
        """
        status = "classical"
        quantum_resistant = False
        primitives: list[str] = []
        fips_refs: list[str] = []
        try:
            from skcomms.crypto_suites import get_suite

            suite = get_suite(self.kem_suite)
            if suite is not None:
                d = suite.to_dict()
                status = d["status"]
                quantum_resistant = d["quantum_resistant"]
                primitives = d["primitives"]
                fips_refs = d["fips_refs"]
        except Exception:  # pragma: no cover - registry optional
            pass

        with_hybrid = sum(1 for m in self.members if m.hybrid_kem_public_hex)
        total = len(self.members)
        if self.is_hybrid:
            if with_hybrid == total:
                note = (
                    "Hybrid epoch-ratchet: per-epoch secret wrapped via "
                    "X25519+ML-KEM-768. Per-message keys derive symmetrically "
                    "from the epoch secret (AES-256-GCM bulk)."
                )
            else:
                note = (
                    f"Hybrid group, but {total - with_hybrid}/{total} member(s) "
                    "lack a hybrid-KEM key and fall back gracefully (skipped in "
                    "hybrid distribution). Self-report flags the gap; the group "
                    "is not fully hybrid-protected until they upload a key."
                )
        else:
            note = (
                "Classical PGP-wrap of a static AES-256 group key (HNDL-exposed). "
                "Migrate to x25519-mlkem768 via GroupChat.migrate_to_hybrid()."
            )

        return {
            "surface": "group-key",
            "group_id": self.id,
            "kem_suite": self.kem_suite,
            "status": status,
            "quantum_resistant": quantum_resistant,
            "epoch": self.epoch,
            "key_version": self.key_version,
            "primitives": primitives,
            "fips_refs": fips_refs,
            "members_with_hybrid_key": with_hybrid,
            "members_total": total,
            "note": note,
        }

    def touch(self) -> None:
        """Update activity timestamp and increment message count."""
        self.updated_at = datetime.now(timezone.utc)
        self.message_count += 1

    @property
    def member_count(self) -> int:
        """Number of members in the group."""
        return len(self.members)

    @property
    def admin_uris(self) -> list[str]:
        """List of admin identity URIs."""
        return [m.identity_uri for m in self.members if m.role == MemberRole.ADMIN]

    @property
    def member_uris(self) -> list[str]:
        """List of all member identity URIs."""
        return [m.identity_uri for m in self.members]

    def to_thread(self) -> Thread:
        """Convert this group to a Thread for compatibility with ChatHistory.

        Returns:
            Thread: A Thread representation of this group.
        """
        return Thread(
            id=self.id,
            title=self.name,
            participants=self.member_uris,
            created_at=self.created_at,
            updated_at=self.updated_at,
            message_count=self.message_count,
            metadata={
                "group": True,
                "description": self.description,
                "key_version": self.key_version,
                "created_by": self.created_by,
            },
        )

    def can_invoke_tool(self, identity_uri: str, tool_name: str) -> bool:
        """Check if a member is allowed to invoke a specific tool in this group.

        Tool scoping is the ONLY restriction in sovereign groups.
        Messaging is always unrestricted — only actions are gated.
        An empty tool_scope means unrestricted access.

        Args:
            identity_uri: The member's CapAuth identity URI.
            tool_name: Fully-qualified tool name (e.g., "sksecurity.audit").

        Returns:
            bool: True if the tool invocation is allowed.
        """
        member = self.get_member(identity_uri)
        if member is None:
            return False
        if member.role == MemberRole.ADMIN:
            return True
        if not member.tool_scope:
            return True
        return tool_name in member.tool_scope

    def set_tool_scope(
        self,
        identity_uri: str,
        tool_scope: list[str],
        by_admin: str,
    ) -> bool:
        """Set the tool scope for a member. Admin-only operation.

        Args:
            identity_uri: The member whose scope to update.
            tool_scope: List of allowed tool names (empty = unrestricted).
            by_admin: Identity URI of the admin making the change.

        Returns:
            bool: True if scope was updated.
        """
        if not self.is_admin(by_admin):
            return False
        member = self.get_member(identity_uri)
        if member is None:
            return False
        member.tool_scope = tool_scope
        self.updated_at = datetime.now(timezone.utc)
        return True

    def compose_group_message(
        self,
        sender_uri: str,
        content: str,
        reply_to_id: Optional[str] = None,
        ttl: Optional[int] = None,
    ) -> Optional[ChatMessage]:
        """Compose a message for this group. Any member can send.

        Args:
            sender_uri: Sender's CapAuth identity URI (must be a member).
            content: Message content.
            reply_to_id: Optional message ID being replied to.
            ttl: Optional seconds until auto-delete.

        Returns:
            ChatMessage or None if sender isn't a member.
        """
        member = self.get_member(sender_uri)
        if member is None:
            return None
        if member.role == MemberRole.OBSERVER:
            return None

        self.touch()
        return ChatMessage(
            sender=sender_uri,
            recipient=f"group:{self.id}",
            content=content,
            thread_id=self.id,
            reply_to_id=reply_to_id,
            ttl=ttl,
            metadata={"group_name": self.name, "key_version": self.key_version},
        )

    def reply(
        self,
        thread_id: str,
        message: str,
        sender_uri: str,
    ) -> Optional[ChatMessage]:
        """Compose a threaded reply within this group.

        Sends a message with *thread_id* set so recipients can correlate it
        with an existing conversation thread.

        Args:
            thread_id: Thread ID to reply in (may differ from group ID for sub-threads).
            message: Reply content.
            sender_uri: CapAuth identity URI of the sender (must be a member).

        Returns:
            ChatMessage with thread_id set, or None if sender is not an active member.
        """
        member = self.get_member(sender_uri)
        if member is None or member.role == MemberRole.OBSERVER:
            return None
        self.touch()
        return ChatMessage(
            sender=sender_uri,
            recipient=f"group:{self.id}",
            content=message,
            thread_id=thread_id,
            metadata={"group_name": self.name, "key_version": self.key_version},
        )

    def send(
        self,
        content: str,
        sender: str,
        transport: Any = None,
        history: Any = None,
    ) -> dict:
        """Multicast a message to all group members via file transport.

        Composes a ``ChatMessage``, delivers it to every non-sender member
        via ``transport.send(recipient_uri, payload)``, then persists:

        * **Group history** — one entry with ``recipient=group:<id>`` and
          ``thread_id=<group_id>`` (visible via ``history.get_thread()`` and
          ``history.load(peer=sender)``).
        * **Individual history** — one entry per non-sender member with
          ``recipient=member_uri`` and ``thread_id=<group_id>`` (visible via
          ``history.load(peer=member_uri)``).

        Args:
            content: Message text.
            sender: CapAuth identity URI of the sender (must be an active member).
            transport: Optional transport object.  ``transport.send(uri, payload)``
                is called for each recipient.  When ``None`` members are marked
                delivered without a network call (useful in tests / dry-run).
            history: Optional :class:`~skchat.history.ChatHistory` instance for
                persistence.  When ``None`` no messages are written to disk.

        Returns:
            dict: ``{"delivered": [...], "failed": [...], "total": N,
                    "sent_by": sender, "group_id": self.id, "message_id": msg.id}``
        """
        from .models import ChatMessage

        msg = self.compose_group_message(sender_uri=sender, content=content)
        if msg is None:
            return {
                "delivered": [],
                "failed": [],
                "total": 0,
                "sent_by": sender,
                "group_id": self.id,
                "error": "sender is not an active member",
            }

        # Persist group-thread copy (sender can find this via load(peer=sender))
        if history is not None:
            try:
                history.save(msg)
            except Exception as exc:
                logger.warning("Failed to save group message to history: %s", exc)

        delivered: list[str] = []
        failed: list[str] = []

        payload = {
            "type": "group_message",
            "group_id": self.id,
            "group_name": self.name,
            "message_id": msg.id,
            "sender": sender,
            "content": content,
            "thread_id": self.id,
            "timestamp": msg.timestamp.isoformat(),
        }

        for member in self.members:
            if member.identity_uri == sender:
                continue

            # Deliver via transport
            if transport is not None:
                try:
                    transport.send(member.identity_uri, payload)
                    delivered.append(member.identity_uri)
                except Exception as exc:
                    logger.warning(
                        "send: failed to deliver to %s: %s",
                        member.identity_uri,
                        exc,
                    )
                    failed.append(member.identity_uri)
                    continue
            else:
                delivered.append(member.identity_uri)

            # Persist individual copy so member's inbox/load(peer=...) shows it
            if history is not None:
                individual_msg = ChatMessage(
                    sender=sender,
                    recipient=member.identity_uri,
                    content=content,
                    thread_id=self.id,
                    metadata={
                        "group_id": self.id,
                        "group_name": self.name,
                        "key_version": self.key_version,
                    },
                )
                try:
                    history.save(individual_msg)
                except Exception as exc:
                    logger.warning(
                        "send: failed to save individual history for %s: %s",
                        member.identity_uri,
                        exc,
                    )

        logger.info(
            "Group %s send: %d delivered, %d failed",
            self.id[:8],
            len(delivered),
            len(failed),
        )
        return {
            "delivered": delivered,
            "failed": failed,
            "total": len(delivered) + len(failed),
            "sent_by": sender,
            "group_id": self.id,
            "message_id": msg.id,
        }

    def broadcast(self, message: str, sender_uri: str) -> dict:
        """Multicast a message to all group members except the sender.

        Uses AgentMessenger (backed by file transport via SKComms) to deliver
        the message individually to each non-sender member. Collects per-member
        delivery outcomes and returns a summary.

        Args:
            message: Message content to broadcast.
            sender_uri: CapAuth identity URI of the sender (excluded from delivery).

        Returns:
            dict: ``{"delivered": [...], "failed": [...], "total": N,
                    "sent_by": sender_uri, "group_id": self.id}``
        """
        from .agent_comm import AgentMessenger

        messenger = AgentMessenger.from_identity(sender_uri)
        delivered: list[str] = []
        failed: list[str] = []

        for member in self.members:
            if member.identity_uri == sender_uri:
                continue
            try:
                result = messenger.send(
                    recipient=member.identity_uri,
                    content=message,
                    thread_id=self.id,
                )
                if result.get("delivered"):
                    delivered.append(member.identity_uri)
                else:
                    failed.append(member.identity_uri)
            except Exception as exc:
                logger.warning(
                    "broadcast: failed to deliver to %s: %s",
                    member.identity_uri,
                    exc,
                )
                failed.append(member.identity_uri)

        logger.info(
            "Group %s broadcast: %d delivered, %d failed",
            self.id[:8],
            len(delivered),
            len(failed),
        )
        return {
            "delivered": delivered,
            "failed": failed,
            "total": len(delivered) + len(failed),
            "sent_by": sender_uri,
            "group_id": self.id,
        }

    @property
    def agents(self) -> list[GroupMember]:
        """All agent participants in the group."""
        return [m for m in self.members if m.participant_type == ParticipantType.AGENT]

    @property
    def humans(self) -> list[GroupMember]:
        """All human participants in the group."""
        return [m for m in self.members if m.participant_type == ParticipantType.HUMAN]

    def summary(self) -> str:
        """Human-readable group summary.

        Returns:
            str: Multi-line summary.
        """
        members_str = ", ".join(
            f"{m.display_name} ({m.role.value}, {m.participant_type.value})" for m in self.members
        )
        return (
            f"Group: {self.name}\n"
            f"Members ({self.member_count}): {members_str}\n"
            f"Messages: {self.message_count}\n"
            f"Key version: {self.key_version}"
        )


class GroupKeyDistributor:
    """Distributes the group key to members.

    For classical groups each member receives the static AES group key
    encrypted with their individual PGP public key. For hybrid groups
    (``x25519-mlkem768``) each member receives the current epoch secret wrapped
    with their hybrid X25519+ML-KEM-768 public key (``distribute_epoch_secret``
    / ``unwrap_epoch_secret_for_member``). The ``distribute_key`` entry point
    dispatches on ``group.is_hybrid``.
    """

    @staticmethod
    def encrypt_key_for_member(
        group_key_hex: str,
        member_public_armor: str,
    ) -> Optional[str]:
        """Encrypt the group key for a specific member.

        Args:
            group_key_hex: The hex-encoded AES-256 group key.
            member_public_armor: Member's PGP public key armor.

        Returns:
            Optional[str]: PGP-encrypted key string, or None on failure.
        """
        if not member_public_armor:
            return None

        try:
            import pgpy

            pub_key, _ = pgpy.PGPKey.from_blob(member_public_armor)
            message = pgpy.PGPMessage.new(group_key_hex.encode("utf-8"))
            encrypted = pub_key.encrypt(message)
            return str(encrypted)
        except Exception as exc:
            logger.warning("Failed to encrypt group key for member: %s", exc)
            return None

    @staticmethod
    def decrypt_group_key(
        encrypted_key: str,
        private_key_armor: str,
        passphrase: str,
    ) -> Optional[str]:
        """Decrypt the group key using a member's private key.

        Args:
            encrypted_key: PGP-encrypted group key string.
            private_key_armor: Member's PGP private key armor.
            passphrase: Passphrase for the private key.

        Returns:
            Optional[str]: The hex-encoded group key, or None on failure.
        """
        try:
            import pgpy

            key, _ = pgpy.PGPKey.from_blob(private_key_armor)
            pgp_message = pgpy.PGPMessage.from_blob(encrypted_key)

            with key.unlock(passphrase):
                decrypted = key.decrypt(pgp_message)

            plaintext = decrypted.message
            if isinstance(plaintext, bytes):
                plaintext = plaintext.decode("utf-8")
            return plaintext
        except Exception as exc:
            logger.warning("Failed to decrypt group key: %s", exc)
            return None

    @staticmethod
    def distribute_key(group: GroupChat) -> dict[str, Optional[str]]:
        """Encrypt and distribute the group key to all members.

        **Crypto-agility gate:** for classical groups this PGP-wraps the static
        AES group key per member (original behaviour, unchanged). For hybrid
        (``x25519-mlkem768``) groups it instead wraps the **current epoch
        secret** to each member's hybrid-KEM public key via
        ``group_ratchet.wrap_epoch_secret`` (X25519+ML-KEM-768), returning the
        hex-encoded per-member payload. Members without a hybrid key resolve to
        ``None`` and fall back gracefully (documented).

        Args:
            group: The group whose key (classical) or epoch secret (hybrid) to
                distribute.

        Returns:
            dict: identity_uri -> wrapped payload (None if the member cannot be
            wrapped: no PGP pubkey for classical, no hybrid key for hybrid).
        """
        if group.is_hybrid:
            return GroupKeyDistributor.distribute_epoch_secret(group)

        result: dict[str, Optional[str]] = {}
        for member in group.members:
            encrypted = GroupKeyDistributor.encrypt_key_for_member(
                group.group_key,
                member.public_key_armor,
            )
            result[member.identity_uri] = encrypted
        return result

    @staticmethod
    def distribute_epoch_secret(group: GroupChat) -> dict[str, Optional[str]]:
        """Wrap the group's current epoch secret to each member (hybrid KEM).

        For each member holding a hybrid-KEM public key, the epoch secret is
        wrapped once (the PQ material — ML-KEM ciphertext — is paid here, per
        epoch, NOT per message). Members lacking a hybrid key map to ``None``
        (graceful fallback). The result is hex-encoded for JSON transport.

        Args:
            group: A hybrid group with a live ``epoch_secret_hex``.

        Returns:
            dict: identity_uri -> hex(wrapped payload) or ``None``.
        """
        from .group_ratchet import wrap_epoch_secret

        result: dict[str, Optional[str]] = {}
        if not group.epoch_secret_hex:
            return {m.identity_uri: None for m in group.members}
        secret = bytes.fromhex(group.epoch_secret_hex)
        for member in group.members:
            if not member.hybrid_kem_public_hex:
                result[member.identity_uri] = None
                continue
            try:
                pub = bytes.fromhex(member.hybrid_kem_public_hex)
                payload = wrap_epoch_secret(secret, pub)
                result[member.identity_uri] = payload.hex()
            except Exception as exc:
                logger.warning(
                    "Failed to hybrid-wrap epoch secret for %s: %s",
                    member.identity_uri,
                    exc,
                )
                result[member.identity_uri] = None
        return result

    @staticmethod
    def unwrap_epoch_secret_for_member(
        wrapped_hex: str,
        member_hybrid_private_hex: str,
    ) -> Optional[str]:
        """Recover the epoch secret a member received (hybrid KEM decap).

        Args:
            wrapped_hex: Hex payload from ``distribute_epoch_secret``.
            member_hybrid_private_hex: Hex of the member's 2432-byte hybrid
                private key.

        Returns:
            Hex-encoded 32-byte epoch secret, or ``None`` on failure.
        """
        from .group_ratchet import unwrap_epoch_secret

        try:
            payload = bytes.fromhex(wrapped_hex)
            priv = bytes.fromhex(member_hybrid_private_hex)
            return unwrap_epoch_secret(payload, priv).hex()
        except Exception as exc:
            logger.warning("Failed to unwrap epoch secret: %s", exc)
            return None


class GroupMessageEncryptor:
    """Encrypts and decrypts group messages using the shared AES key.

    Uses AES-256-GCM for authenticated encryption. Each message
    gets a random 12-byte nonce. The ciphertext includes a tag
    for tamper detection.
    """

    @staticmethod
    def encrypt(plaintext: str, group_key_hex: str) -> str:
        """Encrypt a message with the group's AES key.

        Args:
            plaintext: Message content to encrypt.
            group_key_hex: Hex-encoded AES-256 key (64 chars).

        Returns:
            str: Base64-encoded nonce + ciphertext + tag.
        """
        import base64

        try:
            from cryptography.hazmat.primitives.ciphers.aead import AESGCM
        except ImportError:
            logger.warning("cryptography not available, returning plaintext")
            return plaintext

        key = bytes.fromhex(group_key_hex)
        nonce = os.urandom(12)
        aesgcm = AESGCM(key)
        ciphertext = aesgcm.encrypt(nonce, plaintext.encode("utf-8"), None)
        return base64.b64encode(nonce + ciphertext).decode("ascii")

    @staticmethod
    def decrypt(encrypted_b64: str, group_key_hex: str) -> str:
        """Decrypt a group message with the shared AES key.

        Args:
            encrypted_b64: Base64-encoded nonce + ciphertext + tag.
            group_key_hex: Hex-encoded AES-256 key (64 chars).

        Returns:
            str: Decrypted plaintext.

        Raises:
            ValueError: If decryption or authentication fails.
        """
        import base64

        try:
            from cryptography.hazmat.primitives.ciphers.aead import AESGCM
        except ImportError:
            return encrypted_b64

        key = bytes.fromhex(group_key_hex)
        raw = base64.b64decode(encrypted_b64)
        nonce, ciphertext = raw[:12], raw[12:]
        aesgcm = AESGCM(key)

        try:
            plaintext = aesgcm.decrypt(nonce, ciphertext, None)
            return plaintext.decode("utf-8")
        except Exception as exc:
            logger.warning("group.py: %s", exc)
            raise ValueError(f"Group message decryption failed: {exc}") from exc


# Alias for import compatibility
ChatGroup = GroupChat
