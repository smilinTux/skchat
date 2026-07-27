"""Integration: the real _resolve_peer + skcomms recipient-key seal seam that the
monkeypatched unit tests skip. Requires a real re-keyed peer store (Task 1).

This is the exact seam both live failures hit: the 404 (_resolve_peer misses)
and the 500 (skcomms _load_recipient_key same_box rejects the local key). The
unit tests in test_call_routes.py monkeypatch _list_peers/_send_invite away, so
they never exercise it.
"""

import pytest

pytestmark = pytest.mark.integration

from skchat import call_routes as CR


def _store_ready() -> bool:
    try:
        peers = CR._list_peers()
    except Exception:
        return False
    return any(k.endswith("skworld.io") for k in peers)


@pytest.mark.skipif(not _store_ready(), reason="real re-keyed peer store not present")
def test_prepare_call_resolves_and_seals_for_same_box_agent():
    # Resolve a paired same-box agent (opus) by its real fqid.
    fqid = next(k for k in CR._list_peers() if k.split("@", 1)[0] == "opus")
    resolved = CR._resolve_peer(fqid)
    assert resolved == fqid
    # The recipient key must load (this is what threw the live 500).
    from skcomms.mailbox import _load_recipient_key

    assert _load_recipient_key(resolved), "recipient key must resolve after re-key"


@pytest.mark.skipif(not _store_ready(), reason="real re-keyed peer store not present")
def test_capauth_uri_and_bare_name_resolve_to_same_paired_fqid():
    # The capauth: wire URI the Flutter client sends and the bare agent name both
    # reduce to the same paired fqid the ring seals to.
    fqid = next(k for k in CR._list_peers() if k.split("@", 1)[0] == "opus")
    assert CR._resolve_peer("capauth:opus@skworld.io") == fqid
    assert CR._resolve_peer("opus") == fqid
