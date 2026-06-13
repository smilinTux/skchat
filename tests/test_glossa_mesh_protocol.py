from skcomms.glossa.handshake import CapabilityDescriptor

from skchat.glossa_mesh import protocol


def test_announce_frame_roundtrip():
    d = CapabilityDescriptor(fqid="a@x.y", model_tier="large", max_level=2,
                             codebook_version="cb1", lexicon_version="lx1")
    raw = protocol.frame_announce(d)
    kind, payload = protocol.parse_frame(raw)
    assert kind == protocol.ANNOUNCE
    out = protocol.read_announce(payload)
    assert out == d


def test_message_frame_carries_level():
    raw = protocol.frame_message(level=2, body=b"\x01\x02")
    kind, payload = protocol.parse_frame(raw)
    assert kind == protocol.MESSAGE
    level, body = protocol.read_message(payload)
    assert level == 2
    assert body == b"\x01\x02"


def test_parse_rejects_empty():
    import pytest
    with pytest.raises(ValueError):
        protocol.parse_frame(b"")
