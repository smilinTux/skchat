"""Tests for SKChat group messaging."""

from __future__ import annotations

import pgpy
import pytest
from pgpy.constants import (
    HashAlgorithm,
    KeyFlags,
    PubKeyAlgorithm,
    SymmetricKeyAlgorithm,
)

from skchat.group import (
    GroupChat,
    GroupKeyDistributor,
    GroupMember,
    GroupMessageEncryptor,
    MemberRole,
    ParticipantType,
)

PASSPHRASE = "group-test-2026"


def _keygen(name: str) -> tuple[str, str]:
    """Generate a test PGP keypair."""
    key = pgpy.PGPKey.new(PubKeyAlgorithm.RSAEncryptOrSign, 2048)
    uid = pgpy.PGPUID.new(name, email=f"{name.lower()}@test.io")
    key.add_uid(uid, usage={KeyFlags.Sign, KeyFlags.Certify},
                hashes=[HashAlgorithm.SHA256], ciphers=[SymmetricKeyAlgorithm.AES256])
    sub = pgpy.PGPKey.new(PubKeyAlgorithm.RSAEncryptOrSign, 2048)
    key.add_subkey(sub, usage={KeyFlags.EncryptCommunications, KeyFlags.EncryptStorage})
    key.protect(PASSPHRASE, SymmetricKeyAlgorithm.AES256, HashAlgorithm.SHA256)
    return str(key), str(key.pubkey)


@pytest.fixture(scope="session")
def alice_keys() -> tuple[str, str]:
    """Alice's keypair."""
    return _keygen("Alice")


@pytest.fixture(scope="session")
def bob_keys() -> tuple[str, str]:
    """Bob's keypair."""
    return _keygen("Bob")


@pytest.fixture()
def group(alice_keys: tuple[str, str]) -> GroupChat:
    """A basic group with Alice as admin."""
    _, alice_pub = alice_keys
    return GroupChat.create(
        name="Dev Team",
        creator_uri="capauth:alice@skworld.io",
        creator_public_key=alice_pub,
        description="Main dev channel",
    )


class TestGroupChatCreation:
    """Tests for group creation and management."""

    def test_create_group(self, group: GroupChat) -> None:
        """Happy path: group created with admin member."""
        assert group.name == "Dev Team"
        assert group.member_count == 1
        assert group.is_admin("capauth:alice@skworld.io")
        assert len(group.group_key) == 64

    def test_group_has_uuid(self, group: GroupChat) -> None:
        """Group gets a UUID v4 identifier."""
        assert len(group.id) == 36

    def test_add_member(self, group: GroupChat, bob_keys: tuple[str, str]) -> None:
        """New members can be added."""
        _, bob_pub = bob_keys
        member = group.add_member(
            identity_uri="capauth:bob@skworld.io",
            public_key_armor=bob_pub,
            display_name="Bob",
        )
        assert member is not None
        assert group.member_count == 2

    def test_add_duplicate_returns_none(self, group: GroupChat) -> None:
        """Adding an existing member returns None."""
        result = group.add_member(identity_uri="capauth:alice@skworld.io")
        assert result is None

    def test_remove_member_rotates_key(self, group: GroupChat) -> None:
        """Removing a member triggers key rotation."""
        group.add_member(identity_uri="capauth:bob@test")
        old_key = group.group_key
        old_version = group.key_version

        group.remove_member("capauth:bob@test")
        assert group.group_key != old_key
        assert group.key_version == old_version + 1

    def test_remove_nonexistent_returns_false(self, group: GroupChat) -> None:
        """Removing unknown member returns False."""
        assert group.remove_member("capauth:nobody@test") is False

    def test_get_member(self, group: GroupChat) -> None:
        """Members can be looked up by URI."""
        member = group.get_member("capauth:alice@skworld.io")
        assert member is not None
        assert member.role == MemberRole.ADMIN

    def test_touch_increments(self, group: GroupChat) -> None:
        """touch() updates timestamp and message count."""
        count = group.message_count
        group.touch()
        assert group.message_count == count + 1

    def test_to_thread(self, group: GroupChat) -> None:
        """Group converts to a Thread for ChatHistory compatibility."""
        thread = group.to_thread()
        assert thread.id == group.id
        assert thread.title == "Dev Team"
        assert "capauth:alice@skworld.io" in thread.participants
        assert thread.metadata.get("group") is True

    def test_summary(self, group: GroupChat) -> None:
        """Summary is human-readable."""
        summary = group.summary()
        assert "Dev Team" in summary
        assert "admin" in summary

    def test_add_agent_member(self, group: GroupChat) -> None:
        """Agents join as first-class participants."""
        member = group.add_member(
            identity_uri="capauth:lumina@skworld.io",
            participant_type=ParticipantType.AGENT,
            role=MemberRole.MEMBER,
        )
        assert member is not None
        assert member.participant_type == ParticipantType.AGENT

    def test_legacy_is_ai_sets_agent_type(self, group: GroupChat) -> None:
        """Deprecated is_ai=True should set participant_type to AGENT."""
        member = group.add_member(
            identity_uri="capauth:jarvis@skworld.io",
            is_ai=True,
        )
        assert member is not None
        assert member.participant_type == ParticipantType.AGENT


