#!/usr/bin/env python3
"""Preflight harness for the U14 inbound→reply loop (operator-runnable, offline).

Exercises the **full** skchat U14 adapter loop end-to-end on this machine, with
**no real bot, no network, no LLM** — the fakes live *only* at the two true
external boundaries:

  * the platform/bot edge → a skcomms :class:`FakeAdapter` (in-memory),
  * the consciousness/LLM edge → a stubbed ``_call_consciousness``.

Everything between those boundaries is the **real shipped code**:
:class:`skcomms.adapters.AdapterRegistry`, :class:`skchat.adapter_hub.AdapterHub`,
:class:`skchat.history.ChatHistory`, :class:`skchat.advocacy.AdvocacyEngine`, and
:class:`skchat.adapter_bind.AdapterBinder` / :class:`FqidBindingStore`.

What it proves (each an asserted stage):

  inbound loop
    1. A synthetic inbound ``ChannelMessage`` is fed through ``AdapterHub``.
    2. The sender resolves to a verified sovereign FQID (injected resolver).
    3. The converted ``ChatMessage`` is persisted to ``ChatHistory``.
    4. ``AdvocacyEngine`` fires on the ``@opus`` trigger (LLM mocked) → a reply.
    5. The reply is routed back **out** through the registry to the FakeAdapter,
       and lands in the FakeAdapter's recorded ``sent`` list.

  /bind loop
    6. A ``/bind <fqid>`` command runs through ``AdapterBinder`` with a mock
       CapAuth verifier; on a passed challenge the binding is persisted in the
       restart-durable ``FqidBindingStore`` (YAML on a temp path).

Exits 0 and prints ``PASS`` only when every stage asserted true; exits non-zero
on the first failed assertion.  Style mirrors ``scripts/tier5_verify.py``.

Usage::

    python scripts/preflight_adapter.py
"""

from __future__ import annotations

import asyncio
import json
import sys
import tempfile
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

# --- make the in-repo package importable when run from a checkout -----------
_SRC = Path(__file__).resolve().parent.parent / "src"
if _SRC.is_dir() and str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))


@dataclass
class Result:
    name: str
    passed: bool
    detail: str


@dataclass
class Harness:
    results: list[Result] = field(default_factory=list)

    def check(self, name: str, passed: bool, detail: str = "") -> bool:
        self.results.append(Result(name, bool(passed), detail))
        return bool(passed)

    @property
    def ok(self) -> bool:
        return all(r.passed for r in self.results)

    def render(self) -> None:
        width = max((len(r.name) for r in self.results), default=4)
        print("\n  stage" + " " * (width - 1) + "result  detail")
        print("  " + "-" * (width + 26))
        for r in self.results:
            mark = "PASS" if r.passed else "FAIL"
            print(f"  {r.name.ljust(width)}  {mark}    {r.detail}")
        print()


# Mocked LLM reply text — the only thing standing in for the consciousness call.
MOCK_REPLY = "Hello from preflight Opus — advocacy reply OK."


