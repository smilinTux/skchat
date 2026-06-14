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


# ── QA Area 3: nonce cache hardening ─────────────────────────────────────────


def test_expired_entry_is_evicted_from_store():
    # An expired entry should not linger in the backing dict (unbounded growth
    # guard). After re-accept under ttl=0 the store holds exactly one live key.
    nc = NonceCache()
    nc.check_and_add("a@h", "n1", ttl=0)
    nc.check_and_add("b@h", "n2", ttl=0)   # triggers eviction sweep of n1 too
    assert len(nc._seen) <= 1              # only the most-recent (n2) may remain


def test_replay_rejected_repeatedly_within_ttl():
    nc = NonceCache()
    assert nc.check_and_add("a@h", "n1", ttl=300) is True
    # multiple replays in a row all rejected (not just the first)
    assert nc.check_and_add("a@h", "n1", ttl=300) is False
    assert nc.check_and_add("a@h", "n1", ttl=300) is False


def test_empty_nonce_string_is_still_keyed():
    # An empty nonce is a degenerate but valid key; first use fresh, replay caught.
    nc = NonceCache()
    assert nc.check_and_add("a@h", "", ttl=300) is True
    assert nc.check_and_add("a@h", "", ttl=300) is False


def test_independent_caches_do_not_share_state():
    nc1 = NonceCache()
    nc2 = NonceCache()
    assert nc1.check_and_add("a@h", "n", ttl=300) is True
    # a separate cache (e.g. a different authd replica's process cache) is fresh
    assert nc2.check_and_add("a@h", "n", ttl=300) is True
