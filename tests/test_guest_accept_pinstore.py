"""TOFU pin store: durable admitted-peer records with list + revoke (Mode C
polish / Mode B foundation)."""
from skchat.guest_accept import ConsumedNonces


def test_admission_persist_list_revoke():
    n = ConsumedNonces(":memory:")
    n.record_admission("PEERFP1", "agent@peerop.realm", '{"jti":"j1"}', "sigop", "sigpeer")
    assert n.is_admitted("PEERFP1") is True
    lst = n.list_admissions()
    assert len(lst) == 1 and lst[0]["peer_fp"] == "PEERFP1"
    assert lst[0]["operator_id"] == "agent@peerop.realm"
    # revoke by peer_fp -> drops out + is_admitted False
    n.revoke_pin("PEERFP1")
    assert n.is_admitted("PEERFP1") is False
    assert n.list_admissions() == []


def test_admission_revoked_by_operator_pin():
    n = ConsumedNonces(":memory:")
    n.record_admission("PEERFP2", "agent@peerop.realm", "{}", "s", "s")
    n.revoke_pin("agent@peerop.realm")  # revoking the operator voids the peer
    assert n.is_admitted("PEERFP2") is False
    assert n.list_admissions() == []
