"""Deterministic focus (SFU) selection (spec §7): oldest valid membership wins."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass
class Membership:
    fqid: str
    foci_preferred: str
    issued_at: int


def select_focus(memberships: list[Membership]) -> str | None:
    valid = [m for m in memberships if m.foci_preferred]
    if not valid:
        return None
    winner = min(valid, key=lambda m: (m.issued_at, m.fqid))
    return winner.foci_preferred
