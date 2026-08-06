"""Task 12: end-to-end multi-device DM fanout rollout verification (staging).

This is a REAL integration test against the merged Tasks 1-11 code. It does NOT
stub the seal/open primitives: it publishes device slots into an isolated real
prekey store (``SKCHAT_HOME`` pointed at a tmp dir), seals through the live
``daemon_proxy._seal_hybrid_outbound`` sender fanout, and opens with the live
``skcomms.pqdm.open_multi`` / ``open_sealed`` recipient primitives.

What it proves (the staging rollout checklist, as an automated gate):

* Step 1 - fanout + fallback in ONE send path:
    - Two enrolled device keypairs published as ``codec: "pqdm2"`` slots for a
      peer both open the same DM (each unwraps ITS OWN slot via ``open_multi``).
    - A third peer that never advertised ``pqdm2`` (no codec) still receives, via
      the classical newest-slot ``pqdm1:`` fallback, opened with ``open_sealed``.
* Step 2 - revoke -> graceful lockout:
    - Revoking one device slot (the merged ``remove_peer_bundle``, which the
      operator-only ``DELETE /api/v1/prekey/{peer}/{key_id}`` endpoint from Task 8
      calls) drops that ``key_id`` from every subsequent DM's slot set, and the
      revoked device's ``open_multi`` returns ``None`` - the graceful locked
      placeholder, never a crash and never opaque ciphertext handed upward.
* Step 3 - signed-prekey ENFORCEMENT flag ordering (runbook, NOT a live flip):
    - The ``SKCHAT_REQUIRE_SIGNED_PREKEYS`` enforcement flag ships OFF in code.
    - A skipped-live test documents the ops ordering: flip the flag ONLY after the
      app bundle-signing build (Task 5) ships AND is interop-verified. See the
      RUNBOOK block below for why it is NOT yet interop-ready.

RUNBOOK: SKCHAT_REQUIRE_SIGNED_PREKEYS ordering (do NOT flip prematurely)
------------------------------------------------------------------------
Ordering constraint (plan Global Constraints + Task 4/5/12):
  1. Ship the app bundle-signing build (Task 5) to devices.
  2. Confirm real devices publish SIGNED bundles that the server verifier accepts.
  3. ONLY THEN flip ``SKCHAT_REQUIRE_SIGNED_PREKEYS=1`` in the webui/daemon env.
The env flip is an OPS step, not a code default: the flag defaults OFF so the live
app (which publishes UNSIGNED bundles today) is never locked out.

NOT-YET-INTEROP-READY (finding from Task 5's review):
  The app currently emits a RAW RSA signature as base64, but the server verifier
  (``skchat.prekey_sig.verify_prekey_bundle`` via PGPy/``pgpy``) expects an ARMORED
  OpenPGP signature. Until the app emits armored OpenPGP (or the verifier learns
  the raw-RSA encoding) a flag flip would fail-closed and REJECT every real device
  bundle, orphaning enrollment. Therefore the enforcement flag MUST stay OFF until
  that encoding mismatch is resolved and a signed-bundle round-trip is proven on
  staging. This test asserts the OFF default so no one flips it by editing a
  code path; the live flip stays a deliberate, sequenced ops action.
"""

from __future__ import annotations

import base64
import json

import pytest
from skcomms import pqdm

try:
    from skcomms.pqdm import open_multi, open_sealed
except ImportError:  # pragma: no cover - CI skcomms may predate the fanout primitives
    pytest.skip(
        "skcomms.pqdm.open_multi/open_sealed unavailable (skcomms too old for the "
        "multi-recipient fanout e2e); skipping this module so collection is not aborted",
        allow_module_level=True,
    )

from skchat import daemon_proxy
from skchat import pq_prekeys as PQ

# The sender short name is hard-wired to "lumina" inside _seal_hybrid_outbound
# (the resident agent), so every open MUST reconstruct the AAD with this sender.
_SENDER = "lumina"


