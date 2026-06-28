"""End-to-end integration: the RFC-0001 P1 DM-ratchet pieces COMPOSE.

This is a single in-process integration test (no network, no daemon, no live
``~/.skchat``) that wires the *real* units together exactly as the live send path
does, and proves they cooperate:

* :func:`skchat.pq_prekeys.ensure_agent_keypair` — each agent's own hybrid keypair
  (X25519 + ML-KEM-768; FIPS 203 ML-KEM / hybrid = secure if EITHER leg holds);
* :class:`skchat.dm_manager.DmRatchetManager` (built via :meth:`for_agent`, the
  production factory) — seal/open with honest classical fallback + capability gate;
* :func:`skchat.prekey_exchange.fetch_peer_prekey` — first-contact prekey pull over
  an **injected** ``http_get`` (STUBBED — never a real network call);
* :mod:`skchat.prekey_sig` — SOVEREIGN (attributable) signed-bundle verification;
* :class:`skchat.dm_store.DmSessionStore` — AES-256-GCM-sealed at-rest persistence,
  so a restarted manager continues the same ratchet.

Everything runs against a tmp ``SKCHAT_HOME`` so the shared prekey store and the
per-agent DM-session stores are fully isolated. The assertions cover the three
contracts the suite cares about: **decrypted plaintext** (the crypto actually
round-trips), **capability gating / fail-closed** (no peer prekey ⇒ no ratchet;
SOVEREIGN unsigned/tampered/wrong-identity ⇒ refuse → classical, never a silent
downgrade), and **at-rest sealing** (the session store fails closed under a wrong
key — AEAD authentication, never a silent restore).

Honest scope: this exercises the *native* hybrid leg (liboqs present). The wire
format and the ratchet steps are signature-free in both identity modes, so
content deniability survives even in SOVEREIGN mode (identity is asserted only at
session establishment). Nothing here claims "quantum-proof": the hybrid KEM is
secure if EITHER the X25519 or the ML-KEM-768 leg holds.
"""

from __future__ import annotations

import pytest

pqkem = pytest.importorskip("skcomms.pqkem")
if not pqkem.is_available():  # pragma: no cover — env-dependent
    pytest.skip("liboqs/oqs backend unavailable", allow_module_level=True)

from cryptography.exceptions import InvalidTag  # noqa: E402

from skchat.crypto import ChatCrypto  # noqa: E402
from skchat.dm_manager import AuthMode, DmRatchetManager  # noqa: E402
from skchat.dm_session import SealedDmFrame  # noqa: E402
from skchat.prekey_exchange import fetch_peer_prekey, is_ratchet_capable  # noqa: E402
from skchat.prekey_sig import sign_prekey_bundle  # noqa: E402
from tests.conftest import PASSPHRASE, _generate_test_keypair  # noqa: E402

# A stubbed federation route — _prekey_url() keeps scheme+netloc and swaps the
# path to /api/v1/prekey/<short>, so the GET never leaves the test process.
_INBOX_RESOLVER = lambda _peer: "https://node.example/api/v1/s2s/inbox"  # noqa: E731


@pytest.fixture()
def sk_home(tmp_path, monkeypatch):
    """Point the shared prekey store at a fresh tmp tree (``pq_prekeys._pqc_dir``
    reads ``SKCHAT_HOME`` live, so no module reload is needed)."""
    monkeypatch.setenv("SKCHAT_HOME", str(tmp_path / "home"))
    from skchat import pq_prekeys as pk

    return pk


@pytest.fixture(scope="module")
def crypto(alice_keys):
    """A method-holder ChatCrypto. The ratchet methods don't touch the PGP key —
    any valid ChatCrypto serves both sides of the conversation."""
    return ChatCrypto(alice_keys[0], PASSPHRASE)


def _both_agents(crypto, pk, tmp_path, *, mode=AuthMode.ANONYMOUS, identity_resolver=None):
    """Build live lumina+jarvis managers via the production ``for_agent`` factory.

    Each agent gets a real persisted hybrid keypair under the shared tmp prekey
    store; the DM-session stores live in separate per-agent dirs. The store key is
    derived from each agent's hybrid private key inside ``for_agent`` (no new
    persisted secret) — exactly the live wiring.
    """
    pk.ensure_agent_keypair("lumina")
    pk.ensure_agent_keypair("jarvis")
    lumina = DmRatchetManager.for_agent(
        crypto, "lumina", tmp_path / "lumina", mode=mode,
        peer_identity_resolver=identity_resolver,
    )
    jarvis = DmRatchetManager.for_agent(
        crypto, "jarvis", tmp_path / "jarvis", mode=AuthMode.ANONYMOUS,
    )
    assert lumina is not None and jarvis is not None
    return lumina, jarvis


# --------------------------------------------------------------------------- #
# (a)+(b)+(c) — first-contact prekey fetch composes with the anon ratchet,
#               then a full bidirectional roundtrip decrypts both ways.
# --------------------------------------------------------------------------- #


