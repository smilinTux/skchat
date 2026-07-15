"""Phase-3a Mode-C accept/sign — mutual peer+operator membership proof (server-side core).

Implements the SERVER-SIDE CORE of Mode C from
``docs/2026-07-15-sovereign-invite-join-architecture.md`` §4: inviting a peer on
**another skchat instance you have NOT federated with**, using only their
identity. There is no shared server and no S2S handshake, so trust is bootstrapped
by a **mutual signature exchange**:

* **Step 3 — the peer builds & signs an ACCEPT ASSERTION.** After verifying the
  operator signature and that ``SHA256(bundle) == bc`` (the anti-downgrade lock),
  the accepter signs ``{jti, peer_pubkey, bc, peer_kem_ct, ts}`` plus the
  macaroon-style caveats ``aud`` (only this peer may accept) and ``scope``
  (``dm``|``group``) with its own identity key — :func:`build_accept_assertion` +
  :func:`sign_accept_assertion`, verified by :func:`verify_accept_assertion`.
* **Step 4 — the operator counter-signs → a JOIN RECORD.** The operator reviews,
  burns the invite ``jti`` (single-use bearer cap), and counter-signs
  ``{invite_jti, operator_id, peer_id, operator_bundle_fp, peer_bundle_fp,
  accept_assertion, sig_peer, ts}`` — :func:`build_join_record` +
  :func:`sign_join_record`. The result, carrying **both** signatures, IS the
  membership proof: :func:`verify_join_record` demands both verify (fail-closed).
  No identity server is ever touched.

Everything reuses ``pq_invites`` for the canonical serialization + the detached
PGP sign/verify helpers, so the exact signed bytes are reproducible on both sides
regardless of dict ordering. Gift-wrap transport (Nostr/Funnel rendezvous) and
the Flutter review/counter-sign UI are separate legs — this module is pure core.

**H5 — bearer caps can't be un-shared.** :class:`ConsumedNonces` is a local
accept-list of **burned invite ``jti``** (a replayed ``jti`` → reject) that ALSO
carries **pin revocations** (a revoked operator/peer identity pin voids the
membership proof). It is the sovereign, zero-server equivalent of a CRL.

Fail-closed is the rule (§5 oracle hygiene): a missing/invalid signature, a
caveat mismatch (wrong ``aud``/``scope``), a ``bc`` that does not echo the
operator commitment, a burned ``jti``, or a revoked pin all return ``False`` —
never a silent accept. Gated behind ``SKCHAT_PQ_INVITES_ENABLED`` (default off)
via :func:`pq_invites_enabled`.
"""

from __future__ import annotations

import logging
import os
import sqlite3
import threading
import time
from pathlib import Path
from typing import Optional

from skchat import pq_invites as _pqi
from skchat.pq_invites import pq_invites_enabled, secrets_equal

logger = logging.getLogger("skchat.guest_accept")

__all__ = [
    "pq_invites_enabled",
    "pubkey_fingerprint",
    "build_accept_assertion",
    "sign_accept_assertion",
    "verify_accept_assertion",
    "build_join_record",
    "sign_join_record",
    "verify_join_record",
    "ConsumedNonces",
    "consumed_nonces",
]

#: Caveat scopes a Mode-C peer may be admitted under (§4 "scope=dm|group").
_ALLOWED_SCOPES = ("dm", "group")


def pubkey_fingerprint(pubkey_armor: str) -> str:
    """The 40-char hex PGP fingerprint of an ASCII-armored identity key.

    Reuses :meth:`skchat.crypto.ChatCrypto.fingerprint_from_armor`. The ``aud``
    caveat and the ``*_bundle_fp`` fields are all this fingerprint, so the peer a
    join record was minted for is exactly the key that signed the accept
    assertion. Returns ``""`` (fail-closed) when the key cannot be parsed.
    """
    from skchat.crypto import ChatCrypto

    return ChatCrypto.fingerprint_from_armor(pubkey_armor) or ""


# ── Step 3 — the ACCEPT ASSERTION (peer-signed) ──────────────────────────────


