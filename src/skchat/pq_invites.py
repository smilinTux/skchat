"""Phase-1 sovereign invites — signed invite + bundle commitment + fragment secret.

This closes the two Phase-1 gaps from
``docs/2026-07-15-sovereign-invite-join-architecture.md`` §1.1 / §5, folding in
the adversarial-review hardenings C1, C2, H3, H7:

* **C1 (FQID idm).** The inviter is identified by the *full* sovereign FQID
  ``<agent>@<operator>.<realm>`` (not the non-unique ``capauth:<agent>@skworld.io``
  address), bound to the operator identity key. The display name is advisory.
* **C2 (full pubkey inline).** The invite ships the operator's **full** identity
  public key (PGP armor) inline, not just a fingerprint — a fingerprint cannot
  verify a signature, and needing to look one up would imply a hidden directory.
  ``ik_fp`` remains as a display/comparison aid only.
* **H3 (stable-portion commitment).** ``bc`` commits to the long-lived
  ``identity_key`` + ``signed_prekey`` **only**, never the rotating one-time
  prekeys — so OPK rotation never false-fails the commitment (and never forces
  OPK reuse, which would break forward secrecy).
* **H7 (fragment secret in Phase 1).** The 32-byte link secret ``k`` ships in the
  URL **fragment** (``/app/#/g/<token>&k=…``) from Phase 1, so no phase ever
  ships a joinable secret in the request path/query.

These are **pure helpers + one crypto-resolving assembler**. They ADD a signed,
self-authenticating leg on top of the existing HS256 invite envelope
(``guest_groups.create_group_invite``) — the JWT still owns routing/burn/TTL/
revoke; the operator signature owns identity/anti-forge; ``bc`` owns the
anti-downgrade lock. Everything is gated behind ``SKCHAT_PQ_INVITES_ENABLED``
(default off): when the flag is off the classic invite path is byte-for-byte
unchanged.

Fail-closed is the rule (§5 oracle hygiene): a missing/invalid signature, a
missing key, or an unresolvable operator identity/prekey aborts — never a silent
classical fallback.
"""

from __future__ import annotations

import base64
import hashlib
import json
import logging
import os
import secrets
from typing import Optional

logger = logging.getLogger("skchat.pq_invites")

# ── Feature flag ────────────────────────────────────────────────────────────
FLAG_ENV = "SKCHAT_PQ_INVITES_ENABLED"
_TRUTHY = {"1", "true", "yes", "on"}


def pq_invites_enabled() -> bool:
    """True iff the signed-PQ-invite layer is enabled (default OFF).

    Accepts ``1/true/yes/on`` (case-insensitive). When off, every PQ addition
    here is skipped and the classic ``create_group_invite`` path is unchanged.
    """
    return os.getenv(FLAG_ENV, "").strip().lower() in _TRUTHY


# ── Canonical serialization (shared recipe with prekey_sig.py) ───────────────


def _canonical(payload: dict) -> bytes:
    """Deterministic UTF-8 bytes of *payload* (``sort_keys`` + compact separators).

    The same recipe used by ``prekey_sig._canonical_signed_bytes`` and
    ``guest_groups.canonical_sign_payload`` so signer and verifier reconstruct
    byte-identical input regardless of dict ordering.
    """
    return json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")


def _b64u(data: bytes) -> str:
    """URL-safe base64 with padding stripped (fragment/URL friendly)."""
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def _b64decode_any(s: str) -> bytes:
    """Decode standard- or url-safe base64, tolerating missing padding.

    Guest browser artifacts (SPKI public key, ECDSA signature) are exported as
    standard base64 by WebCrypto; be lenient and also accept url-safe.
    """
    s = (s or "").strip()
    pad = "=" * (-len(s) % 4)
    try:
        return base64.b64decode(s + pad)
    except Exception:
        return base64.urlsafe_b64decode(s + pad)


# ── Bundle commitment (H3: stable portion only) ──────────────────────────────
#: The commitment binds the operator's long-lived identity key and its signed
#: prekey ONLY. One-time prekeys (OPKs) are deliberately excluded so OPK
#: rotation never changes ``bc`` (and OPKs are never forced to be reused).
_COMMIT_FIELDS = ("identity_key", "signed_prekey")


def bundle_commitment(identity_key: str, signed_prekey: str) -> str:
    """``bc`` — url-safe-b64 SHA-256 over canonical ``{identity_key, signed_prekey}``.

    Args:
        identity_key: The operator's long-lived identity public key (PGP armor).
        signed_prekey: The operator's signed hybrid prekey (``hybrid_public_hex``).

    Returns:
        str: The bundle commitment (anti-downgrade lock). Excludes one-time
        prekeys by construction (H3).
    """
    payload = {"identity_key": identity_key or "", "signed_prekey": signed_prekey or ""}
    return _b64u(hashlib.sha256(_canonical(payload)).digest())


