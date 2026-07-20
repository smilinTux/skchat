import base64
import pytest
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives import serialization, hashes
from cryptography.hazmat.primitives.asymmetric.utils import decode_dss_signature
from skchat import operator_auth as oa


def _keypair():
    priv = ec.generate_private_key(ec.SECP256R1())
    spki = priv.public_key().public_bytes(
        serialization.Encoding.DER, serialization.PublicFormat.SubjectPublicKeyInfo)
    return priv, base64.b64encode(spki).decode()


def _sign_p1363(priv, payload: bytes) -> str:
    der = priv.sign(payload, ec.ECDSA(hashes.SHA256()))
    r, s = decode_dss_signature(der)
    raw = r.to_bytes(32, "big") + s.to_bytes(32, "big")  # WebCrypto r||s
    return base64.b64encode(raw).decode()


def test_verify_webcrypto_p1363_signature():
    priv, pub = _keypair()
    payload = b'{"nonce":"n1"}'
    sig = _sign_p1363(priv, payload)
    assert oa.verify_device_signature(device_pubkey_b64=pub, payload=payload, sig_b64=sig) is True
    assert oa.verify_device_signature(device_pubkey_b64=pub, payload=b"other", sig_b64=sig) is False


def test_challenge_nonce_is_single_use():
    nonce, _exp = oa.issue_challenge()
    assert oa.consume_challenge(nonce) is True
    assert oa.consume_challenge(nonce) is False


def test_device_store_enroll_and_lookup(tmp_path):
    store = oa.DeviceStore(tmp_path / "devices.json")
    _priv, pub = _keypair()
    fp = store.enroll(pub)
    assert store.is_enrolled(fp) is True
    assert store.pubkey_for(fp) == pub
    assert store.is_enrolled("not-a-device") is False
