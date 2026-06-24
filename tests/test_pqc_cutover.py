"""PQC confidentiality cut-over tests (Entry #6).

Covers: hybrid-default new groups, agent-aware prekeys, and the migrate-fleet
engine (dry-run, backup, idempotency, round-trip, skip-when-unkeyed).
"""

from __future__ import annotations

import os

import pytest

from skchat import pq_prekeys
from skchat.group import GroupChat


# --------------------------------------------------------------------------- #
# Hybrid-default for NEW groups
# --------------------------------------------------------------------------- #


def test_new_group_defaults_hybrid_suite():
    g = GroupChat.create(name="N", creator_uri="capauth:alice@skworld.io")
    assert g.kem_suite == "x25519-mlkem768"
    assert g.is_hybrid is True


def test_classical_optout_keeps_classical():
    g = GroupChat.create(
        name="C", creator_uri="capauth:alice@skworld.io", kem_suite="rsa-pgp-wrap-v1"
    )
    assert g.is_hybrid is False
    assert g.epoch == 0


def test_field_default_stays_classical_for_deserialization():
    """Groups serialized WITHOUT kem_suite must load as classical (back-compat)."""
    g = GroupChat(name="X", created_by="capauth:alice@skworld.io")
    assert g.kem_suite == "rsa-pgp-wrap-v1"


def test_create_with_creator_hybrid_key_seeds_epoch():
    pub = ("ab" * 1216)  # plausible-length hex (content unused for seeding logic)
    g = GroupChat.create(
        name="H",
        creator_uri="capauth:alice@skworld.io",
        creator_hybrid_kem_public_hex=pub,
    )
    assert g.is_hybrid
    assert g.epoch == 1  # seeded because a member has a hybrid key
    assert g.epoch_secret_hex


# --------------------------------------------------------------------------- #
# Agent-aware prekeys
# --------------------------------------------------------------------------- #


@pytest.mark.skipif(not pq_prekeys.available(), reason="liboqs unavailable")
def test_agent_prekey_is_agent_specific(tmp_path, monkeypatch):
    monkeypatch.setenv("SKCHAT_HOME", str(tmp_path))
    b_lumina = pq_prekeys.agent_bundle("lumina")
    b_opus = pq_prekeys.agent_bundle("opus")
    assert b_lumina["suite"] == "x25519-mlkem768"
    assert b_opus["suite"] == "x25519-mlkem768"
    # Distinct agents → distinct keys, distinct files.
    assert b_lumina["hybrid_public_hex"] != b_opus["hybrid_public_hex"]
    assert (tmp_path / "pqc" / "lumina_hybrid.pub").exists()
    assert (tmp_path / "pqc" / "opus_hybrid.pub").exists()


@pytest.mark.skipif(not pq_prekeys.available(), reason="liboqs unavailable")
def test_lumina_aliases_match_agent_api(tmp_path, monkeypatch):
    monkeypatch.setenv("SKCHAT_HOME", str(tmp_path))
    assert pq_prekeys.lumina_bundle() == pq_prekeys.agent_bundle("lumina")
    assert pq_prekeys.lumina_private() == pq_prekeys.agent_private("lumina")


def test_publish_self_prekey_never_raises(tmp_path, monkeypatch):
    monkeypatch.setenv("SKCHAT_HOME", str(tmp_path))
    bundle = pq_prekeys.publish_self_prekey("tester")
    assert "suite" in bundle  # classical or hybrid — always returns a bundle


# --------------------------------------------------------------------------- #
# migrate-fleet engine
# --------------------------------------------------------------------------- #


def _seed_group(home, name, hybrid_members=0, classical_members=0, already_hybrid=False):
    from skchat.daemon_proxy_groups import save_group

    creator_pub = ("cd" * 1216) if hybrid_members or already_hybrid else ""
    g = GroupChat.create(
        name=name,
        creator_uri="capauth:alice@skworld.io",
        kem_suite="rsa-pgp-wrap-v1" if not already_hybrid else None,
        creator_hybrid_kem_public_hex=creator_pub,
    )
    for i in range(hybrid_members):
        g.add_member(
            identity_uri=f"capauth:hyb{i}@skworld.io",
            hybrid_kem_public_hex=("ef" * 1216),
        )
    for i in range(classical_members):
        g.add_member(identity_uri=f"capauth:cls{i}@skworld.io")
    save_group(g)
    return g


@pytest.mark.skipif(not pq_prekeys.available(), reason="liboqs unavailable")
def test_migrate_fleet_dry_run_writes_nothing(tmp_path, monkeypatch):
    monkeypatch.setenv("SKCHAT_HOME", str(tmp_path))
    from skchat import pqc_migrate as PM

    # Classical group, all members keyed → eligible to migrate.
    g = _seed_group(tmp_path, "Eligible", hybrid_members=1)
    before = (tmp_path / "groups" / f"{g.id}.json").read_text()
    res = PM.migrate_fleet(dry_run=True)
    after = (tmp_path / "groups" / f"{g.id}.json").read_text()
    assert before == after  # dry run wrote nothing
    actions = {p["group_id"]: p["action"] for p in res.groups["plans"]}
    assert actions[g.id] == "migrate"


@pytest.mark.skipif(not pq_prekeys.available(), reason="liboqs unavailable")
def test_migrate_fleet_skips_partially_keyed(tmp_path, monkeypatch):
    monkeypatch.setenv("SKCHAT_HOME", str(tmp_path))
    from skchat import pqc_migrate as PM

    # One keyed member + one classical-only member → must be SKIPPED, not forced.
    g = _seed_group(tmp_path, "Mixed", hybrid_members=1, classical_members=1)
    res = PM.migrate_fleet(dry_run=False, do_backup=True)
    skipped_ids = [s["group_id"] for s in res.groups["skipped"]]
    assert g.id in skipped_ids
    # Still classical on disk.
    from skchat.daemon_proxy_groups import load_group

    reloaded = load_group(g.id)
    assert reloaded.is_hybrid is False


@pytest.mark.skipif(not pq_prekeys.available(), reason="liboqs unavailable")
def test_migrate_fleet_backup_and_roundtrip_and_idempotent(tmp_path, monkeypatch):
    monkeypatch.setenv("SKCHAT_HOME", str(tmp_path))
    from skchat import pqc_migrate as PM
    from skchat.daemon_proxy_groups import load_group

    g = _seed_group(tmp_path, "Eligible", hybrid_members=1)
    res = PM.migrate_fleet(dry_run=False, do_backup=True)

    # Backup exists and contains the group.
    assert res.backup_path and os.path.isdir(res.backup_path)
    assert os.path.exists(os.path.join(res.backup_path, "groups", f"{g.id}.json"))

    migrated_ids = [m["group_id"] for m in res.groups["migrated"]]
    assert g.id in migrated_ids

    # On-disk group is hybrid and round-trips identically.
    mg = load_group(g.id)
    assert mg.is_hybrid
    rpt = mg.crypto_self_report()
    assert rpt["status"] == "hybrid-pq" and rpt["quantum_resistant"] is True
    env = mg.encrypt_message("probe")
    assert mg.decrypt_message(env) == "probe"

    # Idempotent: a second run migrates nothing new.
    res2 = PM.migrate_fleet(dry_run=False, do_backup=False)
    assert g.id not in [m["group_id"] for m in res2.groups["migrated"]]
    assert g.id in res2.groups["already_hybrid"]
