"""An owner that normalises to an EMPTY short name must never reach the store.

Card c5bbb20d. ``_short("@x")`` returns ``""``, and ``_peer_dir("")`` used to
resolve to ``peers/`` itself rather than a per-peer subdirectory. The slot was
then written as ``peers/<key_id>.json``, which is exactly the legacy flat-file
path ``load_peer_bundles`` folds in for back-compat. So a publisher who chose
``owner="@x"`` and ``key_id="<victim>"`` could plant a bundle that
``load_peer_bundle("<victim>")`` returns as the victim's newest slot: a prekey
substitution primitive against another identity.

Reproduced on 2026-08-08 before the fix: a victim with a real 16-hex slot was
overridden by an attacker bundle carrying a higher ``last_published``. The
dedup in the fold-in (skip when ``key_id`` is already ``seen``) does NOT save
you, because a real device key_id is 16 hex chars and never collides with a
short name.

PGP-free and liboqs-free: these exercise path construction only.
"""

from __future__ import annotations

import importlib

import pytest


@pytest.fixture()
def PQ(tmp_path, monkeypatch):
    """pq_prekeys bound to an isolated SKCHAT_HOME (fresh peer store)."""
    monkeypatch.setenv("SKCHAT_HOME", str(tmp_path))
    from skchat import pq_prekeys

    importlib.reload(pq_prekeys)
    return pq_prekeys


def _bundle(pub_byte: str, key_id: str, last_published: float) -> dict:
    return {
        "suite": "x25519-mlkem768",
        "hybrid_public_hex": pub_byte * 32,
        "key_id": key_id,
        "last_published": last_published,
    }


# --------------------------------------------------------------------------- #
# the vulnerability itself
# --------------------------------------------------------------------------- #


def test_empty_owner_cannot_hijack_another_peers_newest_slot(PQ):
    """THE regression. Before the fix this returned the attacker's key."""
    PQ.store_peer_bundle("lumina", _bundle("aa", "7a8ab00748c2bf47", 1000))
    assert PQ.load_peer_bundle("lumina")["hybrid_public_hex"] == "aa" * 32

    # owner "@x" normalises to "", key_id is the VICTIM'S short name, and a huge
    # last_published makes it sort newest-first once folded in as a legacy file.
    with pytest.raises(ValueError):
        PQ.store_peer_bundle("@x", _bundle("ff", "lumina", 99999))

    still = PQ.load_peer_bundle("lumina")
    assert still["hybrid_public_hex"] == "aa" * 32, (
        "an empty-owner publish overrode another identity's prekey"
    )


@pytest.mark.parametrize("owner", ["@x", "@", "capauth:@evil.io", "", "   "])
def test_owner_that_normalises_to_empty_is_rejected(PQ, owner):
    with pytest.raises(ValueError):
        PQ.store_peer_bundle(owner, _bundle("ff", "deadbeefdeadbeef", 1))


def test_peer_dir_refuses_an_empty_short_name(PQ):
    """_peer_dir must not silently hand back the peers/ root."""
    with pytest.raises(ValueError):
        PQ._peer_dir("@x")


def test_nothing_is_written_to_the_peers_root_on_rejection(PQ):
    """A rejected publish must leave no artefact behind, not even an empty dir."""
    PQ.store_peer_bundle("lumina", _bundle("aa", "7a8ab00748c2bf47", 1000))
    with pytest.raises(ValueError):
        PQ.store_peer_bundle("@x", _bundle("ff", "lumina", 99999))

    root = PQ._pqc_dir() / "peers"
    assert sorted(p.name for p in root.iterdir()) == ["lumina"], (
        "a rejected publish left a file in the peers/ root"
    )


# --------------------------------------------------------------------------- #
# the legitimate paths must be untouched
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    "owner,expected",
    [
        ("lumina", "lumina"),
        ("capauth:lumina@skworld.io", "lumina"),
        ("chef@skworld.io", "chef"),
    ],
)
def test_normal_owners_still_resolve_to_their_own_directory(PQ, owner, expected):
    assert PQ._peer_dir(owner).name == expected


def test_a_genuine_legacy_flat_file_is_still_folded_in(PQ):
    """Back-compat must survive the fix.

    A pre-multislot deployment really did store peers/<short>.json, and
    load_peer_bundles folds it in. The fix closes the WRITE path, so a
    legitimate legacy file placed by an older version still reads back.
    """
    root = PQ._pqc_dir() / "peers"
    root.mkdir(parents=True, exist_ok=True)
    import json

    (root / "jarvis.json").write_text(json.dumps(_bundle("bb", "0123456789abcdef", 5)))

    got = PQ.load_peer_bundle("jarvis")
    assert got is not None, "a legitimate legacy flat file must still load"
    assert got["hybrid_public_hex"] == "bb" * 32
