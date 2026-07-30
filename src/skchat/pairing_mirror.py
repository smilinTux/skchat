"""Mirror skchat's guest trust store into capauth.pairing (M2 guest-store fold).

When the pairing kernel is enabled (``SKCHAT_PAIRING_KERNEL``, default ON), every
guest admission, operator trust, and pin revocation is ALSO recorded in
``capauth.pairing`` so the kernel becomes the durable, cross-front-door record of
who is trusted (the source the authz PDP reads), while skchat's SQLite keeps
serving its reads byte-identically as the local cache.

Every mirror call is BEST-EFFORT: any capauth error is logged and swallowed, so a
mirror failure can never break live guest admission. This is the safe first stage
of the fold (dual-write, SQLite still authoritative for reads); retiring SQLite as
a source of truth is a later stage once capauth-vs-SQLite parity is proven.
"""

from __future__ import annotations

import logging
import os
from typing import Optional

from .pairing_gate import kernel_enabled

logger = logging.getLogger("skchat.pairing_mirror")

#: Default scopes a guest device is granted (chat read/send). The authz PDP can
#: later require a minimum enrollment mode per scope; the mode carries the trust.
_DEFAULT_SCOPES = ["chat.read", "chat.send"]


def _base_dir() -> Optional[str]:
    """Capauth storage root for the mirror, or None for the capauth default.

    ``SKCHAT_PAIRING_KERNEL_BASE`` lets tests point the mirror at a tmp home so
    they never touch the real ``~/.skcapstone`` pairing store.
    """
    return os.getenv("SKCHAT_PAIRING_KERNEL_BASE") or None


def mirror_admission(
    peer_fp: str, operator_id: Optional[str], peer_pubkey: Optional[str] = None
) -> None:
    """Record a TOFU admission (guest_accept Mode C) in capauth.pairing."""
    if not kernel_enabled() or not peer_fp:
        return
    try:
        from capauth.pairing import approve, enroll_device

        base = _base_dir()
        enr = enroll_device(
            peer_pubkey or peer_fp,
            list(_DEFAULT_SCOPES),
            mode="tofu",
            subject=peer_fp,
            operator_id=operator_id or None,
            base_dir=base,
        )
        approve(enr.enrollment_id, "skchat", base_dir=base)
    except Exception:
        logger.debug("capauth pairing mirror (admission) failed", exc_info=True)


def mirror_trusted_operator(operator_id: str, operator_pubkey: str) -> None:
    """Record an opt-in trusted operator (guest_accept Mode B) in capauth.pairing."""
    if not kernel_enabled() or not operator_id or not operator_pubkey:
        return
    try:
        from capauth.pairing import approve, enroll_device

        base = _base_dir()
        enr = enroll_device(
            operator_pubkey,
            list(_DEFAULT_SCOPES),
            mode="attested",
            subject=operator_id,
            operator_id=operator_id,
            operator_pubkey=operator_pubkey,
            base_dir=base,
        )
        approve(enr.enrollment_id, "skchat", base_dir=base)
    except Exception:
        logger.debug("capauth pairing mirror (trusted operator) failed", exc_info=True)


def mirror_revocation(pin: str) -> None:
    """Revoke every capauth device whose subject is this pin (peer_fp or op id)."""
    if not kernel_enabled() or not pin:
        return
    try:
        from capauth.pairing import list_devices, revoke

        base = _base_dir()
        for dev in list_devices(subject=pin, base_dir=base):
            if not dev.revoked:
                revoke(dev.device_id, "skchat pin revoked", base_dir=base)
    except Exception:
        logger.debug("capauth pairing mirror (revocation) failed", exc_info=True)


__all__ = ["mirror_admission", "mirror_trusted_operator", "mirror_revocation"]
