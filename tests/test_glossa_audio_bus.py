import asyncio

import pytest

# skchat.glossa_mesh imports skcomms transitively — an optional dep. Skip the
# whole module if skcomms is absent so collection stays clean.
pytest.importorskip("skcomms.glossa", reason="skcomms not installed")

from skchat.glossa_mesh.audio_bus import AudioMeshBus
from skchat.glossa_mesh.bus import MeshBus
from skchat.glossa_mesh.mac import FakeAudioMedium


def test_is_a_meshbus():
    assert issubclass(AudioMeshBus, MeshBus)


@pytest.mark.asyncio
async def test_frame_survives_the_acoustic_round_trip():
    med = FakeAudioMedium()
    a = AudioMeshBus("a", med)
    b = AudioMeshBus("b", med)
    got = []
    b.on_receive(lambda data, src: got.append((data, src)))
    await a.start()
    await b.start()
    await a.broadcast(b"sk-over-audio")
    await asyncio.sleep(0.01)
    assert got == [(b"sk-over-audio", "a")]  # modulated, transmitted, demodulated


@pytest.mark.asyncio
async def test_sender_does_not_hear_itself():
    med = FakeAudioMedium()
    a = AudioMeshBus("a", med)
    got = []
    a.on_receive(lambda d, s: got.append(d))
    await a.start()
    await a.broadcast(b"x")
    await asyncio.sleep(0.01)
    assert got == []
