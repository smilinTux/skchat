"""SK Spaces federation core (sk-lk-authd) — spec §7.

Signed FQID assertions, per-FQID trust policy, deterministic focus selection,
the signed-Nostr discovery codec, and the authorize() orchestration. All crypto,
FQID->pubkey resolution, relay I/O, and LiveKit minting are behind injectable
seams so the whole flow is unit-testable with no keys/relays/SFU.
"""

__all__: list[str] = []
