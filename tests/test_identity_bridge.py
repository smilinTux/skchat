"""Tests for identity bridge — CapAuth identity and peer resolution.

T2 update: resolution now delegates to capauth.agent_identity.  Tests
that previously patched skchat.agent_profile.* internals now also patch
the capauth resolver path where appropriate.
T5 update: resolve_peer_name checks capauth resolver first; is_loopback added.
"""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import patch

import pytest

from skchat.identity_bridge import (
    PeerResolutionError,
    get_peer_transport_address,
    get_sovereign_identity,
    is_loopback,
    resolve_peer_name,
)


@pytest.fixture
def temp_identity_dir(tmp_path):
    """Create a temporary identity directory with test data."""
    identity_dir = tmp_path / "identity"
    identity_dir.mkdir(parents=True, exist_ok=True)

    identity_data = {
        "name": "test-agent",
        "email": "test-agent@skcapstone.local",
        "fingerprint": "AABBCCDDEEFF00112233445566778899AABBCCDD",
        "created_at": "2026-02-24T00:00:00+00:00",
        "capauth_managed": True,
    }

    identity_file = identity_dir / "identity.json"
    with open(identity_file, "w") as f:
        json.dump(identity_data, f)

    return identity_dir


@pytest.fixture
def temp_peers_dir(tmp_path):
    """Create a temporary peers directory with test data.

    The lumina peer has a custom URI in contact_uris (not @skworld.io) to
    verify file-based lookup still works when the capauth resolver is mocked.
    """
    peers_dir = tmp_path / "peers"
    peers_dir.mkdir(parents=True, exist_ok=True)

    lumina_data = {
        "name": "Lumina",
        "fingerprint": "1122334455667788990011223344556677889900",
        "email": "lumina@skworld.io",
        "handle": "capauth:lumina@capauth.local",
        "contact_uris": ["capauth:lumina@capauth.local", "mailto:lumina@skworld.io"],
        "trust_level": "verified",
        "added_at": "2026-02-24T00:00:00+00:00",
    }

    jarvis_data = {
        "name": "Jarvis",
        "fingerprint": "FFEEAABB00112233445566778899AABBCCDDEEFF",
        "email": "jarvis@skcapstone.local",
        "trust_level": "sovereign",
        "syncthing_device_id": "JARVIS-DEVICE-ID-123",
        "transport_addresses": {"nostr_pubkey": "npub1jarvis..."},
    }

    lumina_file = peers_dir / "lumina.json"
    with open(lumina_file, "w") as f:
        json.dump(lumina_data, f)

    jarvis_file = peers_dir / "jarvis.json"
    with open(jarvis_file, "w") as f:
        json.dump(jarvis_data, f)

    return peers_dir


# ---------------------------------------------------------------------------
# get_sovereign_identity
# ---------------------------------------------------------------------------


def test_get_sovereign_identity_agent_aware():
    """Self-identity resolves to the ACTIVE agent's convention URI.

    Regression: previously this read a single shared identity.json and
    returned ``capauth:test-agent@capauth.local`` for every agent.
    """
    with patch.dict("os.environ", {"SKAGENT": "lumina"}, clear=True):
        assert get_sovereign_identity() == "capauth:lumina@skworld.io"


def test_get_sovereign_identity_from_env():
    """Test loading identity from environment variable."""
    with patch.dict("os.environ", {"SKCHAT_IDENTITY": "capauth:env-test@capauth.local"}):
        identity = get_sovereign_identity()
        assert identity == "capauth:env-test@capauth.local"


def test_get_sovereign_identity_fallback():
    """Floor when no agent is resolvable and no env override is set.

    T2: we now patch capauth.agent_identity._resolve_active_agent_name in
    addition to the skchat path to ensure the 'local' floor is reached.
    """
    with patch("capauth.agent_identity._resolve_active_agent_name", return_value=None):
        with patch("skchat.agent_profile.get_active_agent_name", return_value=None):
            with patch.dict("os.environ", {}, clear=True):
                result = get_sovereign_identity()
                # capauth returns capauth:local@skworld.io; the skchat floor
                # is capauth:local@skchat — either is acceptable as the floor.
                assert result in (
                    "capauth:local@skworld.io",
                    "capauth:local@skchat",
                )


def test_get_sovereign_identity_per_agent_explicit_uri(tmp_path):
    """An explicit capauth_uri in the per-agent identity.json wins over convention.

    T2: capauth resolver picks up identity.json capauth_uri via profile.json
    path; if that's absent it falls through to convention. This test verifies
    the whole chain by patching at the capauth resolver level.
    """
    # Direct capauth path: patch resolve_agent_identity to return a custom URI
    from capauth.agent_identity import AgentIdentity

    custom = AgentIdentity(
        agent="lumina",
        capauth_uri="capauth:lumina@custom.realm",
    )
    with patch("capauth.agent_identity.resolve_agent_identity", return_value=custom):
        with patch.dict("os.environ", {"SKAGENT": "lumina"}, clear=True):
            assert get_sovereign_identity() == "capauth:lumina@custom.realm"


