"""Signed hybrid prekey bundles — RFC-0001 SOVEREIGN (attributable) mode.

RFC-0001 defines two identity modes for the hybrid prekey exchange:

* **ANONYMOUS** (Chef's default) — the bundle is UNSIGNED and deniable. Nothing
  here changes that path; ``pq_prekeys.agent_bundle`` keeps ``"signature": None``.
* **SOVEREIGN** (opt-in) — the agent signs its bundle with its PGP *identity*
  key so a peer can confirm the advertised hybrid prekey belongs to the claimed
  identity. This closes the **prekey-substitution** gap: a MITM that swaps in its
  own hybrid public key cannot forge a signature over it under the real identity.

These are **pure helpers**. They do not touch the live send path, the prekey
store, or negotiation — they only ADD a verifiable signed leg on top of the
existing bundle dict shape.

The signature is a *detached* PGP signature over a **canonical** JSON
serialization (``sort_keys=True``, compact separators) of just the identity-
binding fields — ``suite``, ``hybrid_public_hex``, ``key_id`` — so two peers
deterministically reconstruct the same signed bytes regardless of dict ordering
or transport re-serialization. The hybrid KEM itself stays X25519 + ML-KEM-768
(FIPS 203 ML-KEM); this layer only authenticates *which* prekey is being claimed.
"""

from __future__ import annotations

import json
import logging

import pgpy

logger = logging.getLogger(__name__)

#: The bundle fields bound by the identity signature, in canonical form. Only
#: the identity-relevant fields are signed (not volatile metadata like
#: ``device_id``/``ratchet``) so the signature attests "this identity owns this
#: hybrid prekey under this suite".
_SIGNED_FIELDS = ("suite", "hybrid_public_hex", "key_id")


def _canonical_signed_bytes(bundle: dict) -> bytes:
    """Canonical UTF-8 bytes of the identity-binding fields of *bundle*.

    Uses ``json.dumps(sort_keys=True, separators=(",", ":"))`` so both signer and
    verifier reconstruct byte-identical input independent of dict ordering or any
    extra (unsigned) fields.

    Args:
        bundle: A prekey bundle dict (``pq_prekeys.agent_bundle`` shape).

    Returns:
        bytes: Canonical serialization of ``{suite, hybrid_public_hex, key_id}``.
    """
    payload = {field: bundle.get(field) for field in _SIGNED_FIELDS}
    return json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")


def sign_prekey_bundle(crypto, bundle: dict) -> dict:
    """Sign a hybrid prekey *bundle* with *crypto*'s PGP identity key.

    Produces a detached PGP signature over the canonical serialization of the
    bundle's identity-binding fields and returns a copy of the bundle with the
    ``signature`` field set to the ASCII-armored signature. The original dict is
    not mutated.

    SOVEREIGN mode only — callers that want ANONYMOUS deniability simply don't
    call this (the bundle keeps ``signature: None``).

    Args:
        crypto: A :class:`skchat.crypto.ChatCrypto` whose private key signs.
        bundle: The prekey bundle to sign.

    Returns:
        dict: A copy of *bundle* with ``signature`` set to the armored detached
        signature.
    """
    data = _canonical_signed_bytes(bundle)
    pgp_message = pgpy.PGPMessage.new(data, cleartext=False)
    with crypto._private_key.unlock(crypto._passphrase):
        sig = crypto._private_key.sign(pgp_message)
    signed = dict(bundle)
    signed["signature"] = str(sig)
    return signed


def verify_prekey_bundle(bundle: dict, signer_public_armor: str) -> bool:
    """Verify a signed prekey *bundle* against the claimed identity's public key.

    Recomputes the canonical signed bytes from the bundle's *current*
    identity-binding fields and checks the detached signature under
    *signer_public_armor*. A prekey-substitution (tampered ``hybrid_public_hex``),
    a missing signature, or a wrong identity all yield ``False``.

    Args:
        bundle: The prekey bundle carrying a ``signature`` field.
        signer_public_armor: ASCII-armored PGP public key of the claimed identity.

    Returns:
        bool: ``True`` iff the signature is present and valid for that identity
        over the bundle's current identity-binding fields; ``False`` otherwise.
    """
    sig_armor = bundle.get("signature")
    if not sig_armor:
        return False

    try:
        pub_key, _ = pgpy.PGPKey.from_blob(signer_public_armor)
        sig = pgpy.PGPSignature.from_blob(sig_armor)

        data = _canonical_signed_bytes(bundle)
        pgp_message = pgpy.PGPMessage.new(data, cleartext=False)
        pgp_message |= sig

        return bool(pub_key.verify(pgp_message))
    except Exception as exc:
        logger.warning("prekey_sig.py: %s", exc)
        return False
