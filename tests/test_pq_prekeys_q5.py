"""PQC Q5 — app-side prekey store + daemon seal/open helpers.

Proves the daemon/webui half of the Flutter hybrid wiring:
  * the prekey store persists + serves published peer bundles,
  * Lumina generates + publishes her own hybrid prekey,
  * Lumina opens a hybrid token addressed to her (`_open_hybrid_inbound`),
  * Lumina seals a reply to the operator's stored prekey (`_seal_hybrid_outbound`)
    that the operator can open.

Skips cleanly when liboqs is unavailable (no PQ backend).
"""

import base64
import os

import pytest

pqkem = pytest.importorskip("skcomms.pqkem")
pqdm = pytest.importorskip("skcomms.pqdm")

if not pqkem.is_available():
    pytest.skip("liboqs/oqs backend unavailable", allow_module_level=True)


@pytest.fixture()
def pq_home(tmp_path, monkeypatch):
    monkeypatch.setenv("SKCHAT_HOME", str(tmp_path))
    # Re-import fresh so _pqc_dir picks up the env.
    import importlib

    from skchat import pq_prekeys

    importlib.reload(pq_prekeys)
    return pq_prekeys


def test_store_and_fetch_peer_bundle(pq_home):
    kp = pqkem.hybrid_keypair()
    bundle = {
        "suite": pqdm.HYBRID_SUITE,
        "hybrid_public_hex": kp.public_key.hex(),
        "key_id": "dev-1",
        "device_id": "chef-web",
    }
    pq_home.store_peer_bundle("chef@skworld.io", bundle)
    got = pq_home.load_peer_bundle("chef")
    assert got is not None
    assert got["suite"] == pqdm.HYBRID_SUITE
    assert got["hybrid_public_hex"] == kp.public_key.hex()
    assert pq_home.peer_is_hybrid("chef") is True


def test_lumina_keypair_is_stable_and_published(pq_home):
    kp1 = pq_home.ensure_lumina_keypair()
    kp2 = pq_home.ensure_lumina_keypair()
    assert kp1 is not None and kp2 is not None
    # Stable across calls (persisted, not regenerated).
    assert kp1[0] == kp2[0]
    assert kp1[1] == kp2[1]
    b = pq_home.lumina_bundle()
    assert b["suite"] == pqdm.HYBRID_SUITE
    assert b["hybrid_public_hex"] == kp1[0].hex()
    # Private key file is 0600.
    priv_path = pq_home._pqc_dir() / "lumina_hybrid.key"
    mode = oct(os.stat(priv_path).st_mode & 0o777)
    assert mode == "0o600"


def test_daemon_inbound_open_and_outbound_seal_roundtrip(pq_home, monkeypatch):
    """operator → Lumina (open) and Lumina → operator (seal) both round-trip."""
    from skchat import daemon_proxy as DP

    # Lumina's keypair lives in the temp home.
    lum_pub, lum_priv = pq_home.ensure_lumina_keypair()

    # 1) Operator seals a DM to LUMINA's prekey; the daemon opens it.
    lum_bundle = pqdm.PrekeyBundle(
        suite=pqdm.HYBRID_SUITE, hybrid_public_hex=lum_pub.hex()
    )
    sealed = pqdm.seal(
        "hi lumina (hybrid)".encode(), lum_bundle, sender="chef", recipient="lumina"
    )
    token = f"pqdm1:{pqdm.HYBRID_SUITE}:" + base64.b64encode(sealed).decode()
    opened = DP._open_hybrid_inbound(token, sender_short="chef")
    assert opened == "hi lumina (hybrid)"

    # 2) Operator publishes ITS prekey; Lumina seals a reply the operator opens.
    op_kp = pqkem.hybrid_keypair()
    pq_home.store_peer_bundle(
        "chef",
        {"suite": pqdm.HYBRID_SUITE, "hybrid_public_hex": op_kp.public_key.hex()},
    )
    reply_token = DP._seal_hybrid_outbound("reply from lumina", recipient_short="chef")
    assert reply_token is not None and reply_token.startswith("pqdm1:")
    rest = reply_token[len("pqdm1:"):]
    suite, _, b64 = rest.partition(":")
    re_sealed = base64.b64decode(b64)
    clear = pqdm.open_sealed(
        re_sealed, op_kp.private_key,
        sender="lumina", recipient="chef", expected_suite=suite,
    )
    assert clear.decode() == "reply from lumina"


def test_outbound_seal_noop_without_peer_prekey(pq_home):
    from skchat import daemon_proxy as DP

    # No prekey stored for this peer → classical (returns None, caller keeps text).
    assert DP._seal_hybrid_outbound("hello", recipient_short="nobody") is None