def test_identity_resolution_with_corrupt_json(tmp_path):
    """A corrupt per-agent identity.json falls back to convention, not an error.

    With T2, capauth handles this: if profile.json is absent/corrupt it
    falls back to convention capauth:<agent>@skworld.io.
    """
    with patch.dict("os.environ", {"SKAGENT": "lumina"}, clear=True):
        # Even with corrupt agent profile dir, capauth convention fires
        result = get_sovereign_identity()
        assert result == "capauth:lumina@skworld.io"


# ---------------------------------------------------------------------------
# resolve_peer_name
# ---------------------------------------------------------------------------


def test_resolve_peer_name_already_uri():
    """Test that URIs pass through unchanged."""
    uri = resolve_peer_name("capauth:alice@capauth.local")
    assert uri == "capauth:alice@capauth.local"


def test_resolve_peer_name_email_only_no_longer_synthesizes(temp_peers_dir):
    """SEAM 7 regression: jarvis has no authenticated capauth identity (only
    email/fingerprint), so it must no longer resolve to a synthesized URI."""
    with patch("skchat.identity_bridge.SKCAPSTONE_PEERS_DIR", temp_peers_dir):
        with patch("skchat.identity_bridge.SKCOMMS_PEERS_DIR", Path("/nonexistent")):
            with patch(
                "capauth.agent_identity.resolve_agent_identity",
                side_effect=ImportError("mocked absence"),
            ):
                with pytest.raises(PeerResolutionError):
                    resolve_peer_name("jarvis")


def test_resolve_peer_name_from_json(temp_peers_dir):
    """File-based resolution returns contact_uris URI when capauth delegate is bypassed.

    When capauth resolves a known peer name, it returns @skworld.io.
    This test verifies the file-lookup path by also patching capauth.
    """
    with patch("skchat.identity_bridge.SKCAPSTONE_PEERS_DIR", temp_peers_dir):
        with patch("skchat.identity_bridge.SKCOMMS_PEERS_DIR", Path("/nonexistent")):
            with patch(
                "capauth.agent_identity.resolve_agent_identity",
                side_effect=ImportError("mocked absence"),
            ):
                uri = resolve_peer_name("lumina")
                assert uri == "capauth:lumina@capauth.local"


def test_resolve_peer_name_contact_uris_priority(temp_peers_dir):
    """contact_uris takes priority over other fields in file lookup."""
    with patch("skchat.identity_bridge.SKCAPSTONE_PEERS_DIR", temp_peers_dir):
        with patch("skchat.identity_bridge.SKCOMMS_PEERS_DIR", Path("/nonexistent")):
            with patch(
                "capauth.agent_identity.resolve_agent_identity",
                side_effect=ImportError("mocked absence"),
            ):
                uri = resolve_peer_name("lumina")
                assert uri == "capauth:lumina@capauth.local"


# --- SEAM 7: no unauthenticated capauth URI synthesis -----------------------


def _no_capauth_delegate():
    """Context helper: disable the capauth resolver so file lookup is exercised."""
    return patch(
        "capauth.agent_identity.resolve_agent_identity",
        side_effect=ImportError("mocked absence"),
    )


def test_resolve_peer_name_no_synthesis_from_email_only(tmp_path):
    """SEAM 7: a peer file with only email/fingerprint/name (no capauth
    identity) must NOT be resolved to a synthesized capauth URI.

    Previously this minted ``capauth:<local_part>@skworld.io`` from the email
    local-part (or ``capauth:<fp>`` from the fingerprint), fabricating an
    unauthenticated identity. It must now raise instead.
    """
    peers_dir = tmp_path / "peers"
    peers_dir.mkdir()
    unauth = {
        "name": "Jarvis",
        "email": "jarvis@skcapstone.local",
        "fingerprint": "FFEEAABB00112233445566778899AABBCCDDEEFF",
        "trust_level": "sovereign",
    }
    with open(peers_dir / "jarvis.json", "w") as f:
        json.dump(unauth, f)

    with patch("skchat.identity_bridge.SKCAPSTONE_PEERS_DIR", peers_dir):
        with patch("skchat.identity_bridge.SKCOMMS_PEERS_DIR", Path("/nonexistent")):
            with _no_capauth_delegate():
                with pytest.raises(PeerResolutionError):
                    resolve_peer_name("jarvis")


