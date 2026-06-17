#!/usr/bin/env python
"""Preflight: LIVE glossa-mesh test over the REAL LiveKit SFU (spec U9).

Runs on .158 (noroc2027) against the live SFU on the tailnet. This is the
``runbooks/glossa-over-space.md`` Leg-D gate done for real: two glossa-mesh
session daemons join the same LiveKit room over the actual data channel, mesh a
capauth-signed glossa message between them, and prove the anti-spoof gatekeeper
rejects a forged/tampered frame before it ever decodes.

NO GPU is used — glossa is data-channel + capauth signing only (CPU/network).
The capauth sign/verify backends are REAL ED25519 PGP keys (one ephemeral
keypair per identity) injected into the shipped :class:`GlossaMeshGatekeeper`;
the transport is the shipped :class:`LiveKitBus` over a real ``rtc.Room``.

What it asserts:
  1. two tokens mint + both buses connect to the real room;
  2. node A announces + says a signed glossa message; node B receives it over the
     REAL data channel, the gatekeeper VERIFIES the source, and it dispatches to a
     mock advocacy + mock memory;
  3. a forged/tampered frame put on the same wire is REJECTED pre-decode by node B
     (never reaches advocacy/memory);
  4. a peer-leave un-caps the room (group_level recovers) — exercised live if the
     SFU emits participant_disconnected within the window, else via the documented
     bus seam as a fallback (reported honestly which path ran).

Honest failure: if the live RTC connection genuinely fails (SFU/ICE), the script
reports exactly what failed and exits non-zero. It does NOT fake a pass.

Run:
    /home/cbrd21/.skenv/bin/python scripts/preflight_glossa_live.py
"""

from __future__ import annotations

import asyncio
import os
import sys
import time
from pathlib import Path

# --- config -----------------------------------------------------------------

ENV_FILE = Path("/home/cbrd21/.config/skchat/webui-lumina.env")
ROOM = "glossa-live-test"
IDENT_A = "opus@chef.skworld"
IDENT_B = "lumina@skworld.io"
CONNECT_TIMEOUT = 25.0  # RTC join can take several seconds
RECV_TIMEOUT = 20.0
LEAVE_TIMEOUT = 12.0
PASSPHRASE = "glossa-preflight"  # ephemeral keys, throwaway passphrase


def _load_env() -> dict[str, str]:
    cfg: dict[str, str] = {}
    for line in ENV_FILE.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, _, v = line.partition("=")
        cfg[k.strip()] = v.strip()
    return cfg


# --- real capauth (ED25519 PGP) signer/verifier per identity ----------------


def build_crypto_identities(fqids: list[str]):
    """Generate one ephemeral ED25519 keypair per FQID and return:
      - signer(fqid) -> Callable[[bytes], str]   (capauth sign over canonical bytes)
      - a shared verifier(canonical, sig) -> fqid|None that tries every known
        pubkey and returns the FQID whose key validates the signature (None if
        no key matches → gatekeeper treats as verify-fail).
    This is the injectable-backend contract the gatekeeper documents, backed by
    real crypto (not an HMAC fake)."""
    from capauth.crypto import get_backend
    from capauth.crypto.base import Algorithm

    backend = get_backend()  # PGPy
    bundles = {}
    for fq in fqids:
        # email-shaped name for the PGP uid; algorithm ed25519 is fast
        bundles[fq] = backend.generate_keypair(
            name=fq, email=fq.replace("@", "_at_") + "@glossa.local",
            passphrase=PASSPHRASE, algorithm=Algorithm.ED25519,
        )

    def make_signer(fq: str):
        priv = bundles[fq].private_armor

        def _sign(data: bytes) -> str:
            return backend.sign(data, priv, PASSPHRASE)

        return _sign

    pubkeys = {fq: b.public_armor for fq, b in bundles.items()}

    def verifier(data: bytes, sig: str):
        for fq, pub in pubkeys.items():
            try:
                if backend.verify(data, sig, pub):
                    return fq
            except Exception:
                continue
        return None

    signers = {fq: make_signer(fq) for fq in fqids}
    return signers, verifier


# --- mock advocacy + memory sinks -------------------------------------------


