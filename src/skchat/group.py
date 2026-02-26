"""SKChat group messaging -- multi-participant encrypted conversations.

Group chats use a shared AES-256 symmetric key for message encryption.
The group key is distributed to each member by encrypting it with their
PGP public key. When a member is removed, the group key is rotated
and re-distributed to remaining members.

Humans and agents are first-class participants with identical messaging
capabilities. The only distinction is tool invocation scope: admins can
define which skills/tools agents may invoke within the group context.
Conversation itself is unrestricted — actions are scoped, not speech.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import uuid
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Optional

from pydantic import BaseModel, Field

from .models import ChatMessage, ContentType, Thread

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
    metadata: dict[str, Any] = Field(default_factory=dict)

    @classmethod
    def create(
        cls,
        name: str,
        creator_uri: str,
        creator_public_key: str = "",
        description: str = "",
    ) -> GroupChat:
        """Create a new group chat with the creator as admin.

        Args:
            name: Group display name.
            creator_uri: CapAuth identity URI of the creator.
            creator_public_key: Creator's PGP public key.
            description: Optional group description.

        Returns:
            GroupChat: New group with creator as admin member.
        """
        group = cls(
            name=name,
            description=description,
            created_by=creator_uri,
        )
        group.add_member(
            identity_uri=creator_uri,
            role=MemberRole.ADMIN,
            public_key_armor=creator_public_key,
        )
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
            tool_scope=tool_scope or [],
        )
        self.members.append(member)
        self.updated_at = datetime.now(timezone.utc)
        return member

    def remove_member(self, identity_uri: str) -> bool:
        """Remove a member from the group and rotate the key.

        Args:
            identity_uri: CapAuth identity URI to remove.

        Returns:
            bool: True if the member was found and removed.
        """
        before = len(self.members)
        self.members = [m for m in self.members if m.identity_uri != identity_uri]
        removed = len(self.members) < before

        if removed:
            self.rotate_key()
            self.updated_at = datetime.now(timezone.utc)

        return removed

    def get_member(self, identity_uri: str) -> Optional[GroupMember]:
        """Look up a member by identity URI.

        Args:
            identity_uri: CapAuth identity URI.

        Returns:
            Optional[GroupMember]: The member if found.
        """
        return next(
            (m for m in self.members if m.identity_uri == identity_uri),
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

    def rotate_key(self) -> str:
        """Generate a new group key and increment the version.

        Called automatically when a member is removed to maintain
        forward secrecy. The new key must be re-distributed to
        all remaining members.

        Returns:
            str: The new group key (hex-encoded).
        """
        self.group_key = os.urandom(32).hex()
        self.key_version += 1
        logger.info("Group %s key rotated to version %d", self.id[:8], self.key_version)
        return self.group_key

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
        reply_to: Optional[str] = None,
        ttl: Optional[int] = None,
    ) -> Optional[ChatMessage]:
        """Compose a message for this group. Any member can send.

        Args:
            sender_uri: Sender's CapAuth identity URI (must be a member).
            content: Message content.
            reply_to: Optional message ID being replied to.
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
            reply_to=reply_to,
            ttl=ttl,
            metadata={"group_name": self.name, "key_version": self.key_version},
        )

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
            f"{m.display_name} ({m.role.value}, {m.participant_type.value})"
            for m in self.members
        )
        return (
            f"Group: {self.name}\n"
            f"Members ({self.member_count}): {members_str}\n"
            f"Messages: {self.message_count}\n"
            f"Key version: {self.key_version}"
        )


class GroupKeyDistributor:
    """Distributes the group symmetric key to members via PGP.

    Each member receives the group key encrypted with their
    individual PGP public key. Only they can decrypt it.
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

        Args:
            group: The group whose key to distribute.

        Returns:
            dict: identity_uri -> encrypted_key_str (None if member has no pubkey).
        """
        result: dict[str, Optional[str]] = {}
        for member in group.members:
            encrypted = GroupKeyDistributor.encrypt_key_for_member(
                group.group_key, member.public_key_armor,
            )
            result[member.identity_uri] = encrypted
        return result


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
            raise ValueError(f"Group message decryption failed: {exc}") from exc
