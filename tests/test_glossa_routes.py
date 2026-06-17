"""Live glossa-mesh surface: encode→decode round-trips via the REST routes, with
the audit gloss always present + human-readable, and the lexicon version surfaced.

No live LiveKit: the routes use a bus-less GlossaMeshSession on the hot path, and
the in-process GlossaMeshNode round-trip below uses FakeBus.
"""

import base64

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

# skcomms (pulled in transitively by skchat.glossa_mesh) is an optional dep —
# skip the whole module if it is absent so collection stays clean.
pytest.importorskip("skcomms.glossa", reason="skcomms not installed")

from skcomms.glossa import codec
from skcomms.glossa.codebook import default_codebook
from skcomms.glossa.handshake import CapabilityDescriptor
from skcomms.glossa.message import Message

from skchat.glossa_mesh.bus import FakeBus, FakeBusMedium
from skchat.glossa_mesh.node import GlossaMeshNode
from skchat.glossa_mesh.routes import register_glossa_routes
from skchat.glossa_mesh.session import GlossaMeshSession


@pytest.fixture
def client():
    app = FastAPI()
    register_glossa_routes(app)
    return TestClient(app)


def test_caps_surfaces_codebook_version(client):
    r = client.get("/glossa/caps")
    assert r.status_code == 200
    body = r.json()
    assert body["max_level"] == codec.L2_CODEBOOK
    assert body["codebook_version"] == default_codebook().version


def test_encode_returns_wire_gloss_tier_lexicon(client):
    r = client.post("/glossa/encode",
                    json={"intent": "coord.claim", "args": {"task": "abc"}})
    assert r.status_code == 200
    body = r.json()
    assert set(body) >= {"wire", "gloss", "tier", "lexicon_version"}
    # full caps, no peers → densest tier (L2 codebook)
    assert body["tier"] == codec.L2_CODEBOOK
    assert body["lexicon_version"] == default_codebook().version
    # audit invariant: the gloss is present + human-readable English
    assert "intent 'coord.claim'" in body["gloss"]
    assert "task=abc" in body["gloss"]
    # the wire is real base64
    base64.b64decode(body["wire"], validate=True)


def test_encode_decode_round_trip(client):
    enc = client.post("/glossa/encode", json={
        "intent": "status.report", "args": {"svc": "skmem-pg"},
        "text": "all green"}).json()
    dec = client.post("/glossa/decode", json={"wire": enc["wire"]}).json()
    assert dec["intent"] == "status.report"
    assert dec["args"] == {"svc": "skmem-pg"}
    assert dec["message_text"] == "all green"
    # the human gloss is ALWAYS present on decode (the audit view)
    assert dec["text"] == dec["gloss"]
    assert "intent 'status.report'" in dec["gloss"]
    assert "all green" in dec["gloss"]


def test_free_text_floor_is_glossable(client):
    """A bare {text} (no structured intent) still encodes + round-trips with a
    human-readable gloss — no un-glossable language on the path."""
    enc = client.post("/glossa/encode", json={"text": "ping the operator"}).json()
    assert "ping the operator" in enc["gloss"]
    dec = client.post("/glossa/decode", json={"wire": enc["wire"]}).json()
    assert "ping the operator" in dec["gloss"]


def test_weakest_peer_caps_tier_to_english(client):
    """A weak peer (L0-only) caps the negotiated tier to English; the gloss still
    holds and the round-trip works at L0."""
    weak = {"fqid": "weak@x.y", "max_level": codec.L0_ENGLISH}
    enc = client.post("/glossa/encode", json={
        "intent": "ack", "peer_caps": [weak]}).json()
    assert enc["tier"] == codec.L0_ENGLISH
    assert "intent 'ack'" in enc["gloss"]
    dec = client.post("/glossa/decode", json={"wire": enc["wire"]}).json()
    assert dec["intent"] == "ack"
    assert dec["tier"] == codec.L0_ENGLISH


def test_encode_requires_intent_or_text(client):
    r = client.post("/glossa/encode", json={"args": {"x": 1}})
    assert r.status_code == 400


def test_decode_rejects_non_base64(client):
    r = client.post("/glossa/decode", json={"wire": "not base64!!!"})
    assert r.status_code == 400


def test_decode_rejects_non_message_frame(client):
    """An ANNOUNCE frame (kind 0) is not a glossa MESSAGE — decode must 422, never
    leak an un-glossable frame as if it were a message."""
    from skchat.glossa_mesh import protocol
    cb = default_codebook()
    d = CapabilityDescriptor(fqid="a@x.y", model_tier="large",
                             max_level=codec.L2_CODEBOOK,
                             codebook_version=cb.version, lexicon_version="")
    announce = protocol.frame_announce(d)
    r = client.post("/glossa/decode",
                    json={"wire": base64.b64encode(announce).decode()})
    assert r.status_code == 422


def test_session_wire_interops_with_mesh_node():
    """A wire frame produced by the REST-path GlossaMeshSession is decodable by a
    live GlossaMeshNode over FakeBus — the surfaces share protocol framing."""
    cb = default_codebook()
    desc = CapabilityDescriptor(fqid="api@x.y", model_tier="large",
                                max_level=codec.L2_CODEBOOK,
                                codebook_version=cb.version, lexicon_version="")
    session = GlossaMeshSession(descriptor=desc, codebook=cb)
    out = session.encode(Message(intent="coord.claim", args={"task": "z"}))

    medium = FakeBusMedium()
    node = GlossaMeshNode(descriptor=desc, bus=FakeBus("node@x.y", medium),
                          codebook=cb)
    received = []
    node.on_message(lambda src, m: received.append(m))
    # inject the session's wire frame as if it arrived on the bus
    node._on_frame(out["wire"], "api@x.y")
    assert received == [Message(intent="coord.claim", args={"task": "z"})]
    # the node logged the audit gloss
    assert any("intent 'coord.claim'" in line for line in node.audit_log)