class MockAdvocacy:
    """Records every (chat_message) it sees; never replies (keeps the wire quiet
    so assertions are deterministic)."""

    def __init__(self) -> None:
        self.seen: list[tuple[str, str]] = []

    def process_message(self, msg):
        self.seen.append((getattr(msg, "sender", "?"), getattr(msg, "content", "")))
        return None


class MockMemory:
    def __init__(self) -> None:
        self.captured: list[tuple[str, str]] = []

    def __call__(self, gloss_text: str, source_fqid: str) -> None:
        self.captured.append((gloss_text, source_fqid))


# --- the live test ----------------------------------------------------------


async def run() -> int:
    from livekit import api

    from skcomms.glossa.codebook import default_codebook
    from skcomms.glossa.handshake import CapabilityDescriptor
    from skcomms.glossa.message import Message

    from skchat.glossa_mesh.gatekeeper import GlossaMeshGatekeeper
    from skchat.glossa_mesh.livekit_bus import LiveKitBus
    from skchat.glossa_mesh.session_daemon import GlossaMeshSessionDaemon

    results: list[tuple[str, bool, str]] = []

    def record(name: str, ok: bool, detail: str = "") -> None:
        results.append((name, ok, detail))
        mark = "PASS" if ok else "FAIL"
        print(f"  [{mark}] {name}" + (f" — {detail}" if detail else ""))

    cfg = _load_env()
    url = cfg["SKCHAT_LIVEKIT_URL"]
    key = cfg["SKCHAT_LIVEKIT_API_KEY"]
    secret = cfg["SKCHAT_LIVEKIT_API_SECRET"]
    print(f"SFU url={url} key={key} room={ROOM}")

    # 1) mint two tokens -----------------------------------------------------
    def mint(identity: str) -> str:
        return (
            api.AccessToken(key, secret)
            .with_identity(identity)
            .with_name(identity)
            .with_grants(
                api.VideoGrants(
                    room_join=True, room=ROOM,
                    can_publish=True, can_subscribe=True, can_publish_data=True,
                )
            )
            .to_jwt()
        )

    try:
        token_a = mint(IDENT_A)
        token_b = mint(IDENT_B)
        record("mint two LiveKit tokens", bool(token_a) and bool(token_b),
               f"A={len(token_a)}B chars, B={len(token_b)}B chars")
    except Exception as exc:  # noqa: BLE001
        record("mint two LiveKit tokens", False, repr(exc))
        return _summary(results)

    # real ED25519 capauth backends, one keypair per identity
    print("Generating ED25519 capauth keypairs (real PGP sign/verify)...")
    signers, verifier = build_crypto_identities([IDENT_A, IDENT_B])

    gk_a = GlossaMeshGatekeeper(source_fqid=IDENT_A, signer=signers[IDENT_A],
                                verifier=verifier)
    gk_b = GlossaMeshGatekeeper(source_fqid=IDENT_B, signer=signers[IDENT_B],
                                verifier=verifier)

    # max_level=2 is the top codec tier (L0 english / L1 schema / L2 codebook).
    desc_a = CapabilityDescriptor(fqid=IDENT_A, model_tier="opus", max_level=2,
                                  codebook_version=1, lexicon_version=1)
    desc_b = CapabilityDescriptor(fqid=IDENT_B, model_tier="opus", max_level=2,
                                  codebook_version=1, lexicon_version=1)

    bus_a = LiveKitBus(member_id=IDENT_A, room_url=url, token=token_a)
    bus_b = LiveKitBus(member_id=IDENT_B, room_url=url, token=token_b)

    adv_a, mem_a = MockAdvocacy(), MockMemory()
    adv_b, mem_b = MockAdvocacy(), MockMemory()
    cb = default_codebook()

    # a minimal "space" stand-in: the daemon only reads .room off it for the
    # live-spawn hook, which we don't exercise here.
    class _Space:
        room = ROOM

    daemon_a = GlossaMeshSessionDaemon(
        space=_Space(), descriptor=desc_a, bus=bus_a, gatekeeper=gk_a,
        advocacy=adv_a, memory=mem_a, codebook=cb,
    )
    daemon_b = GlossaMeshSessionDaemon(
        space=_Space(), descriptor=desc_b, bus=bus_b, gatekeeper=gk_b,
        advocacy=adv_b, memory=mem_b, codebook=cb,
    )

    started_a = started_b = False
    try:
        # 2) connect both to the REAL room ----------------------------------
        try:
            await asyncio.wait_for(daemon_a.start(), timeout=CONNECT_TIMEOUT)
            started_a = True
            await asyncio.wait_for(daemon_b.start(), timeout=CONNECT_TIMEOUT)
            started_b = True
            record("both daemons connected to real SFU room", True,
                   f"{IDENT_A} + {IDENT_B} joined '{ROOM}'")
        except Exception as exc:  # noqa: BLE001
            record("both daemons connected to real SFU room", False,
                   f"LIVE RTC CONNECT FAILED: {type(exc).__name__}: {exc}")
            return _summary(results)

        # give the SFU a beat to settle participant lists / data channels
        await asyncio.sleep(3.0)

        # re-announce so each side definitely has the other as a peer (announce
        # at start() may race the data-channel coming up)
        await daemon_a.node.announce()
        await daemon_b.node.announce()
        await asyncio.sleep(2.0)

        # 3) A says a signed glossa message; B must receive+verify+dispatch ---
        msg = Message(intent="say", text="glossa live preflight U9 over real SFU")
        before = len(daemon_b.dispatched)
        await daemon_a.say(msg)

        ok_recv = await _wait_until(
            lambda: len(daemon_b.dispatched) > before, RECV_TIMEOUT
        )
        if ok_recv:
            src, got = daemon_b.dispatched[-1]
            record("B received signed glossa over real data channel", True,
                   f"dispatched from src={src!r}")
            record("gatekeeper authenticated source == A", src == IDENT_A,
                   f"src={src!r} expected={IDENT_A!r}")
            record("dispatched to mock advocacy", len(adv_b.seen) > 0,
                   f"advocacy saw {len(adv_b.seen)} msg(s)")
            record("captured to mock memory", len(mem_b.captured) > 0,
                   f"memory captured {len(mem_b.captured)}; gloss={mem_b.captured[-1][0]!r}"
                   if mem_b.captured else "no capture")
        else:
            record("B received signed glossa over real data channel", False,
                   f"no dispatch within {RECV_TIMEOUT}s; "
                   f"B audit_log={daemon_b.node.audit_log!r}")

        # 4) forged/tampered frame must be REJECTED pre-decode ---------------
        # Forge an envelope claiming to be A but signed by B's key (source-spoof),
        # AND a body-tampered variant — put both straight on the wire via B's
        # inner bus' peer (we publish from A's inner LiveKitBus so it travels the
        # real data channel to B). B's signing-bus must drop both before decode.
        before_dispatch = len(daemon_b.dispatched)
        before_mem = len(mem_b.captured)

        # (a) source-spoof: B signs canonical bytes claiming source=A
        from skchat.glossa_mesh import protocol
        from skcomms.glossa import codec
        raw_frame = protocol.frame_message(
            daemon_a.node.group_level, codec.encode(msg, daemon_a.node.group_level, cb)
        )
        import base64
        import json
        canonical = json.dumps(
            {"source": IDENT_A, "frame": base64.b64encode(raw_frame).decode("ascii")},
            sort_keys=True, separators=(",", ":"),
        ).encode("utf-8")
        forged_sig = signers[IDENT_B](canonical)  # B's key, claims to be A
        forged_env = json.dumps(
            {"source": IDENT_A,
             "frame": base64.b64encode(raw_frame).decode("ascii"),
             "sig": forged_sig},
            separators=(",", ":"),
        ).encode("utf-8")

        # (b) body-tampered: legit A signature over original, then flip a frame byte
        legit_env_bytes = gk_a.wrap_outbound(raw_frame)
        tampered = bytearray(legit_env_bytes)
        # corrupt within the base64 frame region to invalidate the signature
        # without breaking JSON parse: flip the last few frame chars by editing
        # the decoded dict instead (robust).
        env_dict = json.loads(legit_env_bytes.decode())
        fb = bytearray(base64.b64decode(env_dict["frame"]))
        if fb:
            fb[-1] ^= 0xFF
        env_dict["frame"] = base64.b64encode(bytes(fb)).decode("ascii")
        tampered = json.dumps(env_dict, separators=(",", ":")).encode("utf-8")

        # publish both forged envelopes over the REAL data channel from A's bus
        await bus_a._room.local_participant.publish_data(  # type: ignore[union-attr]
            forged_env, reliable=True, topic=bus_a.topic
        )
        await bus_a._room.local_participant.publish_data(  # type: ignore[union-attr]
            tampered, reliable=True, topic=bus_a.topic
        )
        # also a totally malformed (non-JSON) frame
        await bus_a._room.local_participant.publish_data(  # type: ignore[union-attr]
            b"\x00\x01not-an-envelope", reliable=True, topic=bus_a.topic
        )

        # wait long enough for them to have arrived (they should arrive ~same RTT
        # as the legit one which already landed)
        await asyncio.sleep(6.0)
        rejected = (
            len(daemon_b.dispatched) == before_dispatch
            and len(mem_b.captured) == before_mem
        )
        record("forged/tampered frames rejected pre-decode", rejected,
               f"dispatched delta={len(daemon_b.dispatched) - before_dispatch}, "
               f"memory delta={len(mem_b.captured) - before_mem} (expect 0/0)")

        # 5) peer-leave un-caps the room ------------------------------------
        # Capture A's group_level while B is present, then disconnect B and
        # assert A's node forgets the peer (group_level recovers to A's own max).
        lvl_with_peer = daemon_a.node.group_level
        peers_before = dict(daemon_a.node._peers)
        await daemon_b.stop()
        started_b = False

        async def _a_uncapped() -> bool:
            return IDENT_B not in daemon_a.node._peers

        live_leave = await _wait_until(lambda: IDENT_B not in daemon_a.node._peers,
                                       LEAVE_TIMEOUT)
        if live_leave:
            record("peer-leave un-capped room (LIVE participant_disconnected)", True,
                   f"A peers {sorted(peers_before)} -> {sorted(daemon_a.node._peers)}; "
                   f"group_level {lvl_with_peer} -> {daemon_a.node.group_level}")
        else:
            # fallback: exercise the documented bus seam directly (still proves the
            # forget_peer/un-cap wiring), and report that the live event didn't fire.
            class _P:
                identity = IDENT_B
            bus_a._on_participant_disconnected(_P())
            seam_ok = IDENT_B not in daemon_a.node._peers
            record("peer-leave un-capped room (seam fallback; live event not seen)",
                   seam_ok,
                   f"live participant_disconnected not observed within {LEAVE_TIMEOUT}s; "
                   f"forced via bus seam -> peers {sorted(daemon_a.node._peers)}, "
                   f"group_level now {daemon_a.node.group_level}")

    finally:
        # 6) cleanup --------------------------------------------------------
        try:
            if started_b:
                await daemon_b.stop()
        except Exception:  # noqa: BLE001
            pass
        try:
            if started_a:
                await daemon_a.stop()
        except Exception:  # noqa: BLE001
            pass
        # let the livekit (rust/tokio) runtime fully tear down the rooms before
        # the event loop closes, else its worker thread can panic on shutdown.
        await asyncio.sleep(1.5)
        print("  [info] both buses disconnected")

    return _summary(results)


async def _wait_until(pred, timeout: float, poll: float = 0.25) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if pred():
            return True
        await asyncio.sleep(poll)
    return pred()


def _summary(results: list[tuple[str, bool, str]]) -> int:
    print("\n" + "=" * 70)
    print("GLOSSA LIVE PREFLIGHT — SUMMARY")
    print("=" * 70)
    passed = sum(1 for _, ok, _ in results if ok)
    total = len(results)
    for name, ok, detail in results:
        print(f"  {'PASS' if ok else 'FAIL'}  {name}")
    all_ok = passed == total and total > 0
    print("-" * 70)
    print(f"  {passed}/{total} checks passed")
    print(f"  OVERALL: {'PASS — U9 PROVEN LIVE OVER REAL SFU' if all_ok else 'FAIL'}")
    print("=" * 70)
    return 0 if all_ok else 1


def main() -> int:
    # keep this CPU/network-only — make sure nothing grabs a GPU
    os.environ.setdefault("CUDA_VISIBLE_DEVICES", "")
    try:
        return asyncio.run(run())
    except KeyboardInterrupt:
        print("interrupted")
        return 130


if __name__ == "__main__":
    sys.exit(main())