class TestToolScoping:
    """Tests for capability scoping — actions are gated, not speech."""

    def test_admin_always_allowed(self, group: GroupChat) -> None:
        """Admins can invoke any tool regardless of scope."""
        assert group.can_invoke_tool("capauth:alice@skworld.io", "sksecurity.audit")

    def test_unrestricted_member(self, group: GroupChat) -> None:
        """Members with empty tool_scope have unrestricted access."""
        group.add_member(
            identity_uri="capauth:agent-free@test",
            participant_type=ParticipantType.AGENT,
        )
        assert group.can_invoke_tool("capauth:agent-free@test", "any.tool")

    def test_scoped_member_allowed(self, group: GroupChat) -> None:
        """Members with a scope can invoke listed tools."""
        group.add_member(
            identity_uri="capauth:scoped@test",
            participant_type=ParticipantType.AGENT,
            tool_scope=["sksecurity.audit", "skcomm.send"],
        )
        assert group.can_invoke_tool("capauth:scoped@test", "sksecurity.audit")

    def test_scoped_member_blocked(self, group: GroupChat) -> None:
        """Members with a scope cannot invoke unlisted tools."""
        group.add_member(
            identity_uri="capauth:limited@test",
            participant_type=ParticipantType.AGENT,
            tool_scope=["sksecurity.audit"],
        )
        assert not group.can_invoke_tool("capauth:limited@test", "skmemory.delete")

    def test_nonmember_cannot_invoke(self, group: GroupChat) -> None:
        """Non-members cannot invoke any tools."""
        assert not group.can_invoke_tool("capauth:stranger@test", "any.tool")

    def test_admin_can_set_scope(self, group: GroupChat) -> None:
        """Admins can change a member's tool scope."""
        group.add_member(identity_uri="capauth:target@test")
        result = group.set_tool_scope(
            "capauth:target@test",
            ["skcomm.send"],
            by_admin="capauth:alice@skworld.io",
        )
        assert result is True
        member = group.get_member("capauth:target@test")
        assert member.tool_scope == ["skcomm.send"]

    def test_nonadmin_cannot_set_scope(self, group: GroupChat) -> None:
        """Non-admins cannot change tool scopes."""
        group.add_member(identity_uri="capauth:regular@test")
        group.add_member(identity_uri="capauth:wannabe-admin@test")
        result = group.set_tool_scope(
            "capauth:regular@test",
            ["restricted.tool"],
            by_admin="capauth:wannabe-admin@test",
        )
        assert result is False


