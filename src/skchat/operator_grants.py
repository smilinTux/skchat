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

#: The five new capabilities (SKWorld Authorization Model L2.2, 2026-08-06).
STATUS_CAPABILITY = "skchat.status"  # read metadata (min mode TOFU)
MEDIA_WRITE_CAPABILITY = "skchat.media.write"  # upload attachment bytes (min mode ATTESTED)
VOICE_CAPABILITY = "skchat.voice"  # STT/TTS compute (min mode ATTESTED)
SEND_CAPABILITY = "skchat.send"  # act on the wire (min mode VERIFIED)
GROUPS_CAPABILITY = "skchat.groups"  # mutate shared group state (min mode VERIFIED)
CALLS_CAPABILITY = "skchat.calls"  # ring/join/mint LiveKit tokens (min mode VERIFIED)

#: The full operator-seat bundle (SKWorld Authorization Model L2.4; Option A
#: adopted 2026-08-06). An operator device is enrolled ``verified`` (see
#: ``_GRANT_MODE``) because the operator pairing ceremony IS a verification event,
#: so the operator holds all eight skchat capabilities, including the verified-tier
#: ``skchat.send`` / ``skchat.groups`` / ``skchat.calls`` its own app traffic
#: exercises. IMPORTANT: verified MODE is necessary-not-sufficient, each capability
#: still requires its own token issued here, so elevating the device mode does NOT
#: over-grant beyond this explicit bundle (the operator gains no skcode/other
#: verified-tier capability without a token for it).
OPERATOR_CAPABILITIES = [
    PREKEY_CAPABILITY,
    INBOX_CAPABILITY,
    STATUS_CAPABILITY,
    MEDIA_WRITE_CAPABILITY,
    VOICE_CAPABILITY,
    SEND_CAPABILITY,
    GROUPS_CAPABILITY,
    CALLS_CAPABILITY,
]

#: No operator grants are deferred now (Option A adopted): the operator ceremony
#: earns ``verified``, so the verified-tier caps are in the bundle above. Kept as
#: an empty constant for back-compat with callers/tests that reference it.
OPERATOR_DEFERRED_CAPABILITIES: list[str] = []

#: The full agent bundle (subjects like ``lumina@chef.skworld`` / ``opus@chef.skworld``),
#: which enroll ``verified`` and so hold all eight capabilities (L2.4). The base
#: three (``send``/``inbox``/``prekey``) already live in the token store; the other
#: five are the proposed additions the agent backfill tops up.
AGENT_CAPABILITIES = [
    SEND_CAPABILITY,
    INBOX_CAPABILITY,
    PREKEY_CAPABILITY,
    STATUS_CAPABILITY,
    MEDIA_WRITE_CAPABILITY,
    VOICE_CAPABILITY,
    GROUPS_CAPABILITY,
    CALLS_CAPABILITY,
]

#: Enrollment mode recorded for the operator device (Option A, 2026-08-06):
#: ``verified``. The operator pairing ceremony is operator-gated (loopback/tailnet
#: or the operator token) AND the device signs over a fresh operator-issued window
#: nonce, cryptographic proof of key possession under sovereign authorization,
#: which IS verification. This lets the operator hold the verified-tier caps
#: (send/groups/calls). Only the full ceremony earns ``verified``; bare TOFU and
#: guest enrollment paths keep their own (lower) modes.
_GRANT_MODE = "verified"


