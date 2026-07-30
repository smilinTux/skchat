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

**Pairing kernel (M2).** The window semantics now live in ``capauth.pairing`` (one
pairing kernel behind skchat, skcomms, and skcode). ``PairingGate`` delegates its
window state to a ``capauth.pairing.PairingWindow`` when the kernel is enabled.
The delegate is byte-identical: capauth's window is a faithful lift of this gate
(same TTL / nonce / accept-cap / rolling-throttle, same ``(ok, reason)`` strings).
``SKCHAT_PAIRING_KERNEL`` defaults ON; set it to ``0``/``off`` to fall back to the
legacy in-process path.
"""

from __future__ import annotations

import os
import secrets
import time
from typing import Callable


def kernel_enabled() -> bool:
    """Whether pairing-window state is served by the capauth.pairing kernel.

    Defaults ON (the M2 flip). An explicit ``SKCHAT_PAIRING_KERNEL`` of
    ``0``/``false``/``off``/``no`` restores the legacy in-process path.
    """
    val = os.getenv("SKCHAT_PAIRING_KERNEL")
    if val is None:
        return True
    return val.strip().lower() not in ("0", "false", "off", "no")


def _new_kernel_window(
    *,
    window_ttl: float,
    max_accepts: int,
    throttle_window: float,
    max_attempts: int,
    now: Callable[[], float],
):
    """A capauth PairingWindow, reset to closed so it matches a fresh gate.

    capauth's PairingWindow opens itself on construction (open_window is its
    factory); a fresh skchat gate starts closed, so we close it immediately.
    """
    from capauth.pairing import PairingWindow

    win = PairingWindow(
        window_ttl=window_ttl,
        max_accepts=max_accepts,
        throttle_window=throttle_window,
        max_attempts_per_throttle=max_attempts,
        now=now,
    )
    win.close()
    return win


class PairingGate:
    """In-memory operator pairing window + nonce + rate limiter.

    When a ``kernel`` window is attached (the M2 path), every operation delegates
    to it; the reason strings and dict shape stay byte-identical to the legacy
    in-process path below.
    """

    def __init__(
        self,
        *,
        window_ttl: float = 300.0,
        max_accepts_per_window: int = 3,
        throttle_window: float = 60.0,
        max_attempts_per_throttle: int = 10,
        now: Callable[[], float] = time.time,
        kernel=None,
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
        self._kernel = kernel

    # -- operator side --------------------------------------------------------
    def open_window(self) -> dict:
        """Operator opens a time-boxed pairing window; returns the nonce."""
        if self._kernel is not None:
            info = self._kernel.open()
            # Return the legacy shape exactly (drop capauth's extra keys).
            return {
                "nonce": info["nonce"],
                "expires_at": info["expires_at"],
                "ttl": info["ttl"],
            }
        self._nonce = secrets.token_urlsafe(16)
        self._expires = self._now() + self._window_ttl
        self._accepts = 0
        return {"nonce": self._nonce, "expires_at": self._expires, "ttl": self._window_ttl}

    def close(self) -> None:
        if self._kernel is not None:
            self._kernel.close()
            return
        self._nonce = None
        self._expires = 0.0

    def is_open(self) -> bool:
        if self._kernel is not None:
            return self._kernel.is_open()
        return self._nonce is not None and self._now() < self._expires

    # -- accept side ----------------------------------------------------------
    def check(self, nonce: str | None) -> tuple[bool, str]:
        """Validate an accept attempt: rate-limit → window → nonce → accept-cap.

        Returns ``(ok, reason)``. Records the attempt for throttling either way.
        """
        if self._kernel is not None:
            return self._kernel.check(nonce)
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
        if self._kernel is not None:
            self._kernel.consume()
            return
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
        kernel = None
        if kernel_enabled():
            kernel = _new_kernel_window(
                window_ttl=300.0,
                max_accepts=3,
                throttle_window=60.0,
                max_attempts=10,
                now=time.time,
            )
        _gate = PairingGate(kernel=kernel)
    return _gate


def gate_required() -> bool:
    """Whether /pair/accept must enforce the gate (set when Funnel is enabled)."""
    return os.getenv("SKCHAT_PAIRING_REQUIRE_GATE", "").lower() in ("1", "true", "yes")
