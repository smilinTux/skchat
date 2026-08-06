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

#: The operator-seat bundle that is grantable TODAY at the device's ``attested``
#: enrollment (SKWorld Authorization Model L2.4). Every capability here has a
#: minimum mode of TOFU or ATTESTED, so ``decide`` allows it for an attested
#: operator device with no pairing change. ``skchat.prekey`` + ``skchat.inbox``
#: are the shipped grants; ``skchat.status`` (read metadata), ``skchat.media.write``
#: (uploads), and ``skchat.voice`` (STT/TTS) are the newly-minted additions.
OPERATOR_CAPABILITIES = [
    PREKEY_CAPABILITY,
    INBOX_CAPABILITY,
    STATUS_CAPABILITY,
    MEDIA_WRITE_CAPABILITY,
    VOICE_CAPABILITY,
]

#: DEFERRED operator grants (SKWorld Authorization Model L2.4): the verified-tier
#: capabilities the operator's own app traffic exercises (``skchat.send`` POSTs,
#: group creation, placing calls). They are NOT minted here because an operator
#: device enrolls ``attested`` while these require ``verified``. Granting them
#: needs the "operator ceremony == verified" pairing change (its own review +
#: backfill, Chef's call), which this work deliberately does NOT touch. Recorded
#: as a constant so the deferral is explicit and diff-reviewable, not a silent gap.
OPERATOR_DEFERRED_CAPABILITIES = [SEND_CAPABILITY, GROUPS_CAPABILITY, CALLS_CAPABILITY]

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

#: Enrollment mode recorded for the operator device. ``skchat.prekey`` requires
#: at least ``attested``; the operator-gated, signature-verified handshake earns it
#: (and ``attested`` also satisfies ``skchat.inbox``'s ``TOFU`` minimum, plus the
#: TOFU/ATTESTED floors of ``status``/``media.write``/``voice``).
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