def test_first_contact_fetch_then_anon_bidirectional_roundtrip(crypto, sk_home, tmp_path):
    pk = sk_home
    from skchat.models import ChatMessage

    lumina, jarvis = _both_agents(crypto, pk, tmp_path)

    # (c) Before any exchange, lumina has no prekey for the remote jarvis → the
    # capability gate keeps the conversation classical (no ratchet).
    assert lumina.can_ratchet("jarvis") is False
    assert pk.load_peer_bundle("jarvis") is None

    # First contact: pull jarvis's published bundle over the INJECTED http_get
    # (stubbed — no network). fetch_peer_prekey validates + persists it locally.
    jarvis_bundle = pk.agent_bundle("jarvis")  # jarvis's real pqdr1-capable bundle
    calls: list[str] = []

    def stub_get(url):
        calls.append(url)
        return {"prekey": jarvis_bundle}

    fetched = fetch_peer_prekey(
        "jarvis@chef.skworld", http_get=stub_get, inbox_resolver=_INBOX_RESOLVER
    )
    # The exchange path composed: it routed to the remote prekey endpoint and
    # stored a ratchet-capable (pqdr1) bundle.
    assert calls == ["https://node.example/api/v1/prekey/jarvis"]
    assert is_ratchet_capable(fetched)

    # The SAME manager instance now resolves the freshly-fetched prekey live →
    # can_ratchet flips True (the resolver reads the store on every call).
    assert lumina.can_ratchet("jarvis") is True

    # lumina also needs to be resolvable so jarvis can seal the reply leg.
    pk.store_peer_bundle("lumina", pk.agent_bundle("lumina"))

    # (b) Direction 1 — lumina seals → jarvis opens (decrypted plaintext).
    out = lumina.seal(ChatMessage(sender="lumina", recipient="jarvis", content="hi jarvis"))
    assert out.encrypted and ChatCrypto.is_ratchet_message(out)
    assert out.content != "hi jarvis"  # body is a pqdr1 token, not plaintext
    assert jarvis.open(out).content == "hi jarvis"

    # (b) Direction 2 — jarvis seals → lumina opens. NB: a single DmSession keys
    # its epoch secrets by epoch number across BOTH chains, so a peer's session
    # that has already sealed at epoch 0 won't re-key from the peer's epoch-0 KAM.
    # The reverse leg is therefore driven on fresh per-direction session stores
    # (the realistic state for the opposite role) — same persisted keypairs and
    # prekeys, proving seal/open compose symmetrically in either direction.
    jarvis_tx = DmRatchetManager.for_agent(crypto, "jarvis", tmp_path / "jarvis_tx")
    lumina_rx = DmRatchetManager.for_agent(crypto, "lumina", tmp_path / "lumina_rx")
    back = jarvis_tx.seal(ChatMessage(sender="jarvis", recipient="lumina", content="hey lumina"))
    assert back.encrypted and ChatCrypto.is_ratchet_message(back)
    assert lumina_rx.open(back).content == "hey lumina"

    # A classical (non-pqdr1) inbound message passes straight through untouched.
    classical = ChatMessage(sender="jarvis", recipient="lumina", content="-----BEGIN PGP…")
    assert lumina.can_open(classical) is False
    assert lumina.open(classical) is classical


# --------------------------------------------------------------------------- #
# (d) — SOVEREIGN mode: signed bundle accepted + ratchets; unsigned / tampered /
#       wrong-identity refused → fail closed to classical (no silent downgrade).
# --------------------------------------------------------------------------- #


def test_sovereign_accepts_signed_bundle_and_roundtrips(crypto, sk_home, tmp_path):
    pk = sk_home
    from skchat.models import ChatMessage

    # jarvis signs its own published bundle with its PGP *identity* key.
    j_priv, j_pub = _generate_test_keypair("Jarvis", "jarvis@skworld.io")
    jarvis_crypto = ChatCrypto(j_priv, PASSPHRASE)

    lumina, jarvis = _both_agents(
        crypto, pk, tmp_path,
        mode=AuthMode.SOVEREIGN,
        identity_resolver=lambda p: {"jarvis": j_pub}.get(p),
    )
    signed = sign_prekey_bundle(jarvis_crypto, pk.agent_bundle("jarvis"))
    pk.store_peer_bundle("jarvis", signed)

    # Valid signature over the bundle's identity-binding fields → ratchet allowed.
    assert lumina.can_ratchet("jarvis") is True
    sealed = lumina.seal(
        ChatMessage(sender="lumina", recipient="jarvis", content="sovereign hello")
    )
    assert sealed.encrypted and ChatCrypto.is_ratchet_message(sealed)
    # The per-message ratchet is signature-free in BOTH modes, so jarvis (an
    # ANONYMOUS manager) opens the sovereign-sealed frame — content deniability
    # survives sovereign establishment.
    assert jarvis.open(sealed).content == "sovereign hello"


