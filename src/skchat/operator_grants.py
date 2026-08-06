"""Grant an enrolled operator device its skchat capabilities (prekey + inbox).

Task 7 (multi-device DM fanout): after a device completes the operator
enrollment handshake it can obtain a valid operator session, but the authz PDP
(:func:`capauth.authz.decide`, wired into skchat's ``dataplane_auth`` under
``SKCHAT_AUTHZ_PDP=enforce``) still denies a request unless there is a
cryptographic fact granting that subject the requested capability. This is the
exact gap that stopped Chef's web device link from taking on 2026-08-02:
authentication passed, authorization did not.

``decide`` allows a capability only when BOTH hold for the subject
``operator:<device_fp>``:

  1. a non-revoked pairing ``DeviceRecord`` whose enrollment mode is at least the
     capability's ``minimum_mode``, and
  2. an active, non-revoked capability token granting that capability.

Operator enrollment is already operator-gated (loopback/tailnet or the operator
token) AND device-signature verified over the window nonce, so recording the
device at ``attested`` is the honest mode: the operator vouched for it.

This grants two capabilities (CR-3.1, closing the 227k-hit PDP-shadow divergence
that blocked the enforce flip):

  * ``skchat.prekey`` (min mode ``attested``) - publish a prekey bundle.
  * ``skchat.inbox`` (min mode ``TOFU``, the LEAST-sensitive capability, "read the
    subject's own inbox") - the operator seat polls its own inbox, so the PDP must
    grant it or every poll diverges from the legitimate legacy allow.

``skchat.send`` requires ``verified`` and is deliberately NOT minted here (least
privilege). The tokens are issued **non-expiring** (``ttl_hours=None``): an
operator device is a persistent, revocable trust anchor, and a short TTL is what
left prekey grants intermittently expired (the residual prekey divergences).

Every call is BEST-EFFORT and mirrors :mod:`skchat.pairing_mirror`: any capauth
error is logged and swallowed so a grant failure can never break the enrollment
response. Both the pairing record and the tokens are written under the SAME
``capauth`` storage root the PDP reads (``default_base_dir()`` / ``~/.skcapstone``),
so what the grant writes is what ``decide`` sees.
"""

from __future__ import annotations

import logging

from .dataplane_auth import operator_subject

logger = logging.getLogger("skchat.operator_grants")

#: Prekey-publish capability (min enrollment mode ``attested``).
PREKEY_CAPABILITY = "skchat.prekey"

#: Read-own-inbox capability: the least-sensitive rule (min mode ``TOFU``). The
#: operator seat polls its own inbox, so this must be granted for the authz PDP to
#: agree with the legitimate legacy allow.
INBOX_CAPABILITY = "skchat.inbox"

#: The capabilities an enrolled operator device is granted. ``skchat.send``
#: (verified-only) is deliberately excluded (least privilege).
OPERATOR_CAPABILITIES = [PREKEY_CAPABILITY, INBOX_CAPABILITY]

#: Enrollment mode recorded for the operator device. ``skchat.prekey`` requires
#: at least ``attested``; the operator-gated, signature-verified handshake earns it
#: (and ``attested`` also satisfies ``skchat.inbox``'s ``TOFU`` minimum).
_GRANT_MODE = "attested"


def grant_operator_capabilities(device_fp: str, device_pubkey_b64: str) -> bool:
    """Grant ``skchat.prekey`` + ``skchat.inbox`` to ``operator:<device_fp>``.

    Records an attested pairing device for the subject AND issues a non-expiring
    capability token granting both capabilities, both under capauth's default
    storage root (the same root the authz PDP reads). Returns True on success,
    False if any step failed (best-effort: logged and swallowed, never raised).
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
            OPERATOR_CAPABILITIES,
            mode=_GRANT_MODE,
            subject=subject,
            base_dir=base,
        )
        approve(enrollment.enrollment_id, "skchat", base_dir=base)
        # The PDP checks only presence/activity/revocation of the grant, not the
        # token signature, so an unsigned token is sufficient and avoids a hard
        # dependency on a signing key being resident. Non-expiring: a persistent
        # (revocable) operator device should not need a daily re-grant.
        issue_token(base, subject, OPERATOR_CAPABILITIES, ttl_hours=None, sign=False)
        return True
    except Exception:
        logger.warning(
            "operator capability grant failed for %s (best-effort)",
            subject,
            exc_info=True,
        )
        return False


#: Back-compat alias: the enrollment route imports this name.
grant_operator_prekey_capability = grant_operator_capabilities


def backfill_operator_capabilities(base_dir=None) -> int:
    """Grant ``skchat.inbox`` (+ non-expiring prekey) to every already-enrolled
    operator device that is missing it. Idempotent; returns the number of
    subjects updated. Use once after deploying the inbox grant to reconcile
    devices enrolled before it (they carry prekey-only, so their inbox polls
    diverge under the PDP).
    """
    try:
        from capauth.pairing import default_base_dir
        from capauth.tokens import issue_token, list_tokens
    except Exception:
        logger.warning("backfill: capauth unavailable", exc_info=True)
        return 0

    base = base_dir if base_dir is not None else default_base_dir()
    # Every enrolled operator subject (from ANY token, active or expired), and the
    # subset that already holds an ACTIVE inbox grant. The polling device that
    # diverges is typically one whose short-TTL token expired, so discovery must
    # NOT filter on active - only the "already covered" check does.
    all_operators: set[str] = set()
    has_active_inbox: set[str] = set()
    for t in list_tokens(base):
        subj = getattr(t.payload, "subject", None)
        if not subj or not str(subj).startswith("operator:"):
            continue
        all_operators.add(subj)
        caps = getattr(t.payload, "capabilities", []) or []
        if t.payload.is_active and INBOX_CAPABILITY in caps:
            has_active_inbox.add(subj)

    updated = 0
    for subject in all_operators - has_active_inbox:
        try:
            issue_token(base, subject, OPERATOR_CAPABILITIES, ttl_hours=None, sign=False)
            updated += 1
        except Exception:
            logger.warning("backfill: grant failed for %s", subject, exc_info=True)
    return updated


__all__ = [
    "grant_operator_capabilities",
    "grant_operator_prekey_capability",
    "backfill_operator_capabilities",
    "PREKEY_CAPABILITY",
    "INBOX_CAPABILITY",
    "OPERATOR_CAPABILITIES",
]
