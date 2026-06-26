"""Fleet prekey self-registration (RFC-0001 P1 cutover prerequisite).

Co-resident agents share one ``~/.skchat/pqc/`` (and thus the ``peers/`` store), so
the prekey "exchange" for the local fleet is each agent registering its own hybrid
bundle into that shared store on publish — after which any co-resident agent resolves
it via ``load_peer_bundle`` and DMs negotiate the Level-3 ratchet.
"""

from __future__ import annotations

import pytest


def test_publish_self_registers_in_shared_peer_store(tmp_path, monkeypatch):
    monkeypatch.setenv("SKCHAT_HOME", str(tmp_path / "home"))
    from skchat import pq_prekeys as pk

    if not pk.available():
        pytest.skip("no PQ backend (liboqs) available")

    pk.publish_self_prekey("alice")
    bundle = pk.load_peer_bundle("alice")  # the resolver reads this

    assert bundle is not None
    assert bundle["suite"] == "x25519-mlkem768"
    assert bundle["hybrid_public_hex"]
    assert pk.peer_is_hybrid("alice") is True


def test_sync_fleet_publishes_all_resident_agents(tmp_path, monkeypatch):
    monkeypatch.setenv("SKCHAT_HOME", str(tmp_path / "home"))
    from skchat import pq_prekeys as pk

    if not pk.available():
        pytest.skip("no PQ backend (liboqs) available")

    pk.ensure_agent_keypair("alice")
    pk.ensure_agent_keypair("bob")

    published = pk.sync_fleet_prekeys()

    assert {"alice", "bob"} <= set(published)
    assert pk.peer_is_hybrid("alice") is True
    assert pk.peer_is_hybrid("bob") is True