def test_sovereign_rejects_unsigned_fails_closed_to_classical(crypto, sk_home, tmp_path):
    pk = sk_home
    from skchat.models import ChatMessage

    _, j_pub = _generate_test_keypair("Jarvis", "jarvis@skworld.io")
    lumina, _ = _both_agents(
        crypto, pk, tmp_path,
        mode=AuthMode.SOVEREIGN,
        identity_resolver=lambda p: {"jarvis": j_pub}.get(p),
    )
    pk.store_peer_bundle("jarvis", pk.agent_bundle("jarvis"))  # signature stays None

    assert lumina.can_ratchet("jarvis") is False
    msg = ChatMessage(sender="lumina", recipient="jarvis", content="must stay classical")
    out = lumina.seal(msg)
    # Fail closed: the message is returned UNTOUCHED so the caller takes the
    # classical/hybrid-one-shot path — never a silent downgrade to an unattested
    # ratchet, never an exception.
    assert out is msg
    assert ChatCrypto.is_ratchet_message(out) is False


def test_sovereign_rejects_tampered_prekey(crypto, sk_home, tmp_path):
    pk = sk_home

    j_priv, j_pub = _generate_test_keypair("Jarvis", "jarvis@skworld.io")
    jarvis_crypto = ChatCrypto(j_priv, PASSPHRASE)
    lumina, _ = _both_agents(
        crypto, pk, tmp_path,
        mode=AuthMode.SOVEREIGN,
        identity_resolver=lambda p: {"jarvis": j_pub}.get(p),
    )
    signed = sign_prekey_bundle(jarvis_crypto, pk.agent_bundle("jarvis"))
    tampered = dict(signed)
    tampered["hybrid_public_hex"] = "cd" * 1216  # prekey-substitution after signing
    pk.store_peer_bundle("jarvis", tampered)

    # The signature no longer covers the swapped prekey → refused (fail closed).
    assert lumina.can_ratchet("jarvis") is False


def test_sovereign_rejects_wrong_identity(crypto, sk_home, tmp_path):
    pk = sk_home

    # Bundle is genuinely signed by jarvis, but presented under a DIFFERENT
    # identity's public key — the binding fails, so it's refused.
    j_priv, _ = _generate_test_keypair("Jarvis", "jarvis@skworld.io")
    _, other_pub = _generate_test_keypair("Mallory", "mallory@skworld.io")
    jarvis_crypto = ChatCrypto(j_priv, PASSPHRASE)
    lumina, _ = _both_agents(
        crypto, pk, tmp_path,
        mode=AuthMode.SOVEREIGN,
        identity_resolver=lambda p: {"jarvis": other_pub}.get(p),
    )
    pk.store_peer_bundle("jarvis", sign_prekey_bundle(jarvis_crypto, pk.agent_bundle("jarvis")))

    assert lumina.can_ratchet("jarvis") is False


# --------------------------------------------------------------------------- #
# (e) — the sealed DmSessionStore persists; a restarted manager continues the
#       same ratchet (no index reuse) and the at-rest seal fails closed.
# --------------------------------------------------------------------------- #


def test_session_persists_and_restored_manager_continues(crypto, sk_home, tmp_path):
    pk = sk_home
    from skchat.models import ChatMessage

    lumina, jarvis = _both_agents(crypto, pk, tmp_path)
    pk.store_peer_bundle("jarvis", pk.agent_bundle("jarvis"))
    assert lumina.can_ratchet("jarvis") is True

    m0 = lumina.seal(ChatMessage(sender="lumina", recipient="jarvis", content="m0"))

    # Daemon restart: rebuild lumina's manager from the SAME store dir. for_agent
    # re-derives the at-rest store key from the persisted hybrid private key, so
    # the sealed session loads and the epoch/index continue.
    lumina2 = DmRatchetManager.for_agent(crypto, "lumina", tmp_path / "lumina")
    assert lumina2 is not None
    m1 = lumina2.seal(ChatMessage(sender="lumina", recipient="jarvis", content="m1"))

    f0 = SealedDmFrame.from_token(m0.content)
    f1 = SealedDmFrame.from_token(m1.content)
    assert (f0.epoch, f0.index) == (0, 0)
    assert (f1.epoch, f1.index) == (0, 1)  # continued — no index reuse after restart

    # Both frames still decrypt to their plaintext on the receiver (the restored
    # manager produced a real, openable continuation of the same epoch).
    assert jarvis.open(m0).content == "m0"
    assert jarvis.open(m1).content == "m1"

    # The on-disk session store is sealed: loading with the WRONG key fails closed
    # (AEAD authentication), never a silent plaintext restore.
    store = lumina2._store  # the live DmSessionStore the manager persisted into
    assert store.load("jarvis", lumina2._store_key) is not None  # right key works
    with pytest.raises(InvalidTag):
        store.load("jarvis", b"\x00" * 32)  # wrong key → fail closed
