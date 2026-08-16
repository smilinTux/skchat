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
token) AND device-signature verified over the window nonce. That used to be the
whole argument for recording the device at a raised mode on skchat's say-so.
Since capauth card N10 it is not sufficient by itself: capauth wants a signature
over ITS challenge, and the mode is only recorded as high as the evidence
actually presented proves. See the N10 section at the bottom of this docstring.

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

Card N10 (capauth 0.3.0): the enrollment mode is no longer taken on the caller's
say-so
-------------------------------------------------------------------------------

``enroll_device(mode="verified")`` now REQUIRES a ``proof``: a signature by the
device's own key over :func:`capauth.pairing.verified_challenge`'s bytes. Without
one it raises, and this module used to swallow that into ``return False``, so an
operator grant enrolled nothing, issued no token, and every later ``decide()``
denied the device for "no enrolled device" with nothing pointing at the cause.
Because a Python process holds the capauth it imported at start, that landed on
the next RESTART of each service rather than at upgrade time.

The device CAN produce that proof. capauth's ``_proof_verifies`` dispatches on
key shape and accepts skchat's existing base64 DER SPKI WebCrypto ECDSA P-256
device key, verified through capauth's own ``verify_device_signature`` (the
``enroll_device`` docstring still says "an ASCII-armored PGP signature", which is
stale). So the proof rides in on the enroll request as ``capauth_proof`` and the
grant presents it; see :func:`verified_enrollment_challenge` for the exact bytes.

