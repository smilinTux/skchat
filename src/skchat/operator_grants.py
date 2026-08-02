"""Grant an enrolled operator device the ``skchat.prekey`` capability.

Task 7 (multi-device DM fanout): after a device completes the operator
enrollment handshake it can obtain a valid operator session, but the authz PDP
(:func:`capauth.authz.decide`, wired into skchat's ``dataplane_auth`` under
``SKCHAT_AUTHZ_PDP=enforce``) still denies ``POST /api/v1/prekey`` unless there
is a cryptographic fact granting that subject the prekey-publish capability.
This is the exact gap that stopped Chef's web device link from taking on
2026-08-02: authentication passed, authorization did not.

``decide`` allows ``skchat.prekey`` only when BOTH hold for the subject
``operator:<device_fp>``:

  1. a non-revoked pairing ``DeviceRecord`` whose enrollment mode is at least
     ``attested`` (prekey binds key material to the identity), and
  2. an active, non-revoked capability token granting ``skchat.prekey``.

Operator enrollment is already operator-gated (loopback/tailnet or the operator
token) AND device-signature verified over the window nonce, so recording the
device at ``attested`` is the honest mode: the operator vouched for it. The grant
is deliberately scoped to ``skchat.prekey`` ONLY. ``skchat.send`` requires
``verified`` and so remains denied for an attested device (least privilege), and
no ``skchat.inbox`` grant is minted here.

Every call is BEST-EFFORT and mirrors :mod:`skchat.pairing_mirror`: any capauth
error is logged and swallowed so a grant failure can never break the enrollment
response. Both the pairing record and the token are written under the SAME
``capauth`` storage root the PDP reads (``default_base_dir()`` /
``~/.skcapstone``), so what the grant writes is what ``decide`` sees.
"""

from __future__ import annotations

import logging

from .dataplane_auth import operator_subject

logger = logging.getLogger("skchat.operator_grants")

#: The single capability an enrolled operator device is granted here. Scoped to
#: prekey-publish on purpose; send (verified-only) and inbox are not minted.
PREKEY_CAPABILITY = "skchat.prekey"

#: Enrollment mode recorded for the operator device. ``skchat.prekey`` requires
#: at least ``attested``; the operator-gated, signature-verified handshake earns it.
_GRANT_MODE = "attested"


def grant_operator_prekey_capability(device_fp: str, device_pubkey_b64: str) -> bool:
    """Grant ``skchat.prekey`` to the ``operator:<device_fp>`` subject.

    Records an attested pairing device for the subject AND issues a capability
    token granting ``skchat.prekey``, both under capauth's default storage root
    (the same root the authz PDP reads). Returns True on success, False if any
    step failed (best-effort: the failure is logged and swallowed, never raised).
    """
    if not device_fp or not device_pubkey_b64:
        return False

    subject = operator_subject(device_fp)
    try:
        from capauth.pairing import approve, default_base_dir, enroll_device
        from capauth.tokens import issue_token

        base = default_base_dir()
        enrollment = enroll_device(
            device_pubkey_b64,
            [PREKEY_CAPABILITY],
            mode=_GRANT_MODE,
            subject=subject,
            base_dir=base,
        )
        approve(enrollment.enrollment_id, "skchat", base_dir=base)
        # The PDP checks only presence/activity/revocation of the grant, not the
        # token signature, so an unsigned token is sufficient and avoids a hard
        # dependency on a signing key being resident.
        issue_token(base, subject, [PREKEY_CAPABILITY], sign=False)
        return True
    except Exception:
        logger.warning(
            "operator prekey-capability grant failed for %s (best-effort)",
            subject,
            exc_info=True,
        )
        return False


__all__ = ["grant_operator_prekey_capability", "PREKEY_CAPABILITY"]