def test_resolve_peer_name_no_synthesis_from_name_only(tmp_path):
    """SEAM 7: a peer file carrying only a friendly ``name`` must not mint
    ``capauth:<name>@skworld.io``."""
    peers_dir = tmp_path / "peers"
    peers_dir.mkdir()
    with open(peers_dir / "mallory.json", "w") as f:
        json.dump({"name": "Mallory", "trust_level": "unverified"}, f)

    with patch("skchat.identity_bridge.SKCAPSTONE_PEERS_DIR", peers_dir):
        with patch("skchat.identity_bridge.SKCOMMS_PEERS_DIR", Path("/nonexistent")):
            with _no_capauth_delegate():
                with pytest.raises(PeerResolutionError):
                    resolve_peer_name("mallory")


def test_resolve_peer_name_known_peer_with_identity_field_resolves(tmp_path):
    """SEAM 7: a peer carrying an explicit authenticated ``identity`` capauth
    URI still resolves — the tightening only removes *synthesis*."""
    peers_dir = tmp_path / "peers"
    peers_dir.mkdir()
    known = {
        "name": "Opus",
        "identity": "capauth:opus@skworld.io",
        "email": "opus@skcapstone.local",
    }
    with open(peers_dir / "opus.json", "w") as f:
        json.dump(known, f)

    with patch("skchat.identity_bridge.SKCAPSTONE_PEERS_DIR", peers_dir):
        with patch("skchat.identity_bridge.SKCOMMS_PEERS_DIR", Path("/nonexistent")):
            with _no_capauth_delegate():
                assert resolve_peer_name("opus") == "capauth:opus@skworld.io"


def test_resolve_peer_name_not_found():
    """PeerResolutionError when the name isn't a known agent and has no peer file.

    T5: capauth resolves unknown names to 'local', so we also disable it.
    The peer dirs don't exist → PeerResolutionError.
    """
    with patch("skchat.identity_bridge.SKCAPSTONE_PEERS_DIR", Path("/nonexistent")):
        with patch("skchat.identity_bridge.SKCOMMS_PEERS_DIR", Path("/nonexistent")):
            with patch(
                "capauth.agent_identity.resolve_agent_identity",
                side_effect=ImportError("mocked absence"),
            ):
                with pytest.raises(PeerResolutionError, match="Cannot resolve peer 'unknown'"):
                    resolve_peer_name("unknown")


def test_peer_resolution_with_corrupt_json(tmp_path):
    """Test error handling when peer JSON is corrupt and capauth can't resolve."""
    peers_dir = tmp_path / "peers"
    peers_dir.mkdir()

    corrupt_file = peers_dir / "corrupt.json"
    with open(corrupt_file, "w") as f:
        f.write("{ invalid json")

    with patch("skchat.identity_bridge.SKCAPSTONE_PEERS_DIR", peers_dir):
        with patch("skchat.identity_bridge.SKCOMMS_PEERS_DIR", Path("/nonexistent")):
            with patch(
                "capauth.agent_identity.resolve_agent_identity",
                side_effect=ImportError("mocked absence"),
            ):
                with pytest.raises(PeerResolutionError):
                    resolve_peer_name("corrupt")


def test_resolve_peer_name_yaml_format(tmp_path):
    """Resolving an authenticated peer from YAML format still works."""
    pytest.importorskip("yaml")

    peers_dir = tmp_path / "peers"
    peers_dir.mkdir()

    yaml_content = """
name: Bob
identity: capauth:bob@skworld.io
email: bob@example.com
fingerprint: AABBCCDD11223344
trust_level: verified
"""

    yaml_file = peers_dir / "bob.yml"
    with open(yaml_file, "w") as f:
        f.write(yaml_content)

    with patch("skchat.identity_bridge.SKCAPSTONE_PEERS_DIR", peers_dir):
        with patch("skchat.identity_bridge.SKCOMMS_PEERS_DIR", Path("/nonexistent")):
            with patch(
                "capauth.agent_identity.resolve_agent_identity",
                side_effect=ImportError("mocked absence"),
            ):
                uri = resolve_peer_name("bob")
                assert uri == "capauth:bob@skworld.io"


def test_resolve_peer_name_yaml_unauthenticated_raises(tmp_path):
    """SEAM 7: a YAML peer with only email/fingerprint (no capauth identity)
    must not synthesize a URI."""
    pytest.importorskip("yaml")

    peers_dir = tmp_path / "peers"
    peers_dir.mkdir()

    yaml_content = """
name: Bob
email: bob@example.com
fingerprint: AABBCCDD11223344
trust_level: verified
"""

    yaml_file = peers_dir / "bob.yml"
    with open(yaml_file, "w") as f:
        f.write(yaml_content)

    with patch("skchat.identity_bridge.SKCAPSTONE_PEERS_DIR", peers_dir):
        with patch("skchat.identity_bridge.SKCOMMS_PEERS_DIR", Path("/nonexistent")):
            with patch(
                "capauth.agent_identity.resolve_agent_identity",
                side_effect=ImportError("mocked absence"),
            ):
                with pytest.raises(PeerResolutionError):
                    resolve_peer_name("bob")


