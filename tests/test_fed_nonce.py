"""In-process replay nonce cache (S5 review I1)."""

from skchat.spaces.federation.nonce import NonceCache


def test_first_use_is_fresh():
    nc = NonceCache()
    assert nc.check_and_add("a@h", "n1", ttl=300) is True


def test_replay_within_ttl_is_rejected():
    nc = NonceCache()
    assert nc.check_and_add("a@h", "n1", ttl=300) is True
    assert nc.check_and_add("a@h", "n1", ttl=300) is False


def test_distinct_fqid_same_nonce_is_independent():
    nc = NonceCache()
    assert nc.check_and_add("a@h", "n1", ttl=300) is True
    # same nonce string from a DIFFERENT fqid is a different key → still fresh
    assert nc.check_and_add("b@h", "n1", ttl=300) is True


def test_distinct_nonce_same_fqid_is_independent():
    nc = NonceCache()
    assert nc.check_and_add("a@h", "n1", ttl=300) is True
    assert nc.check_and_add("a@h", "n2", ttl=300) is True


def test_expired_nonce_is_fresh_again():
    nc = NonceCache()
    # ttl=0 → the just-recorded entry is already expired on the next check
    assert nc.check_and_add("a@h", "n1", ttl=0) is True
    assert nc.check_and_add("a@h", "n1", ttl=0) is True
