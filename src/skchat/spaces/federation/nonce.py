"""In-process replay-nonce cache for federation assertions (S5 review I1).

Each verified assertion carries a `(fqid, nonce)` pair. The first presentation
within the freshness window is accepted and recorded; a second presentation of
the same pair before it expires is a replay and is rejected.

NOTE: this is a single-process cache. A multi-replica sk-lk-authd MUST back this
with a shared store (e.g. Redis with per-key TTL) so a replay can't simply be
routed to a different replica. The interface here is deliberately minimal so
that backend can be swapped in behind `check_and_add`.
"""

from __future__ import annotations

import time


class NonceCache:
    def __init__(self) -> None:
        # (fqid, nonce) -> expiry epoch seconds
        self._seen: dict[tuple[str, str], float] = {}

    def _evict_expired(self, now: float) -> None:
        for key in [k for k, exp in self._seen.items() if exp <= now]:
            del self._seen[key]

    def check_and_add(self, fqid: str, nonce: str, ttl: int) -> bool:
        """Return True if (fqid, nonce) is fresh (and record it); False if it
        was already seen within `ttl`."""
        now = time.time()
        self._evict_expired(now)
        key = (fqid, nonce)
        exp = self._seen.get(key)
        if exp is not None and exp > now:
            return False
        self._seen[key] = now + ttl
        return True
