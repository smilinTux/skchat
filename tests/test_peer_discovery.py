"""Tests for PeerDiscovery — peer store loading, lookup, and identity resolution."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from skchat.peer_discovery import PeerDiscovery


# ─────────────────────────────────────────────────────────────
# Fixtures
# ─────────────────────────────────────────────────────────────

LUMINA_DATA = {
    "name": "Lumina",
    "fingerprint": "AABB1122CCDD3344EEFF5566AABB1122CCDD3344",
    "entity_type": "ai-agent",
    "handle": "lumina@skworld.io",
    "email": "lumina@skworld.io",
    "capabilities": ["capauth:identity", "skcomm:messaging", "skmemory:persistence"],
    "contact_uris": [
        "capauth:AABB1122CCDD3344EEFF5566AABB1122CCDD3344",
        "capauth:lumina@skworld.io",
        "mailto:lumina@skworld.io",
    ],
    "trust_level": "verified",
    "added_at": "2026-03-03T00:00:00+00:00",
    "last_seen": None,
    "source": "manual",
    "notes": "Sovereign AI agent",
}

CLAUDE_DATA = {
    "name": "Claude",
    "fingerprint": "CLAUDE000CODE000ANTHROPIC000PEER000TOOL00",
    "entity_type": "ai-agent",
    "handle": "claude@skworld.io",
    "email": "claude@skworld.io",
    "capabilities": ["capauth:identity", "tool:code-editing"],
    "contact_uris": [
        "capauth:claude@skworld.io",
        "mailto:claude@skworld.io",
    ],
    "trust_level": "verified",
    "added_at": "2026-03-03T00:00:00+00:00",
    "last_seen": None,
    "source": "manual",
    "notes": "Claude Code — Anthropic tool agent",
}

ALICE_DATA = {
    "name": "Alice",
    "entity_type": "human",
    "handle": "alice@skworld.io",
    "email": "alice@skworld.io",
    "capabilities": [],
    "contact_uris": [
        "capauth:alice@skworld.io",
        "mailto:alice@skworld.io",
    ],
    "trust_level": "trusted",
    "added_at": "2026-03-03T00:00:00+00:00",
    "last_seen": None,
    "source": "manual",
    "notes": "",
}


@pytest.fixture()
def peers_dir(tmp_path: Path) -> Path:
    """Create a temporary peers directory with three test peer files."""
    d = tmp_path / "peers"
    d.mkdir()

    (d / "lumina.json").write_text(json.dumps(LUMINA_DATA))
    (d / "claude.json").write_text(json.dumps(CLAUDE_DATA))
    (d / "alice.json").write_text(json.dumps(ALICE_DATA))
    return d


@pytest.fixture()
def disc(peers_dir: Path) -> PeerDiscovery:
    """A PeerDiscovery instance pointing at the test peers directory."""
    return PeerDiscovery(peers_dir=peers_dir)


@pytest.fixture()
def empty_disc(tmp_path: Path) -> PeerDiscovery:
    """A PeerDiscovery instance pointing at an empty directory."""
    d = tmp_path / "empty_peers"
    d.mkdir()
    return PeerDiscovery(peers_dir=d)


# ─────────────────────────────────────────────────────────────
# list_peers
# ─────────────────────────────────────────────────────────────


class TestListPeers:
    def test_returns_all_peers(self, disc: PeerDiscovery) -> None:
        """Happy path: all JSON files loaded and returned."""
        peers = disc.list_peers()
        assert len(peers) == 3

    def test_peers_contain_expected_names(self, disc: PeerDiscovery) -> None:
        """Each loaded peer has the correct name."""
        names = {p["name"] for p in disc.list_peers()}
        assert names == {"Lumina", "Claude", "Alice"}

    def test_empty_dir_returns_empty_list(self, empty_disc: PeerDiscovery) -> None:
        """Edge case: empty peers directory returns empty list."""
        assert empty_disc.list_peers() == []

    def test_nonexistent_dir_returns_empty_list(self, tmp_path: Path) -> None:
        """Edge case: peers directory does not exist — returns empty list."""
        disc = PeerDiscovery(peers_dir=tmp_path / "does_not_exist")
        assert disc.list_peers() == []

    def test_skips_malformed_json(self, tmp_path: Path) -> None:
        """Robustness: malformed JSON files are skipped, valid ones returned."""
        d = tmp_path / "peers"
        d.mkdir()
        (d / "good.json").write_text(json.dumps(LUMINA_DATA))
        (d / "bad.json").write_text("not valid json {{")
        disc = PeerDiscovery(peers_dir=d)
        peers = disc.list_peers()
        assert len(peers) == 1
        assert peers[0]["name"] == "Lumina"

    def test_returns_sorted_by_filename(self, peers_dir: Path) -> None:
        """Peers are returned in filename-sorted order (alice, claude, lumina)."""
        peers = PeerDiscovery(peers_dir=peers_dir).list_peers()
        assert [p["name"] for p in peers] == ["Alice", "Claude", "Lumina"]


# ─────────────────────────────────────────────────────────────
# get_peer
# ─────────────────────────────────────────────────────────────


class TestGetPeer:
    def test_find_by_short_name(self, disc: PeerDiscovery) -> None:
        """Happy path: find peer by short handle name."""
        peer = disc.get_peer("lumina")
        assert peer is not None
        assert peer["name"] == "Lumina"

    def test_find_by_at_prefix(self, disc: PeerDiscovery) -> None:
        """Leading @ is stripped before matching."""
        peer = disc.get_peer("@lumina")
        assert peer is not None
        assert peer["name"] == "Lumina"

    def test_find_by_full_handle(self, disc: PeerDiscovery) -> None:
        """Match against full handle field (name@domain)."""
        peer = disc.get_peer("claude@skworld.io")
        assert peer is not None
        assert peer["name"] == "Claude"

    def test_find_by_capauth_uri(self, disc: PeerDiscovery) -> None:
        """Match against a contact_uri value."""
        peer = disc.get_peer("capauth:alice@skworld.io")
        assert peer is not None
        assert peer["name"] == "Alice"

    def test_find_by_name_field(self, disc: PeerDiscovery) -> None:
        """Match against the name field (case-insensitive)."""
        peer = disc.get_peer("alice")
        assert peer is not None
        assert peer["name"] == "Alice"

    def test_returns_none_for_unknown(self, disc: PeerDiscovery) -> None:
        """Edge case: unknown handle returns None."""
        assert disc.get_peer("nobody") is None

    def test_case_insensitive_match(self, disc: PeerDiscovery) -> None:
        """Match is case-insensitive (LUMINA == lumina)."""
        peer = disc.get_peer("LUMINA")
        assert peer is not None
        assert peer["name"] == "Lumina"

    def test_find_by_fingerprint_uri(self, disc: PeerDiscovery) -> None:
        """Match against fingerprint-style capauth URI in contact_uris."""
        peer = disc.get_peer("capauth:AABB1122CCDD3344EEFF5566AABB1122CCDD3344")
        assert peer is not None
        assert peer["name"] == "Lumina"


# ─────────────────────────────────────────────────────────────
# resolve_identity
# ─────────────────────────────────────────────────────────────


class TestResolveIdentity:
    def test_lumina_resolves_to_capauth_uri(self, disc: PeerDiscovery) -> None:
        """lumina → capauth:lumina@skworld.io"""
        assert disc.resolve_identity("lumina") == "capauth:lumina@skworld.io"

    def test_at_lumina_resolves(self, disc: PeerDiscovery) -> None:
        """@lumina → capauth:lumina@skworld.io"""
        assert disc.resolve_identity("@lumina") == "capauth:lumina@skworld.io"

    def test_claude_resolves(self, disc: PeerDiscovery) -> None:
        """claude → capauth:claude@skworld.io"""
        assert disc.resolve_identity("claude") == "capauth:claude@skworld.io"

    def test_at_claude_resolves(self, disc: PeerDiscovery) -> None:
        """@claude → capauth:claude@skworld.io"""
        assert disc.resolve_identity("@claude") == "capauth:claude@skworld.io"

    def test_unknown_name_falls_back_to_handle(self, disc: PeerDiscovery) -> None:
        """chef (not in store) → chef@skworld.io"""
        result = disc.resolve_identity("chef")
        assert result == "chef@skworld.io"

    def test_full_uri_returned_unchanged(self, disc: PeerDiscovery) -> None:
        """Already-full URIs are returned as-is."""
        uri = "capauth:opus@skworld.io"
        assert disc.resolve_identity(uri) == uri

    def test_at_handle_with_domain_returned_as_is(self, disc: PeerDiscovery) -> None:
        """name@domain (not in store) → name@domain unchanged."""
        result = disc.resolve_identity("someone@otherdomain.org")
        assert result == "someone@otherdomain.org"

    def test_empty_after_strip_returns_none(self, disc: PeerDiscovery) -> None:
        """Edge case: '@' alone strips to empty string → None."""
        assert disc.resolve_identity("@") is None

    def test_prefers_email_style_capauth_uri(self, disc: PeerDiscovery) -> None:
        """Lumina has both a fingerprint URI and an email-style URI;
        email-style (capauth:lumina@skworld.io) should be preferred."""
        uri = disc.resolve_identity("lumina")
        assert "@" in uri
        assert uri.startswith("capauth:")


# ─────────────────────────────────────────────────────────────
# to_tab_completions
# ─────────────────────────────────────────────────────────────


class TestToTabCompletions:
    def test_returns_short_handles(self, disc: PeerDiscovery) -> None:
        """Returns the local part of each peer's handle, sorted."""
        completions = disc.to_tab_completions()
        assert completions == ["alice", "claude", "lumina"]

    def test_sorted_and_deduplicated(self, tmp_path: Path) -> None:
        """Output is sorted and deduplicated even with duplicates."""
        d = tmp_path / "peers"
        d.mkdir()
        (d / "a.json").write_text(json.dumps({**LUMINA_DATA, "handle": "lumina@skworld.io"}))
        (d / "b.json").write_text(json.dumps({**LUMINA_DATA, "handle": "lumina@skworld.io"}))
        disc = PeerDiscovery(peers_dir=d)
        assert disc.to_tab_completions() == ["lumina"]

    def test_empty_peers_returns_empty_list(self, empty_disc: PeerDiscovery) -> None:
        """Empty peers dir → empty completions list."""
        assert empty_disc.to_tab_completions() == []

    def test_falls_back_to_name_when_no_handle(self, tmp_path: Path) -> None:
        """If peer has no handle, lowercase name is used for completion."""
        d = tmp_path / "peers"
        d.mkdir()
        peer = {**LUMINA_DATA}
        del peer["handle"]
        (d / "nohandle.json").write_text(json.dumps(peer))
        disc = PeerDiscovery(peers_dir=d)
        assert disc.to_tab_completions() == ["lumina"]
