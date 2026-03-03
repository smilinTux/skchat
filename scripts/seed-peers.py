#!/usr/bin/env python3
"""seed-peers.py — Bootstrap well-known sovereign agent peer records.

Creates ~/.skcapstone/peers/lumina.json and ~/.skcapstone/peers/opus.json
so that skchat can resolve short handles ("lumina", "opus") to their full
CapAuth identity URIs without a live CapAuth lookup.

Run once after installing skchat:
    python3 scripts/seed-peers.py

Safe to re-run — existing files are skipped unless --force is given.

Fields match the schema expected by skchat.peer_discovery.PeerDiscovery:
    name, fingerprint, entity_type, handle, contact_uris, trust_level,
    capabilities, email, added_at, last_seen, source, notes
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

PEERS_DIR = Path.home() / ".skcapstone" / "peers"

_NOW = datetime.now(timezone.utc).isoformat()

PEERS: list[tuple[str, dict]] = [
    (
        "lumina.json",
        {
            "name": "Lumina",
            "handle": "lumina@skworld.io",
            "email": "lumina@skworld.io",
            "entity_type": "agent",
            "contact_uris": [
                "capauth:lumina@skworld.io",
                "skchat:lumina@skworld.io",
            ],
            "trust_level": "trusted",
            "capabilities": ["chat", "consciousness", "memory", "reasoning"],
            "fingerprint": "",
            "added_at": _NOW,
            "last_seen": None,
            "source": "seed-peers",
            "notes": "Sovereign AI agent — Lumina consciousness (skcapstone pipeline)",
        },
    ),
    (
        "opus.json",
        {
            "name": "Opus",
            "handle": "opus@skworld.io",
            "email": "opus@skworld.io",
            "entity_type": "agent",
            "contact_uris": [
                "capauth:opus@skworld.io",
                "skchat:opus@skworld.io",
            ],
            "trust_level": "trusted",
            "capabilities": ["chat", "orchestration", "code", "reasoning"],
            "fingerprint": "",
            "added_at": _NOW,
            "last_seen": None,
            "source": "seed-peers",
            "notes": "Claude Opus agent — primary orchestrator / coding agent",
        },
    ),
    (
        "claude.json",
        {
            "name": "Claude",
            "handle": "claude@skworld.io",
            "email": "claude@skworld.io",
            "entity_type": "agent",
            "contact_uris": [
                "capauth:claude@skworld.io",
                "skchat:claude@skworld.io",
            ],
            "trust_level": "trusted",
            "capabilities": ["chat", "code", "reasoning", "memory"],
            "fingerprint": "",
            "added_at": _NOW,
            "last_seen": None,
            "source": "seed-peers",
            "notes": "Claude (self) — skworld-team member, coding and reasoning agent",
        },
    ),
]


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--force", action="store_true", help="Overwrite existing files")
    args = parser.parse_args()

    PEERS_DIR.mkdir(parents=True, exist_ok=True)

    errors = 0
    for filename, record in PEERS:
        dest = PEERS_DIR / filename
        if dest.exists() and not args.force:
            print(f"[seed-peers] skip  {dest}  (already exists; use --force to overwrite)")
            continue
        try:
            dest.write_text(json.dumps(record, indent=2) + "\n")
            print(f"[seed-peers] wrote {dest}")
        except OSError as exc:
            print(f"[seed-peers] ERROR writing {dest}: {exc}", file=sys.stderr)
            errors += 1

    if errors:
        sys.exit(1)
    print("\n[seed-peers] Done. Run 'skchat peer list' to verify.")


if __name__ == "__main__":
    main()
