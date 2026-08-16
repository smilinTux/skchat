"""Mirror skchat's guest trust store into capauth.pairing (M2 guest-store fold).

When the pairing kernel is enabled (``SKCHAT_PAIRING_KERNEL``, default ON), every
guest admission, operator trust, and pin revocation is ALSO recorded in
``capauth.pairing`` so the kernel becomes the durable, cross-front-door record of
who is trusted (the source the authz PDP reads), while skchat's SQLite keeps
serving its reads byte-identically as the local cache.

Every mirror call is BEST-EFFORT: any capauth error is logged and swallowed, so a
mirror failure can never break live guest admission. It is logged at WARNING, not
the DEBUG it used to use: a swallowed mirror failure means capauth's record and
skchat's SQLite have silently diverged, which is invisible at every later read,
and DEBUG is why the card-N10 ``attested`` breakage below went unnoticed.

This is the safe first stage of the fold (dual-write, SQLite still authoritative
for reads); retiring SQLite as a source of truth is a later stage once
capauth-vs-SQLite parity is proven.
"""

from __future__ import annotations

import logging
import os
import re
from typing import Optional

from .pairing_gate import kernel_enabled

logger = logging.getLogger("skchat.pairing_mirror")

#: Default scopes a guest device is granted (chat read/send). The authz PDP can
#: later require a minimum enrollment mode per scope; the mode carries the trust.
_DEFAULT_SCOPES = ["chat.read", "chat.send"]

#: A skchat peer pin that is a bare PGP fingerprint. capauth's fqid grammar
#: (IDENTITY_NAMING_STANDARD.md sec 1) admits exactly two shapes, and a bare
#: fingerprint is NEITHER: it needs the ``device:`` seat prefix. The bound
#: matches capauth's own ``device:[0-9a-f]{16,64}`` so the two cannot drift.
_BARE_FINGERPRINT_RE = re.compile(r"[0-9a-fA-F]{16,64}")


def _capauth_subject(pin: str) -> str:
    """Map a skchat pin to capauth's canonical subject form.

    A skchat pin is either a peer PGP fingerprint (a device seat) or an
    operator fqid; :meth:`GuestTrustStore.revoke_pin` takes either. capauth's
    grammar admits only ``device:<16-64 hex>`` or ``<local>@...<org-domain>``,
    and since the canonical-subject work on capauth main it REFUSES anything
    else outright rather than storing it verbatim as 0.3.1 did.

    ALL THREE mirror paths must agree on this mapping, which is why it lives in
    one function instead of at each call site. If admission enrolls under
    ``device:<fp>`` while revocation looks up a bare ``<fp>``,
    :func:`capauth.pairing.list_devices` matches nothing and the revoke is a
    silent no-op. That is the one failure mode here that loses a security
    property rather than a record, and nothing downstream would report it.

    Raises:
        capauth.exceptions.SubjectNamingError: ``pin`` is neither a bare
            fingerprint nor a canonicalizable fqid. Callers swallow this into a
            WARNING; it means capauth and skchat's SQLite have diverged for
            this pin, not that the pin is untrusted.
    """
    from capauth.subject import canonical_subject

    if _BARE_FINGERPRINT_RE.fullmatch(pin):
        return canonical_subject(f"device:{pin}")
    return canonical_subject(pin)


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
            subject=_capauth_subject(peer_fp),
            operator_id=operator_id or None,
            base_dir=base,
        )
        approve(enr.enrollment_id, "skchat", base_dir=base)
    except Exception:
        logger.warning("capauth pairing mirror (admission) failed", exc_info=True)


