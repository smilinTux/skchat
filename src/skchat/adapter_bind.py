"""adapter_bind — CapAuth ``/bind`` flow for platform↔sovereign identity.

The :class:`AdapterBinder` closes the **identity-binding** half of the
inbound→reply loop (U14 Phase 3).  When a platform user types ``/bind <fqid>``
in a channel, the binder:

1. Parses the command and the claimed sovereign FQID.
2. Issues a CapAuth challenge (via an **injectable** verifier so tests mock it
   and production wires the real PGP challenge-response from
   :mod:`capauth.identity`).
3. On a verified challenge, calls the skcomms adapter's
   :meth:`~skcomms.adapters.base.ChannelAdapter.bind_fqid` to persist the
   ``PlatformIdentity → FQID`` mapping in the adapter's own store, **and**
   mirrors it into a local :class:`FqidBindingStore` (YAML) so the binding
   survives a skchat restart even when the adapter is reconstructed.

Everything external — CapAuth and the adapter — is injected, so the whole flow
unit-tests with plain mocks (no PGP keys, no live bots).

Production wiring sketch::

    binder = AdapterBinder(
        adapter=telegram_adapter,
        verifier=PgpCapAuthVerifier(my_fingerprint),
        store=FqidBindingStore(Path.home() / ".skchat" / "bindings.yml"),
    )
    result = await binder.bind(platform_identity, "/bind chef@skworld.io")
    if result.ok:
        ...  # reply "Bound ✓"
"""

from __future__ import annotations

import logging
import re
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional, Protocol, runtime_checkable

import yaml

logger = logging.getLogger("skchat.adapter_bind")

# The slash command that triggers a bind.  Case-insensitive on the verb.
BIND_COMMAND = "/bind"

# Trust level recorded against a successfully challenge-verified binding.
# Mirrors skcomms ``TrustLevel.VERIFIED`` without importing the enum.
TRUST_VERIFIED = "verified"

# A permissive FQID shape: ``local@domain`` (e.g. ``chef@skworld.io``) or a
# ``capauth:agent@domain`` wire URI.  Validation is deliberately lenient — the
# CapAuth challenge is the real gate, this just rejects obvious garbage.
_FQID_RE = re.compile(r"^(?:capauth:)?[A-Za-z0-9._+-]+@[A-Za-z0-9.-]+$")


@runtime_checkable
class CapAuthVerifier(Protocol):
    """Injectable CapAuth challenge gate.

    Implementations issue a challenge for the claimed FQID and return whether
    the prover satisfied it.  In tests this is a mock; in production it wraps
    :func:`capauth.identity.create_challenge` /
    :func:`capauth.identity.verify_challenge`.
    """

    async def verify(self, fqid: str, platform: Any) -> bool:
        """Return True iff *platform* proved ownership of *fqid* via CapAuth."""
        ...


@dataclass
class BindResult:
    """Outcome of one ``/bind`` attempt.

    Attributes:
        ok: True when the binding was challenge-verified and persisted.
        fqid: The parsed sovereign FQID (``None`` when parsing failed).
        reason: Machine-readable failure reason when ``ok`` is False — one of
            ``"not_a_command"`` / ``"missing_fqid"`` / ``"bad_fqid"`` /
            ``"challenge_failed"`` / ``"verifier_error"``.  ``None`` on success.
    """

    ok: bool
    fqid: Optional[str] = None
    reason: Optional[str] = None


class FqidBindingStore:
    """A minimal, restart-durable ``canonical_key → fqid`` map (YAML-backed).

    Plain-text YAML keyed by the skcomms ``PlatformIdentity.canonical_key``
    (``"telegram:user:123"``).  Reads/writes are guarded by a lock so the store
    is safe to share across the adapter inbound tasks.  When ``path`` is
    ``None`` the store is purely in-memory (used by tests that do not care
    about persistence).
    """

    def __init__(self, path: Optional[Path] = None) -> None:
        self._path = Path(path) if path is not None else None
        self._lock = threading.Lock()
        self._map: dict[str, str] = {}
        if self._path is not None and self._path.exists():
            self._load()

    def _load(self) -> None:
        try:
            raw = yaml.safe_load(self._path.read_text(encoding="utf-8")) or {}
        except Exception as exc:  # corrupt store must not crash startup
            logger.warning("adapter_bind: could not load %s: %s", self._path, exc)
            return
        if isinstance(raw, dict):
            self._map = {str(k): str(v) for k, v in raw.items()}

    def _flush(self) -> None:
        if self._path is None:
            return
        self._path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self._path.with_suffix(self._path.suffix + ".tmp")
        tmp.write_text(
            yaml.safe_dump(self._map, default_flow_style=False, sort_keys=True),
            encoding="utf-8",
        )
        tmp.replace(self._path)  # atomic on POSIX

    def get(self, canonical_key: str) -> Optional[str]:
        """Return the FQID bound to *canonical_key*, or None."""
        with self._lock:
            return self._map.get(canonical_key)

    def put(self, canonical_key: str, fqid: str) -> None:
        """Persist a binding (overwrites any existing one)."""
        with self._lock:
            self._map[canonical_key] = fqid
            self._flush()

    def all(self) -> dict[str, str]:
        """Return a copy of the full mapping."""
        with self._lock:
            return dict(self._map)