@pytest.fixture()
def pqc_home(tmp_path, monkeypatch):
    """Point the real prekey store at an isolated tmp SKCHAT_HOME.

    ``pq_prekeys._pqc_dir()`` reads ``SKCHAT_HOME`` on every call, so setting it
    here makes ``store_peer_bundle`` / ``load_peer_bundles`` / ``remove_peer_bundle``
    operate on a clean, throwaway store - a genuine store round-trip, not a stub.
    Also clears the enforcement flag so intake behaviour is the shipped default.
    """
    monkeypatch.setenv("SKCHAT_HOME", str(tmp_path))
    monkeypatch.delenv(PQ.REQUIRE_SIGNED_PREKEYS_ENV, raising=False)
    return tmp_path


def _device() -> tuple[str, str]:
    """A fresh enrolled device: ``(public_hex, private_hex)`` hybrid keypair."""
    return pqdm.generate_hybrid_keypair()


def _key_id(public_hex: str) -> str:
    """The 16-hex slot id derived from a device public key (store convention)."""
    return public_hex[:16]


def _publish(peer: str, public_hex: str, *, codec: str | None, ts: int) -> str:
    """Publish one device slot for *peer* into the real store; return its key_id."""
    bundle = {
        "suite": pqdm.HYBRID_SUITE,
        "hybrid_public_hex": public_hex,
        "key_id": _key_id(public_hex),
        "last_published": ts,
    }
    if codec is not None:
        bundle["codec"] = codec
    PQ.store_peer_bundle(peer, bundle)
    return _key_id(public_hex)


def _pqdm2_kids(token: str) -> set[str]:
    """The slot-set (key_ids) recorded in a pqdm2 token header."""
    header_b64 = token[len("pqdm2:") :].split(".", 1)[0]
    header = json.loads(base64.b64decode(header_b64))
    return set(header["kids"])


def _open_as(token: str, public_hex: str, private_hex: str, *, recipient: str):
    """Open a pqdm2 token AS the device that owns ``public_hex`` (its own slot)."""
    return open_multi(
        token,
        my_key_id=_key_id(public_hex),
        my_private_hex=private_hex,
        sender=_SENDER,
        recipient_id=recipient,
    )


def _open_pqdm1(token: str, private_hex: str, *, recipient: str) -> bytes:
    """Open a classical ``pqdm1:`` fallback token (mirror of the daemon open)."""
    assert token.startswith("pqdm1:")
    rest = token[len("pqdm1:") :]
    suite, _, b64 = rest.partition(":")
    sealed = base64.b64decode(b64)
    return open_sealed(
        sealed,
        bytes.fromhex(private_hex),
        sender=_SENDER,
        recipient=recipient,
        expected_suite=suite,
    )


# --------------------------------------------------------------------------- #
# Step 1: two devices open the fanout DM; a pqdm1-only peer still receives.
# --------------------------------------------------------------------------- #
def test_step1_two_devices_open_and_pqdm1_only_peer_falls_back(pqc_home):
    # Two enrolled devices of the peer "chef", both advertising pqdm2.
    dev1_pub, dev1_priv = _device()
    dev2_pub, dev2_priv = _device()
    kid1 = _publish("chef", dev1_pub, codec="pqdm2", ts=2)
    kid2 = _publish("chef", dev2_pub, codec="pqdm2", ts=1)

    # A pqdm1-only third peer "bob": one hybrid slot, NO codec advert.
    bob_pub, bob_priv = _device()
    _publish("bob", bob_pub, codec=None, ts=1)

    msg = "fanout rollout ping"

    # ---- pqdm2 fanout to the peer's two devices ----
    token = daemon_proxy._seal_hybrid_outbound(msg, recipient_short="chef")
    assert token is not None, "seal returned None (KEM backend or store failure)"
    assert token.startswith("pqdm2:"), f"expected pqdm2 fanout token, got {token[:12]!r}"

    # The slot-set is exactly the two enrolled devices (no lumina-own slots
    # published in this isolated store), and BOTH devices open their own slot.
    assert _pqdm2_kids(token) == {kid1, kid2}
    assert _open_as(token, dev1_pub, dev1_priv, recipient="chef") == msg.encode()
    assert _open_as(token, dev2_pub, dev2_priv, recipient="chef") == msg.encode()

    # A device with no slot in this envelope (bob) gets the locked placeholder.
    assert _open_as(token, bob_pub, bob_priv, recipient="chef") is None

    # ---- pqdm1 newest-slot fallback for the codec-less peer ----
    token_bob = daemon_proxy._seal_hybrid_outbound(msg, recipient_short="bob")
    assert token_bob is not None
    assert token_bob.startswith("pqdm1:"), (
        f"pqdm1-only peer must get a classical token, got {token_bob[:12]!r}"
    )
    assert _open_pqdm1(token_bob, bob_priv, recipient="bob") == msg.encode()


