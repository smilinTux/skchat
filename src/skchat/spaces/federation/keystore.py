"""Realm-qualified directory key pinning for federation (spec §7 / S5 review C1).

TOFU / directory key pinning: a remote peer's verification key is pinned per the
FULL fqid (`agent@host.realm`), stored armored at
`~/.skchat/federation-peers/<safe>.asc`. `<safe>` is the fqid with filesystem-
unsafe characters neutralised so a hostile fqid can NEVER traverse outside the
pin directory.

This intentionally does NOT fall back to local agent/operator keys, and does NOT
key on the bare agent component — both would let a different realm impersonate a
trusted peer (`lumina@chef.skworld` vs `lumina@evil.attacker` MUST be distinct).
"""

from __future__ import annotations

from pathlib import Path

_DEFAULT_BASE = Path.home() / ".skchat" / "federation-peers"

# Characters that must never appear in the on-disk filename: path separators,
# null, and the parent-dir token. Replacing `.` would collide distinct realms,
# so we keep `.` but explicitly strip the `..` traversal token.
_UNSAFE = ("/", "\\", "\x00")


def _safe_name(fqid: str) -> str:
    name = fqid
    for ch in _UNSAFE:
        name = name.replace(ch, "_")
    name = name.replace("..", "_")
    return name


def federation_pubkey(fqid: str, *, base: Path | None = None) -> str | None:
    """Return the armored pubkey pinned for the FULL `fqid`, or None if absent.

    Never traverses outside `base`. Returns None (→ deny) when no pin exists.
    """
    root = (Path(base) if base is not None else _DEFAULT_BASE).resolve()
    path = (root / f"{_safe_name(fqid)}.asc").resolve()
    # Defence in depth: ensure the resolved path is still inside `root`.
    try:
        path.relative_to(root)
    except ValueError:
        return None
    if not path.is_file():
        return None
    try:
        return path.read_text(encoding="utf-8")
    except OSError:
        return None
