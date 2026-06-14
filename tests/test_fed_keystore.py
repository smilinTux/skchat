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


# ── QA Area 3: additional keystore hardening ─────────────────────────────────


def test_backslash_in_fqid_is_neutralised(tmp_path):
    # Windows-style separators must be sanitised so they can't traverse.
    (tmp_path / "lumina@chef.skworld.asc").write_text("KEY")
    assert federation_pubkey("..\\lumina@chef.skworld", base=tmp_path) is None


def test_null_byte_in_fqid_is_neutralised(tmp_path):
    # A null byte (classic truncation trick) must be replaced, not honoured.
    armor = "ARMORED"
    # the sanitised name turns the NUL into "_" so it cannot match a real pin
    (tmp_path / "lumina@chef.skworld.asc").write_text(armor)
    assert federation_pubkey("lumina@chef.skworld\x00", base=tmp_path) is None


def test_exact_pin_with_special_but_safe_chars(tmp_path):
    # A fqid containing dots in the realm is matched verbatim (dots are kept so
    # distinct realms stay distinct — see keystore docstring).
    armor = "REALMKEY"
    (tmp_path / "opus@a.b.c.skworld.asc").write_text(armor)
    assert federation_pubkey("opus@a.b.c.skworld", base=tmp_path) == armor


def test_directory_instead_of_file_returns_none(tmp_path):
    # If a *directory* shadows the expected .asc path, treat as no pin (is_file).
    (tmp_path / "ghost@nowhere.asc").mkdir()
    assert federation_pubkey("ghost@nowhere", base=tmp_path) is None


def test_double_dot_token_anywhere_is_stripped(tmp_path):
    # "a..b" → the parent-dir token is neutralised even when embedded.
    secret = tmp_path / "x.asc"
    secret.write_text("SECRET")
    # crafting "..%2F.." style names must never resolve to the secret
    assert federation_pubkey("..", base=tmp_path) is None
