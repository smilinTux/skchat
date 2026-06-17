import pytest

# skchat.glossa_mesh imports skcomms transitively — an optional dep. Skip the
# whole module if skcomms is absent so collection stays clean.
pytest.importorskip("skcomms.glossa", reason="skcomms not installed")

from skchat.glossa_mesh.modem import AudioModem


def test_byte_roundtrip_through_samples():
    m = AudioModem()
    data = b"hello-glossa"
    samples = m.encode(data)
    assert isinstance(samples, list) and len(samples) > 0
    assert m.decode(samples) == data


def test_empty_and_binary_roundtrip():
    m = AudioModem()
    assert m.decode(m.encode(b"")) == b""
    payload = bytes(range(256))
    assert m.decode(m.encode(payload)) == payload


def test_decode_tolerates_mild_amplitude_scaling():
    m = AudioModem()
    samples = [s * 0.5 for s in m.encode(b"AB")]   # quieter, same tones
    assert m.decode(samples) == b"AB"
