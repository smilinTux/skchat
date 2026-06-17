"""Per-speaker recording consent (spec §8): a room-composite recording may start
only when every on-stage speaker has consented. Pure logic + a json-backed ledger."""

from __future__ import annotations

import json
from pathlib import Path

_DEFAULT_PATH = Path.home() / ".skchat" / "spaces-consent.json"


class ConsentLedger:
    def __init__(self, path: Path | None = None) -> None:
        self.path = Path(path) if path else _DEFAULT_PATH
        self._by_space: dict[str, set[str]] = {}
        self._load()

    def _load(self) -> None:
        if not self.path.exists():
            return
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            return
        self._by_space = {k: set(v) for k, v in raw.items()}

    def _save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(
            json.dumps({k: sorted(v) for k, v in self._by_space.items()}, indent=2),
            encoding="utf-8",
        )

    def add(self, space_id: str, identity: str) -> None:
        self._by_space.setdefault(space_id, set()).add(identity)
        self._save()

    def revoke(self, space_id: str, identity: str) -> None:
        self._by_space.get(space_id, set()).discard(identity)
        self._save()

    def has(self, space_id: str, identity: str) -> bool:
        return identity in self._by_space.get(space_id, set())


def can_record(
    speakers: list[str], space_id: str, ledger: ConsentLedger
) -> tuple[bool, list[str]]:
    """Return (ok, missing). ok iff every speaker has consented in this space."""
    missing = [s for s in speakers if not ledger.has(space_id, s)]
    return (not missing, missing)