When no usable proof arrives (any client built before this shipped), the device
is enrolled at ``tofu``, the tier that IS provable, and the downgrade is logged
at WARNING naming every capability it costs. It is NEVER recorded as
``verified``: that would be claiming a tier nobody proved, which is exactly what
N10 exists to stop, and ``verified`` gates ``skcode.dispatch``, remote code
execution as the subject.
"""

from __future__ import annotations

import logging
from typing import NamedTuple, Optional

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

#: The full agent bundle (subjects like ``lumina@chef.skworld.io`` / ``opus@chef.skworld.io``),
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

#: The mode enrolled when the intended tier cannot be PROVEN. Pin-on-first-use
#: needs no upfront evidence, so it is the one tier this path can always defend,
#: and enrolling it is a strict improvement on the N10 breakage (which recorded
#: nothing at all, so even the least-sensitive capability was denied).
_FALLBACK_MODE = "tofu"


class GrantOutcome(NamedTuple):
    """What a grant attempt actually achieved, as opposed to what it asked for.

    ``ok`` is the historical boolean: the device is enrolled and a token was
    issued. ``mode`` is the tier ACTUALLY recorded (never the requested one), and
    ``reason`` explains any shortfall. Callers that only need the boolean keep
    using :func:`grant_operator_capabilities`.
    """

    ok: bool
    mode: Optional[str]
    reason: Optional[str]


def verified_enrollment_challenge(
    device_pubkey_b64: str, *, subject: Optional[str] = None
) -> bytes:
    """The exact bytes a device must sign to enroll ``verified``.

    Re-derives, from the public key alone, what ``enroll_device`` computes
    internally. Two details are easy to get wrong and both make the signature
    fail:

      * the fingerprint is CAPAUTH's (``fingerprint_for``: a 40-char UPPERCASE
        hex), not skchat's 16-char :func:`skchat.operator_auth.device_fingerprint`;
      * the identity is the CANONICALIZED subject, so the ``operator:<fp>``
        skchat passes is recorded, and challenged, as ``device:<fp>``.

    Everything here is a pure function of the device's PUBLIC key, so a client
    that knows its own key can be handed these bytes with no extra round trip and
    no secret disclosed. ``tests/test_verified_enrollment_proof.py`` round-trips
    the result through the real ``enroll_device`` so that if capauth ever changes
    either derivation the suite fails loudly, rather than production sliding
    silently back to the tofu floor.

    capauth has no public helper for this today; if it grows one, delete this and
    call it.
    """
    from capauth.pairing import verified_challenge
    from capauth.pairing.store import fingerprint_for
    from capauth.subject import canonical_subject

    from .operator_auth import device_fingerprint

    subject = subject or operator_subject(device_fingerprint(device_pubkey_b64))
    return verified_challenge(fingerprint_for(device_pubkey_b64), canonical_subject(subject))


def _capabilities_denied_at(subject: str, base) -> list[str]:
    """Which of the operator bundle the PDP will refuse, by ASKING the PDP.

    Invokes the real ``capauth.authz.decide`` rather than re-implementing its
    mode ladder, so the log names exactly what is broken and cannot drift from
    the enforcement it describes.
    """
    try:
        from capauth.authz import decide
    except Exception:  # noqa: BLE001 - a diagnostic must never break the grant
        return []
    denied = []
    for cap in OPERATOR_CAPABILITIES:
        try:
            if not decide(subject, cap, base_dir=base).allow:
                denied.append(cap)
        except Exception:  # noqa: BLE001
            continue
    return denied


def _enroll_at_best_provable_mode(
    device_pubkey_b64: str, subject: str, base, capauth_proof: Optional[str]
) -> tuple[object, str, Optional[str]]:
    """Enroll ``verified`` if the proof checks out, else ``tofu``. Never claims.

    Returns ``(enrollment, mode_recorded, shortfall_reason)``. capauth is the
    only judge of whether the proof is real: this presents it and lets
    ``enroll_device`` refuse, rather than pre-validating it here (a caller that
    picks its own verifier could pick a weak one).
    """
    from capauth.pairing import enroll_device
    from capauth.pairing.kernel import PairingError

    if capauth_proof:
        try:
            enrollment = enroll_device(
                device_pubkey_b64,
                OPERATOR_CAPABILITIES,
                mode=_GRANT_MODE,
                subject=subject,
                base_dir=base,
                proof=capauth_proof,
            )
            return enrollment, _GRANT_MODE, None
        except PairingError as exc:
            # A proof was PRESENTED and REJECTED. That is a bad client, a
            # mismatched capauth, or an attempt to buy a tier: all louder than a
            # client that simply has no proof to offer.
            logger.error(
                "operator device %s presented a capauth enrollment proof that DID NOT "
                "VERIFY; refusing to record '%s' on an unproven claim and enrolling "
                "'%s' instead: %s",
                subject,
                _GRANT_MODE,
                _FALLBACK_MODE,
                exc,
            )
            reason = f"presented proof did not verify ({exc})"
    else:
        reason = "no capauth_proof was supplied by the enrolling client"

    enrollment = enroll_device(
        device_pubkey_b64,
        OPERATOR_CAPABILITIES,
        mode=_FALLBACK_MODE,
        subject=subject,
        base_dir=base,
    )
    return enrollment, _FALLBACK_MODE, reason


def _issue_bundle_token(base, subject: str) -> bool:
    """Mint the operator's capability token, SIGNED if this box can sign.

    The grant used to pass ``sign=False`` unconditionally, with the comment "the
    PDP checks only presence/activity/revocation of the grant, not the token
    signature". That stopped being true: ``capauth.authz.decide`` now rejects a
    token that carries no signature ("is unsigned: no signature is present"), and
    capauth's own ``issue_token`` docstring says so outright, so an unsigned
    token authorizes NOTHING. Same failure class as the mode bug above and on the
    same code path: something was issued, nothing was granted, and no error was
    raised.

    ``sign=True`` raises when no key is resident, which is exactly why the
    original avoided it, so this attempts the real thing and falls back rather
    than making a working grant depend on a keyring. The fallback is never worse
    than the previous behavior (always unsigned) and is loud, because an unsigned
    token authorizes nothing unless the operator has explicitly set capauth's
    time-boxed ``CAPAUTH_LEGACY_UNSIGNED_GRACE_UNTIL``.

    Returns True when the stored token is signed.
    """
    from capauth.tokens import issue_token

    try:
        issue_token(base, subject, OPERATOR_CAPABILITIES, ttl_hours=None, sign=True)
        return True
    except Exception as exc:  # noqa: BLE001 - TokenSigningError and any gpg failure
        logger.error(
            "could not SIGN the capability token for %s (%s); storing an UNSIGNED token, "
            "which capauth's PDP refuses ('is unsigned: no signature is present') unless "
            "CAPAUTH_LEGACY_UNSIGNED_GRACE_UNTIL is set. This device will be denied every "
            "capability until a signing key is resident and it re-links.",
            subject,
            exc,
        )
        issue_token(base, subject, OPERATOR_CAPABILITIES, ttl_hours=None, sign=False)
        return False


def grant_operator_capabilities_detailed(
    device_fp: str, device_pubkey_b64: str, *, capauth_proof: Optional[str] = None
) -> GrantOutcome:
    """Grant the operator bundle to ``operator:<device_fp>``, reporting the tier.

    Records a pairing device for the subject AND issues a non-expiring capability
    token, both under capauth's default storage root (the same root the authz PDP
    reads). ``capauth_proof`` is the device's signature over
    :func:`verified_enrollment_challenge`; with it the device record lands
    ``verified``, without it ``tofu``, and the shortfall is logged at WARNING with
    the capabilities it costs.

    The full token bundle is issued either way. The token is not the gate: the
    PDP checks the DEVICE RECORD's mode against each capability's minimum, so a
    tofu device holding a ``skchat.send`` token is still denied ``skchat.send``.
    Issuing the whole bundle keeps a later proof-bearing re-enroll a one-step fix
    (re-enroll upserts the record in place; no re-grant needed).

    Best-effort by contract: a capauth failure is logged and swallowed so it can
    never break the enrollment response. It is logged at ERROR, not the previous
    WARNING, because a total failure means the device is authenticated but
    authorized for NOTHING, and the resulting denials name a missing enrollment
    rather than this.
    """
    if not device_fp or not device_pubkey_b64:
        return GrantOutcome(False, None, "missing device fingerprint or public key")

    subject = operator_subject(device_fp)
    try:
        from capauth.pairing import approve, default_base_dir

        base = default_base_dir()
        enrollment, mode, reason = _enroll_at_best_provable_mode(
            device_pubkey_b64, subject, base, capauth_proof
        )
        approve(enrollment.enrollment_id, "skchat", base_dir=base)
        # Non-expiring: a persistent (revocable) operator device should not need
        # a daily re-grant. Signed where possible; see _issue_bundle_token.
        _issue_bundle_token(base, subject)

        if reason is not None:
            denied = _capabilities_denied_at(subject, base)
            logger.warning(
                "operator device %s enrolled '%s', NOT '%s': %s. The authz PDP will now "
                "DENY %s for this device. Re-link from a client that signs the capauth "
                "enrollment challenge (POST /api/v1/auth/enroll/open with device_pubkey, "
                "then send capauth_proof on /api/v1/auth/enroll) to restore them.",
                subject,
                mode,
                _GRANT_MODE,
                reason,
                ", ".join(denied) if denied else "no capability",
            )
        return GrantOutcome(True, mode, reason)
    except Exception:
        logger.error(
            "operator capability grant FAILED for %s: the device is authenticated but "
            "authorized for nothing, and later PDP denials will report a missing "
            "enrollment rather than this failure",
            subject,
            exc_info=True,
        )
        return GrantOutcome(False, None, "capauth grant raised")


def grant_operator_capabilities(
    device_fp: str, device_pubkey_b64: str, *, capauth_proof: Optional[str] = None
) -> bool:
    """Boolean form of :func:`grant_operator_capabilities_detailed` (back-compat).

    True means the device is enrolled and holds a token; it does NOT mean the
    device reached ``verified``. Check ``.mode`` on the detailed form for that.
    """
    return grant_operator_capabilities_detailed(
        device_fp, device_pubkey_b64, capauth_proof=capauth_proof
    ).ok


#: Back-compat alias: the enrollment route imports this name.
grant_operator_prekey_capability = grant_operator_capabilities


def backfill_operator_capabilities(base_dir=None) -> int:
    """Re-issue the full non-expiring :data:`OPERATOR_CAPABILITIES` token bundle
    to every already-enrolled operator subject. Idempotent; returns the number of
    subjects updated.

    This used to ALSO re-enroll each device at ``verified`` to elevate its record
    (Option A). Card N10 removed that possibility and it must not be faked: the
    backfill holds only each device's stored PUBLIC key, so it can never produce
    the device's signature over :func:`verified_enrollment_challenge`. Its two
    remaining options were both wrong. Asking for ``verified`` without a proof now
    raises, so the elevation silently stopped happening; and re-enrolling at the
    ``tofu`` fallback would DOWNGRADE device records that are legitimately
    verified today, a live regression.

    So it no longer touches enrollment modes at all. It tops the TOKENS up, which
    is its actual job, and reports any subject whose device record sits below
    ``verified`` at WARNING: elevating one requires the device itself to re-link
    and sign the challenge, which only that device's private key can do.
    """
    try:
        from capauth.pairing import default_base_dir, list_devices
        from capauth.tokens import list_tokens
    except Exception:
        logger.error("backfill: capauth unavailable", exc_info=True)
        return 0

    base = base_dir if base_dir is not None else default_base_dir()
    # Every enrolled operator subject (from ANY token, active or expired).
    all_operators: set[str] = set()
    for t in list_tokens(base):
        subj = getattr(t.payload, "subject", None)
        if subj and str(subj).startswith("operator:"):
            all_operators.add(subj)

    updated = 0
    under_tier: list[str] = []
    for subject in all_operators:
        try:
            for dev in list_devices(subject, base_dir=base):
                mode = getattr(dev, "mode", None)
                mode = getattr(mode, "value", mode)
                if mode != _GRANT_MODE and not getattr(dev, "revoked", False):
                    under_tier.append(f"{subject} (mode={mode})")
            # Re-issue the full non-expiring bundle. Modes are left alone.
            _issue_bundle_token(base, subject)
            updated += 1
        except Exception:
            logger.error("backfill: grant failed for %s", subject, exc_info=True)

    if under_tier:
        logger.warning(
            "backfill topped up tokens but CANNOT elevate %d device record(s) below '%s': "
            "%s. Only the device's own private key can sign the capauth enrollment "
            "challenge (card N10), so each must re-link from a client that sends "
            "capauth_proof. Until then the authz PDP denies their higher-tier "
            "capabilities.",
            len(under_tier),
            _GRANT_MODE,
            "; ".join(sorted(under_tier)),
        )
    return updated


def grant_agent_capabilities(subject: str, base_dir=None) -> bool:
    """Issue the full agent capability bundle to ``subject`` (token-only, L2.4).

    Agents (``lumina@chef.skworld.io`` etc.) already enroll ``verified`` on the agent
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
    ``lumina@chef.skworld.io`` / ``opus@chef.skworld.io``). A subject already carrying
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
    "GrantOutcome",
    "verified_enrollment_challenge",
    "grant_operator_capabilities",
    "grant_operator_capabilities_detailed",
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
