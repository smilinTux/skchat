"""In-memory + JSON-backed registry of Spaces on this host (the 'live now' list)."""

from __future__ import annotations

import json
from dataclasses import asdict, fields
from pathlib import Path

from skchat.spaces.space import Space, SpaceStatus

_DEFAULT_PATH = Path.home() / ".skchat" / "spaces.json"


class SpaceRegistry:
    def __init__(self, path: Path | None = None) -> None:
        self.path = Path(path) if path else _DEFAULT_PATH
        self._spaces: dict[str, Space] = {}
        self._load()

    def _load(self) -> None:
        if not self.path.exists():
            return
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            return
        known = {f.name for f in fields(Space)}
        for d in raw.get("spaces", []):
            d = {k: v for k, v in d.items() if k in known}
            if "space_id" not in d:
                continue
            d["status"] = SpaceStatus(d.get("status", "open"))
            try:
                self._spaces[d["space_id"]] = Space(**d)
            except (TypeError, ValueError):
                continue  # skip malformed record, keep the rest

    def _save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        data = {"spaces": []}
        for s in self._spaces.values():
            d = asdict(s)
            d["status"] = s.status.value
            data["spaces"].append(d)
        self.path.write_text(json.dumps(data, indent=2), encoding="utf-8")

    def add(self, space: Space) -> None:
        self._spaces[space.space_id] = space
        self._save()

    def get(self, space_id: str) -> Space | None:
        return self._spaces.get(space_id)

    def end(self, space_id: str) -> None:
        s = self._spaces.get(space_id)
        if s is not None:
            s.status = SpaceStatus.ENDED
            self._save()

    def live(self) -> list[Space]:
        return [s for s in self._spaces.values() if s.status != SpaceStatus.ENDED]