def build_accept_assertion(
    invite_jti: str,
    accepter_pubkey: str,
    bc: str,
    peer_kem_ct: str,
    ts,
    *,
    scope: str = "dm",
    aud: Optional[str] = None,
) -> dict:
    """Build the accept assertion the peer signs (§4 step 3).

    Args:
        invite_jti: The ``jti`` of the invite being accepted (the bearer cap).
        accepter_pubkey: The accepter's ASCII-armored identity public key.
        bc: The operator bundle commitment the peer verified (echoed back, so the
            assertion is bound to the exact bundle — the anti-downgrade lock).
        peer_kem_ct: The peer's ML-KEM encapsulation to the operator bundle
            (opaque here; the KEM combiner lives in the handshake layer).
        ts: Timestamp; stringified verbatim so both sides reproduce the bytes.
        scope: Macaroon caveat — ``dm`` or ``group`` (what the cap may do).
        aud: Macaroon caveat — the audience fingerprint (who may accept).
            Defaults to the accepter's own fingerprint; an explicit value that
            does not match the signing key is rejected at verify (wrong aud).

    Returns:
        dict: The canonical-serializable assertion (all fields JSON-native).
    """
    if aud is None:
        aud = pubkey_fingerprint(accepter_pubkey)
    return {
        "aud": aud,
        "bc": bc,
        "jti": invite_jti,
        "peer_kem_ct": peer_kem_ct,
        "peer_pubkey": accepter_pubkey,
        "scope": scope,
        "ts": str(ts),
    }


def sign_accept_assertion(crypto, assertion: dict) -> str:
    """Detached PGP signature over the canonical accept *assertion* (peer key)."""
    return _pqi.sign_canonical(crypto, assertion)


def verify_accept_assertion(
    assertion: dict,
    sig: str,
    accepter_pubkey: str,
    *,
    expected_bc: Optional[str] = None,
    expected_scope: Optional[str] = None,
) -> bool:
    """Verify the peer *sig* over *assertion* + enforce its caveats (fail-closed).

    Checks, in order (any failure → ``False``):

    * the signature verifies over the canonical assertion under *accepter_pubkey*;
    * ``bc`` is present and, when *expected_bc* is given, echoes the operator
      commitment exactly (anti-downgrade);
    * the ``aud`` caveat equals the accepter's own fingerprint (only the addressed
      peer may accept — a wrong ``aud`` is rejected);
    * the ``scope`` caveat is one of ``dm``/``group`` and, when *expected_scope*
      is given, matches it.
    """
    if not isinstance(assertion, dict) or not sig or not accepter_pubkey:
        return False

    # 1. Signature over the exact canonical bytes, under the accepter's key.
    if not _pqi.verify_canonical(assertion, sig, accepter_pubkey):
        return False

    # 2. Anti-downgrade: bc present and (if known) echoes the operator commitment.
    bc = assertion.get("bc") or ""
    if not bc:
        return False
    if expected_bc is not None and not secrets_equal(bc, expected_bc):
        return False

    # 3. aud caveat: only the addressed peer (== signer) may accept.
    if not secrets_equal(assertion.get("aud") or "", pubkey_fingerprint(accepter_pubkey)):
        return False

    # 4. scope caveat: attenuated to dm|group (and to expected_scope when given).
    scope = assertion.get("scope") or ""
    if scope not in _ALLOWED_SCOPES:
        return False
    if expected_scope is not None and scope != expected_scope:
        return False

    return True


# ── Step 4 — the JOIN RECORD (operator-counter-signed, mutual) ───────────────


def build_join_record(
    invite_jti: str,
    operator_id: str,
    peer_id: str,
    operator_bundle_fp: str,
    peer_bundle_fp: str,
    accept_assertion: dict,
    sig_peer: str,
    ts,
) -> dict:
    """Build the mutual join record the operator counter-signs (§4 step 4).

    Embeds the peer's accept assertion + its signature, so the single operator
    signature over this record binds the whole exchange. Both sides persist the
    record + both signatures — this IS the self-authenticating membership proof
    (zero identity server). ``ts`` is stringified for byte reproducibility.
    """
    return {
        "accept_assertion": accept_assertion,
        "invite_jti": invite_jti,
        "operator_bundle_fp": operator_bundle_fp,
        "operator_id": operator_id,
        "peer_bundle_fp": peer_bundle_fp,
        "peer_id": peer_id,
        "sig_peer": sig_peer,
        "ts": str(ts),
    }


