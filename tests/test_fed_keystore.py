"""Realm-qualified directory key pinning (S5 review C1).

Federation MUST bind the FULL fqid (agent@host.realm) to a specific pinned
pubkey — NEVER the bare agent component (which would let `lumina@chef.skworld`
and `lumina@evil.attacker` resolve to the same key → impersonation).
"""

from skchat.spaces.federation.keystore import federation_pubkey


def test_pinned_full_fqid_returns_armored_key(tmp_path):
    armor = "-----BEGIN PGP PUBLIC KEY BLOCK-----\nABC\n-----END-----\n"
    (tmp_path / "lumina@chef.skworld.asc").write_text(armor)
    assert federation_pubkey("lumina@chef.skworld", base=tmp_path) == armor


def test_different_realm_has_no_key(tmp_path):
    # only chef.skworld is pinned; the evil realm must NOT resolve to it
    (tmp_path / "lumina@chef.skworld.asc").write_text("KEY")
    assert federation_pubkey("lumina@evil.attacker", base=tmp_path) is None


def test_absent_pin_returns_none(tmp_path):
    assert federation_pubkey("nobody@nowhere", base=tmp_path) is None


def test_path_traversal_does_not_escape_base(tmp_path):
    # plant a secret file one level above base
    secret = tmp_path.parent / "secret.asc"
    secret.write_text("LEAKED")
    base = tmp_path
    # a fqid crafted to traverse out must NOT read the secret
    assert federation_pubkey("../secret", base=base) is None
    assert federation_pubkey("../../etc/passwd", base=base) is None
    # a fqid with a path separator must not reach into subdirs either
    sub = base / "sub"
    sub.mkdir()
    (sub / "k.asc").write_text("SUBKEY")
    assert federation_pubkey("sub/k", base=base) is None


def test_no_fallback_to_bare_agent(tmp_path):
    # a pin keyed only on the bare agent name must NOT satisfy a full fqid
    (tmp_path / "lumina.asc").write_text("BAREKEY")
    assert federation_pubkey("lumina@chef.skworld", base=tmp_path) is None
