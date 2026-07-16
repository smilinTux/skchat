"""Mode B secure trust inheritance: the attestation gate + opt-in trust store.

Security invariants: a peer can inherit trust ONLY with a valid operator-signed
attestation over ITS key, under the operator's RECORDED pubkey; a self-declared
or forged claim never inherits."""
import pgpy
from pgpy.constants import (
    PubKeyAlgorithm, KeyFlags, HashAlgorithm, SymmetricKeyAlgorithm,
)

from skchat.crypto import ChatCrypto
from skchat.guest_accept import (
    ConsumedNonces, sign_operator_attestation, verify_operator_attestation,
)

PASS = "p"


def _gen(name):
    k = pgpy.PGPKey.new(PubKeyAlgorithm.RSAEncryptOrSign, 2048)
    k.add_uid(pgpy.PGPUID.new(name), usage={KeyFlags.Sign, KeyFlags.Certify},
              hashes=[HashAlgorithm.SHA256], ciphers=[SymmetricKeyAlgorithm.AES256])
    k.protect(PASS, SymmetricKeyAlgorithm.AES256, HashAlgorithm.SHA256)
    return str(k), str(k.pubkey)


def test_operator_attestation_gate():
    op_priv, op_pub = _gen("Operator X")
    _, agent_pub = _gen("Agent A")
    att = sign_operator_attestation(ChatCrypto(op_priv, PASS), agent_pub)
    # valid attestation under the operator's key
    assert verify_operator_attestation(op_pub, agent_pub, att) is True
    # WRONG operator key (the agent's own) must NOT verify -> no spoofing
    assert verify_operator_attestation(agent_pub, agent_pub, att) is False
    # empty / forged signature
    assert verify_operator_attestation(op_pub, agent_pub, "") is False
    # attestation is bound to THIS agent key; a different agent can't reuse it
    _, other_pub = _gen("Agent B")
    assert verify_operator_attestation(op_pub, other_pub, att) is False


def test_trust_store_opt_in_and_revoke():
    n = ConsumedNonces(":memory:")
    assert n.is_operator_trusted("op@x.realm") is False       # default: no trust
    n.trust_operator("op@x.realm", "PUBKEYARMOR")             # EXPLICIT opt-in
    assert n.is_operator_trusted("op@x.realm") is True
    assert n.operator_pubkey("op@x.realm") == "PUBKEYARMOR"
    assert n.list_trusted_operators()[0]["operator_id"] == "op@x.realm"
    n.revoke_pin("op@x.realm")                                 # revoke (H5)
    assert n.is_operator_trusted("op@x.realm") is False
    assert n.operator_pubkey("op@x.realm") is None
    assert n.list_trusted_operators() == []


def test_retrust_and_readmit_override_revocation():
    n = ConsumedNonces(":memory:")
    # operator: trust -> revoke -> re-trust un-revokes
    n.trust_operator("op@x", "K1")
    n.revoke_pin("op@x")
    assert n.is_operator_trusted("op@x") is False
    n.trust_operator("op@x", "K2")
    assert n.is_operator_trusted("op@x") is True
    assert n.operator_pubkey("op@x") == "K2"
    # peer: admit -> revoke -> re-admit un-revokes
    n.record_admission("PFP", "op@x", "{}", "s", "s")
    n.revoke_pin("PFP")
    assert n.is_admitted("PFP") is False
    n.record_admission("PFP", "op@x", "{}", "s", "s")
    assert n.is_admitted("PFP") is True
