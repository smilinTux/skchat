# Prekey Scoped Operator Attestation Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make `SKCHAT_REQUIRE_SIGNED_PREKEYS` safe to flip by scoping the daemon-key signer fallback to the operator identity only, and adding a shadow mode that reports who would break before anything is rejected.

**Architecture:** Two files change. `pq_prekeys.py` gains an `off`/`shadow`/`enforce` mode parse (mirroring the repo's existing `dataplane_auth.authz_pdp_mode()`) plus reason-coded verification and a structured log line. `daemon_proxy.py` replaces the unscoped signer fallthrough with an explicit operator-only branch and makes the intake call site mode-aware. No wire-format change, no data migration.

**Tech Stack:** Python 3.11+, FastAPI, pytest, pgpy (via `skchat.prekey_sig`). PGP-only, so no liboqs needed for any test here.

**Spec:** `docs/superpowers/specs/2026-08-08-prekey-signature-identity-design.md`

## Global Constraints

- Worktree: `/home/cbrd21/skworld-worktrees/s2-prekey-attest`, branch `feat/prekey-scoped-attestation`. Do not work in `~/clawd/skcapstone-repos/skchat`; other sessions share that checkout.
- Run tests with `~/.skenv/bin/python -m pytest`.
- Default behaviour with the env var unset MUST stay byte-identical to today. This ships dark.
- The existing truthy set is exactly `{"1", "true", "yes", "on"}` and must keep meaning enforce.
- `_resolve_signer_pubkey` MUST keep its current signature `(peer: str) -> str | None`. Six existing tests monkeypatch it as `lambda peer: alice_pub`; they must keep passing untouched. That is a deliberate regression signal, not an accident.
- `store_app_prekey_bundle`'s new parameter MUST be keyword-only with a default, so every existing caller and test keeps working unchanged.
- Never log key material. Log the truncated `key_id` only.
- HARD RULE: no em dashes or en dashes in any code, comment, docstring, commit message, or doc.
- Commit messages end with `Co-Authored-By: Claude Opus 4.7 <noreply@anthropic.com>`.

---

### Task 1: Mode tri-state in `pq_prekeys`

**Files:**
- Modify: `src/skchat/pq_prekeys.py:59-66`
- Test: `tests/test_prekey_verify_mode.py` (create)

**Interfaces:**
- Consumes: nothing from earlier tasks.
- Produces: `pq_prekeys.prekey_verify_mode() -> str` returning exactly one of `"off"`, `"shadow"`, `"enforce"`. `pq_prekeys.require_signed_prekeys() -> bool` stays, redefined as `prekey_verify_mode() == "enforce"`.

- [ ] **Step 1: Write the failing test**

Create `tests/test_prekey_verify_mode.py`:

```python
"""Tri-state parse for SKCHAT_REQUIRE_SIGNED_PREKEYS (off / shadow / enforce).

Mirrors the repo's existing rollout idiom, dataplane_auth.authz_pdp_mode(): read
at call time so an operator can stage a rollout without a reimport, and anything
unrecognized reads as the safe default. The historical truthy values keep meaning
enforce so no existing reader changes behaviour.
"""

from __future__ import annotations

import pytest

from skchat import pq_prekeys as PQ

ENV = "SKCHAT_REQUIRE_SIGNED_PREKEYS"


def test_unset_is_off(monkeypatch):
    monkeypatch.delenv(ENV, raising=False)
    assert PQ.prekey_verify_mode() == "off"
    assert PQ.require_signed_prekeys() is False


@pytest.mark.parametrize("value", ["1", "true", "yes", "on", "TRUE", " On "])
def test_historical_truthy_values_mean_enforce(monkeypatch, value):
    """Back-compat: every value that used to enable the gate still enforces."""
    monkeypatch.setenv(ENV, value)
    assert PQ.prekey_verify_mode() == "enforce"
    assert PQ.require_signed_prekeys() is True


@pytest.mark.parametrize("value", ["shadow", "SHADOW", " shadow "])
def test_shadow_is_its_own_mode(monkeypatch, value):
    monkeypatch.setenv(ENV, value)
    assert PQ.prekey_verify_mode() == "shadow"
    assert PQ.require_signed_prekeys() is False, "shadow must never reject"


@pytest.mark.parametrize("value", ["", "0", "off", "no", "banana", "enforce-ish"])
def test_unrecognized_reads_as_off(monkeypatch, value):
    monkeypatch.setenv(ENV, value)
    assert PQ.prekey_verify_mode() == "off"
    assert PQ.require_signed_prekeys() is False


def test_mode_is_read_at_call_time(monkeypatch):
    """No reimport needed to stage a rollout."""
    monkeypatch.delenv(ENV, raising=False)
    assert PQ.prekey_verify_mode() == "off"
    monkeypatch.setenv(ENV, "shadow")
    assert PQ.prekey_verify_mode() == "shadow"
    monkeypatch.setenv(ENV, "1")
    assert PQ.prekey_verify_mode() == "enforce"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `~/.skenv/bin/python -m pytest tests/test_prekey_verify_mode.py -q`
Expected: FAIL with `AttributeError: module 'skchat.pq_prekeys' has no attribute 'prekey_verify_mode'`

- [ ] **Step 3: Write minimal implementation**

In `src/skchat/pq_prekeys.py`, replace the `require_signed_prekeys` definition (currently lines 64-66) with:

```python
def prekey_verify_mode() -> str:
    """Return the app-path prekey verification mode.

    One of ``'off'`` (default), ``'shadow'``, or ``'enforce'``. Mirrors
    :func:`skchat.dataplane_auth.authz_pdp_mode` so both rollouts stage the same
    way. Read at call time so an operator can move a live daemon between modes
    without a reimport. Anything unrecognized reads as ``'off'``.

    Back-compat: every value in :data:`_TRUTHY` (the historical "flag on" set)
    reads as ``'enforce'``, so no existing reader changes behaviour.
    """
    raw = os.environ.get(REQUIRE_SIGNED_PREKEYS_ENV, "").strip().lower()
    if raw == "shadow":
        return "shadow"
    return "enforce" if raw in _TRUTHY else "off"


def require_signed_prekeys() -> bool:
    """Whether unsigned/invalid app-path prekey bundles must be rejected.

    True only in ``'enforce'``. Shadow verifies and reports but never rejects.
    """
    return prekey_verify_mode() == "enforce"
```

- [ ] **Step 4: Run test to verify it passes**

Run: `~/.skenv/bin/python -m pytest tests/test_prekey_verify_mode.py -q`
Expected: PASS (18 passed)

- [ ] **Step 5: Prove no existing reader regressed**

Run: `~/.skenv/bin/python -m pytest tests/test_prekey_intake_signed.py tests/test_prekey_publish_signed.py tests/test_prekey_armored_interop.py -q`
Expected: PASS, same counts as before the change.

- [ ] **Step 6: Commit**

```bash
git add tests/test_prekey_verify_mode.py src/skchat/pq_prekeys.py
git commit -m "feat(pqc): off/shadow/enforce tri-state for the prekey verify flag

Mirrors dataplane_auth.authz_pdp_mode(), the idiom this repo already uses to
stage exactly this kind of rollout. The historical truthy set keeps meaning
enforce, so no existing reader changes behaviour and the default stays off.

Co-Authored-By: Claude Opus 4.7 <noreply@anthropic.com>"
```

---

### Task 2: Reason codes and the shadow store path

**Files:**
- Modify: `src/skchat/pq_prekeys.py:193-242` (rewrite `store_app_prekey_bundle`, delete `_prekey_signature_ok`, add `_prekey_verify_reason` and `_log_prekey_verify`)
- Test: `tests/test_prekey_shadow_mode.py` (create)

**Interfaces:**
- Consumes: `prekey_verify_mode()` from Task 1.
- Produces:
  - `pq_prekeys._prekey_verify_reason(bundle: dict, signer_public_armor: str | None) -> str | None` returning `None` when verified, else one of `"unsigned"`, `"no-signer-key"`, `"bad-signature"`.
  - `pq_prekeys.store_app_prekey_bundle(peer, bundle, *, signer_public_armor=None, signer_source="none") -> bool`. The new `signer_source` is keyword-only with a default, so existing callers are unaffected. Task 4 passes the real label.
  - A log line at INFO on accept and WARNING on reject, prefixed `prekey-verify `.

- [ ] **Step 1: Write the failing test**

Create `tests/test_prekey_shadow_mode.py`:

```python
"""Shadow mode: verify and report, but store anyway.

The point of shadow is to answer "who breaks if I flip this" from the log before
anything is rejected. Two properties matter and both are tested here:

  1. shadow NEVER rejects (a bad bundle is still stored), and
  2. shadow actually VERIFIES (a good bundle logs ACCEPT, a bad one logs REJECT
     with a reason). A shadow mode that logged REJECT for everything would make
     the soak worthless, which is the specific failure this guards.

PGP only, no liboqs needed.
"""

from __future__ import annotations

import importlib
import logging

import pytest

from skchat.crypto import ChatCrypto
from skchat.prekey_sig import sign_prekey_bundle

PASSPHRASE = "test-passphrase-123"
ENV = "SKCHAT_REQUIRE_SIGNED_PREKEYS"


@pytest.fixture()
def PQ(tmp_path, monkeypatch):
    """pq_prekeys bound to an isolated SKCHAT_HOME (fresh peer store)."""
    monkeypatch.setenv("SKCHAT_HOME", str(tmp_path))
    from skchat import pq_prekeys

    importlib.reload(pq_prekeys)
    return pq_prekeys


@pytest.fixture()
def alice_crypto(alice_keys: tuple[str, str]) -> ChatCrypto:
    private, _ = alice_keys
    return ChatCrypto(private, PASSPHRASE)


@pytest.fixture()
def unsigned_bundle() -> dict:
    pub_hex = "ab" * 32
    return {
        "suite": "x25519-mlkem768",
        "hybrid_public_hex": pub_hex,
        "signature": None,
        "key_id": pub_hex[:16],
        "device_id": "chef-web",
        "ratchet": "pqdr1",
    }


# --------------------------------------------------------------------------- #
# reason codes
# --------------------------------------------------------------------------- #


def test_reason_unsigned(PQ, alice_keys, unsigned_bundle):
    _, alice_pub = alice_keys
    assert PQ._prekey_verify_reason(unsigned_bundle, alice_pub) == "unsigned"


def test_reason_no_signer_key(PQ, alice_crypto, unsigned_bundle):
    signed = sign_prekey_bundle(alice_crypto, unsigned_bundle)
    assert PQ._prekey_verify_reason(signed, None) == "no-signer-key"


def test_reason_bad_signature(PQ, alice_crypto, bob_keys, unsigned_bundle):
    """Signed by alice, verified against bob's key."""
    _, bob_pub = bob_keys
    signed = sign_prekey_bundle(alice_crypto, unsigned_bundle)
    assert PQ._prekey_verify_reason(signed, bob_pub) == "bad-signature"


def test_reason_none_when_valid(PQ, alice_crypto, alice_keys, unsigned_bundle):
    _, alice_pub = alice_keys
    signed = sign_prekey_bundle(alice_crypto, unsigned_bundle)
    assert PQ._prekey_verify_reason(signed, alice_pub) is None


# --------------------------------------------------------------------------- #
# shadow stores anyway
# --------------------------------------------------------------------------- #


def test_shadow_stores_an_unsigned_bundle(PQ, monkeypatch, alice_keys, unsigned_bundle):
    _, alice_pub = alice_keys
    monkeypatch.setenv(ENV, "shadow")

    stored = PQ.store_app_prekey_bundle("chef", unsigned_bundle, signer_public_armor=alice_pub)

    assert stored is True, "shadow must never reject"
    assert PQ.load_peer_bundle("chef") is not None


def test_enforce_rejects_the_same_bundle(PQ, monkeypatch, alice_keys, unsigned_bundle):
    _, alice_pub = alice_keys
    monkeypatch.setenv(ENV, "1")

    stored = PQ.store_app_prekey_bundle("chef", unsigned_bundle, signer_public_armor=alice_pub)

    assert stored is False
    assert PQ.load_peer_bundle("chef") is None


def test_off_stores_without_verifying(PQ, monkeypatch, unsigned_bundle):
    monkeypatch.delenv(ENV, raising=False)

    stored = PQ.store_app_prekey_bundle("chef", unsigned_bundle, signer_public_armor=None)

    assert stored is True
    assert PQ.load_peer_bundle("chef") is not None


# --------------------------------------------------------------------------- #
# shadow actually verifies (the soak-is-meaningful property)
# --------------------------------------------------------------------------- #


def test_shadow_logs_reject_with_reason(PQ, monkeypatch, caplog, alice_keys, unsigned_bundle):
    _, alice_pub = alice_keys
    monkeypatch.setenv(ENV, "shadow")

    with caplog.at_level(logging.INFO, logger="skchat.pq_prekeys"):
        PQ.store_app_prekey_bundle(
            "chef", unsigned_bundle, signer_public_armor=alice_pub, signer_source="daemon-attest"
        )

    line = next(r.getMessage() for r in caplog.records if "prekey-verify" in r.getMessage())
    assert "mode=shadow" in line
    assert "owner=chef" in line
    assert "result=REJECT" in line
    assert "reason=unsigned" in line
    assert "signer=daemon-attest" in line


def test_shadow_logs_accept_for_a_valid_bundle(
    PQ, monkeypatch, caplog, alice_crypto, alice_keys, unsigned_bundle
):
    """The property that makes a soak meaningful: a GOOD bundle logs ACCEPT.

    If the signer were never resolved in shadow, everything would log REJECT and
    the soak would tell us nothing.
    """
    _, alice_pub = alice_keys
    signed = sign_prekey_bundle(alice_crypto, unsigned_bundle)
    monkeypatch.setenv(ENV, "shadow")

    with caplog.at_level(logging.INFO, logger="skchat.pq_prekeys"):
        PQ.store_app_prekey_bundle(
            "chef", signed, signer_public_armor=alice_pub, signer_source="daemon-attest"
        )

    line = next(r.getMessage() for r in caplog.records if "prekey-verify" in r.getMessage())
    assert "result=ACCEPT" in line
    assert "reason=" not in line, "an accept carries no reason code"


def test_log_never_contains_key_material(
    PQ, monkeypatch, caplog, alice_crypto, alice_keys, unsigned_bundle
):
    _, alice_pub = alice_keys
    signed = sign_prekey_bundle(alice_crypto, unsigned_bundle)
    monkeypatch.setenv(ENV, "shadow")

    with caplog.at_level(logging.INFO, logger="skchat.pq_prekeys"):
        PQ.store_app_prekey_bundle("chef", signed, signer_public_armor=alice_pub)

    line = next(r.getMessage() for r in caplog.records if "prekey-verify" in r.getMessage())
    assert signed["hybrid_public_hex"] not in line
    assert str(signed["signature"]) not in line
    assert "BEGIN PGP" not in line
    assert signed["key_id"][:8] in line, "the truncated key_id IS logged"


def test_off_mode_logs_nothing(PQ, monkeypatch, caplog, unsigned_bundle):
    monkeypatch.delenv(ENV, raising=False)

    with caplog.at_level(logging.INFO, logger="skchat.pq_prekeys"):
        PQ.store_app_prekey_bundle("chef", unsigned_bundle)

    assert not [r for r in caplog.records if "prekey-verify" in r.getMessage()]
```

- [ ] **Step 2: Run test to verify it fails**

Run: `~/.skenv/bin/python -m pytest tests/test_prekey_shadow_mode.py -q`
Expected: FAIL with `AttributeError: module 'skchat.pq_prekeys' has no attribute '_prekey_verify_reason'`

The `alice_keys` and `bob_keys` fixtures are session-scoped PGP keypairs already in `tests/conftest.py:68` and `:78`, each returning `(private_armor, public_armor)`. Do not add new keypair fixtures.

- [ ] **Step 3: Write minimal implementation**

In `src/skchat/pq_prekeys.py`, replace `store_app_prekey_bundle` and `_prekey_signature_ok` with:

```python
def store_app_prekey_bundle(
    peer: str,
    bundle: dict,
    *,
    signer_public_armor: Optional[str] = None,
    signer_source: str = "none",
) -> bool:
    """Intake a prekey bundle published over the **app path** (``POST /api/v1/prekey``).

    Behaviour is chosen by :func:`prekey_verify_mode`:

    * ``'off'`` (default) - stored as-is, nothing verified, nothing logged.
      Byte-identical to the historical unflagged path.
    * ``'shadow'`` - the signature IS verified and the outcome logged, but the
      bundle is stored either way. This is the soak mode: it answers "who breaks
      if I enforce" without breaking anyone.
    * ``'enforce'`` - stored only if the signature verifies. A null/missing
      signature, a missing signer key, or a failed verification (prekey
      substitution / wrong identity) rejects the bundle and stores nothing,
      closing the handshake MITM gap.

    Args:
        peer: The publishing peer (short name or URI); keys the stored bundle.
        bundle: The published prekey bundle dict.
        signer_public_armor: ASCII-armored PGP public key of the claimed identity.
            Used in ``shadow`` and ``enforce``.
        signer_source: Label for WHICH source resolved that key
            (``daemon-attest`` / ``peer-store`` / ``none``), for the audit line.

    Returns:
        ``True`` if the bundle was stored, ``False`` if it was rejected.
    """
    mode = prekey_verify_mode()
    if mode == "off":
        store_peer_bundle(peer, bundle)
        return True

    reason = _prekey_verify_reason(bundle, signer_public_armor)
    _log_prekey_verify(mode, peer, bundle, signer_source, reason)

    if mode == "enforce" and reason is not None:
        return False
    store_peer_bundle(peer, bundle)
    return True


def _log_prekey_verify(
    mode: str, peer: str, bundle: dict, signer_source: str, reason: Optional[str]
) -> None:
    """Emit the one-line audit record for a verified intake.

    Stable ``prekey-verify`` prefix so a soak is greppable straight out of
    journalctl. Carries only the TRUNCATED key_id; never a public key, never a
    signature.
    """
    kid = str(bundle.get("key_id") or "?")[:8]
    line = "prekey-verify mode=%s owner=%s kid=%s signer=%s result=%s" % (
        mode,
        _short(peer),
        kid,
        signer_source,
        "ACCEPT" if reason is None else "REJECT",
    )
    if reason is None:
        logger.info(line)
    else:
        logger.warning("%s reason=%s", line, reason)


def _prekey_verify_reason(bundle: dict, signer_public_armor: Optional[str]) -> Optional[str]:
    """``None`` if the bundle's signature verifies, else a short reason code.

    Reason codes are stable strings meant for the audit line and for triage:
    ``unsigned`` (no signature on the bundle), ``no-signer-key`` (no key resolved
    for the claimed owner), ``bad-signature`` (present but does not verify:
    prekey substitution or wrong identity).
    """
    if not bundle.get("signature"):
        return "unsigned"
    if not signer_public_armor:
        return "no-signer-key"
    from . import prekey_sig

    if not prekey_sig.verify_prekey_bundle(bundle, signer_public_armor):
        return "bad-signature"
    return None
```

**Delete `_prekey_signature_ok` entirely.** Verified before writing this plan: its only caller is the `store_app_prekey_bundle` line this task replaces, and no test references it by name. `_prekey_verify_reason` fully supersedes it. Keeping it as a wrapper would leave a function with zero callers.

- [ ] **Step 4: Run test to verify it passes**

Run: `~/.skenv/bin/python -m pytest tests/test_prekey_shadow_mode.py -q`
Expected: PASS

- [ ] **Step 5: Prove the existing intake contract is unchanged**

Run: `~/.skenv/bin/python -m pytest tests/test_prekey_intake_signed.py -q`
Expected: PASS, unchanged count. These tests call `store_app_prekey_bundle` without `signer_source` and assert the old off/enforce behaviour, so they are the regression gate for the new default.

- [ ] **Step 6: Commit**

```bash
git add tests/test_prekey_shadow_mode.py src/skchat/pq_prekeys.py
git commit -m "feat(pqc): shadow mode + reason-coded prekey verification

Shadow verifies and logs a one-line greppable audit record but stores the
bundle either way, so the flip criterion becomes evidence from a soak instead
of a guess about which app builds are deployed. Reason codes (unsigned /
no-signer-key / bad-signature) make a REJECT triageable. The log carries only
the truncated key_id: no public key, no signature.

Co-Authored-By: Claude Opus 4.7 <noreply@anthropic.com>"
```

---

### Task 3: Scoped operator attestation in the resolver

**Files:**
- Modify: `src/skchat/daemon_proxy.py:720-751` (`_resolve_signer_pubkey`)
- Test: `tests/test_prekey_signer_scope.py` (create)

**Interfaces:**
- Consumes: nothing from earlier tasks.
- Produces: `_resolve_signer_pubkey(peer: str) -> str | None` with UNCHANGED signature but scoped behaviour. Also `daemon_proxy._signer_source_label(owner: str, signer: str | None) -> str` returning `"daemon-attest"`, `"peer-store"`, or `"none"`, consumed by Task 4.

- [ ] **Step 1: Write the failing test**

Create `tests/test_prekey_signer_scope.py`:

```python
"""Scoped operator attestation: who is allowed to be the signer for whom.

Before this change the daemon-agent key was an unscoped FALLBACK: it resolved
for ANY owner missing from the peer store, so a bundle published under any
made-up owner name verified against it. Measured on .158 on 2026-08-08:
chef, mallory, totally-made-up and lumina ALL resolved to the daemon key.

Two consequences, both fixed here:

  1. chef verified only because ~/.skcapstone/peers/chef.json happens not to
     exist. Creating that file would have silently started failing every app
     publish closed. The landmine test below pins that it no longer can.
  2. The signature attested "the operator daemon signed this", not "this owner
     owns this key".

The model now: owner == OPERATOR_ID resolves to the daemon attestation key and
ONLY that; every other owner must self-sign against its peer-store key.
"""

from __future__ import annotations

import pytest

from skchat import daemon_proxy


@pytest.fixture()
def daemon_key(monkeypatch, alice_keys):
    """Stub load_agent_crypto() so the daemon attestation key is alice's."""
    alice_priv, alice_pub = alice_keys

    class _FakeKey:
        def __init__(self, pub):
            self._pub = pub

        def __str__(self):
            return self._pub

    class _FakeCrypto:
        can_sign = True

        def __init__(self, pub):
            self._private_key = type("K", (), {"pubkey": _FakeKey(pub)})()

    from skchat import crypto as _crypto

    monkeypatch.setattr(_crypto, "load_agent_crypto", lambda: _FakeCrypto(alice_pub))
    return alice_pub


@pytest.fixture()
def peer_store(monkeypatch):
    """Control what the peer store returns, including 'file not found'."""
    from skchat import crypto as _crypto

    table: dict[str, str] = {}

    def _load(peer):
        if peer not in table:
            raise _crypto.CryptoError(f"Peer file not found: {peer}")
        return table[peer]

    monkeypatch.setattr(_crypto, "_load_peer_public_key", _load)
    return table


def test_operator_resolves_to_the_daemon_attestation_key(daemon_key, peer_store):
    assert daemon_proxy._resolve_signer_pubkey("chef") == daemon_key


def test_operator_still_resolves_to_daemon_key_when_chef_json_exists(
    daemon_key, peer_store, bob_keys
):
    """THE landmine test.

    Eight peers already carry a public_key in the live store. Before this change,
    the peer store won source-order, so the day someone created chef.json the
    operator path would have started failing closed with no warning.
    """
    _, bob_pub = bob_keys
    peer_store["chef"] = bob_pub

    assert daemon_proxy._resolve_signer_pubkey("chef") == daemon_key, (
        "operator attestation must not be overridable by a peer-store entry"
    )


def test_non_operator_with_a_peer_key_resolves_to_that_key(daemon_key, peer_store, bob_keys):
    _, bob_pub = bob_keys
    peer_store["opus"] = bob_pub

    assert daemon_proxy._resolve_signer_pubkey("opus") == bob_pub


@pytest.mark.parametrize("owner", ["mallory", "totally-made-up", "lumina"])
def test_unknown_owner_no_longer_falls_back_to_the_daemon_key(daemon_key, peer_store, owner):
    """The owner-spoofing surface: these all used to resolve to the daemon key."""
    assert daemon_proxy._resolve_signer_pubkey(owner) is None


def test_operator_resolves_none_when_the_daemon_cannot_sign(monkeypatch, peer_store):
    """Fail closed: no attestation key means no signer, not a silent pass."""
    from skchat import crypto as _crypto

    monkeypatch.setattr(_crypto, "load_agent_crypto", lambda: None)
    assert daemon_proxy._resolve_signer_pubkey("chef") is None


# --------------------------------------------------------------------------- #
# the source label used by the audit line
# --------------------------------------------------------------------------- #


def test_source_label_operator():
    assert daemon_proxy._signer_source_label("chef", "ARMOR") == "daemon-attest"


def test_source_label_peer():
    assert daemon_proxy._signer_source_label("opus", "ARMOR") == "peer-store"


@pytest.mark.parametrize("owner", ["chef", "opus"])
def test_source_label_none_when_unresolved(owner):
    assert daemon_proxy._signer_source_label(owner, None) == "none"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `~/.skenv/bin/python -m pytest tests/test_prekey_signer_scope.py -q`
Expected: FAIL. `test_unknown_owner_no_longer_falls_back_to_the_daemon_key` fails because the current unscoped fallback returns the daemon key, and the `_signer_source_label` tests fail with `AttributeError`.

- [ ] **Step 3: Write minimal implementation**

In `src/skchat/daemon_proxy.py`, replace `_resolve_signer_pubkey` entirely with:

```python
def _resolve_signer_pubkey(peer: str) -> str | None:
    """ASCII-armored PGP public key that MUST have signed *peer*'s prekey bundle.

    Used when :func:`skchat.pq_prekeys.prekey_verify_mode` is not ``'off'``.
    Scoped operator attestation: exactly one source per owner, chosen by identity,
    never a fallthrough.

      * The OPERATOR (``OPERATOR_ID``) resolves to THIS daemon's agent key, and
        only that. The Flutter app cannot hold the operator identity key, so it
        delegates to the operator-gated ``POST /api/v1/prekey/sign`` oracle, which
        signs with ``load_agent_crypto``. So an operator bundle is an ATTESTATION
        ("an authenticated operator session vouched for this device"), not a
        self-signature, and the peer store is deliberately NOT consulted: a
        ``chef.json`` appearing later must not be able to break the operator path.
      * Every OTHER owner must self-sign, and resolves to its own published key
        from the skcapstone peer store. There is no fallback to the daemon key:
        that would let a bundle published under any unknown owner name verify
        against an attestation that says nothing about that owner.

    Returns ``None`` if the required key does not resolve. Intake then fails
    closed in ``'enforce'``, and records ``reason=no-signer-key`` in ``'shadow'``.
    """
    if peer == _short_name(OPERATOR_ID):
        try:
            from skchat import crypto as _crypto

            cc = _crypto.load_agent_crypto()
            if cc is not None and getattr(cc, "can_sign", False):
                return str(cc._private_key.pubkey)
        except Exception:
            logger.debug("no local agent attestation key", exc_info=True)
        return None

    try:
        from skchat.crypto import _load_peer_public_key

        return _load_peer_public_key(peer) or None
    except Exception:
        logger.debug("no peer-store signer pubkey for %s", peer, exc_info=True)
        return None


def _signer_source_label(owner: str, signer: str | None) -> str:
    """Which source produced *signer*, for the ``prekey-verify`` audit line.

    Derivable from the owner alone because :func:`_resolve_signer_pubkey` gives
    each owner exactly ONE permitted source, so this cannot drift from it.
    """
    if signer is None:
        return "none"
    return "daemon-attest" if owner == _short_name(OPERATOR_ID) else "peer-store"
```

- [ ] **Step 4: Run test to verify it passes**

Run: `~/.skenv/bin/python -m pytest tests/test_prekey_signer_scope.py -q`
Expected: PASS

- [ ] **Step 5: Prove the six monkeypatching tests still pass**

Run: `~/.skenv/bin/python -m pytest tests/test_prekey_publish_signed.py tests/test_prekey_armored_interop.py -q`
Expected: PASS. These patch `_resolve_signer_pubkey` as `lambda peer: alice_pub`, which still type-checks against the unchanged signature. If any fail, the signature was changed and must be reverted to `(peer: str) -> str | None`.

- [ ] **Step 6: Commit**

```bash
git add tests/test_prekey_signer_scope.py src/skchat/daemon_proxy.py
git commit -m "fix(pqc): scope the prekey attestation key to the operator identity

The daemon-agent key was an unscoped fallback, so it resolved for ANY owner
missing from the peer store. Measured on .158: chef, mallory, totally-made-up
and lumina all resolved to it. Two problems fixed.

The operator path worked only because ~/.skcapstone/peers/chef.json happens not
to exist; creating it would have silently failed every app publish closed. And
the signature attested that the operator daemon signed something, not that the
claimed owner owned the key.

Now each owner has exactly one permitted source: the operator resolves to the
daemon attestation key and only that (peer store deliberately not consulted),
every other owner must self-sign against its own peer-store key.

Co-Authored-By: Claude Opus 4.7 <noreply@anthropic.com>"
```

---

### Task 4: Mode-aware intake call site and the bypass invariant

**Files:**
- Modify: `src/skchat/daemon_proxy.py:1186-1192` (inside `api_publish_prekey`)
- Test: `tests/test_prekey_intake_modes.py` (create)

**Interfaces:**
- Consumes: `prekey_verify_mode()` (Task 1), `store_app_prekey_bundle(..., signer_source=...)` (Task 2), `_resolve_signer_pubkey` and `_signer_source_label` (Task 3).
- Produces: the wired end-to-end route behaviour. Nothing later depends on it.

- [ ] **Step 1: Write the failing test**

Create `tests/test_prekey_intake_modes.py`:

```python
"""End-to-end intake behaviour per mode, through the real FastAPI route.

The specific defect this guards: the intake used to gate signer resolution on a
BOOLEAN (`if PQ.require_signed_prekeys()`), which is false in shadow. Left as-is,
shadow would resolve no signer, log REJECT for every bundle including good ones,
and the soak would be worthless. The first test below is that regression.

Also pins the bypass invariant: publish_self_prekey writes via store_peer_bundle
and must stay UNGATED even in enforce, because lumina's and opus's live
self-published slots are unsigned and must keep working after the flip.
"""

from __future__ import annotations

import importlib
import logging

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from skchat import daemon_proxy
from skchat.crypto import ChatCrypto
from skchat.prekey_sig import sign_prekey_bundle

PASSPHRASE = "test-passphrase-123"
ENV = "SKCHAT_REQUIRE_SIGNED_PREKEYS"


@pytest.fixture()
def PQ(tmp_path, monkeypatch):
    monkeypatch.setenv("SKCHAT_HOME", str(tmp_path))
    from skchat import pq_prekeys

    importlib.reload(pq_prekeys)
    return pq_prekeys


@pytest.fixture()
def client(PQ, monkeypatch, alice_keys):
    """TestClient over the daemon router with the operator signer stubbed."""
    _, alice_pub = alice_keys
    monkeypatch.setattr(daemon_proxy, "_resolve_signer_pubkey", lambda peer: alice_pub)
    app = FastAPI()
    app.include_router(daemon_proxy.router, prefix="/api")
    return TestClient(app)


@pytest.fixture()
def alice_crypto(alice_keys) -> ChatCrypto:
    private, _ = alice_keys
    return ChatCrypto(private, PASSPHRASE)


def _bundle() -> dict:
    pub_hex = "cd" * 32
    return {
        "suite": "x25519-mlkem768",
        "hybrid_public_hex": pub_hex,
        "signature": None,
        "key_id": pub_hex[:16],
        "device_id": "chef-web",
    }


def test_shadow_resolves_a_signer_and_accepts_a_good_bundle(
    client, PQ, monkeypatch, caplog, alice_crypto
):
    """THE regression: shadow must resolve the signer, not skip it."""
    signed = sign_prekey_bundle(alice_crypto, _bundle())
    monkeypatch.setenv(ENV, "shadow")

    with caplog.at_level(logging.INFO, logger="skchat.pq_prekeys"):
        resp = client.post("/api/v1/prekey", json=signed)

    assert resp.status_code == 200
    line = next(r.getMessage() for r in caplog.records if "prekey-verify" in r.getMessage())
    assert "result=ACCEPT" in line, (
        "shadow resolved no signer: the intake is still gating on a boolean"
    )
    assert "signer=daemon-attest" in line


def test_shadow_stores_an_unsigned_bundle_but_flags_it(client, PQ, monkeypatch, caplog):
    monkeypatch.setenv(ENV, "shadow")

    with caplog.at_level(logging.INFO, logger="skchat.pq_prekeys"):
        resp = client.post("/api/v1/prekey", json=_bundle())

    assert resp.status_code == 200, "shadow never rejects"
    assert PQ.load_peer_bundle("chef") is not None
    line = next(r.getMessage() for r in caplog.records if "prekey-verify" in r.getMessage())
    assert "result=REJECT" in line
    assert "reason=unsigned" in line


def test_enforce_rejects_an_unsigned_bundle(client, PQ, monkeypatch):
    monkeypatch.setenv(ENV, "1")

    resp = client.post("/api/v1/prekey", json=_bundle())

    assert resp.status_code == 400
    assert PQ.load_peer_bundle("chef") is None


def test_enforce_accepts_a_signed_bundle(client, PQ, monkeypatch, alice_crypto):
    signed = sign_prekey_bundle(alice_crypto, _bundle())
    monkeypatch.setenv(ENV, "1")

    resp = client.post("/api/v1/prekey", json=signed)

    assert resp.status_code == 200
    assert PQ.load_peer_bundle("chef") is not None


def test_off_is_unchanged(client, PQ, monkeypatch):
    monkeypatch.delenv(ENV, raising=False)

    resp = client.post("/api/v1/prekey", json=_bundle())

    assert resp.status_code == 200
    assert PQ.load_peer_bundle("chef") is not None


def test_self_publish_bypasses_the_gate_even_in_enforce(PQ, monkeypatch):
    """Bypass invariant.

    lumina's and opus's LIVE slots are unsigned and are written by
    publish_self_prekey -> store_peer_bundle, which never traverses the gated
    app path. If this ever starts going through store_app_prekey_bundle, the
    flip would silently stop the resident agent from publishing its own prekey.
    """
    monkeypatch.setenv(ENV, "1")

    bundle = PQ.publish_self_prekey("lumina")

    assert bundle is not None
    assert PQ.load_peer_bundle("lumina") is not None
```

- [ ] **Step 2: Run test to verify it fails**

Run: `~/.skenv/bin/python -m pytest tests/test_prekey_intake_modes.py -q`
Expected: FAIL on `test_shadow_resolves_a_signer_and_accepts_a_good_bundle` (logs `result=REJECT reason=no-signer-key`, because the boolean guard skipped resolution) and on the shadow tests that expect a 200 with a log line.

- [ ] **Step 3: Write minimal implementation**

In `src/skchat/daemon_proxy.py`, inside `api_publish_prekey`, replace the single `signer = ...` line (currently line 1189) and its two preceding comment lines with:

```python
    # Verification is staged by SKCHAT_REQUIRE_SIGNED_PREKEYS (off/shadow/enforce).
    # Resolve the claimed identity's signer key whenever we are NOT off: shadow
    # needs it too, otherwise every bundle would report no-signer-key and the
    # soak would measure nothing.
    mode = PQ.prekey_verify_mode()
    signer = _resolve_signer_pubkey(owner) if mode != "off" else None
    source = _signer_source_label(owner, signer)
    if not PQ.store_app_prekey_bundle(
        owner, body, signer_public_armor=signer, signer_source=source
    ):
        raise HTTPException(400, "prekey bundle rejected: unsigned or invalid signature")
```

- [ ] **Step 4: Run test to verify it passes**

Run: `~/.skenv/bin/python -m pytest tests/test_prekey_intake_modes.py -q`
Expected: PASS

- [ ] **Step 5: Run the full prekey suite**

Run: `~/.skenv/bin/python -m pytest tests/ -q -k "prekey or pq_prekeys"`
Expected: PASS, with the pre-existing counts plus the new tests. Any failure here is a regression introduced by this plan, not pre-existing.

- [ ] **Step 6: Run the whole suite and compare to baseline**

Before judging, capture the baseline on the merge-base:

```bash
git stash && ~/.skenv/bin/python -m pytest tests/ -q 2>&1 | tail -3 && git stash pop
```

Then:

```bash
~/.skenv/bin/python -m pytest tests/ -q 2>&1 | tail -3
```

Expected: the same set of failures as the baseline (skchat's suite is known to be pre-existingly red), plus zero new ones. Report both numbers rather than claiming green.

- [ ] **Step 7: Commit**

```bash
git add tests/test_prekey_intake_modes.py src/skchat/daemon_proxy.py
git commit -m "feat(pqc): make the prekey intake call site mode-aware

The intake gated signer resolution on a boolean, which is false in shadow. Left
alone, shadow would have resolved no signer, logged REJECT for every bundle
including valid ones, and made the soak worthless. Resolve whenever the mode is
not off, and pass the signer-source label through for the audit line.

Also pins the bypass invariant: publish_self_prekey writes via store_peer_bundle
and stays ungated even in enforce, which is what keeps lumina's and opus's
unsigned self-published slots working after the flip.

Co-Authored-By: Claude Opus 4.7 <noreply@anthropic.com>"
```

---

## Deployment (not part of the code tasks)

Do NOT flip anything as part of implementation. This ships dark: with the env var
unset, behaviour is byte-identical to today.

The rollout, for whoever runs it later:

1. Merge and deploy to .158, restart `skchat-webui@lumina`.
2. Set `SKCHAT_REQUIRE_SIGNED_PREKEYS=shadow`, restart, soak.
3. `journalctl --user -u skchat-webui@lumina | grep prekey-verify` and confirm
   every distinct publishing device shows `result=ACCEPT`.
4. Fix anything showing `result=REJECT`.
5. Flip to `1`. Confirm a signed publish is accepted and an unsigned one 400s.

Rollback at any point is unsetting the env var and restarting. No data migration,
no stored state changes shape.
