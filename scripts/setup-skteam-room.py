#!/usr/bin/env python3
"""Create the 'skteam' group room and save it to ~/.skchat/groups/skteam.json.

Usage:
    python scripts/setup-skteam-room.py [--force]

Flags:
    --force    Overwrite existing skteam.json if present.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

# Allow running from the project root without installing the package.
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from skchat.group import GroupChat, MemberRole, ParticipantType  # noqa: E402

CREATOR = "capauth:opus@skworld.io"
MEMBERS = [
    "capauth:lumina@skworld.io",
    "capauth:claude@skworld.io",
]
DISPLAY_NAMES = {
    "capauth:opus@skworld.io": "Opus",
    "capauth:lumina@skworld.io": "Lumina",
    "capauth:claude@skworld.io": "Claude",
}

OUTPUT_DIR = Path.home() / ".skchat" / "groups"
OUTPUT_FILE = OUTPUT_DIR / "skteam.json"


def build_group() -> GroupChat:
    group = GroupChat.create(
        name="skteam",
        creator_uri=CREATOR,
        description="Sovereign agent team room — Opus, Lumina, Claude",
    )
    # Creator is added as ADMIN by GroupChat.create(); update display name.
    creator_member = group.get_member(CREATOR)
    if creator_member:
        creator_member.display_name = DISPLAY_NAMES[CREATOR]
        creator_member.participant_type = ParticipantType.AGENT

    for uri in MEMBERS:
        group.add_member(
            identity_uri=uri,
            role=MemberRole.MEMBER,
            participant_type=ParticipantType.AGENT,
            display_name=DISPLAY_NAMES[uri],
        )

    return group


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--force", action="store_true", help="Overwrite existing file")
    args = parser.parse_args()

    if OUTPUT_FILE.exists() and not args.force:
        print(f"[skteam] already exists at {OUTPUT_FILE}  (use --force to overwrite)")
        sys.exit(0)

    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

    group = build_group()
    payload = json.loads(group.model_dump_json())
    OUTPUT_FILE.write_text(json.dumps(payload, indent=2))

    print(f"[skteam] created → {OUTPUT_FILE}")
    print(group.summary())


if __name__ == "__main__":
    main()
