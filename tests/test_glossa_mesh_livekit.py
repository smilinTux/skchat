from types import SimpleNamespace

import pytest

# skchat.glossa_mesh imports skcomms transitively — an optional dep. Skip the
# whole module if skcomms is absent so collection stays clean.
pytest.importorskip("skcomms.glossa", reason="skcomms not installed")

from skchat.glossa_mesh.bus import MeshBus
from skchat.glossa_mesh.livekit_bus import LiveKitBus


def _bus() -> LiveKitBus:
    return LiveKitBus(
        member_id="lumina@chef.skworld",
        room_url="wss://noroc2027.tail204f0c.ts.net:8443",
        token="x",
        topic="skglossa.mesh",
    )


def test_is_a_meshbus_and_constructs_without_a_live_room():
    assert issubclass(LiveKitBus, MeshBus)
    bus = _bus()
    assert bus.member_id == "lumina@chef.skworld"
    assert bus.topic == "skglossa.mesh"


def test_participant_disconnected_fires_on_leave_with_departed_id():
    bus = _bus()
    seen: list[str] = []
    bus.on_leave(seen.append)

    # simulate the room 'participant_disconnected' event — no live LiveKit room.
    bus._on_participant_disconnected(SimpleNamespace(identity="ava@chef.skworld"))

    assert seen == ["ava@chef.skworld"]


def test_participant_disconnected_is_noop_when_no_callback_registered():
    bus = _bus()
    # must not raise without an on_leave callback registered.
    bus._on_participant_disconnected(SimpleNamespace(identity="ava@chef.skworld"))


def test_participant_disconnected_ignores_identityless_event():
    bus = _bus()
    seen: list[str] = []
    bus.on_leave(seen.append)

    # a participant with no identity (or empty) must not fire the leave callback.
    bus._on_participant_disconnected(SimpleNamespace(identity=""))
    bus._on_participant_disconnected(SimpleNamespace())

    assert seen == []


def test_on_leave_wires_node_forget_peer_to_uncap_departed_peer():
    """End-to-end seam: a disconnect drives the registered LeaveCb (the Node wires
    forget_peer here), proving the bus un-caps a departed peer test-only."""
    bus = _bus()
    forgotten: list[str] = []

    # stand-in for GlossaMeshNode.forget_peer — same (member_id) -> None signature.
    bus.on_leave(forgotten.append)
    bus._on_participant_disconnected(SimpleNamespace(identity="weakling@chef.skworld"))

    assert forgotten == ["weakling@chef.skworld"]