def sign_join_record(crypto, record: dict) -> str:
    """Detached PGP signature over the canonical join *record* (operator key)."""
    return _pqi.sign_canonical(crypto, record)


def verify_join_record(
    record: dict,
    sig_operator: str,
    sig_peer: str,
    operator_pubkey: str,
    peer_pubkey: str,
    *,
    expected_bc: Optional[str] = None,
    expected_scope: Optional[str] = None,
    nonces: Optional["ConsumedNonces"] = None,
) -> bool:
    """Verify the mutual membership proof — BOTH sigs must verify (fail-closed).

    A valid join record requires, in order (any failure → ``False``):

    * the operator counter-signature verifies over the canonical record under
      *operator_pubkey*;
    * the embedded ``sig_peer`` matches the *sig_peer* argument and the peer's
      accept assertion verifies under *peer_pubkey* (with its caveats — aud,
      scope, and the *expected_bc* anti-downgrade echo);
    * the record is internally consistent: ``invite_jti`` matches the assertion's
      ``jti`` and ``peer_bundle_fp`` matches the peer key that signed it;
    * when *nonces* is supplied (H5): the peer/operator identity pins are not
      revoked, and the invite ``jti`` has not already been burned — the FIRST
      valid verify atomically burns it, so a replay of the same ``jti`` rejects.
    """
    if not isinstance(record, dict) or not sig_operator or not sig_peer:
        return False
    if not operator_pubkey or not peer_pubkey:
        return False

    # 1. Operator counter-signature over the whole canonical record.
    if not _pqi.verify_canonical(record, sig_operator, operator_pubkey):
        return False

    # 2. The embedded peer signature must match what the operator counter-signed.
    if not secrets_equal(record.get("sig_peer") or "", sig_peer):
        return False

    # 3. The peer's accept assertion verifies under the peer key, caveats and all.
    assertion = record.get("accept_assertion")
    if not isinstance(assertion, dict):
        return False
    if not verify_accept_assertion(
        assertion, sig_peer, peer_pubkey, expected_bc=expected_bc, expected_scope=expected_scope
    ):
        return False

    # 4. Internal consistency: the record must describe the same invite + peer.
    if (record.get("invite_jti") or "") != (assertion.get("jti") or ""):
        return False
    if not secrets_equal(record.get("peer_bundle_fp") or "", pubkey_fingerprint(peer_pubkey)):
        return False

    # 5. H5 — pin revocations + single-use burn (only when a nonce store is given).
    if nonces is not None:
        for pin in (
            record.get("operator_id"),
            record.get("peer_id"),
            record.get("operator_bundle_fp"),
            record.get("peer_bundle_fp"),
        ):
            if pin and nonces.is_pin_revoked(pin):
                return False
        # Atomic burn: the race/replay loser (already-consumed jti) is rejected.
        if not nonces.mark_consumed(record.get("invite_jti") or ""):
            return False

    return True


# ── H5 — local accept-list of burned jti + pin revocations ───────────────────

_NONCES_DB_ENV = "SKCHAT_CONSUMED_NONCES_DB"
_DEFAULT_NONCES_DB = "~/.skchat/consumed_nonces.db"


def _nonces_db_path() -> str:
    """Resolve the consumed-nonce store path (env override → ``~/.skchat`` default)."""
    raw = os.getenv(_NONCES_DB_ENV, "").strip() or _DEFAULT_NONCES_DB
    return str(Path(raw).expanduser())


