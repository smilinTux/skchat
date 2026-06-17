import pytest

# skcomms (also pulled in transitively by skchat.glossa_mesh) is an optional dep
# — skip the whole module if it is absent so collection stays clean.
pytest.importorskip("skcomms.glossa", reason="skcomms not installed")

from skcomms.glossa.handshake import CapabilityDescriptor

from skchat.glossa_mesh import protocol


def test_announce_frame_roundtrip():
    d = CapabilityDescriptor(
        fqid="a@x.y",
        model_tier="large",
        max_level=2,
        codebook_version="cb1",
        lexicon_version="lx1",
    )
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


# ---------------------------------------------------------------------------
# QA Area 2 — additional protocol coverage
# ---------------------------------------------------------------------------


def test_read_message_rejects_empty_payload():
    import pytest

    with pytest.raises(ValueError):
        protocol.read_message(b"")


def test_message_level_byte_is_masked_to_one_byte():
    """A level >255 is masked with & 0xFF so the frame stays well-formed."""
    raw = protocol.frame_message(level=0x102, body=b"\x09")  # 0x102 & 0xFF == 0x02
    kind, payload = protocol.parse_frame(raw)
    assert kind == protocol.MESSAGE
    level, body = protocol.read_message(payload)
    assert level == 0x02
    assert body == b"\x09"


def test_message_frame_with_empty_body_roundtrips():
    raw = protocol.frame_message(level=1, body=b"")
    kind, payload = protocol.parse_frame(raw)
    assert kind == protocol.MESSAGE
    level, body = protocol.read_message(payload)
    assert level == 1
    assert body == b""


def test_announce_roundtrip_preserves_all_fields():
    d = CapabilityDescriptor(
        fqid="agent7@op.realm",
        model_tier="small",
        max_level=0,
        codebook_version="cbX",
        lexicon_version="lxY",
    )
    kind, payload = protocol.parse_frame(protocol.frame_announce(d))
    assert kind == protocol.ANNOUNCE
    out = protocol.read_announce(payload)
    assert out == d
    assert out.model_tier == "small"
    assert out.max_level == 0


def test_announce_and_message_kinds_are_distinct():
    a = protocol.frame_announce(
        CapabilityDescriptor(
            fqid="a@x", model_tier="large", max_level=2, codebook_version="c", lexicon_version="l"
        )
    )
    m = protocol.frame_message(level=2, body=b"x")
    assert protocol.parse_frame(a)[0] == protocol.ANNOUNCE
    assert protocol.parse_frame(m)[0] == protocol.MESSAGE
    assert protocol.ANNOUNCE != protocol.MESSAGE
