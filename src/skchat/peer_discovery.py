"""Peer auto-discovery from the skcapstone peer store.

Loads peer records from ~/.skcapstone/peers/ and exposes them for
identity resolution, CLI display, and MCP tool responses.

Each peer file is a JSON document with fields:
    name, fingerprint, entity_type, handle, contact_uris, trust_level,
    capabilities, email, added_at, last_seen, source, notes.

Usage:
    disc = PeerDiscovery()
    peers = disc.list_peers()
    peer = disc.get_peer("lumina")
    uri = disc.resolve_identity("lumina")  # "capauth:lumina@skworld.io"
    handles = disc.to_tab_completions()    # ["claude", "lumina", "opus"]
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Optional

logger = logging.getLogger("skchat.peer_discovery")

SKCAPSTONE_PEERS_DIR = Path.home() / ".skcapstone" / "peers"


class PeerDiscovery:
    """Discover and resolve agent peers from the skcapstone peer store.

    Reads peer JSON files from ~/.skcapstone/peers/ and provides
    lookup, identity resolution, and tab-completion helpers.

    Args:
        peers_dir: Path to the peers directory.
            Defaults to ~/.skcapstone/peers/.
    """

    def __init__(self, peers_dir: Optional[Path] = None) -> None:
        self.peers_dir = peers_dir or SKCAPSTONE_PEERS_DIR

    def list_peers(self) -> list[dict]:
        """Load all peer JSON files from the peers directory.

        Files are loaded in sorted (alphabetical) order by filename.
        Malformed or unreadable files are skipped with a warning.

        Returns:
            list[dict]: Peer records. Empty list if the directory does
                not exist or contains no valid JSON files.
        """
        if not self.peers_dir.exists():
            return []

        peers = []
        for path in sorted(self.peers_dir.glob("*.json")):
            try:
                with open(path) as f:
                    data = json.load(f)
                peers.append(data)
            except (json.JSONDecodeError, OSError) as exc:
                logger.warning("Failed to load peer from %s: %s", path, exc)

        return peers

    def get_peer(self, handle: str) -> Optional[dict]:
        """Find a peer by handle, short name, contact URI, or fingerprint.

        Matching strategy (first match wins, case-insensitive):
          1. Exact match on ``handle`` field (e.g. "lumina@skworld.io")
          2. Local-part match on ``handle`` (e.g. "lumina")
          3. Exact match on ``name`` field
          4. Exact / local-part match on ``email`` field
          5. Exact match on any ``contact_uri``
          6. URI-body match (after stripping the scheme, e.g. "capauth:")
          7. Local-part match on any URI
          8. Direct ``fingerprint`` field match (bare hex or scheme-prefixed)
          9. Exact / URI-body / local-part match on ``identity`` field

        A leading ``@`` is stripped before matching.

        Args:
            handle: Peer identifier to search for. Examples: "lumina",
                "@lumina", "lumina@skworld.io",
                "capauth:lumina@skworld.io",
                "AABB1122CCDD3344EEFF5566AABB1122CCDD3344",
                "capauth:AABB1122CCDD3344EEFF5566AABB1122CCDD3344".

        Returns:
            Optional[dict]: First matching peer record, or None.
        """
        query = handle.lstrip("@").lower()
        # Pre-compute scheme-stripped form once (e.g. "capauth:fp" → "fp")
        query_body = query.split(":", 1)[1] if ":" in query else query

        for peer in self.list_peers():
            # 1–2: handle field
            peer_handle = peer.get("handle", "").lower()
            if query == peer_handle:
                return peer
            if peer_handle and query == peer_handle.split("@")[0]:
                return peer

            # 3: name field
            if query == peer.get("name", "").lower():
                return peer

            # 4: email field
            peer_email = peer.get("email", "").lower()
            if peer_email and (query == peer_email or query == peer_email.split("@")[0]):
                return peer

            # 5–7: contact_uris
            for uri in peer.get("contact_uris", []):
                uri_lower = uri.lower()
                if query == uri_lower:
                    return peer
                # Strip scheme: "capauth:lumina@skworld.io" → "lumina@skworld.io"
                if ":" in uri_lower:
                    body = uri_lower.split(":", 1)[1]
                    if query == body:
                        return peer
                    # Local part: "lumina@skworld.io" → "lumina"
                    local = body.split("@")[0]
                    if query == local:
                        return peer

            # 8: direct fingerprint field match — handles bare hex and scheme-prefixed forms
            peer_fp = peer.get("fingerprint", "").lower()
            if peer_fp and query:
                if query == peer_fp or query_body == peer_fp:
                    return peer
                # Short fingerprint prefix (≥8 hex chars) — useful for envelope.sender
                # short IDs without requiring the full 40-char fingerprint.
                q = query_body if ":" in query else query
                if len(q) >= 8 and peer_fp.startswith(q):
                    return peer

            # 9: identity field (e.g. "capauth:lumina@skworld.io")
            peer_id = peer.get("identity", "").lower()
            if peer_id:
                if query == peer_id:
                    return peer
                if ":" in peer_id:
                    id_body = peer_id.split(":", 1)[1]
                    if query == id_body or query_body == id_body:
                        return peer
                    id_local = id_body.split("@")[0]
                    if query == id_local:
                        return peer

        return None

    def resolve_identity(self, short_name: str) -> Optional[str]:
        """Resolve a short name or @handle to an identity URI.

        Resolution order:
          1. If ``short_name`` already contains ``:`` (and no leading ``@``),
             return it unchanged (already a full URI).
          2. Look up in peer store; return the first ``capauth:X@Y`` URI from
             ``contact_uris``.
          3. If peer found but no capauth: URI, construct from handle/email.
          4. If no peer found, return ``{name}@skworld.io`` as a best-effort
             fallback (no ``capauth:`` prefix).
          5. Return None only if ``short_name`` is empty after normalisation.

        Args:
            short_name: Short name, @handle, or partial URI.
                Examples: "lumina", "@lumina", "claude", "chef".

        Returns:
            Optional[str]: Resolved URI or best-effort handle.
                ``None`` if the input is empty.

        Examples:
            >>> disc = PeerDiscovery()
            >>> disc.resolve_identity("lumina")
            'capauth:lumina@skworld.io'
            >>> disc.resolve_identity("@claude")
            'capauth:claude@skworld.io'
            >>> disc.resolve_identity("chef")
            'chef@skworld.io'
        """
        # Already a full URI (contains ":" without leading "@")
        if ":" in short_name and not short_name.startswith("@"):
            return short_name

        peer = self.get_peer(short_name)
        if peer is not None:
            # Prefer capauth: URI that also contains "@" (email-style)
            for uri in peer.get("contact_uris", []):
                if uri.startswith("capauth:") and "@" in uri:
                    return uri
            # Fall back to any capauth: URI
            for uri in peer.get("contact_uris", []):
                if uri.startswith("capauth:"):
                    return uri
            # Construct from handle or email
            handle = peer.get("handle", "")
            if handle and "@" in handle:
                return f"capauth:{handle}"
            email = peer.get("email", "")
            if email:
                return f"capauth:{email}"

        # No peer found — best-effort fallback
        name = short_name.lstrip("@")
        if not name:
            return None
        if "@" in name:
            return name
        return f"{name}@skworld.io"

    def to_tab_completions(self) -> list[str]:
        """Return handles suitable for CLI tab completion.

        Extracts the local part of each peer's ``handle`` field
        (e.g. "lumina" from "lumina@skworld.io"). Falls back to a
        lower-cased ``name`` field if ``handle`` is absent.

        Returns:
            list[str]: Sorted, deduplicated short handle names.
        """
        completions: set[str] = set()
        for peer in self.list_peers():
            handle = peer.get("handle", "")
            if handle:
                local = handle.split("@")[0]
                if local:
                    completions.add(local)
            elif peer.get("name"):
                completions.add(peer["name"].lower())
        return sorted(completions)