# --------------------------------------------------------------------------- #
# Step 2: revoke one device -> its slot leaves the DM, that device locks out.
# --------------------------------------------------------------------------- #
def test_step2_revoke_removes_slot_and_locks_out_device(pqc_home):
    dev1_pub, dev1_priv = _device()
    dev2_pub, dev2_priv = _device()
    kid1 = _publish("chef", dev1_pub, codec="pqdm2", ts=2)
    kid2 = _publish("chef", dev2_pub, codec="pqdm2", ts=1)

    msg = "post-revoke ping"

    # Before revoke: both devices are in the slot set and both can open.
    before = daemon_proxy._seal_hybrid_outbound(msg, recipient_short="chef")
    assert before is not None and before.startswith("pqdm2:")
    assert _pqdm2_kids(before) == {kid1, kid2}

    # Revoke device 1 via the merged store call (the DELETE endpoint from Task 8
    # calls exactly this ``remove_peer_bundle`` after its operator-auth gate).
    assert PQ.remove_peer_bundle("chef", kid1) is True
    # Idempotent: revoking an already-gone slot is a no-op, not an error.
    assert PQ.remove_peer_bundle("chef", kid1) is False

    # After revoke: subsequent DMs no longer carry the revoked slot's key_id.
    after = daemon_proxy._seal_hybrid_outbound(msg, recipient_short="chef")
    assert after is not None and after.startswith("pqdm2:")
    kids_after = _pqdm2_kids(after)
    assert kid1 not in kids_after, "revoked device key_id still present in DM"
    assert kids_after == {kid2}

    # The revoked device gets the graceful locked placeholder (open -> None);
    # the surviving device still opens the DM.
    assert _open_as(after, dev1_pub, dev1_priv, recipient="chef") is None
    assert _open_as(after, dev2_pub, dev2_priv, recipient="chef") == msg.encode()


# --------------------------------------------------------------------------- #
# Step 3: signed-prekey ENFORCEMENT flag ships OFF; the live flip is ops-gated.
# --------------------------------------------------------------------------- #
def test_step3_signed_prekey_enforcement_defaults_off(pqc_home):
    """The enforcement flag ships OFF, so unsigned app bundles are NOT rejected.

    This is the code-default half of the ordering guard: nobody can flip signed
    prekey enforcement on by editing a default. The live flip stays a deliberate
    ops action, sequenced after the app signing build ships (see module RUNBOOK).
    """
    assert PQ.require_signed_prekeys() is False


@pytest.mark.skip(
    reason=(
        "RUNBOOK / live-flip only: do NOT flip SKCHAT_REQUIRE_SIGNED_PREKEYS=1 "
        "until the app signing build (Task 5) ships AND signed-bundle interop is "
        "proven on staging. Currently NOT interop-ready: the app emits raw RSA "
        "base64 but the server pgpy verifier needs armored OpenPGP, so enabling "
        "the flag would reject every real device bundle. This test is the staged "
        "ops checklist, not an automated gate - run it live only after step 2 of "
        "the module RUNBOOK is confirmed."
    )
)
def test_step3_signed_prekey_flip_accepts_signed_rejects_unsigned():
    """Live staging check (run manually AFTER app signing ships + interop proven).

    Procedure, on staging with a device build that emits ARMORED OpenPGP sigs:
      1. Flip ``SKCHAT_REQUIRE_SIGNED_PREKEYS=1`` in the webui/daemon env.
      2. Publish a correctly SIGNED bundle -> ``POST /api/v1/prekey`` stores it.
      3. Publish an UNSIGNED bundle -> the intake fails closed (rejected, 0 stored).
    Encoded here as the acceptance contract, skipped until interop is real.
    """
    raise AssertionError("live-only staging step; see skip reason + module RUNBOOK")