def commitment_for_bundle(identity_key: str, bundle: dict) -> str:
    """``bc`` for an operator prekey *bundle* dict (identity_key + signed prekey).

    Reads the bundle's signed prekey from ``signed_prekey`` (falling back to the
    existing ``hybrid_public_hex`` field) and ignores any one-time-prekey field
    (``one_time_prekeys`` / ``opks``), so rotating OPKs never changes the result.
    """
    signed_prekey = bundle.get("signed_prekey") or bundle.get("hybrid_public_hex") or ""
    return bundle_commitment(identity_key, signed_prekey)


def verify_commitment(identity_key: str, signed_prekey: str, bc: str) -> bool:
    """Fail-closed check that ``bc`` commits to *identity_key* + *signed_prekey*.

    The joiner runs this AFTER fetching the operator prekey bundle and BEFORE
    the handshake: a mismatch (or a missing ``bc``) is an abort, never a silent
    classical fallback (the anti-downgrade lock, §5).
    """
    if not bc:
        return False
    return secrets_equal(bundle_commitment(identity_key, signed_prekey), bc)


def secrets_equal(a: str, b: str) -> bool:
    """Constant-time-ish string compare (thin wrapper over ``secrets``)."""
    return secrets.compare_digest((a or "").encode("utf-8"), (b or "").encode("utf-8"))


# ── Operator-signed invite claims ({idm, bc, mode}) ──────────────────────────
#: Fields the operator identity signature covers. ``ik_fp``/``operator_pubkey``
#: are carried alongside (display aid / verification key) but NOT signed — the
#: signature is anchored to the identity, and ``bc`` already binds identity_key.
_CLAIM_FIELDS = ("bc", "idm", "mode")


def canonical_claims(idm: str, bc: str, mode: str) -> dict:
    """The exact claim dict the operator signs / the joiner verifies."""
    return {"bc": bc, "idm": idm, "mode": mode}


def _claims_bytes(claims: dict) -> bytes:
    """Canonical bytes of just the signed claim fields (order-independent)."""
    payload = {field: claims.get(field) for field in _CLAIM_FIELDS}
    return _canonical(payload)


def sign_invite_claims(crypto, claims: dict) -> str:
    """Detached PGP signature over the canonical invite *claims* (SOVEREIGN).

    Mirrors ``prekey_sig.sign_prekey_bundle`` — a detached signature over the
    canonical serialization, returned ASCII-armored. Reuses the operator's PGP
    identity key held by *crypto* (a :class:`skchat.crypto.ChatCrypto`).
    """
    import pgpy

    data = _claims_bytes(claims)
    pgp_message = pgpy.PGPMessage.new(data, cleartext=False)
    with crypto._private_key.unlock(crypto._passphrase):
        sig = crypto._private_key.sign(pgp_message)
    return str(sig)


def verify_invite_claims(claims: dict, operator_sig: str, operator_pubkey_armor: str) -> bool:
    """Verify the operator *operator_sig* over *claims* under the inline pubkey.

    Fail-closed: a missing signature, a missing/invalid key, or a tampered claim
    (``bc``/``idm``/``mode`` swap) all return ``False``. The joiner runs this
    against the FULL operator public key shipped inline in the invite (C2) — no
    directory lookup, so the invite is self-authenticating against the operator
    identity, independent of the HS256 server secret.
    """
    if not operator_sig or not operator_pubkey_armor:
        return False
    try:
        import pgpy

        pub_key, _ = pgpy.PGPKey.from_blob(operator_pubkey_armor)
        sig = pgpy.PGPSignature.from_blob(operator_sig)
        pgp_message = pgpy.PGPMessage.new(_claims_bytes(claims), cleartext=False)
        pgp_message |= sig
        return bool(pub_key.verify(pgp_message))
    except Exception as exc:  # noqa: BLE001 — any parse/verify error = reject
        logger.warning("pq_invites: operator sig verify failed: %s", exc)
        return False


# ── Fragment secret k (H7) ───────────────────────────────────────────────────


def new_fragment_secret() -> str:
    """A fresh 32-byte link secret ``k`` as url-safe base64 (no padding).

    Lives ONLY in the URL fragment (``/app/#/g/<token>&k=…``); it is never sent
    to the server, so Funnel/CDN/daemon access logs never see a joinable secret.
    """
    return _b64u(secrets.token_bytes(32))


def build_join_url(token: str, fragment_secret: Optional[str]) -> str:
    """Assemble the Flutter guest deep link, keeping every secret in the fragment.

    ``/app/#/g/<token>`` (classic) or ``/app/#/g/<token>&k=<k>`` when a fragment
    secret is present. Both ``token`` and ``k`` sit after ``#`` — nothing
    joinable ever lands in the path or query (H7 / §5).
    """
    url = f"/app/#/g/{token}"
    if fragment_secret:
        url = f"{url}&k={fragment_secret}"
    return url


# ── Guest key binding ({jti, guest_pubkey, bc}) ──────────────────────────────


def guest_binding_bytes(jti: str, guest_pubkey: str, bc: str) -> bytes:
    """Canonical bytes the guest signs to bind its key to a specific invite.

    A stolen link cannot be replayed by a third party who lacks the guest's key:
    the guest signs ``{jti, guest_pubkey, bc}`` with its browser key, binding the
    freshly-generated guest key to this exact invite (§5 "stolen link replayed").
    """
    return _canonical({"bc": bc, "guest_pubkey": guest_pubkey, "jti": jti})


