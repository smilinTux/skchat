"""FastAPI routes for the SKGlossa mesh (spec §7) — a live, opt-in encode/decode
surface so an agent or the app can round-trip glossa over HTTP and ALWAYS get the
English audit gloss back.

Routes (additive; mounted alongside the spaces routes):
  POST /glossa/encode  body {text|intent, args?, refs?, peer_caps?, model_tier?, max_level?}
                       -> {wire(b64), gloss, tier, lexicon_version}
  POST /glossa/decode  body {wire(b64)} -> {text(gloss), gloss, tier, intent, args, refs}
  GET  /glossa/caps    -> the local capability descriptor (debug/handshake aid)

The audit invariant (spec §5) is enforced in GlossaMeshSession: the returned gloss
is computed by re-decoding the produced wire frame, so a frame that cannot be
rendered to human-readable English never reaches a caller. Raw, un-glossable
language is never put on this path.

Wire bytes travel as base64 in JSON (the frame is binary CBOR at L1/L2).
"""

from __future__ import annotations

import base64
import logging
import os

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse
from skcomms.glossa import codec
from skcomms.glossa.codebook import default_codebook
from skcomms.glossa.handshake import CapabilityDescriptor
from skcomms.glossa.message import Message

from skchat.glossa_mesh.session import GlossaMeshSession

logger = logging.getLogger("skchat.glossa_mesh.routes")


def _local_fqid() -> str:
    return (os.getenv("SKCHAT_GLOSSA_FQID")
            or os.getenv("SKCHAT_IDENTITY")
            or f"{os.getenv('SKAGENT', 'agent')}@skworld.io")


def _local_descriptor(model_tier: str = "large",
                      max_level: int = codec.L2_CODEBOOK) -> CapabilityDescriptor:
    cb = default_codebook()
    return CapabilityDescriptor(
        fqid=_local_fqid(), model_tier=model_tier, max_level=max_level,
        codebook_version=cb.version, lexicon_version="")


def _parse_peer_caps(raw: list | None) -> list[CapabilityDescriptor]:
    """Build peer descriptors from a forgiving JSON list. Each item may give just
    {max_level} (the weaker-peer signal); missing fields default to a fully-capable
    peer holding our codebook so it doesn't spuriously cap the room."""
    peers: list[CapabilityDescriptor] = []
    cb_version = default_codebook().version
    for item in raw or []:
        if not isinstance(item, dict):
            raise HTTPException(400, "each peer_caps entry must be an object")
        peers.append(CapabilityDescriptor(
            fqid=str(item.get("fqid", "peer@unknown")),
            model_tier=str(item.get("model_tier", "large")),
            max_level=int(item.get("max_level", codec.L2_CODEBOOK)),
            codebook_version=str(item.get("codebook_version", cb_version)),
            lexicon_version=str(item.get("lexicon_version", "")),
        ))
    return peers


def _message_from_body(body: dict) -> Message:
    """Accept either a structured {intent, args, refs, text} message or a bare
    {text} (free-text → the text slot of a `say` intent). Either way the result is
    a typed Message that always renders to an English gloss."""
    intent = (body.get("intent") or "").strip()
    text = body.get("text") or ""
    if not intent and not text:
        raise HTTPException(400, "intent or text required")
    if not intent:
        intent = "say"  # free-text floor: a glossable intent carrying the text slot
    args = body.get("args") or {}
    refs = body.get("refs") or []
    if not isinstance(args, dict):
        raise HTTPException(400, "args must be an object")
    if not isinstance(refs, list):
        raise HTTPException(400, "refs must be an array")
    return Message(intent=intent, args=dict(args), refs=list(refs), text=str(text))


def register_glossa_routes(app: FastAPI) -> None:
    @app.get("/glossa/caps")
    async def glossa_caps() -> JSONResponse:
        d = _local_descriptor()
        return JSONResponse({
            "fqid": d.fqid, "model_tier": d.model_tier, "max_level": d.max_level,
            "codebook_version": d.codebook_version,
            "lexicon_version": d.lexicon_version,
        })

    @app.post("/glossa/encode")
    async def glossa_encode(request: Request) -> JSONResponse:
        try:
            body = await request.json()
        except Exception as exc:  # noqa: BLE001
            raise HTTPException(400, "malformed body: expected JSON") from exc
        if not isinstance(body, dict):
            raise HTTPException(400, "body must be a JSON object")
        msg = _message_from_body(body)
        peers = _parse_peer_caps(body.get("peer_caps"))
        max_level = int(body.get("max_level", codec.L2_CODEBOOK))
        model_tier = str(body.get("model_tier", "large"))
        session = GlossaMeshSession(
            descriptor=_local_descriptor(model_tier=model_tier, max_level=max_level),
            peers=peers)
        try:
            out = session.encode(msg)
        except ValueError as exc:  # un-glossable / codec failure → never emit
            raise HTTPException(422, f"not encodable with audit gloss: {exc}") from exc
        return JSONResponse({
            "wire": base64.b64encode(out["wire"]).decode(),
            "gloss": out["gloss"],
            "tier": out["tier"],
            "lexicon_version": out["lexicon_version"],
        })

    @app.post("/glossa/decode")
    async def glossa_decode(request: Request) -> JSONResponse:
        try:
            body = await request.json()
        except Exception as exc:  # noqa: BLE001
            raise HTTPException(400, "malformed body: expected JSON") from exc
        if not isinstance(body, dict) or "wire" not in body:
            raise HTTPException(400, "body must be {wire}")
        try:
            wire = base64.b64decode(str(body["wire"]), validate=True)
        except Exception as exc:  # noqa: BLE001
            raise HTTPException(400, "wire must be base64") from exc
        session = GlossaMeshSession(descriptor=_local_descriptor())
        try:
            out = session.decode(wire)
        except ValueError as exc:
            raise HTTPException(422, f"undecodable glossa frame: {exc}") from exc
        m: Message = out["message"]
        return JSONResponse({
            "text": out["gloss"],   # the always-available human gloss (audit view)
            "gloss": out["gloss"],
            "tier": out["tier"],
            "intent": m.intent, "args": m.args, "refs": m.refs,
            "message_text": m.text,
        })