async def run() -> Harness:
    h = Harness()

    # ------------------------------------------------------------------
    # Real shipped components (fakes only at the bot + LLM boundaries).
    # ------------------------------------------------------------------
    from skcomms.adapters import FakeAdapter
    from skcomms.adapters.models import (
        ChannelMessage,
        ChannelType,
        MessageKind,
        PlatformIdentity,
    )
    from skcomms.adapters.registry import AdapterRegistry

    import skchat.advocacy as advocacy_mod
    from skchat.adapter_bind import AdapterBinder, FqidBindingStore
    from skchat.adapter_hub import (
        TRUST_VERIFIED,
        AdapterHub,
    )
    from skchat.advocacy import AdvocacyEngine
    from skchat.history import ChatHistory

    tmp = Path(tempfile.mkdtemp(prefix="preflight-adapter-"))

    # --- boundary fake #1: the bot/platform edge ----------------------
    fake = FakeAdapter()
    adapter_name = fake.adapter_name  # registry key (FakeAdapter -> "fake")
    registry = AdapterRegistry()
    registry.register(fake)
    h.check(
        "registry.register",
        registry.get(adapter_name) is fake,
        f"adapter {adapter_name!r} registered",
    )

    # --- boundary fake #2: the LLM/consciousness edge -----------------
    # AdvocacyEngine.process_message calls module-level _call_consciousness.
    # Patch it so no MCP/LLM is invoked; the rest of the engine runs for real.
    real_call = advocacy_mod._call_consciousness
    advocacy_mod._call_consciousness = lambda prompt: MOCK_REPLY  # noqa: E731

    # --- real history + advocacy + hub --------------------------------
    history = ChatHistory(history_dir=tmp / "history")
    advocacy = AdvocacyEngine(identity="capauth:opus@skworld.io")

    # Verified-resolver: maps the known platform sender to a sovereign FQID.
    known_fqid = "chef@skworld.io"
    sender = PlatformIdentity(
        channel=ChannelType.TELEGRAM,
        platform_id="424242",
        platform_name="Chef",
        room_id="-100123456789",
    )

    def resolve_fqid(platform: object) -> str | None:
        key = getattr(platform, "canonical_key", None)
        return known_fqid if key == sender.canonical_key else None

    hub = AdapterHub(
        history=history,
        advocacy=advocacy,
        resolve_fqid=resolve_fqid,
        agent_identity="capauth:opus@skworld.io",
        registry=registry,
        outbound_adapter=adapter_name,
    )

    # --- synthetic inbound message (contains the @opus trigger) -------
    inbound = ChannelMessage(
        channel=ChannelType.TELEGRAM,
        kind=MessageKind.TEXT,
        text="@opus are you receiving this through the loop?",
        sender=sender,
        room_id=sender.room_id,
        platform_msg_id="msg-1",
    )

    try:
        sent_before = len(fake.sent)
        result = await hub.dispatch_inbound(inbound)

        # stage 2: sender resolved to a verified sovereign FQID
        h.check(
            "resolve_sender",
            result.fqid == known_fqid and result.trust == TRUST_VERIFIED,
            f"fqid={result.fqid} trust={result.trust}",
        )

        # stage 3: converted ChatMessage persisted to history.
        # ChatHistory.save() (what the hub calls) appends a JSON line to
        # history_dir/YYYY-MM-DD.jsonl — read it back to prove the write.
        date_str = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        jsonl = (tmp / "history") / f"{date_str}.jsonl"
        stored = []
        if jsonl.exists():
            stored = [json.loads(ln) for ln in jsonl.read_text().splitlines() if ln.strip()]
        landed = [
            d
            for d in stored
            if d.get("sender") == known_fqid and "receiving this" in d.get("content", "")
        ]
        h.check(
            "history.persist",
            len(landed) == 1,
            f"{len(stored)} line(s) in {jsonl.name}; 1 matches the inbound",
        )

        # stage 4: advocacy fired (LLM mocked) and produced the reply
        h.check(
            "advocacy.reply",
            result.reply == MOCK_REPLY,
            f"reply={result.reply!r}",
        )

        # stage 5: reply routed back out to the FakeAdapter via the registry
        routed = fake.sent[sent_before:]
        replied = [m for m in routed if m.text == MOCK_REPLY]
        landed_room = replied and replied[-1].room_id == sender.room_id
        h.check(
            "route_reply.out",
            len(replied) == 1 and landed_room,
            f"{len(routed)} outbound on fake adapter; reply -> room {sender.room_id}",
        )
    finally:
        advocacy_mod._call_consciousness = real_call

    # ------------------------------------------------------------------
    # /bind loop — mock CapAuthVerifier, real binder + durable store.
    # ------------------------------------------------------------------
    class MockVerifier:
        """Stand-in for the real PGP CapAuth gate (the external boundary)."""

        def __init__(self) -> None:
            self.calls: list[tuple[str, str]] = []

        async def verify(self, fqid: str, platform: object) -> bool:
            self.calls.append((fqid, getattr(platform, "canonical_key", "?")))
            return True  # challenge passes

    bindings_path = tmp / "bindings.yml"
    store = FqidBindingStore(path=bindings_path)
    verifier = MockVerifier()
    # Real adapter as system-of-record sink for bind_fqid (FakeAdapter records it).
    binder = AdapterBinder(adapter=fake, verifier=verifier, store=store)

    bind_result = await binder.bind(sender, f"/bind {known_fqid}")
    h.check(
        "bind.verified",
        bind_result.ok and bind_result.fqid == known_fqid and len(verifier.calls) == 1,
        f"ok={bind_result.ok} fqid={bind_result.fqid} verifier_calls={len(verifier.calls)}",
    )

    # stage 6: binding persisted in the restart-durable store (survives reload)
    reloaded = FqidBindingStore(path=bindings_path)
    h.check(
        "bindstore.persist",
        bindings_path.exists()
        and reloaded.get(sender.canonical_key) == known_fqid,
        f"{bindings_path.name} -> {sender.canonical_key} = {reloaded.get(sender.canonical_key)}",
    )

    return h


def main() -> int:
    h = asyncio.run(run())
    h.render()

    if not h.ok:
        failed = [r.name for r in h.results if not r.passed]
        print(f"FAIL — {len(failed)} stage(s) failed: {', '.join(failed)}")
        return 1

    print("=" * 64)
    print("PASS — U14 inbound→reply loop verified end-to-end (offline).")
    print(
        "  Composed REAL: AdapterRegistry, AdapterHub, ChatHistory,\n"
        "                 AdvocacyEngine, AdapterBinder, FqidBindingStore."
    )
    print("  Faked ONLY at the boundary: FakeAdapter (bot) + _call_consciousness (LLM).")
    print("=" * 64)
    print()
    print("TO GO LIVE — the operator must supply:")
    print("  1. Telegram bot token        -> export SKCHAT_TG_BOT_TOKEN=<token>")
    print("     (BotFather token for the real bot identity)")
    print("  2. Telegram group / chat id  -> the real -100... supergroup id to bind")
    print("  3. Real CapAuth endpoint     -> swap MockVerifier for the PGP")
    print("     challenge-response gate (capauth.identity create/verify_challenge)")
    print("  4. Replace FakeAdapter with TelegramAdapter in the AdapterRegistry,")
    print("     and unstub _call_consciousness (real skcapstone MCP consciousness).")
    print()
    print("NEXT RUNBOOK STEP:")
    print("  runbooks/browser-call-test.md is for WebRTC; for THIS loop, wire the")
    print("  live TelegramAdapter then run scripts/qa_suite.sh against the running")
    print("  webui (scripts/tier5_verify.py for the LIVE Spaces/lane checks).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
