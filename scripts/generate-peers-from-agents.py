#!/usr/bin/env python3
"""Generate ~/.skcapstone/peers/{name}.json for every agent under
~/.skcapstone/agents/{name}/ that has either a soul or an identity file.

Existing peer files are NOT overwritten unless --force is passed; instead
they are reported and skipped so manually-curated entries (e.g. opus.json
with its full PGP key) survive untouched.

Convention:
- handle / identity = capauth:{name}@skworld.io  (matches peer_discovery
  short-name resolution + the de-facto URI in groups)
- entity_type = ai-agent
- capabilities sourced from soul if present, else default ai-agent set
- display_name / notes pulled from soul.display_name / soul.vibe when there
- fingerprint pulled from agent's identity.json when available
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

DEFAULT_CAPS = [
    "capauth:identity",
    "skcomm:messaging",
    "skchat:p2p-chat",
    "skmemory:persistence",
]
SKIP_AGENTS = {"sovereign-test", "lumina-template"}


def load_json(path: Path) -> dict:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}


def build_peer(name: str, agent_home: Path) -> dict:
    soul = load_json(agent_home / "soul" / "base.json")
    identity = load_json(agent_home / "identity" / "identity.json")

    display_name = (
        soul.get("display_name") or soul.get("name") or identity.get("name") or name.capitalize()
    )
    vibe = soul.get("vibe") or soul.get("philosophy") or ""
    notes = vibe[:200] if vibe else f"Sovereign agent: {name}"

    fingerprint = identity.get("fingerprint", "")
    handle = f"{name}@skworld.io"
    uri = f"capauth:{handle}"

    contact_uris = [uri, f"mailto:{handle}"]
    if fingerprint:
        contact_uris.insert(0, f"capauth:{fingerprint}")

    caps = list(DEFAULT_CAPS)
    if soul:
        caps.append("consciousness:active")

    return {
        "name": display_name,
        "identity": uri,
        "fingerprint": fingerprint,
        "public_key": "",
        "entity_type": "ai-agent",
        "handle": handle,
        "email": handle,
        "capabilities": caps,
        "contact_uris": contact_uris,
        "trust_level": "verified",
        "added_at": datetime.now(timezone.utc).isoformat(),
        "last_seen": None,
        "source": "auto-generated:generate-peers-from-agents",
        "agent_type": "ai",
        "notes": notes,
        "transport_addresses": {"file": f"file://{Path.home()}/.skcomm/inbox"},
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--force", action="store_true", help="overwrite existing peer files")
    parser.add_argument("--dry-run", action="store_true", help="print what would happen")
    parser.add_argument(
        "--agents-dir",
        type=Path,
        default=Path.home() / ".skcapstone" / "agents",
    )
    parser.add_argument(
        "--peers-dir",
        type=Path,
        default=Path.home() / ".skcapstone" / "peers",
    )
    args = parser.parse_args()

    if not args.agents_dir.exists():
        print(f"agents dir not found: {args.agents_dir}", file=sys.stderr)
        return 2

    args.peers_dir.mkdir(parents=True, exist_ok=True)

    written = skipped = 0
    for agent_home in sorted(args.agents_dir.iterdir()):
        if not agent_home.is_dir():
            continue
        name = agent_home.name
        if name.startswith(".") or name in SKIP_AGENTS:
            continue
        # require at least one of soul or identity
        if not ((agent_home / "soul" / "base.json").exists()
                or (agent_home / "identity" / "identity.json").exists()):
            continue

        out_path = args.peers_dir / f"{name}.json"
        if out_path.exists() and not args.force:
            print(f"  skip   {name:14} (exists, use --force)")
            skipped += 1
            continue

        peer = build_peer(name, agent_home)
        if args.dry_run:
            print(f"  dry    {name:14} -> {out_path.name}")
        else:
            out_path.write_text(json.dumps(peer, indent=2) + "\n", encoding="utf-8")
            print(f"  wrote  {name:14} -> {out_path.name}")
        written += 1

    print(f"\n{written} written, {skipped} skipped")
    return 0


if __name__ == "__main__":
    sys.exit(main())