def parse_bind_command(text: str) -> tuple[bool, Optional[str], Optional[str]]:
    """Parse a ``/bind <fqid>`` command.

    Args:
        text: The raw message text.

    Returns:
        ``(is_command, fqid, reason)``:
          * ``is_command`` — True when the text starts with ``/bind``.
          * ``fqid`` — the validated FQID, or ``None``.
          * ``reason`` — failure reason when ``fqid`` is ``None`` and it *was*
            a command (``"missing_fqid"`` / ``"bad_fqid"``); else ``None``.
    """
    stripped = (text or "").strip()
    parts = stripped.split()
    if not parts or parts[0].lower() != BIND_COMMAND:
        return False, None, "not_a_command"
    if len(parts) < 2 or not parts[1].strip():
        return True, None, "missing_fqid"
    candidate = parts[1].strip()
    if not _FQID_RE.match(candidate):
        return True, None, "bad_fqid"
    return True, candidate, None


class AdapterBinder:
    """Drives the CapAuth ``/bind`` flow against an injected adapter + verifier.

    Args:
        adapter: A skcomms ``ChannelAdapter`` exposing
            ``async bind_fqid(platform_identity, fqid, trust_level)``.
        verifier: A :class:`CapAuthVerifier` (mock in tests, real PGP gate in
            production).  When ``None``, every bind fails ``"verifier_error"``
            — we never bind an identity without a passed challenge.
        store: Optional :class:`FqidBindingStore` for restart-durable mirroring
            of successful bindings.  When ``None`` no local mirror is kept (the
            adapter remains the system of record).
    """

    def __init__(
        self,
        adapter: Any,
        *,
        verifier: Optional[CapAuthVerifier] = None,
        store: Optional[FqidBindingStore] = None,
    ) -> None:
        self._adapter = adapter
        self._verifier = verifier
        self._store = store

    async def bind(self, platform: Any, text: str) -> BindResult:
        """Run the full ``/bind`` flow for one platform message.

        Pipeline: parse → challenge (CapAuth) → ``adapter.bind_fqid`` →
        mirror to local store.  Any non-command text short-circuits with
        ``ok=False, reason="not_a_command"`` so callers can cheaply test every
        message.

        Args:
            platform: The skcomms ``PlatformIdentity`` of the requester.
            text: The raw message text (expected ``"/bind <fqid>"``).

        Returns:
            A :class:`BindResult`.
        """
        is_cmd, fqid, reason = parse_bind_command(text)
        if not is_cmd:
            return BindResult(ok=False, reason="not_a_command")
        if fqid is None:
            return BindResult(ok=False, reason=reason)

        if self._verifier is None:
            logger.warning("adapter_bind: no verifier configured; refusing bind")
            return BindResult(ok=False, fqid=fqid, reason="verifier_error")

        try:
            verified = await self._verifier.verify(fqid, platform)
        except Exception as exc:
            logger.error("adapter_bind: CapAuth verifier raised: %s", exc)
            return BindResult(ok=False, fqid=fqid, reason="verifier_error")

        if not verified:
            return BindResult(ok=False, fqid=fqid, reason="challenge_failed")

        # Challenge passed → persist via the adapter (system of record) and
        # mirror locally so the binding survives a skchat restart.
        await self._adapter.bind_fqid(platform, fqid, TRUST_VERIFIED)
        if self._store is not None:
            key = getattr(platform, "canonical_key", None)
            if key:
                self._store.put(key, fqid)

        logger.info("adapter_bind: bound %s → %s", _key(platform), fqid)
        return BindResult(ok=True, fqid=fqid)


def _key(platform: Any) -> str:
    """Best-effort canonical key for logging."""
    return getattr(platform, "canonical_key", None) or str(platform)