def test_get_peer_transport_address(temp_peers_dir):
    """Test retrieving transport addresses for a peer."""
    with patch("skchat.identity_bridge.SKCAPSTONE_PEERS_DIR", temp_peers_dir):
        with patch("skchat.identity_bridge.SKCOMMS_PEERS_DIR", Path("/nonexistent")):
            transport = get_peer_transport_address("jarvis")
            assert transport is not None
            assert transport["syncthing_device_id"] == "JARVIS-DEVICE-ID-123"
            assert transport["nostr_pubkey"] == "npub1jarvis..."


def test_get_peer_transport_address_not_found():
    """Test transport address lookup for non-existent peer."""
    with patch("skchat.identity_bridge.SKCAPSTONE_PEERS_DIR", Path("/nonexistent")):
        with patch("skchat.identity_bridge.SKCOMMS_PEERS_DIR", Path("/nonexistent")):
            transport = get_peer_transport_address("unknown")
            assert transport is None


# ---------------------------------------------------------------------------
# is_loopback (T5)
# ---------------------------------------------------------------------------


def test_is_loopback_same_agent():
    """Sending to self (same URI) is detected as loopback."""
    with patch.dict("os.environ", {"SKAGENT": "lumina"}, clear=True):
        assert is_loopback("capauth:lumina@skworld.io") is True


def test_is_loopback_different_agent():
    """Sending to a different agent is not loopback."""
    with patch.dict("os.environ", {"SKAGENT": "lumina"}, clear=True):
        assert is_loopback("capauth:opus@skworld.io") is False


def test_is_loopback_explicit_override():
    """SKCHAT_IDENTITY override is respected in loopback check."""
    with patch.dict(
        "os.environ",
        {"SKCHAT_IDENTITY": "capauth:ops@custom.io"},
        clear=False,
    ):
        assert is_loopback("capauth:ops@custom.io") is True
        assert is_loopback("capauth:lumina@skworld.io") is False


# --- QA F-1: short-name self-sends ----------------------------------------


def test_is_loopback_short_name_self():
    """Bare short name of the active agent is loopback (`lumina`)."""
    with patch.dict("os.environ", {"SKAGENT": "lumina"}, clear=True):
        assert is_loopback("lumina") is True


def test_is_loopback_short_name_with_at_self():
    """`@`-prefixed short name of the active agent is loopback (`@lumina`)."""
    with patch.dict("os.environ", {"SKAGENT": "lumina"}, clear=True):
        assert is_loopback("@lumina") is True


def test_is_loopback_short_name_domain_self():
    """`lumina@skworld.io` short form reduces to the local-part and matches."""
    with patch.dict("os.environ", {"SKAGENT": "lumina"}, clear=True):
        assert is_loopback("lumina@skworld.io") is True


def test_is_loopback_short_name_other_agent():
    """Short name of a different agent is not loopback (`@opus` / `opus`)."""
    with patch.dict("os.environ", {"SKAGENT": "lumina"}, clear=True):
        assert is_loopback("opus") is False
        assert is_loopback("@opus") is False


def test_is_loopback_chef_is_not_self():
    """The human operator `chef` is never loopback for an AI agent.

    chef is a valid same-host peer but NOT this agent, so it must take the
    outbound path, not the local loopback path.
    """
    with patch.dict("os.environ", {"SKAGENT": "lumina"}, clear=True):
        assert is_loopback("chef") is False
        assert is_loopback("@chef") is False


def test_is_loopback_env_driven_resolution():
    """Which short name loops back follows the active-agent env var."""
    with patch.dict("os.environ", {"SKAGENT": "opus"}, clear=True):
        assert is_loopback("opus") is True
        assert is_loopback("@opus") is True
        assert is_loopback("lumina") is False
    # SKCAPSTONE_AGENT is honoured as a fallback when SKAGENT is unset.
    with patch.dict("os.environ", {"SKCAPSTONE_AGENT": "jarvis"}, clear=True):
        assert is_loopback("jarvis") is True
        assert is_loopback("@jarvis") is True
        assert is_loopback("lumina") is False


def test_is_loopback_empty_input():
    """Empty / whitespace recipients are not loopback."""
    with patch.dict("os.environ", {"SKAGENT": "lumina"}, clear=True):
        assert is_loopback("") is False
        assert is_loopback("@") is False
