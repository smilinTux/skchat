"""L3 token-stream codec (G2). Round-trip + streaming + malformed-frame gates,
and L3 dispatch through codec_ext and the mesh session/node.
"""

from __future__ import annotations

import asyncio

import pytest

pytest.importorskip("skcomms.glossa", reason="skcomms not installed")

import cbor2  # noqa: E402

from skcomms.glossa.codebook import default_codebook  # noqa: E402
from skcomms.glossa.handshake import CapabilityDescriptor  # noqa: E402
from skcomms.glossa.message import Message  # noqa: E402

from skchat.glossa_mesh import codec_ext, tokenstream  # noqa: E402
from skchat.glossa_mesh.bus import FakeBus, FakeBusMedium  # noqa: E402
from skchat.glossa_mesh.node import GlossaMeshNode  # noqa: E402
from skchat.glossa_mesh.session import GlossaMeshSession  # noqa: E402


def _msg() -> Message:
    return Message(
        intent="coord.claim",
        args={"task": "c07d4fa0", "prio": 2},
        refs=[1, 2, 3],
        text="claiming the glossa G2 sprint task",
    )


def test_l3_is_above_l2():
    assert tokenstream.L3_TOKENSTREAM == 3
    assert codec_ext.L3_TOKENSTREAM > codec_ext.L2_CODEBOOK


def test_roundtrip_no_codebook():
    m = _msg()
    raw = tokenstream.encode_l3(m)
    assert tokenstream.decode_l3(raw) == m


def test_roundtrip_with_codebook_compresses_intent():
    cb = default_codebook()
    m = _msg()
    raw_cb = tokenstream.encode_l3(m, cb)
    raw_plain = tokenstream.encode_l3(m)
    # Codebook variant round-trips AND the intent token is an int code (denser).
    assert tokenstream.decode_l3(raw_cb, cb) == m
    tokens = cbor2.loads(raw_cb)
    assert tokens[0][0] == tokenstream.T_INTENT
    assert isinstance(tokens[0][1], int)
    # Plain variant carries the raw intent string.
    assert isinstance(cbor2.loads(raw_plain)[0][1], str)


def test_roundtrip_empty_text_and_no_args():
    m = Message(intent="ack")
    assert tokenstream.decode_l3(tokenstream.encode_l3(m)) == m


def test_text_chunking_streams_but_reconstructs():
    m = Message(intent="status.report", text="abcdefghij")
    raw = tokenstream.encode_l3(m, text_chunk=3)
    tokens = cbor2.loads(raw)
    text_tokens = [t for t in tokens if t[0] == tokenstream.T_TEXT]
    assert len(text_tokens) == 4  # 10 chars / 3 -> 4 chunks (streamed)
    assert tokenstream.decode_l3(raw) == m


def test_incremental_decoder_matches_batch():
    m = _msg()
    tokens = list(tokenstream.iter_tokens(m, text_chunk=5))
    dec = tokenstream.TokenStreamDecoder()
    # Feed token-by-token; a preview Message is available before END.
    for tok in tokens[:-1]:
        dec.push(tok)
        assert not dec.complete
    preview = dec.message
    assert preview.intent == m.intent  # streaming preview usable pre-END
    dec.push(tokens[-1])
    assert dec.complete
    assert dec.message == m


def test_unterminated_stream_raises():
    m = Message(intent="ack")
    tokens = list(tokenstream.iter_tokens(m))[:-1]  # drop END
    with pytest.raises(ValueError, match="unterminated"):
        tokenstream.decode_l3(cbor2.dumps(tokens))


def test_unknown_codebook_code_raises():
    tokens = [[tokenstream.T_INTENT, 99999], [tokenstream.T_END, None]]
    with pytest.raises(ValueError, match="codebook version skew"):
        tokenstream.decode_l3(cbor2.dumps(tokens), default_codebook())


def test_malformed_token_raises():
    with pytest.raises(ValueError, match="malformed L3 frame"):
        tokenstream.decode_l3(cbor2.dumps({"not": "a list"}))


def test_codec_ext_dispatches_l3_and_delegates_l0_l2():
    cb = default_codebook()
    m = _msg()
    for level in (codec_ext.L0_ENGLISH, codec_ext.L1_SCHEMA, codec_ext.L2_CODEBOOK,
                  codec_ext.L3_TOKENSTREAM):
        raw = codec_ext.encode(m, level, cb)
        assert codec_ext.decode(raw, level, cb) == m


def _desc(fqid: str, max_level: int) -> CapabilityDescriptor:
    cb_v = default_codebook().version
    return CapabilityDescriptor(fqid=fqid, model_tier="large",
                                max_level=max_level, codebook_version=cb_v)


def test_mesh_session_roundtrips_l3_when_negotiated():
    cb = default_codebook()
    local = _desc("a@x", codec_ext.L3_TOKENSTREAM)
    peer = _desc("b@x", codec_ext.L3_TOKENSTREAM)
    sess = GlossaMeshSession(descriptor=local, codebook=cb, peers=[peer])
    assert sess.group_level == codec_ext.L3_TOKENSTREAM
    m = _msg()
    out = sess.encode(m)
    assert out["tier"] == codec_ext.L3_TOKENSTREAM
    assert sess.decode(out["wire"])["message"] == m


def test_mesh_node_broadcasts_l3_roundtrip():
    async def run():
        cb = default_codebook()
        medium = FakeBusMedium()
        a = GlossaMeshNode(descriptor=_desc("a@x", 3),
                           bus=FakeBus("a", medium), codebook=cb)
        b = GlossaMeshNode(descriptor=_desc("b@x", 3),
                           bus=FakeBus("b", medium), codebook=cb)
        got: list = []
        b.on_message(lambda src, m: got.append((src, m)))
        await a.start()
        await b.start()
        await a.announce()
        await b.announce()
        assert a.group_level == 3
        m = _msg()
        await a.say(m)
        assert got == [("a", m)]
    asyncio.run(run())