def mirror_trusted_operator(
    operator_id: str, operator_pubkey: str, attestation: Optional[str] = None
) -> None:
    """Record an opt-in trusted operator (guest_accept Mode B) in capauth.pairing.

    ``attested`` means a VOUCHING OPERATOR signed
    :func:`capauth.pairing.attested_challenge`'s bytes for this device/identity
    pair. Since capauth 0.3.0 (card N10) that signature is actually checked, and
    this call used to ask for ``attested`` with an ``operator_pubkey`` but no
    ``attestation`` at all, inside a ``logger.debug`` handler. The result was a
    silent no-op: skchat's SQLite recorded the trust and capauth recorded
    nothing, so the PDP never saw the operator this whole mirror exists to
    publish.

    There is no attestation to supply on this path and none can be manufactured.
    :meth:`skchat.guest_accept.GuestTrustStore.trust_operator` records a remote
    operator's PUBLIC key because the local operator chose to trust it; skchat
    holds no private key of that operator's, and its own key vouching for someone
    else's identity is not what ``attested`` asserts. "Pin a key the operator
    chose to trust, on first sight" is the definition of ``tofu``, so that is the
    honest floor, taken loudly rather than silently. A caller that genuinely
    holds an attestation passes it and gets the real tier.
    """
    if not kernel_enabled() or not operator_id or not operator_pubkey:
        return
    try:
        from capauth.pairing import approve, enroll_device
        from capauth.pairing.kernel import PairingError

        base = _base_dir()
        enr = None
        if attestation:
            try:
                enr = enroll_device(
                    operator_pubkey,
                    list(_DEFAULT_SCOPES),
                    mode="attested",
                    subject=_capauth_subject(operator_id),
                    operator_id=operator_id,
                    operator_pubkey=operator_pubkey,
                    attestation=attestation,
                    base_dir=base,
                )
            except PairingError as exc:
                logger.error(
                    "trusted operator %s presented an attestation that DID NOT VERIFY; "
                    "refusing to record 'attested' on an unproven claim, recording "
                    "'tofu' instead: %s",
                    operator_id,
                    exc,
                )
        if enr is None:
            if not attestation:
                logger.warning(
                    "trusted operator %s mirrored as 'tofu', NOT 'attested': no "
                    "attestation over capauth's attested challenge was available, and "
                    "only that operator's own key can produce one (card N10). The authz "
                    "PDP will deny this subject any capability above the tofu tier.",
                    operator_id,
                )
            enr = enroll_device(
                operator_pubkey,
                list(_DEFAULT_SCOPES),
                mode="tofu",
                subject=_capauth_subject(operator_id),
                operator_id=operator_id,
                operator_pubkey=operator_pubkey,
                base_dir=base,
            )
        approve(enr.enrollment_id, "skchat", base_dir=base)
    except Exception:
        logger.warning("capauth pairing mirror (trusted operator) failed", exc_info=True)


def mirror_revocation(pin: str) -> None:
    """Revoke every capauth device whose subject is this pin (peer_fp or op id).

    Deliberately passes the RAW pin, not :func:`_capauth_subject` of it.
    :func:`capauth.pairing.list_devices` already matches a subject filter
    against both the raw spelling as given and, when it canonicalizes, the
    canonical fqid (capauth card N3). So a bare fingerprint here still finds
    the ``device:<fp>`` record that :func:`mirror_admission` enrolled, AND a
    legacy record stored verbatim under a non-canonical subject still matches
    its raw form. Canonicalizing first would actually be a REGRESSION: it
    would stop matching those legacy raw records, which are precisely the
    oldest and longest-trusted ones. Migrating them is capauth's own job, via
    :func:`capauth.pairing.canonicalize.apply_canonical_rewrite`.
    """
    if not kernel_enabled() or not pin:
        return
    try:
        from capauth.pairing import list_devices, revoke

        base = _base_dir()
        for dev in list_devices(subject=pin, base_dir=base):
            if not dev.revoked:
                revoke(dev.device_id, "skchat pin revoked", base_dir=base)
    except Exception:
        logger.warning("capauth pairing mirror (revocation) failed", exc_info=True)


__all__ = ["mirror_admission", "mirror_trusted_operator", "mirror_revocation"]
