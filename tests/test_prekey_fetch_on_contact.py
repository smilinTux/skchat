"""First-contact prekey fetch — wire prekey_exchange into the transport (coord 77ddd0d4).

When the 1:1 DM ratchet is enabled (``SKCHAT_DM_RATCHET``) and we send/receive to a
REMOTE federated peer whose pqdr1 prekey bundle is not yet in our local store, the
transport pulls it once over S2S (real ``urllib`` getter in production; STUBBED here —
never the network) and stores it, so the next DM negotiates the Level-3 ratchet
cross-node (lumina <-> jarvis).

Honest + downgrade-safe:
  * gated entirely behind ``SKCHAT_DM_RATCHET`` (off ⇒ no fetch attempted at all);
  * an unroutable peer / a bundle without the pqdr1 capability ⇒ stays classical,
    never an error;
  * the fetch is one-shot per process (no re-hammering a classical/unroutable peer).
"""

from __future__ import annotations

import importlib

import pytest

pqkem = pytest.importorskip("skcomms.pqkem")
if not pqkem.is_available():
    pytest.skip("liboqs/oqs backend unavailable", allow_module_level=True)

from skchat.crypto import ChatCrypto  # noqa: E402
from skchat.history import ChatHistory  # noqa: E402
from skchat.models import ChatMessage  # noqa: E402
from skchat.transport import ChatTransport  # noqa: E402
from tests.conftest import PASSPHRASE  # noqa: E402

_RESOLVER = lambda _peer: "https://remote-node.example/api/v1/s2s/inbox"  # noqa: E731


