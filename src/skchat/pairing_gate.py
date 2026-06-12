"""Pairing gate — makes /pair/accept safe to expose publicly (Tailscale Funnel).

Today /pair/accept is tailnet-protected and has no operator auth, so public
exposure would let anyone POST a pairing bundle and try to get their key
TOFU-added. This gate adds three controls so accept is safe over Funnel:

1. **Operator-opened, time-boxed window.** Accept is rejected unless the operator
   has opened a pairing window (``open_window``) — a short TTL during which they
   *intend* to pair a device. No always-on public pairing.
2. **One-time-ish nonce.** Each window has a nonce the accept must present; the
   window auto-closes after ``max_accepts`` successful pairings.
3. **Rate limit.** Accept *attempts* are throttled (per rolling window) to blunt
   brute-force / DoS.

Enforcement is opt-in (``SKCHAT_PAIRING_REQUIRE_GATE``) so existing tailnet pairing
is unchanged; the Funnel deployment turns it on.
"""

from __future__ import annotations

import os
import secrets
import time
from typing import Callable


class PairingGate:
    """In-memory operator pairing window + nonce + rate limiter."""

    def __init__(
        self,
        *,
        window_ttl: float = 300.0,
        max_accepts_per_window: int = 3,
        throttle_window: float = 60.0,
        max_attempts_per_throttle: int = 10,
        now: Callable[[], float] = time.time,
    ) -> None:
        self._window_ttl = window_ttl
        self._max_accepts = max_accepts_per_window
        self._throttle_window = throttle_window
        self._max_attempts = max_attempts_per_throttle
        self._now = now
        self._nonce: str | None = None
        self._expires: float = 0.0
        self._accepts: int = 0
        self._attempts: list[float] = []

    # -- operator side --------------------------------------------------------
    def open_window(self) -> dict:
        """Operator opens a time-boxed pairing window; returns the nonce."""
        self._nonce = secrets.token_urlsafe(16)
        self._expires = self._now() + self._window_ttl
        self._accepts = 0
        return {"nonce": self._nonce, "expires_at": self._expires, "ttl": self._window_ttl}

    def close(self) -> None:
        self._nonce = None
        self._expires = 0.0

    def is_open(self) -> bool:
        return self._nonce is not None and self._now() < self._expires

    # -- accept side ----------------------------------------------------------
    def check(self, nonce: str | None) -> tuple[bool, str]:
        """Validate an accept attempt: rate-limit → window → nonce → accept-cap.

        Returns ``(ok, reason)``. Records the attempt for throttling either way.
        """
        if self._throttled():
            return False, "rate limited: too many pairing attempts"
        if not self.is_open():
            return False, "pairing window not open"
        if not nonce or nonce != self._nonce:
            return False, "invalid or missing pairing nonce"
        if self._accepts >= self._max_accepts:
            return False, "pairing window accept limit reached"
        return True, "ok"

    def consume(self) -> None:
        """Record a successful pairing; auto-close once the cap is hit."""
        self._accepts += 1
        if self._accepts >= self._max_accepts:
            self.close()

    # -- internals ------------------------------------------------------------
    def _throttled(self) -> bool:
        t = self._now()
        self._attempts = [a for a in self._attempts if a > t - self._throttle_window]
        self._attempts.append(t)
        return len(self._attempts) > self._max_attempts


# Process-wide gate (one operator per agent process).
_gate: PairingGate | None = None


def get_gate() -> PairingGate:
    global _gate
    if _gate is None:
        _gate = PairingGate()
    return _gate


def gate_required() -> bool:
    """Whether /pair/accept must enforce the gate (set when Funnel is enabled)."""
    return os.getenv("SKCHAT_PAIRING_REQUIRE_GATE", "").lower() in ("1", "true", "yes")
