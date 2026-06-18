"""Conference room model + Conf id derivation (mirrors spaces.space).

A ``Conf`` is the multi-party VIDEO room. It reuses the Spaces lifecycle status
enum (``SpaceStatus`` → re-exported as ``ConfStatus``) and the same hashing
pattern as ``spaces.derive_space_id`` / ``call_session.derive_room`` for stable,
named rooms — but ALSO supports ad-hoc rooms with a random suffix for the
"new meeting" flow (no slug).

``ConfRegistry`` is a thin parallel of ``spaces.registry.SpaceRegistry`` (same
JSON-backed load/save/lifecycle shape) keyed on :class:`Conf` records, kept in a
separate ``confs.json`` so it never disturbs the audio-only Spaces store.
"""

from __future__ import annotations

import base64
import hashlib
import json
import secrets
import time
from dataclasses import asdict, dataclass, field, fields
from pathlib import Path

from skchat.spaces.space import SpaceStatus

# Reuse the Spaces lifecycle enum verbatim — open/live/ended apply identically to
# a conference. Re-exported here so conf callers need not reach into spaces.
ConfStatus = SpaceStatus

_CONF_PREFIX = "conf-"
_CONF_SUFFIX_LEN = 16  # 16 base32 chars ≈ 80 bits, matching derive_space_id
_DEFAULT_PATH = Path.home() / ".skchat" / "confs.json"


def derive_conf_id(host_fqid: str, slug: str | None = None) -> str:
    """Derive a Conf id from the host FQID, optionally pinned to a ``slug``.

    Two modes:

    * **Named / stable** (``slug`` given) — DETERMINISTIC: SHA-256 over
      ``"host_fqid/slug"``, base32 (lowercased, no padding), first 16 chars.
      Re-derives the same room for the same ``(host, slug)`` pair, exactly like
      :func:`skchat.spaces.space.derive_space_id`.
    * **Ad-hoc / "new meeting"** (``slug is None``) — RANDOM: a fresh
      ``secrets.token_hex`` suffix, so each call yields a unique room.

    Returns:
        ``"conf-" + <16 chars>``.
    """
    if slug is None:
        return _CONF_PREFIX + secrets.token_hex(_CONF_SUFFIX_LEN // 2)
    digest = hashlib.sha256(f"{host_fqid.strip()}/{slug.strip()}".encode()).digest()
    b32 = base64.b32encode(digest).decode().lower().rstrip("=")
    return _CONF_PREFIX + b32[:_CONF_SUFFIX_LEN]


@dataclass
class Conf:
    """A multi-party video conference room (mirrors :class:`spaces.space.Space`)."""

    conf_id: str
    host_fqid: str
    title: str
    status: SpaceStatus = SpaceStatus.OPEN
    participant_cap: int = 20
    created_at: float = 0.0
    slug: str = ""
    participants: list[str] = field(default_factory=list)
    recording: bool = False
    egress_id: str = ""

    @property
    def room(self) -> str:
        """The LiveKit room name is the Conf id (room auto-created on join)."""
        return self.conf_id


class ConfRegistry:
    """In-memory + JSON-backed registry of Confs on this host (the 'live now' list).

    A thin parallel of :class:`skchat.spaces.registry.SpaceRegistry`: same
    load/save/lifecycle shape, but typed on :class:`Conf` and persisted to a
    separate ``confs.json`` so the audio-only Spaces store is untouched.
    """

    def __init__(self, path: Path | None = None) -> None:
        self.path = Path(path) if path else _DEFAULT_PATH
        self._confs: dict[str, Conf] = {}
        self._load()

    def _load(self) -> None:
        if not self.path.exists():
            return
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            return
        known = {f.name for f in fields(Conf)}
        for d in raw.get("confs", []):
            d = {k: v for k, v in d.items() if k in known}
            if "conf_id" not in d:
                continue
            d["status"] = SpaceStatus(d.get("status", "open"))
            try:
                self._confs[d["conf_id"]] = Conf(**d)
            except (TypeError, ValueError):
                continue  # skip malformed record, keep the rest

    def _save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        data = {"confs": []}
        for c in self._confs.values():
            d = asdict(c)
            d["status"] = c.status.value
            data["confs"].append(d)
        self.path.write_text(json.dumps(data, indent=2), encoding="utf-8")

    def add(self, conf: Conf) -> None:
        """Register (or replace) a Conf and persist. Lower-level than ``create``."""
        self._confs[conf.conf_id] = conf
        self._save()

    def create(
        self,
        host_fqid: str,
        title: str,
        slug: str | None = None,
        participant_cap: int = 20,
    ) -> Conf:
        """Build, register, persist, and return a new Conf.

        ``slug`` controls id derivation: named/stable when given, ad-hoc random
        when ``None`` (see :func:`derive_conf_id`).
        """
        conf = Conf(
            conf_id=derive_conf_id(host_fqid, slug),
            host_fqid=host_fqid,
            title=title,
            status=SpaceStatus.OPEN,
            participant_cap=participant_cap,
            created_at=time.time(),
            slug=slug or "",
        )
        self.add(conf)
        return conf

    def get(self, conf_id: str) -> Conf | None:
        return self._confs.get(conf_id)

    def end(self, conf_id: str) -> None:
        c = self._confs.get(conf_id)
        if c is not None:
            c.status = SpaceStatus.ENDED
            self._save()

    def list_live(self) -> list[Conf]:
        return [c for c in self._confs.values() if c.status != SpaceStatus.ENDED]
