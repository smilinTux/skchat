"""Identity bridge for SKChat — sovereign identity resolution.

This module provides automatic identity resolution from CapAuth sovereign
profiles at ~/.skcapstone/identity/ and peer name resolution from the peer
registry at ~/.skcapstone/peers/ or ~/.skcomm/peers/.

Functions:
    get_sovereign_identity() -> Resolves the local user's CapAuth identity
    resolve_peer_name() -> Resolves friendly names to capauth URIs
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Optional

# Reason: Multiple possible locations for identity and peer data
# based on whether using skcapstone or standalone skcomm
SKCAPSTONE_IDENTITY_DIR = Path.home() / ".skcapstone" / "identity"
SKCAPSTONE_PEERS_DIR = Path.home() / ".skcapstone" / "peers"
SKCOMM_PEERS_DIR = Path.home() / ".skcomm" / "peers"


class IdentityResolutionError(Exception):
    """Raised when identity cannot be resolved."""


class PeerResolutionError(Exception):
    """Raised when peer name cannot be resolved."""


def get_sovereign_identity() -> str:
    """Load the local user's CapAuth identity URI from sovereign profile.

    Reads from ~/.skcapstone/identity/identity.json which is created by
    the CapAuth sovereign identity system. Falls back to environment
    variable SKCHAT_IDENTITY or "capauth:local@skchat" if not found.

    Returns:
        str: CapAuth identity URI (e.g., "capauth:alice@capauth.local")

    Examples:
        >>> identity = get_sovereign_identity()
        >>> identity
        'capauth:sovereign-test@capauth.local'
    """
    import os

    env_identity = os.environ.get("SKCHAT_IDENTITY")
    if env_identity:
        return env_identity

    identity_file = SKCAPSTONE_IDENTITY_DIR / "identity.json"
    if identity_file.exists():
        try:
            with open(identity_file) as f:
                data = json.load(f)
            name = data.get("name", "local")
            email = data.get("email")
            fingerprint = data.get("fingerprint")

            if email and "@" in email:
                handle = email.split("@")[0]
                return f"capauth:{handle}@capauth.local"
            elif fingerprint:
                short_fp = fingerprint[:16]
                return f"capauth:{short_fp}"
            elif name:
                return f"capauth:{name}@capauth.local"
        except (json.JSONDecodeError, OSError, KeyError):
            pass

    return "capauth:local@skchat"


def resolve_peer_name(name: str) -> str:
    """Resolve a friendly peer name to a capauth URI.

    Looks up the peer in the peer registry at ~/.skcapstone/peers/ or
    ~/.skcomm/peers/. Supports both JSON and YAML peer files.

    The peer registry maps friendly names (like "lumina" or "jarvis") to
    their full identity information including fingerprint and capauth URI.

    Args:
        name: Friendly name of the peer (e.g., "lumina", "jarvis")

    Returns:
        str: Resolved capauth URI (e.g., "capauth:lumina@capauth.local")

    Raises:
        PeerResolutionError: If peer name cannot be resolved.

    Examples:
        >>> resolve_peer_name("lumina")
        'capauth:lumina@capauth.local'

        >>> resolve_peer_name("jarvis")
        'capauth:jarvis@capauth.local'
    """
    if name.startswith("capauth:"):
        return name

    peer_files_to_check = []
    for peers_dir in [SKCAPSTONE_PEERS_DIR, SKCOMM_PEERS_DIR]:
        if peers_dir.exists():
            peer_files_to_check.extend([
                peers_dir / f"{name}.json",
                peers_dir / f"{name}.yml",
                peers_dir / f"{name}.yaml",
            ])

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
                    handle = email.split("@")[0]
                    return f"capauth:{handle}@capauth.local"
                elif fingerprint:
                    short_fp = fingerprint[:16]
                    return f"capauth:{short_fp}"
                elif peer_name:
                    return f"capauth:{peer_name.lower()}@capauth.local"

            except (json.JSONDecodeError, OSError, KeyError):
                continue

    raise PeerResolutionError(
        f"Cannot resolve peer '{name}'. No peer file found in "
        f"{SKCAPSTONE_PEERS_DIR} or {SKCOMM_PEERS_DIR}"
    )


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
    for peers_dir in [SKCAPSTONE_PEERS_DIR, SKCOMM_PEERS_DIR]:
        if peers_dir.exists():
            peer_files_to_check.extend([
                peers_dir / f"{name}.json",
                peers_dir / f"{name}.yml",
                peers_dir / f"{name}.yaml",
            ])

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