def grant_operator_capabilities(
    device_fp: str, device_pubkey_b64: str, proof: str | None = None
) -> bool:
    """Grant the full operator capability bundle to ``operator:<device_fp>``.

    Records a ``verified`` pairing device for the subject AND issues a
    non-expiring capability token granting :data:`OPERATOR_CAPABILITIES`, both
    under capauth's default storage root (the same root the authz PDP reads).
    Returns True on success, False if any step failed (best-effort: logged and
    swallowed, never raised, per this module's docstring).

    ``proof`` (inc-c72a9120, capauth card N10 / ``83c1fa2``): capauth's
    ``enroll_device`` requires real evidence of device-key possession for
    ``mode="verified"`` -- a signature, made by the device's own key, over
    ``capauth.pairing.verified_challenge(fingerprint, canonical_subject)``.
    ``proof`` is that signature (base64, WebCrypto P1363 ``r||s`` or DER),
    produced by the device itself and threaded through unmodified from the
    enrollment route (:mod:`skchat.operator_auth_routes`) to
    ``enroll_device``. This function never computes or checks the challenge
    itself; capauth is the one source of truth for what "verified" requires.

    A caller with no proof (an older client, or one that failed to sign) is
    refused OUTRIGHT here, before ever calling capauth: not silently granted
    (the original bug -- enroll_device raised, the grant failed, and NOTHING
    told the operator), and not silently downgraded to a weaker mode either
    (``skchat.send``/``groups``/``calls`` are min-mode VERIFIED, so a `tofu`
    device-record would just make ``decide()`` deny those later, for a reason
    that looks unrelated to "no proof was ever presented"). The refusal is
    logged at ERROR, naming the subject (which embeds ``device_fp``), so it is
    visible in the daemon's own logs even before anyone checks
    ``skchat devices ...`` or the web Linked Devices list -- the caller
    (``operator_auth_routes.enroll``) also records this outcome onto the
    device's registry row via ``device_registry.record_grant_result`` so BOTH
    surfaces show a device that enrolled but holds zero capabilities, rather
    than looking indistinguishable from a fully working one.
    """
    if not device_fp or not device_pubkey_b64:
        return False

    subject = operator_subject(device_fp)

    if not proof:
        logger.error(
            "operator capability grant REFUSED for %s: no enrollment proof was "
            "presented (older client, or it failed to sign); this device is "
            "enrolled but holds ZERO skchat capabilities (no send/groups/calls/"
            "prekey/inbox) until it re-links from a client that signs a "
            "verified-mode proof (inc-c72a9120, card N10)",
            subject,
        )
        return False

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
            proof=proof,
        )
        approve(enrollment.enrollment_id, "skchat", base_dir=base)
        # The PDP checks only presence/activity/revocation of the grant, not the
        # token signature, so an unsigned token is sufficient and avoids a hard
        # dependency on a signing key being resident. Non-expiring: a persistent
        # (revocable) operator device should not need a daily re-grant.
        issue_token(base, subject, OPERATOR_CAPABILITIES, ttl_hours=None, sign=False)
        return True
    except Exception:
        # ERROR, not WARNING: this device is left with ZERO skchat
        # capabilities (send/groups/calls/prekey/inbox all denied), and a
        # WARNING is exactly the level that let the original bug go unnoticed.
        logger.error(
            "operator capability grant failed for %s (device holds ZERO "
            "skchat capabilities; inc-c72a9120)",
            subject,
            exc_info=True,
        )
        return False


#: Back-compat alias: the enrollment route imports this name.
grant_operator_prekey_capability = grant_operator_capabilities


def backfill_operator_capabilities(base_dir=None) -> int:
    """Reconcile every already-enrolled operator device to the CURRENT model:
    elevate its device record to ``verified`` (Option A) and re-issue the full
    non-expiring :data:`OPERATOR_CAPABILITIES` bundle. Idempotent; returns the
    number of subjects updated.

    Elevation matters because the PDP checks the DEVICE RECORD's enrollment mode
    (not the token) against each capability's minimum mode, so the verified-tier
    caps (send/groups/calls) only pass once the device record itself is verified.
    Re-enrolling with the device's stored pubkey upserts that record in place
    (``store.upsert_device``), it does not create a duplicate or a new device.
    Best-effort per subject; a capauth error is logged and skipped, never raised.

    ⚠ Pre-existing gap, NOT addressed here (out of scope for inc-c72a9120 part
    2): this re-enroll passes no ``proof``, so under card N10 it will now fail
    closed for every subject the same way the live enrollment path did before
    this fix -- it just never had a device online to sign one, since this is a
    batch reconciliation over ALREADY-enrolled records, not a live handshake.
    Backfilling a device that predates the proof requirement needs either a
    stored/re-derivable proof or the device itself back online to re-sign; this
    function does neither yet.
    """
    try:
        from capauth.pairing import (
            approve,
            default_base_dir,
            enroll_device,
            list_devices,
        )
        from capauth.tokens import issue_token, list_tokens
    except Exception:
        logger.warning("backfill: capauth unavailable", exc_info=True)
        return 0

    base = base_dir if base_dir is not None else default_base_dir()
    # Every enrolled operator subject (from ANY token, active or expired).
    all_operators: set[str] = set()
    for t in list_tokens(base):
        subj = getattr(t.payload, "subject", None)
        if subj and str(subj).startswith("operator:"):
            all_operators.add(subj)

    updated = 0
    for subject in all_operators:
        try:
            # Elevate every device record for this operator subject to verified,
            # re-enrolling with its own stored pubkey (upsert in place).
            for dev in list_devices(subject, base_dir=base):
                pubkey = getattr(dev, "pubkey", None)
                if not pubkey:
                    continue
                enrollment = enroll_device(
                    pubkey,
                    OPERATOR_CAPABILITIES,
                    mode=_GRANT_MODE,
                    subject=subject,
                    base_dir=base,
                )
                approve(enrollment.enrollment_id, "skchat", base_dir=base)
            # Re-issue the full non-expiring bundle.
            issue_token(base, subject, OPERATOR_CAPABILITIES, ttl_hours=None, sign=False)
            updated += 1
        except Exception:
            logger.warning("backfill: grant failed for %s", subject, exc_info=True)
    return updated


