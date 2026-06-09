"""Tests for identity bridge — CapAuth identity and peer resolution."""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import patch

import pytest

from skchat.identity_bridge import (
    PeerResolutionError,
    get_peer_transport_address,
    get_sovereign_identity,
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
    """Create a temporary peers directory with test data."""
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
    """Floor when no agent is resolvable and no env override is set."""
    with patch("skchat.agent_profile.get_active_agent_name", return_value=None):
        with patch.dict("os.environ", {}, clear=True):
            assert get_sovereign_identity() == "capauth:local@skchat"


def test_get_sovereign_identity_per_agent_explicit_uri(tmp_path):
    """An explicit capauth_uri in the per-agent identity.json wins over convention."""
    agent_dir = tmp_path / "lumina"
    (agent_dir / "identity").mkdir(parents=True)
    (agent_dir / "identity" / "identity.json").write_text(
        json.dumps({"capauth_uri": "capauth:lumina@custom.realm"})
    )
    with patch("skchat.agent_profile.get_active_agent_name", return_value="lumina"):
        with patch("skchat.agent_profile._agent_base", return_value=agent_dir):
            with patch.dict("os.environ", {}, clear=True):
                assert get_sovereign_identity() == "capauth:lumina@custom.realm"


def test_resolve_peer_name_from_json(temp_peers_dir):
    """Test resolving peer name from JSON peer file."""
    with patch("skchat.identity_bridge.SKCAPSTONE_PEERS_DIR", temp_peers_dir):
        with patch("skchat.identity_bridge.SKCOMM_PEERS_DIR", Path("/nonexistent")):
            uri = resolve_peer_name("lumina")
            assert uri == "capauth:lumina@capauth.local"


def test_resolve_peer_name_from_email(temp_peers_dir):
    """Test resolving peer name when only email is available."""
    with patch("skchat.identity_bridge.SKCAPSTONE_PEERS_DIR", temp_peers_dir):
        with patch("skchat.identity_bridge.SKCOMM_PEERS_DIR", Path("/nonexistent")):
            uri = resolve_peer_name("jarvis")
            assert uri.startswith("capauth:jarvis@") or uri.startswith("capauth:FFEEAABB")


def test_resolve_peer_name_already_uri():
    """Test that URIs pass through unchanged."""
    uri = resolve_peer_name("capauth:alice@capauth.local")
    assert uri == "capauth:alice@capauth.local"


def test_resolve_peer_name_not_found():
    """Test error when peer name cannot be resolved."""
    with patch("skchat.identity_bridge.SKCAPSTONE_PEERS_DIR", Path("/nonexistent")):
        with patch("skchat.identity_bridge.SKCOMM_PEERS_DIR", Path("/nonexistent")):
            with pytest.raises(PeerResolutionError, match="Cannot resolve peer 'unknown'"):
                resolve_peer_name("unknown")


def test_get_peer_transport_address(temp_peers_dir):
    """Test retrieving transport addresses for a peer."""
    with patch("skchat.identity_bridge.SKCAPSTONE_PEERS_DIR", temp_peers_dir):
        with patch("skchat.identity_bridge.SKCOMM_PEERS_DIR", Path("/nonexistent")):
            transport = get_peer_transport_address("jarvis")
            assert transport is not None
            assert transport["syncthing_device_id"] == "JARVIS-DEVICE-ID-123"
            assert transport["nostr_pubkey"] == "npub1jarvis..."


def test_get_peer_transport_address_not_found():
    """Test transport address lookup for non-existent peer."""
    with patch("skchat.identity_bridge.SKCAPSTONE_PEERS_DIR", Path("/nonexistent")):
        with patch("skchat.identity_bridge.SKCOMM_PEERS_DIR", Path("/nonexistent")):
            transport = get_peer_transport_address("unknown")
            assert transport is None


def test_resolve_peer_name_yaml_format(tmp_path):
    """Test resolving peer from YAML format."""
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
        with patch("skchat.identity_bridge.SKCOMM_PEERS_DIR", Path("/nonexistent")):
            uri = resolve_peer_name("bob")
            assert uri == "capauth:bob@skworld.io"


def test_resolve_peer_name_contact_uris_priority(temp_peers_dir):
    """Test that contact_uris takes priority over other fields."""
    with patch("skchat.identity_bridge.SKCAPSTONE_PEERS_DIR", temp_peers_dir):
        with patch("skchat.identity_bridge.SKCOMM_PEERS_DIR", Path("/nonexistent")):
            uri = resolve_peer_name("lumina")
            assert uri == "capauth:lumina@capauth.local"


def test_identity_resolution_with_corrupt_json(tmp_path):
    """A corrupt per-agent identity.json falls back to convention, not an error."""
    agent_dir = tmp_path / "lumina"
    (agent_dir / "identity").mkdir(parents=True)
    (agent_dir / "identity" / "identity.json").write_text("{ invalid json")

    with patch("skchat.agent_profile.get_active_agent_name", return_value="lumina"):
        with patch("skchat.agent_profile._agent_base", return_value=agent_dir):
            with patch.dict("os.environ", {}, clear=True):
                assert get_sovereign_identity() == "capauth:lumina@skworld.io"


def test_peer_resolution_with_corrupt_json(tmp_path):
    """Test error handling when peer JSON is corrupt."""
    peers_dir = tmp_path / "peers"
    peers_dir.mkdir()

    corrupt_file = peers_dir / "corrupt.json"
    with open(corrupt_file, "w") as f:
        f.write("{ invalid json")

    with patch("skchat.identity_bridge.SKCAPSTONE_PEERS_DIR", peers_dir):
        with patch("skchat.identity_bridge.SKCOMM_PEERS_DIR", Path("/nonexistent")):
            with pytest.raises(PeerResolutionError):
                resolve_peer_name("corrupt")
