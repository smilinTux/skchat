"""MemoryBridge — skmemory search + snapshot for the voice engine.

The actual skmemory calls are injected (defaults use the SDK) so the engine
stays testable and skmemory stays an optional runtime dependency.
"""

from __future__ import annotations

import logging
from typing import Awaitable, Callable, Optional

from skchat.memory_reader import PUBLIC, search_as

log = logging.getLogger("skchat.voice_engine.memory")

# (query, agent, limit, reader). reader None = the verified operator (owner scope).
SearchFn = Callable[[str, str, int, Optional[str]], Awaitable[list[str]]]
SnapshotFn = Callable[[str, str, str], Awaitable[bool]]


async def _sdk_search(query: str, agent: str, limit: int, reader: Optional[str]) -> list[str]:
    from skmemory import MemoryStore  # imported lazily — optional dep

    # MemoryStore() resolves the agent from SKAGENT/SKCAPSTONE_AGENT env vars;
    # the `agent` parameter is kept for interface symmetry with the injected fakes.
    store = MemoryStore()
    hits = search_as(store, query, limit, reader)
    return [getattr(h, "content", str(h)) for h in hits]


async def _sdk_snapshot(content: str, agent: str, tags: str) -> bool:
    from skmemory import MemoryStore

    # MemoryStore() resolves the agent from SKAGENT/SKCAPSTONE_AGENT env vars.
    store = MemoryStore()
    store.snapshot(content[:60], content, tags=[tags])
    return True


class MemoryBridge:
    def __init__(self, _search: SearchFn | None = None, _snapshot: SnapshotFn | None = None):
        self._search = _search or _sdk_search
        self._snapshot = _snapshot or _sdk_snapshot

    async def search(
        self, query: str, agent: str, limit: int = 3, reader: Optional[str] = PUBLIC
    ) -> str:
        """Return a prompt-ready context block, or '' if nothing/error.

        *reader* defaults to ``@public`` (SKMEM-P0-18): owner scope (``None``)
        must be asked for explicitly, by a caller holding a verified operator.
        """
        try:
            hits = await self._search(query, agent, limit, reader)
        except Exception as e:
            log.error("memory search failed: %s", e)
            return ""
        if not hits:
            return ""
        body = "\n".join(f"- {h}" for h in hits)
        return f"[Relevant memories]\n{body}"

    async def snapshot(self, content: str, agent: str, tags: str = "voice-chat") -> bool:
        try:
            return await self._snapshot(content, agent, tags)
        except Exception as e:
            log.error("memory snapshot failed: %s", e)
            return False
