"""Per-FQID trust policy (spec §7) — the allowlist analog, cryptographic by FQID.

Config (~/.skchat/federation-trust.json):
  {"full_access": ["chef.skworld", "opus@chef.skworld"], "default": "subscribe"|"deny"}
An entry matches a full FQID (`a@b.c`) OR a host suffix (`b.c`). `default` applies
to anything unmatched. Missing config => deny (safe default)."""

from __future__ import annotations

import json
from enum import Enum
from pathlib import Path

_DEFAULT_PATH = Path.home() / ".skchat" / "federation-trust.json"


class AccessLevel(str, Enum):
    FULL = "full"  # may publish (speaker/host per role)
    SUBSCRIBE = "subscribe"  # listen-only
    DENY = "deny"  # rejected


class TrustPolicy:
    def __init__(self, path: Path | None = None) -> None:
        self.path = Path(path) if path else _DEFAULT_PATH
        self._full: set[str] = set()
        self._default = AccessLevel.DENY
        self._remote_max_role = "speaker"
        self._load()

    def _load(self) -> None:
        if not self.path.exists():
            return
        try:
            d = json.loads(self.path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            return
        self._full = set(d.get("full_access", []))
        try:
            self._default = AccessLevel(d.get("default", "deny"))
        except ValueError:
            self._default = AccessLevel.DENY
        rmr = d.get("remote_max_role", "speaker")
        self._remote_max_role = rmr if rmr in ("speaker", "listener") else "speaker"

    @property
    def remote_max_role(self) -> str:
        return self._remote_max_role

    def access_for(self, fqid: str) -> AccessLevel:
        host = fqid.split("@", 1)[1] if "@" in fqid else fqid
        if fqid in self._full or host in self._full:
            return AccessLevel.FULL
        return self._default