class ConsumedNonces:
    """A local, SQLite-backed accept-list of burned invite ``jti`` + revoked pins.

    Bearer caps can't be un-shared, so once an invite ``jti`` is accepted it is
    **burned** here (``mark_consumed`` returns ``False`` for any replay). The same
    store carries **pin revocations** (H5): revoking an operator/peer identity pin
    voids every join record that leans on it. The SQLite row is the source of
    truth (survives restart, shared across processes); pass ``":memory:"`` for an
    ephemeral store (tests) or a path/env override for persistence.
    """

    def __init__(self, db_path: Optional[str] = None) -> None:
        if db_path is None:
            db_path = _nonces_db_path()
        if db_path != ":memory:":
            Path(db_path).expanduser().parent.mkdir(parents=True, exist_ok=True)
        # check_same_thread=False + an explicit lock: safe for the daemon's mix of
        # poll-loop and request threads sharing one process-wide store instance.
        self._conn = sqlite3.connect(db_path, check_same_thread=False)
        self._lock = threading.Lock()
        with self._lock:
            self._conn.execute(
                "CREATE TABLE IF NOT EXISTS burned_jtis ("
                "  jti TEXT PRIMARY KEY,"
                "  burned_at REAL NOT NULL"
                ")"
            )
            self._conn.execute(
                "CREATE TABLE IF NOT EXISTS revoked_pins ("
                "  pin TEXT PRIMARY KEY,"
                "  revoked_at REAL NOT NULL"
                ")"
            )
            self._conn.commit()

    def mark_consumed(self, jti: str) -> bool:
        """Burn *jti*. Returns ``True`` on the first burn, ``False`` on any replay.

        The insert is atomic (``INSERT OR IGNORE`` on a PRIMARY KEY), so under a
        race only one caller sees ``True`` — the single-use guarantee for a
        bearer cap that can't be un-shared.
        """
        if not jti:
            return False
        with self._lock:
            cur = self._conn.execute(
                "INSERT OR IGNORE INTO burned_jtis (jti, burned_at) VALUES (?, ?)",
                (jti, time.time()),
            )
            self._conn.commit()
            return cur.rowcount == 1

    def is_consumed(self, jti: str) -> bool:
        """True iff *jti* has already been burned."""
        if not jti:
            return False
        with self._lock:
            row = self._conn.execute(
                "SELECT 1 FROM burned_jtis WHERE jti = ? LIMIT 1", (jti,)
            ).fetchone()
        return row is not None

    def revoke_pin(self, pin: str) -> None:
        """Revoke an identity *pin* (operator/peer id or bundle fingerprint)."""
        if not pin:
            return
        with self._lock:
            self._conn.execute(
                "INSERT OR IGNORE INTO revoked_pins (pin, revoked_at) VALUES (?, ?)",
                (pin, time.time()),
            )
            self._conn.commit()

    def is_pin_revoked(self, pin: str) -> bool:
        """True iff *pin* has been revoked (its join records no longer count)."""
        if not pin:
            return False
        with self._lock:
            row = self._conn.execute(
                "SELECT 1 FROM revoked_pins WHERE pin = ? LIMIT 1", (pin,)
            ).fetchone()
        return row is not None

    def close(self) -> None:
        """Close the underlying SQLite connection."""
        with self._lock:
            self._conn.close()


class _LazyConsumedNonces:
    """Process-wide default :class:`ConsumedNonces`, opened on first use.

    Deferring the connection keeps module import side-effect-free (no ``~/.skchat``
    write just by importing) and honours a late ``SKCHAT_CONSUMED_NONCES_DB``.
    """

    def __init__(self) -> None:
        self._inst: Optional[ConsumedNonces] = None
        self._lock = threading.Lock()

    def _ensure(self) -> ConsumedNonces:
        with self._lock:
            if self._inst is None:
                self._inst = ConsumedNonces()
            return self._inst

    def mark_consumed(self, jti: str) -> bool:
        return self._ensure().mark_consumed(jti)

    def is_consumed(self, jti: str) -> bool:
        return self._ensure().is_consumed(jti)

    def revoke_pin(self, pin: str) -> None:
        self._ensure().revoke_pin(pin)

    def is_pin_revoked(self, pin: str) -> bool:
        return self._ensure().is_pin_revoked(pin)


#: Process-wide default accept-list; explicit instances win where passed.
consumed_nonces = _LazyConsumedNonces()
