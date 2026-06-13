"""Signed command envelope — F1 §1.

Defines the CommandEnvelope dataclass (the wire schema), VerifyResult, and
verify_envelope(), which runs the full §1.3 verification pipeline:

  1. Signature verification (injected verifier — injectable for testing)
  2. node-binding check (sub == self)
  3. Freshness (exp)
  4. Anti-replay (LRU cache of seen cmd ids)
  5. Issuer role resolution (injected role resolver)

The verifier and role resolver are injected so tests can supply fakes without
needing real capauth/PGP infrastructure.

Spec: docs/superpowers/specs/2026-06-13-skreach-security.md §1
"""

from __future__ import annotations

import os
import time
from collections import OrderedDict
from dataclasses import dataclass, field
from enum import Enum
from typing import Callable, Optional

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

ENVELOPE_TYPE = "SKREACH_CMD"
ENVELOPE_VERSION = 1
MAX_TTL_S = 300  # §1.3 step 3: max envelope lifetime
MAX_CLOCK_SKEW_S = int(os.environ.get("SKREACH_MAX_CLOCK_SKEW_S", "5"))
REPLAY_CACHE_WINDOW_S = 600  # 10-minute anti-replay window
REPLAY_CACHE_MAX = 10_000  # LRU eviction bound


# ---------------------------------------------------------------------------
# Envelope dataclass
# ---------------------------------------------------------------------------


@dataclass
class CmdPayload:
    """The cmd block inside a CommandEnvelope (§1.2)."""

    cls: str  # command class — maps to CommandClass in rbac.py
    op: str  # specific operation (e.g. "run", "stop")
    args: list[str] = field(default_factory=list)  # argv; NO shell interpolation
    env: dict[str, str] = field(default_factory=dict)  # additional env (scrubbed by sandbox)
    cwd: str = ""  # working directory; validated against allowed_cwd
    stdin: Optional[str] = None  # optional base64 blob (max 64 KB); unused in MVP
    stream: bool = False  # streaming mode (extended wall-clock timeout)
    confirm_token: Optional[str] = None  # required for destructive ops (§3.3)


@dataclass
class CommandEnvelope:
    """Full signed command envelope as defined in F1 §1.2.

    Attributes:
        type:    Must equal ENVELOPE_TYPE ("SKREACH_CMD").
        v:       Schema version (1).
        id:      Random 128-bit hex string — idempotency + audit key.
        iss:     Issuer FQID (signer's capauth identity).
        sub:     Target node FQID (which skreachd this is addressed to).
        iat:     Issued-at Unix timestamp (seconds).
        exp:     Expiry Unix timestamp (seconds); iat + TTL, max TTL=300s.
        cmd:     The CmdPayload.
        raw:     The original serialised bytes/string (for signature verification).
        _sig:    Opaque signature material (passed to the verifier callable).
    """

    id: str
    iss: str  # issuer FQID
    sub: str  # target node FQID
    iat: float
    exp: float
    cmd: CmdPayload
    type: str = ENVELOPE_TYPE
    v: int = ENVELOPE_VERSION
    raw: bytes = field(default_factory=bytes, repr=False)  # serialised cleartext
    _sig: object = field(default=None, repr=False)  # signature material


# ---------------------------------------------------------------------------
# Verify result
# ---------------------------------------------------------------------------


class DropReason(str, Enum):
    """Why an envelope was dropped by verify_envelope()."""

    SIG_INVALID = "sig_invalid"
    MISDIRECTED = "misdirected_cmd"
    EXPIRED = "expired_cmd"
    REPLAY = "replay_cmd"
    UNAUTHORIZED_ISS = "unauthorized_iss"
    # Not a drop — envelope is valid
    OK = "ok"


@dataclass
class VerifyResult:
    """Result of verify_envelope().

    Attributes:
        valid:   True iff the envelope passed all checks and is safe to dispatch.
        reason:  DropReason explaining the outcome.
        role:    Resolved issuer role (set on DropReason.OK; None otherwise).
        env_id:  The envelope id (for logging; may be partial on early drops).
    """

    valid: bool
    reason: DropReason
    role: Optional[str] = None  # "owner" | "operator" | "member" | "agent" | "guest"
    env_id: str = ""


# ---------------------------------------------------------------------------
# Replay cache (module-level singleton; injectable in tests via _ReplayCache)
# ---------------------------------------------------------------------------


