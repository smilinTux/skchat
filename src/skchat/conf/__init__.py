"""Multi-party video CONFERENCE rooms (distinct from audio-only Spaces).

A ``Conf`` is a many-to-many video meeting (everyone may publish, up to
``participant_cap``), as opposed to a ``Space`` (audio-only broadcast: a small
set of speakers, many passive listeners). The two share lifecycle shape and the
same ``SpaceStatus`` enum (open/live/ended), so this module imports those
building blocks from ``skchat.spaces`` rather than copying them.

This is the MODEL + lifecycle only — no REST routes, tokens/roles, or UI.
"""

from __future__ import annotations

from skchat.conf.room import Conf, ConfRegistry, ConfStatus, derive_conf_id

__all__ = ["Conf", "ConfRegistry", "ConfStatus", "derive_conf_id"]
