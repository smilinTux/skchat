"""Identity bridge for SKChat — sovereign identity resolution.

This module provides automatic identity resolution from CapAuth sovereign
profiles at ~/.skcapstone/identity/ and peer name resolution from the peer
registry at ~/.skcapstone/peers/ or ~/.skcomms/peers/.

Functions:
    get_sovereign_identity() -> Resolves the local user's CapAuth identity
    resolve_peer_name() -> Resolves friendly names to capauth URIs

T2 (coord 1fec05a8): identity resolution now delegates to
``capauth.agent_identity.resolve_agent_identity`` — the canonical resolver.
skchat no longer maintains its own identity logic; it is a thin consumer.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Optional

logger = logging.getLogger("skchat.identity_bridge")

# Canonical wire domain for SK agents/peers. The de-facto standard used by the
# bridge scripts, peer registry, and per-agent resolver. (Historically some
# fallback paths emitted "@capauth.local", which mismatched self-identity and
# broke same-host loopback delivery — unified here.)
SK_DEFAULT_DOMAIN = "skworld.io"

# Reason: Multiple possible locations for identity and peer data
# based on whether using skcapstone or standalone skcomms
SKCAPSTONE_IDENTITY_DIR = Path.home() / ".skcapstone" / "identity"
SKCAPSTONE_PEERS_DIR = Path.home() / ".skcapstone" / "peers"
SKCOMMS_PEERS_DIR = Path.home() / ".skcomms" / "peers"


class IdentityResolutionError(Exception):
    """Raised when identity cannot be resolved."""


class PeerResolutionError(Exception):
    """Raised when peer name cannot be resolved."""


def get_sovereign_identity() -> str:
    """Resolve the *running agent's* CapAuth identity URI (agent-aware).

    T2 delegate: resolution is fully handled by
    ``capauth.agent_identity.resolve_agent_identity``.  skchat is a thin
    consumer — it no longer maintains its own resolver logic.

    Resolution order:
        1. ``SKCHAT_IDENTITY`` env var — explicit operator override.
        2. ``capauth.resolve_agent_identity()`` — canonical resolver
           (SKAGENT env → skmemory.agents → "local" fallback).
        3. ``capauth:local@skchat`` — absolute floor when capauth absent.

    Returns:
        str: CapAuth identity URI (e.g. ``capauth:lumina@skworld.io``).

    Examples:
        >>> import os; os.environ["SKAGENT"] = "lumina"
        >>> get_sovereign_identity()
        'capauth:lumina@skworld.io'
    """
    import os

    env_identity = os.environ.get("SKCHAT_IDENTITY")
    if env_identity:
        return env_identity

    # T2: delegate to the capauth canonical resolver
    try:
        from capauth.agent_identity import resolve_agent_identity

        return resolve_agent_identity().capauth_uri
    except Exception as exc:
        logger.debug("capauth resolver unavailable, trying agent_profile: %s", exc)

    # Graceful fallback when capauth not installed (older envs)
    try:
        from .agent_profile import get_agent_identity

        return get_agent_identity()
    except Exception as exc:  # pragma: no cover - defensive fallback
        logger.warning("agent-aware identity resolution failed: %s", exc)
        return "capauth:local@skchat"


def resolve_peer_name(name: str) -> str:
    """Resolve a friendly peer name to a capauth URI.

    T5 (coord f93f5db6): peer resolution uses the same @skworld.io domain
    as self-identity, closing the loopback delivery mismatch.  The peer
    registry ``identity`` field is also checked (populated by T4's
    identity.json writes).

    Looks up the peer in the peer registry at ~/.skcapstone/peers/ or
    ~/.skcomms/peers/. Supports both JSON and YAML peer files.

    Args:
        name: Friendly name of the peer (e.g., "lumina", "jarvis"),
              or an already-resolved URI (returned as-is).

    Returns:
        str: Resolved capauth URI (e.g., "capauth:lumina@skworld.io")

    Raises:
        PeerResolutionError: If peer name cannot be resolved.

    Examples:
        >>> resolve_peer_name("lumina")
        'capauth:lumina@skworld.io'

        >>> resolve_peer_name("jarvis")
        'capauth:jarvis@skworld.io'
    """
    if name.startswith("capauth:"):
        return name

    # T5: delegate to capauth resolver for known short names first —
    # this is the canonical path and ensures consistent @skworld.io URIs.
    # We still fall through to peer-file lookup for custom peers.
    try:
        from capauth.agent_identity import resolve_agent_identity

        ident = resolve_agent_identity(name)
        # Only use the canonical result if it's a real agent (not "local" fallback)
        if ident.agent == name:
            return ident.capauth_uri
    except Exception as exc:
        logger.debug("capauth resolver unavailable for peer '%s': %s", name, exc)

    peer_files_to_check = []
    for peers_dir in [SKCAPSTONE_PEERS_DIR, SKCOMMS_PEERS_DIR]:
        if peers_dir.exists():
            peer_files_to_check.extend(
                [
                    peers_dir / f"{name}.json",
                    peers_dir / f"{name}.yml",
                    peers_dir / f"{name}.yaml",
                ]
            )

    for peer_file in peer_files_to_check:
        if peer_file.exists():
            try:
                if peer_file.suffix == ".json":
                    with open(peer_file) as f:
                        peer_data = json.load(f)
                else:
                    try:
                        import yaml

                        with open(peer_file) as f:
                            peer_data = yaml.safe_load(f)
                    except ImportError:
                        continue

                # Prefer the explicit `identity` field (written by T4)
                identity = peer_data.get("identity")
                if isinstance(identity, str) and identity.startswith("capauth:") and "@" in identity:
                    return identity

                contact_uris = peer_data.get("contact_uris", [])
                if contact_uris:
                    for uri in contact_uris:
                        if uri.startswith("capauth:") and "@" in uri:
                            return uri

                handle = peer_data.get("handle")
                if handle and handle.startswith("capauth:"):
                    return handle

                email = peer_data.get("email")
                fingerprint = peer_data.get("fingerprint")
                peer_name = peer_data.get("name", name)

                if email and "@" in email:
                    local_part = email.split("@")[0]
                    return f"capauth:{local_part}@{SK_DEFAULT_DOMAIN}"
                elif fingerprint:
                    short_fp = fingerprint[:16]
                    return f"capauth:{short_fp}"
                elif peer_name:
                    return f"capauth:{peer_name.lower()}@{SK_DEFAULT_DOMAIN}"

            except (json.JSONDecodeError, OSError, KeyError):
                continue

    raise PeerResolutionError(
        f"Cannot resolve peer '{name}'. No peer file found in "
        f"{SKCAPSTONE_PEERS_DIR} or {SKCOMMS_PEERS_DIR}"
    )


def is_loopback(recipient_uri: str) -> bool:
    """Return True when *recipient_uri* matches the running agent's own identity.

    T5 (coord f93f5db6): closes the same-host loopback delivery bug class.
    A message addressed to ``capauth:lumina@skworld.io`` when the daemon IS
    lumina must be detected as loopback and delivered to the local inbox
    rather than going through the outbound transport.

    Args:
        recipient_uri: CapAuth URI of the intended recipient.

    Returns:
        bool: True when sender == recipient (same agent on same host).
    """
    try:
        self_uri = get_sovereign_identity()
        return recipient_uri == self_uri
    except Exception:
        return False


def resolve_display_name(uri: str) -> str:
    """Resolve a CapAuth URI or fingerprint to a human-friendly display name.

    Resolution order:
      1. Exact URI match — PeerDiscovery.get_peer() checks handle, name,
         email, and all contact_uris.
      2. Fingerprint match — get_peer() checks the ``fingerprint`` field
         directly (bare hex or scheme-prefixed like ``capauth:AABB1122...``).
      3. Name/identity field match — get_peer() also checks the ``identity``
         field and ``name`` field of each peer record.
      4. Fallback: derive a short label from the URI itself.
         Returns the local part of a ``capauth:X@Y`` URI, the first 8
         uppercase hex chars of a bare fingerprint, or the input capitalized.
         Never returns the string "unknown".

    Args:
        uri: A CapAuth URI, fingerprint, or short name to display.
            May be ``None`` or empty — returns empty string in that case.

    Returns:
        str: Friendly display name (e.g. "Lumina", "Opus").
    """
    if not uri:
        return ""

    # Steps 1–3: peer-store reverse lookup via handle / URI / fingerprint / identity
    try:
        from .peer_discovery import PeerDiscovery

        peer = PeerDiscovery().get_peer(uri)
        if peer is not None:
            # Prefer explicit name field, then handle local-part
            name = peer.get("name", "")
            if name:
                return name
            handle = peer.get("handle", "")
            if handle:
                local = handle.split("@")[0]
                if local:
                    return local.capitalize()
    except Exception as e:
        logger.warning("identity_bridge.py: %s", e)
        pass

    # Step 4: string-based fallback — never return "unknown"
    try:
        local = uri
        if ":" in local:
            local = local.split(":", 1)[1]
        if "@" in local:
            local = local.split("@", 1)[0]
        # Fingerprint heuristic: all-hex string longer than 16 chars → shorten
        if len(local) > 16 and all(c in "0123456789abcdefABCDEF" for c in local):
            return local[:8].upper()
        # Avoid surfacing the literal word "unknown" as a display name
        if local.lower() in ("unknown", "none"):
            return "?"
        return local.capitalize() if local else uri
    except Exception as e:
        logger.warning("identity_bridge.py: %s", e)
        return uri


def get_peer_transport_address(name: str) -> Optional[dict]:
    """Get transport address information for a peer.

    Reads peer configuration to find transport-specific addresses
    (e.g., Syncthing device ID, Nostr pubkey, etc.).

    Args:
        name: Friendly name of the peer

    Returns:
        Optional[dict]: Transport address info, or None if not found

    Examples:
        >>> get_peer_transport_address("lumina")
        {'syncthing_device_id': 'ABC123...', 'nostr_pubkey': 'npub1...'}
    """
    peer_files_to_check = []
    for peers_dir in [SKCAPSTONE_PEERS_DIR, SKCOMMS_PEERS_DIR]:
        if peers_dir.exists():
            peer_files_to_check.extend(
                [
                    peers_dir / f"{name}.json",
                    peers_dir / f"{name}.yml",
                    peers_dir / f"{name}.yaml",
                ]
            )

    for peer_file in peer_files_to_check:
        if peer_file.exists():
            try:
                if peer_file.suffix == ".json":
                    with open(peer_file) as f:
                        peer_data = json.load(f)
                else:
                    try:
                        import yaml

                        with open(peer_file) as f:
                            peer_data = yaml.safe_load(f)
                    except ImportError:
                        continue

                transport_info = {}
                if "syncthing_device_id" in peer_data:
                    transport_info["syncthing_device_id"] = peer_data["syncthing_device_id"]
                if "nostr_pubkey" in peer_data:
                    transport_info["nostr_pubkey"] = peer_data["nostr_pubkey"]
                if "transport_addresses" in peer_data:
                    transport_info.update(peer_data["transport_addresses"])

                if transport_info:
                    return transport_info

            except (json.JSONDecodeError, OSError, KeyError):
                continue

    return None