class _ReplayCache:
    """LRU-bounded, time-windowed in-memory anti-replay store.

    Entries are keyed by envelope id; values are the exp timestamp.
    Expired entries are lazily evicted on each check() call.
    The cache is capped at REPLAY_CACHE_MAX entries via LRU eviction.
    """

    def __init__(
        self, window_s: float = REPLAY_CACHE_WINDOW_S, maxsize: int = REPLAY_CACHE_MAX
    ) -> None:
        self._window_s = window_s
        self._maxsize = maxsize
        self._store: OrderedDict[str, float] = OrderedDict()  # id → exp

    def check_and_insert(self, cmd_id: str, exp: float) -> bool:
        """Return True (replay!) if cmd_id was seen before; else insert and return False."""
        self._evict_expired()
        if cmd_id in self._store:
            return True  # replay detected
        # LRU eviction when at capacity
        if len(self._store) >= self._maxsize:
            self._store.popitem(last=False)
        self._store[cmd_id] = exp
        return False

    def _evict_expired(self) -> None:
        now = time.time()
        cutoff = now - self._window_s
        stale = [k for k, v in self._store.items() if v < cutoff]
        for k in stale:
            del self._store[k]

    def clear(self) -> None:
        """For test isolation."""
        self._store.clear()


# Module-level default replay cache used by verify_envelope()
_DEFAULT_REPLAY_CACHE = _ReplayCache()


# ---------------------------------------------------------------------------
# Public verify function
# ---------------------------------------------------------------------------

# Type aliases for injectable callables
SigVerifier = Callable[[CommandEnvelope], bool]
"""Callable(envelope) -> bool; returns True iff the PGP signature is valid."""

RoleResolver = Callable[[str], str]
"""Callable(iss_fqid) -> role string (owner/operator/member/agent/guest)."""


def verify_envelope(
    envelope: CommandEnvelope,
    self_fqid: str,
    sig_verifier: SigVerifier,
    role_resolver: RoleResolver,
    *,
    replay_cache: Optional[_ReplayCache] = None,
    now: Optional[float] = None,
) -> VerifyResult:
    """Run the full §1.3 verification pipeline.

    Steps (in order):
      1. Signature check — sig_verifier(envelope) must return True.
      2. Node-binding — envelope.sub must equal self_fqid.
      3. Freshness — now must be <= envelope.exp + MAX_CLOCK_SKEW_S.
      4. Anti-replay — envelope.id must not be in the replay cache.
      5. Role resolution — role_resolver(envelope.iss) must not be "guest".

    Args:
        envelope:      The parsed CommandEnvelope to verify.
        self_fqid:     This node's own FQID (used for node-binding check).
        sig_verifier:  Injectable signature verifier (calls capauth in prod).
        role_resolver: Injectable role resolver (calls resolve_speaker_role in prod).
        replay_cache:  Optional cache instance; uses module-level default if None.
        now:           Override current time (Unix seconds); uses time.time() if None.

    Returns:
        VerifyResult with valid=True only if all checks pass.
    """
    if replay_cache is None:
        replay_cache = _DEFAULT_REPLAY_CACHE
    if now is None:
        now = time.time()

    partial_id = envelope.id[:16] if envelope.id else "<empty>"

    # Step 1: Signature check
    try:
        sig_ok = sig_verifier(envelope)
    except Exception:
        sig_ok = False
    if not sig_ok:
        return VerifyResult(valid=False, reason=DropReason.SIG_INVALID, env_id=partial_id)

    # Step 2: Node-binding (sub == self)
    if envelope.sub != self_fqid:
        return VerifyResult(valid=False, reason=DropReason.MISDIRECTED, env_id=partial_id)

    # Step 3: Freshness
    if now > envelope.exp + MAX_CLOCK_SKEW_S:
        return VerifyResult(valid=False, reason=DropReason.EXPIRED, env_id=partial_id)

    # Step 4: Anti-replay
    if replay_cache.check_and_insert(envelope.id, envelope.exp):
        return VerifyResult(valid=False, reason=DropReason.REPLAY, env_id=partial_id)

    # Step 5: Role resolution
    try:
        role = role_resolver(envelope.iss)
    except Exception:
        role = "guest"
    if role in ("guest",) or not role:
        return VerifyResult(
            valid=False, reason=DropReason.UNAUTHORIZED_ISS, env_id=partial_id
        )

    return VerifyResult(valid=True, reason=DropReason.OK, role=role, env_id=envelope.id)