def grant_agent_capabilities(subject: str, base_dir=None) -> bool:
    """Issue the full agent capability bundle to ``subject`` (token-only, L2.4).

    Agents (``lumina@chef.skworld`` etc.) already enroll ``verified`` on the agent
    enrollment path, so this mints ONLY a non-expiring capability token granting
    :data:`AGENT_CAPABILITIES`; it does NOT enroll a device or touch any enrollment
    mode (that stays the pairing layer's job). Best-effort: any capauth error is
    logged and swallowed, never raised. Returns True on success.
    """
    if not subject:
        return False
    try:
        from capauth.pairing import default_base_dir
        from capauth.tokens import issue_token

        base = base_dir if base_dir is not None else default_base_dir()
        issue_token(base, subject, AGENT_CAPABILITIES, ttl_hours=None, sign=False)
        return True
    except Exception:
        logger.warning(
            "agent capability grant failed for %s (best-effort)",
            subject,
            exc_info=True,
        )
        return False


def backfill_agent_capabilities(subjects=None, base_dir=None) -> int:
    """Top every agent subject up to the full :data:`AGENT_CAPABILITIES` bundle.

    ``subjects`` may be an explicit iterable of agent subject strings. When None,
    agents are auto-discovered as subjects that already hold an ACTIVE token
    granting ``skchat.send`` and are NOT ``operator:`` seats (the live store's
    ``lumina@chef.skworld`` / ``opus@chef.skworld``). A subject already carrying
    every bundle capability in one active token is skipped. Idempotent; returns the
    number of subjects updated. Read-then-grant only, never enrolls or re-modes.
    """
    try:
        from capauth.pairing import default_base_dir
        from capauth.tokens import issue_token, list_tokens
    except Exception:
        logger.warning("agent backfill: capauth unavailable", exc_info=True)
        return 0

    base = base_dir if base_dir is not None else default_base_dir()
    wanted = set(AGENT_CAPABILITIES)

    if subjects is None:
        discovered: set[str] = set()
        for t in list_tokens(base):
            subj = getattr(t.payload, "subject", None)
            if not subj or str(subj).startswith("operator:"):
                continue
            caps = getattr(t.payload, "capabilities", []) or []
            if t.payload.is_active and SEND_CAPABILITY in caps:
                discovered.add(subj)
        subjects = discovered

    # Subjects that already hold the whole bundle in a single active token.
    fully_granted: set[str] = set()
    for t in list_tokens(base):
        subj = getattr(t.payload, "subject", None)
        if not subj or subj not in set(subjects):
            continue
        caps = set(getattr(t.payload, "capabilities", []) or [])
        if t.payload.is_active and wanted <= caps:
            fully_granted.add(subj)

    updated = 0
    for subject in set(subjects) - fully_granted:
        try:
            issue_token(base, subject, AGENT_CAPABILITIES, ttl_hours=None, sign=False)
            updated += 1
        except Exception:
            logger.warning("agent backfill: grant failed for %s", subject, exc_info=True)
    return updated


def audit_grants(subjects, capabilities_by_subject=None, base_dir=None) -> list[tuple]:
    """Grant-audit-by-simulation (L1.5 / L2.4): call ``decide`` for each pair.

    For every subject and every capability its bundle promises, literally invoke
    ``capauth.authz.decide`` (NOT a re-implementation of its logic) and collect the
    denials. ``capabilities_by_subject`` maps a subject to the capability list to
    check; when None each subject is audited against :data:`OPERATOR_CAPABILITIES`
    if it is an ``operator:`` seat, else :data:`AGENT_CAPABILITIES`. Returns a list
    of ``(subject, capability, reason)`` for every DENY (empty == clean audit).
    Catches both missing grants AND insufficient enrollment modes in one pass.
    """
    try:
        from capauth.authz import decide
        from capauth.pairing import default_base_dir
    except Exception:
        logger.warning("grant audit: capauth unavailable", exc_info=True)
        return []

    base = base_dir if base_dir is not None else default_base_dir()
    denials: list[tuple] = []
    for subject in subjects:
        if capabilities_by_subject is not None:
            caps = capabilities_by_subject.get(subject, [])
        elif str(subject).startswith("operator:"):
            caps = OPERATOR_CAPABILITIES
        else:
            caps = AGENT_CAPABILITIES
        for cap in caps:
            try:
                decision = decide(subject, cap, base_dir=base)
            except Exception as exc:  # noqa: BLE001
                denials.append((subject, cap, f"decide() error: {exc}"))
                continue
            if not decision.allow:
                denials.append((subject, cap, decision.reason))
    return denials


__all__ = [
    "grant_operator_capabilities",
    "grant_operator_prekey_capability",
    "backfill_operator_capabilities",
    "grant_agent_capabilities",
    "backfill_agent_capabilities",
    "audit_grants",
    "PREKEY_CAPABILITY",
    "INBOX_CAPABILITY",
    "STATUS_CAPABILITY",
    "MEDIA_WRITE_CAPABILITY",
    "VOICE_CAPABILITY",
    "SEND_CAPABILITY",
    "GROUPS_CAPABILITY",
    "CALLS_CAPABILITY",
    "OPERATOR_CAPABILITIES",
    "OPERATOR_DEFERRED_CAPABILITIES",
    "AGENT_CAPABILITIES",
]
