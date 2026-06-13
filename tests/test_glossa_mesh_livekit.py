from skchat.glossa_mesh.bus import MeshBus
from skchat.glossa_mesh.livekit_bus import LiveKitBus


def test_is_a_meshbus_and_constructs_without_a_live_room():
    assert issubclass(LiveKitBus, MeshBus)
    bus = LiveKitBus(member_id="lumina@chef.skworld",
                     room_url="wss://noroc2027.tail204f0c.ts.net:8443",
                     token="x", topic="skglossa.mesh")
    assert bus.member_id == "lumina@chef.skworld"
    assert bus.topic == "skglossa.mesh"
