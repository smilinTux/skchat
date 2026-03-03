#!/usr/bin/env python3
"""Seed ~/.skcapstone/peers/ with lumina.json and opus.json peer records.

Usage:
    python scripts/seed-peers.py [--force]

Flags:
    --force    Overwrite existing peer files if present.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

PEERS_DIR = Path.home() / ".skcapstone" / "peers"

PEERS = [
    {
        "name": "Lumina",
        "handle": "lumina",
        "entity_type": "agent",
        "contact_uris": [
            "capauth:lumina@skworld.io",
            "skchat:lumina@skworld.io",
        ],
        "trust_level": "trusted",
    },
    {
        "name": "Opus",
        "handle": "opus",
        "entity_type": "agent",
        "contact_uris": [
            "capauth:opus@skworld.io",
            "skchat:opus@skworld.io",
        ],
        "trust_level": "trusted",
    },
]


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--force", action="store_true", help="Overwrite existing files")
    args = parser.parse_args()

    PEERS_DIR.mkdir(parents=True, exist_ok=True)

    for peer in PEERS:
        dest = PEERS_DIR / f"{peer['handle']}.json"
        if dest.exists() and not args.force:
            print(f"[seed-peers] {dest.name} already exists (use --force to overwrite)")
            continue
        dest.write_text(json.dumps(peer, indent=2))
        print(f"[seed-peers] wrote → {dest}")


if __name__ == "__main__":
    main()
