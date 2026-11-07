"""Which audience a chat surface reads memory as (SKMEM-P0-18).

skmemory 0.11.94 added a ``reader`` to ``MemoryStore.search``. An undeclared
reader is the OWNER, who sees every memory including ``@chef-only`` ones. Every
skchat surface that puts recalled memories into a reply other people can read
must therefore declare who it is reading for:

* a group / thread  -> ``channel:<id>`` (unknown channels resolve to @public)
* a DM or anything unknown -> ``@public``
* owner scope only where an EXISTING identity check says the peer is the
  verified operator (the voice transport's ``speaker_is_operator``).

skchat has no verified-Chef check on the text DM path, so text DMs read as
``@public`` until one exists.
"""

from __future__ import annotations

import inspect
import logging
from typing import Any

logger = logging.getLogger("skchat.memory_reader")

PUBLIC = "@public"
OWNER: str | None = None  # undeclared reader == owner in skmemory

_ONE_TO_ONE = frozenset({"sacred", "private"})


def channel_reader(channel_id: str | None) -> str:
    """``channel:<id>`` for a group or thread, ``@public`` when there is no id."""
    cid = (channel_id or "").strip()
    return f"channel:{cid}" if cid else PUBLIC


def reader_for_message(thread_id: str | None, recipient: str | None = None) -> str:
    """Reader for a chat message: its thread/group channel, else ``@public``."""
    gid = (thread_id or "").strip()
    if not gid:
        r = (recipient or "").strip()
        if r.startswith("group:"):
            gid = r
    return channel_reader(gid)


def voice_reader(mode: str | None, is_operator: object, room: str | None = None) -> str | None:
    """Owner only for a verified operator in a 1:1 call, else the room or public.

    ``is_operator`` must be literally ``True``; a missing or truthy-but-not-True
    value never widens access.
    """
    if is_operator is True and (mode or "").strip().lower() in _ONE_TO_ONE:
        return OWNER
    return channel_reader(room)


def store_supports_reader(store: Any) -> bool:
    """True when ``store.search`` accepts a ``reader`` keyword."""
    try:
        params = inspect.signature(store.search).parameters
    except (TypeError, ValueError):
        return False
    return "reader" in params or any(
        p.kind is inspect.Parameter.VAR_KEYWORD for p in params.values()
    )


def search_as(store: Any, query: str, limit: int, reader: str | None) -> list:
    """``store.search`` for *reader*, failing CLOSED on an old skmemory.

    A store whose ``search`` has no ``reader`` would silently return owner-scope
    results for a group, so for any declared reader it returns ``[]`` instead of
    falling back to an unfiltered search. ``reader=None`` (verified owner) keeps
    the plain call.
    """
    if reader is None:
        return store.search(query, limit=limit)
    if not store_supports_reader(store):
        logger.warning(
            "installed skmemory has no search(reader=...); refusing unfiltered recall "
            "(need skmemory>=0.11.94)"
        )
        return []
    return store.search(query, limit=limit, reader=reader)
