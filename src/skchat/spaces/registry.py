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

    def set_recording(self, space_id: str, recording: bool, egress_id: str = "") -> None:
        s = self._spaces.get(space_id)
        if s is not None:
            s.recording = recording
            s.egress_id = egress_id
            self._save()

    def add_speaker(self, space_id: str, identity: str) -> None:
        """Mark an identity as on-stage (authoritative). Idempotent."""
        s = self._spaces.get(space_id)
        if s is not None and identity not in s.speakers:
            s.speakers.append(identity)
            self._save()

    def remove_speaker(self, space_id: str, identity: str) -> None:
        """Remove an identity from the on-stage set (authoritative). Idempotent."""
        s = self._spaces.get(space_id)
        if s is not None and identity in s.speakers:
            s.speakers.remove(identity)
            self._save()

    def live(self) -> list[Space]:
        """The 'live now' list, newest-created first.

        Sorted at the source (the registry) so every consumer of ``live()`` -
        the GET /spaces route (web directory + Flutter app both read it) and
        any future caller - gets a consistent newest-on-top order without
        having to re-sort itself. Spaces created before ``created_at`` existed
        default to 0.0 and sort last, not wherever insertion order happened to
        put them.
        """
        live = [s for s in self._spaces.values() if s.status != SpaceStatus.ENDED]
        return sorted(live, key=lambda s: s.created_at, reverse=True)