class TestGroupMessaging:
    """Tests for group message composition — humans and agents equal."""

    def test_member_can_compose(self, group: GroupChat) -> None:
        """Any member can compose a group message."""
        msg = group.compose_group_message(
            sender_uri="capauth:alice@skworld.io",
            content="Hello team!",
        )
        assert msg is not None
        assert msg.recipient == f"group:{group.id}"
        assert msg.thread_id == group.id

    def test_agent_can_compose(self, group: GroupChat) -> None:
        """Agents compose messages identically to humans."""
        group.add_member(
            identity_uri="capauth:lumina-msg@test",
            participant_type=ParticipantType.AGENT,
        )
        msg = group.compose_group_message(
            sender_uri="capauth:lumina-msg@test",
            content="I have a suggestion for the architecture.",
        )
        assert msg is not None
        assert msg.sender == "capauth:lumina-msg@test"

    def test_nonmember_cannot_compose(self, group: GroupChat) -> None:
        """Non-members cannot compose messages."""
        msg = group.compose_group_message(
            sender_uri="capauth:outsider@test",
            content="Let me in!",
        )
        assert msg is None

    def test_observer_cannot_compose(self, group: GroupChat) -> None:
        """Observers can watch but not speak."""
        group.add_member(
            identity_uri="capauth:watcher@test",
            role=MemberRole.OBSERVER,
        )
        msg = group.compose_group_message(
            sender_uri="capauth:watcher@test",
            content="I shouldn't be able to say this.",
        )
        assert msg is None

    def test_compose_increments_count(self, group: GroupChat) -> None:
        """Composing a message increments the message count."""
        before = group.message_count
        group.compose_group_message(
            sender_uri="capauth:alice@skworld.io",
            content="Counting messages.",
        )
        assert group.message_count == before + 1

    def test_agents_and_humans_properties(self, group: GroupChat) -> None:
        """Group can list agents and humans separately."""
        group.add_member(
            identity_uri="capauth:human-check@test",
            participant_type=ParticipantType.HUMAN,
        )
        group.add_member(
            identity_uri="capauth:agent-check@test",
            participant_type=ParticipantType.AGENT,
        )
        assert any(m.identity_uri == "capauth:agent-check@test" for m in group.agents)
        assert any(m.identity_uri == "capauth:human-check@test" for m in group.humans)


class TestGroupKeyDistribution:
    """Tests for PGP key distribution."""

    def test_encrypt_key_for_member(self, alice_keys: tuple[str, str]) -> None:
        """Group key encrypted for a member is non-empty."""
        _, alice_pub = alice_keys
        encrypted = GroupKeyDistributor.encrypt_key_for_member(
            "ab" * 32, alice_pub,
        )
        assert encrypted is not None
        assert "PGP MESSAGE" in encrypted

    def test_encrypt_decrypt_roundtrip(self, alice_keys: tuple[str, str]) -> None:
        """Key encrypted for Alice can be decrypted by Alice."""
        alice_priv, alice_pub = alice_keys
        group_key = "cd" * 32

        encrypted = GroupKeyDistributor.encrypt_key_for_member(group_key, alice_pub)
        decrypted = GroupKeyDistributor.decrypt_group_key(encrypted, alice_priv, PASSPHRASE)
        assert decrypted == group_key

    def test_encrypt_no_pubkey_returns_none(self) -> None:
        """No public key returns None."""
        result = GroupKeyDistributor.encrypt_key_for_member("ab" * 32, "")
        assert result is None

    def test_distribute_key_to_all(
        self, group: GroupChat, bob_keys: tuple[str, str],
    ) -> None:
        """distribute_key encrypts for all members with public keys."""
        _, bob_pub = bob_keys
        group.add_member(
            identity_uri="capauth:bob@skworld.io",
            public_key_armor=bob_pub,
        )
        distribution = GroupKeyDistributor.distribute_key(group)
        assert len(distribution) == group.member_count
        for uri, encrypted in distribution.items():
            if group.get_member(uri).public_key_armor:
                assert encrypted is not None


class TestGroupMessageEncryption:
    """Tests for AES-256-GCM group message encryption."""

    def test_encrypt_decrypt_roundtrip(self) -> None:
        """Message encrypted with group key decrypts correctly."""
        key = "ef" * 32
        plaintext = "Hello sovereign group!"

        encrypted = GroupMessageEncryptor.encrypt(plaintext, key)
        assert encrypted != plaintext

        decrypted = GroupMessageEncryptor.decrypt(encrypted, key)
        assert decrypted == plaintext

    def test_wrong_key_fails(self) -> None:
        """Decryption with wrong key raises ValueError."""
        key1 = "ab" * 32
        key2 = "cd" * 32

        encrypted = GroupMessageEncryptor.encrypt("secret", key1)
        with pytest.raises(ValueError, match="decryption failed"):
            GroupMessageEncryptor.decrypt(encrypted, key2)

    def test_different_nonces(self) -> None:
        """Same plaintext produces different ciphertext (random nonce)."""
        key = "ef" * 32
        e1 = GroupMessageEncryptor.encrypt("same message", key)
        e2 = GroupMessageEncryptor.encrypt("same message", key)
        assert e1 != e2

    def test_long_message(self) -> None:
        """Large messages encrypt and decrypt correctly."""
        key = "ab" * 32
        long_msg = "Sovereignty! " * 10000

        encrypted = GroupMessageEncryptor.encrypt(long_msg, key)
        decrypted = GroupMessageEncryptor.decrypt(encrypted, key)
        assert decrypted == long_msg