def verify_guest_binding(guest_sig: str, guest_pubkey: str, jti: str, bc: str) -> bool:
    """Verify the guest ECDSA-P256 signature binding its key to ``{jti,pubkey,bc}``.

    ``guest_pubkey`` is the browser's exported SPKI public key (base64); the
    signature is either WebCrypto raw ``r||s`` (64 bytes) or DER. Fail-closed:
    a missing signature/key/commitment, an unavailable crypto backend, or a bad
    signature all return ``False`` (so a replay without the guest key → reject).
    """
    if not guest_sig or not guest_pubkey or not bc:
        return False
    try:
        from cryptography.hazmat.primitives import hashes, serialization
        from cryptography.hazmat.primitives.asymmetric import ec
        from cryptography.hazmat.primitives.asymmetric.utils import encode_dss_signature
    except Exception:  # pragma: no cover — cryptography ships with pgpy in prod
        logger.warning("pq_invites: cryptography unavailable; guest binding fails closed")
        return False
    try:
        pub = serialization.load_der_public_key(_b64decode_any(guest_pubkey))
        raw = _b64decode_any(guest_sig)
        if len(raw) == 64:  # WebCrypto P1363 raw r||s → DER
            r = int.from_bytes(raw[:32], "big")
            s = int.from_bytes(raw[32:], "big")
            der = encode_dss_signature(r, s)
        else:
            der = raw
        pub.verify(der, guest_binding_bytes(jti, guest_pubkey, bc), ec.ECDSA(hashes.SHA256()))
        return True
    except Exception as exc:  # noqa: BLE001 — InvalidSignature or any parse error = reject
        logger.info("pq_invites: guest binding verify failed: %s", exc)
        return False


# ── Operator invite material assembler (crypto-resolving) ────────────────────


def _resolve_operator_fqid() -> str:
    """Resolve the operator's FULL sovereign FQID ``<agent>@<operator>.<realm>``.

    C1: the classic ``capauth:<agent>@skworld.io`` address is NOT unique per
    instance, so the invite MUST carry the full FQID. Fail-closed: raise if no
    FQID can be resolved (never fall back to the ambiguous address).
    """
    try:
        from capauth import resolve_agent_identity  # type: ignore

        fqid = getattr(resolve_agent_identity(), "fqid", "") or ""
        if fqid:
            return fqid
    except Exception as exc:  # pragma: no cover — capauth optional in some envs
        logger.debug("pq_invites: capauth fqid resolve failed (%s)", exc)
    try:
        from skchat.agent_profile import resolve_agent_identity as _rai  # type: ignore

        fqid = getattr(_rai(), "fqid", "") or ""
        if fqid:
            return fqid
    except Exception as exc:  # pragma: no cover
        logger.debug("pq_invites: agent_profile fqid resolve failed (%s)", exc)
    raise RuntimeError("PQ invite: cannot resolve operator FQID (idm) — fail-closed")


def resolve_operator_material(mode: str) -> dict:
    """Assemble the signed operator invite claims + inline pubkey for ``mode``.

    Resolves the operator FQID (C1), loads the operator PGP identity key, reads
    the operator's signed hybrid prekey, computes the stable-portion commitment
    ``bc`` (H3), and produces the detached operator signature over the canonical
    ``{idm, bc, mode}`` claims. The operator's FULL public key ships inline (C2).

    Raises:
        RuntimeError: if the FQID, the signing key, or a signed prekey cannot be
            resolved — fail-closed (§5): PQ-on must never emit an unsigned or
            all-classical invite.

    Returns:
        dict: ``{idm, operator_pubkey, ik_fp, signed_prekey, bc, mode,
        operator_sig}``.
    """
    from skchat import crypto as _crypto
    from skchat import pq_prekeys as _pq

    idm = _resolve_operator_fqid()

    chat_crypto = _crypto.load_agent_crypto()
    if chat_crypto is None or not getattr(chat_crypto, "can_sign", False):
        raise RuntimeError("PQ invite: operator signing key unavailable — fail-closed")
    operator_pubkey = str(chat_crypto._private_key.pubkey)

    bundle = _pq.agent_bundle()
    signed_prekey = (bundle.get("hybrid_public_hex") or "").strip()
    if not signed_prekey:
        raise RuntimeError(
            "PQ invite: no signed (hybrid) prekey available — fail-closed "
            "(refusing to emit a classical-only invite)"
        )

    bc = bundle_commitment(operator_pubkey, signed_prekey)
    claims = canonical_claims(idm, bc, mode)
    operator_sig = sign_invite_claims(chat_crypto, claims)

    return {
        "idm": idm,
        "operator_pubkey": operator_pubkey,
        "ik_fp": getattr(chat_crypto, "fingerprint", ""),
        "signed_prekey": signed_prekey,
        "bc": bc,
        "mode": mode,
        "operator_sig": operator_sig,
    }