@pytest.fixture()
def pq(tmp_path, monkeypatch):
    """Isolated pq_prekeys + DM-session store + ratchet flag ON.

    SKCHAT_HOME scopes BOTH the prekey store and (since this wiring) the
    transport's DM-session store onto a fresh tmp tree, so the test never touches
    the live agent's ratchet state at the real ``~/.skchat/pqc``.
    """
    monkeypatch.setenv("SKCHAT_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("SKCHAT_DM_RATCHET", "1")
    from skchat import pq_prekeys

    importlib.reload(pq_prekeys)
    return pq_prekeys


def _transport(tmp_path, alice_keys):
    tx = ChatTransport(
        skcomms=_FakeSkcomms(),
        history=ChatHistory(history_dir=tmp_path / "h"),
        crypto=ChatCrypto(alice_keys[0], PASSPHRASE),
        identity="capauth:lumina@skworld.io",
    )
    # Keep all loopback/file IO inside the tmp tree.
    tx._file_inbox_root = tmp_path / "inbox"
    # Inject a STUBBED federation route so no real network is touched.
    tx._prekey_inbox_resolver = _RESOLVER
    return tx


class _FakeReport:
    delivered = True
    successful_transport = "file"


class _FakeSkcomms:
    def __init__(self):
        self.sent = []
        self._inbound = []

    def send(self, recipient, message, **kw):
        self.sent.append((recipient, message, kw))
        return _FakeReport()

    def receive(self):
        out, self._inbound = self._inbound, []
        return out


def _remote_pqdr1_bundle(pq):
    kp = pqkem.hybrid_keypair()
    return {
        "suite": pq.HYBRID_SUITE,
        "hybrid_public_hex": kp.public_key.hex(),
        "key_id": kp.public_key.hex()[:16],
        "device_id": "jarvis-daemon",
        "ratchet": pq.RATCHET_CAP,
        "signature": None,
    }


# --- direct method ---------------------------------------------------------- #


def test_maybe_fetch_stores_remote_pqdr1_bundle(tmp_path, alice_keys, pq):
    tx = _transport(tmp_path, alice_keys)
    bundle = _remote_pqdr1_bundle(pq)
    calls = []

    def stub_get(url):
        calls.append(url)
        return {"prekey": bundle}

    tx._prekey_http_get = stub_get
    tx._federation_target = lambda r: "jarvis@chef.skworld"

    assert tx._maybe_fetch_remote_prekey("jarvis@chef.skworld") is True
    # GET hit the remote daemon's prekey endpoint for the short name.
    assert calls == ["https://remote-node.example/api/v1/prekey/jarvis"]
    stored = pq.load_peer_bundle("jarvis")
    assert stored is not None and stored["ratchet"] == pq.RATCHET_CAP


def test_disabled_when_ratchet_flag_off(tmp_path, alice_keys, monkeypatch):
    monkeypatch.setenv("SKCHAT_HOME", str(tmp_path / "home"))
    monkeypatch.delenv("SKCHAT_DM_RATCHET", raising=False)
    from skchat import pq_prekeys

    importlib.reload(pq_prekeys)
    tx = _transport(tmp_path, alice_keys)

    def stub_get(url):  # must never run when the ratchet is disabled
        raise AssertionError("http_get must not run when SKCHAT_DM_RATCHET is off")

    tx._prekey_http_get = stub_get
    tx._federation_target = lambda r: "jarvis@chef.skworld"
    assert tx._maybe_fetch_remote_prekey("jarvis@chef.skworld") is False


def test_unroutable_peer_is_downgrade_safe(tmp_path, alice_keys, pq):
    tx = _transport(tmp_path, alice_keys)

    def stub_get(url):  # must never run when the peer is not a remote node
        raise AssertionError("http_get must not run for a non-federated peer")

    tx._prekey_http_get = stub_get
    tx._federation_target = lambda r: None
    assert tx._maybe_fetch_remote_prekey("localfriend") is False
    assert pq.load_peer_bundle("localfriend") is None


def test_fetch_is_one_shot_per_process(tmp_path, alice_keys, pq):
    """A classical/empty response stores nothing and is NOT re-fetched (no hammer)."""
    tx = _transport(tmp_path, alice_keys)
    calls = []

    def stub_get(url):
        calls.append(url)
        return None  # malformed/empty → fetch_peer_prekey stores nothing

    tx._prekey_http_get = stub_get
    tx._federation_target = lambda r: "ghost@chef.skworld"

    assert tx._maybe_fetch_remote_prekey("ghost@chef.skworld") is False
    assert tx._maybe_fetch_remote_prekey("ghost@chef.skworld") is False
    assert calls == ["https://remote-node.example/api/v1/prekey/ghost"]  # only once


# --- send path -------------------------------------------------------------- #


def test_send_to_federated_peer_fetches_then_ratchets(tmp_path, alice_keys, pq):
    tx = _transport(tmp_path, alice_keys)
    bundle = _remote_pqdr1_bundle(pq)
    calls = []

    def stub_get(url):
        calls.append(url)
        return {"prekey": bundle}

    tx._prekey_http_get = stub_get
    tx._federation_target = lambda r: "jarvis@chef.skworld"

    msg = ChatMessage(sender="lumina", recipient="jarvis", content="cross-node hello")
    # recipient_public_armor present so the crypto branch is taken; the ratchet
    # supersedes the classical PGP seal once the prekey is fetched.
    tx.send_message(msg, recipient_public_armor=alice_keys[1])

    assert calls, "expected a one-shot prekey fetch on first contact"
    # The wire payload is a pqdr1 ratchet body (not classical PGP).
    assert tx._skcomms.sent, "message should have been sent over skcomms"
    sent_payload = tx._skcomms.sent[-1][1]
    sent_msg = ChatMessage.model_validate_json(sent_payload)
    assert ChatCrypto.is_ratchet_message(sent_msg) is True


def test_send_stays_classical_when_no_remote_bundle(tmp_path, alice_keys, pq):
    """No federation route ⇒ no fetch, classical send still works (never errors)."""
    tx = _transport(tmp_path, alice_keys)
    tx._federation_target = lambda r: None
    tx._prekey_http_get = lambda url: (_ for _ in ()).throw(AssertionError("no fetch"))

    msg = ChatMessage(sender="lumina", recipient="bob", content="classical hello")
    res = tx.send_message(msg, recipient_public_armor=alice_keys[1])
    assert res["delivered"] is True
    sent_msg = ChatMessage.model_validate_json(tx._skcomms.sent[-1][1])
    assert ChatCrypto.is_ratchet_message(sent_msg) is False  # classical PGP path


# --- receive path (pre-warm) ----------------------------------------------- #


def test_receive_from_federated_peer_prewarms_prekey(tmp_path, alice_keys, pq):
    tx = _transport(tmp_path, alice_keys)
    bundle = _remote_pqdr1_bundle(pq)
    calls = []

    def stub_get(url):
        calls.append(url)
        return {"prekey": bundle}

    tx._prekey_http_get = stub_get
    tx._federation_target = lambda r: "jarvis@chef.skworld"

    inbound = ChatMessage(sender="capauth:jarvis@skworld.io", recipient=tx.identity,
                          content="classical inbound")
    tx._skcomms._inbound = [{"sender": "capauth:jarvis@skworld.io",
                             "payload": {"content": inbound.model_dump_json()}}]

    got = tx.poll_inbox()
    assert any(m.content == "classical inbound" for m in got)
    # Receiving from the remote node pre-warmed its pqdr1 prekey for our reply.
    assert calls, "inbound from a federated peer should pre-warm its prekey"
    assert pq.load_peer_bundle("jarvis") is not None
