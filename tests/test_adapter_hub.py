"""Unit tests for skchat.adapter_hub — AdapterHub inbound bridge.

All collaborators are injected as mocks: no network, no real skcomms adapters,
no skmem-pg.  We use the REAL skcomms ChannelMessage / PlatformIdentity
dataclasses so conversion fidelity is tested against the actual shape.
"""

from __future__ import annotations

from datetime import datetime, timezone
from unittest.mock import MagicMock

import pytest
from skcomms.adapters.fake import FakeAdapter
from skcomms.adapters.models import (
    ChannelMessage,
    ChannelType,
    MediaAttachment,
    MessageKind,
    PlatformIdentity,
)

from skchat.adapter_hub import (
    TRUST_UNTRUSTED,
    TRUST_VERIFIED,
    AdapterHub,
    InboundResult,
)
from skchat.models import ChatMessage, ContentType

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

AGENT = "capauth:opus@skworld.io"


def _platform(
    channel: ChannelType = ChannelType.TELEGRAM,
    platform_id: str = "123456789",
    platform_name: str = "Chef David",
    room_id: str = "-5134021983",
) -> PlatformIdentity:
    return PlatformIdentity(
        channel=channel,
        platform_id=platform_id,
        platform_name=platform_name,
        room_id=room_id,
    )


def _chan_msg(
    text: str = "hello there",
    kind: MessageKind = MessageKind.TEXT,
    channel: ChannelType = ChannelType.TELEGRAM,
    timestamp: datetime | None = None,
    attachments: list[MediaAttachment] | None = None,
    platform: PlatformIdentity | None = None,
) -> ChannelMessage:
    platform = platform or _platform(channel=channel)
    return ChannelMessage(
        channel=channel,
        kind=kind,
        text=text,
        sender=platform,
        room_id=platform.room_id,
        attachments=attachments or [],
        timestamp=timestamp or datetime(2026, 6, 17, 12, 0, 0, tzinfo=timezone.utc),
        platform_msg_id="plat-msg-1",
    )


@pytest.fixture
def history():
    """A mock ChatHistory exposing save()."""
    h = MagicMock()
    h.save = MagicMock()
    return h


@pytest.fixture
def advocacy():
    """A mock advocacy engine; process_message returns None by default."""
    a = MagicMock()
    a.process_message = MagicMock(return_value=None)
    return a


@pytest.fixture
def resolver_map():
    """A resolve_fqid that maps a known platform identity to an FQID."""
    known = {"telegram:user:123456789": "chef@skworld.io"}

    def _resolve(platform: PlatformIdentity):
        return known.get(platform.canonical_key)

    return _resolve


@pytest.fixture
def hub(history, advocacy, resolver_map):
    return AdapterHub(
        history=history,
        advocacy=advocacy,
        resolve_fqid=resolver_map,
        agent_identity=AGENT,
    )


# ---------------------------------------------------------------------------
# Construction / injection
# ---------------------------------------------------------------------------


class TestConstruction:
    def test_default_agent_identity(self, history, advocacy):
        h = AdapterHub(history=history, advocacy=advocacy)
        assert h.agent_identity == AdapterHub.DEFAULT_AGENT_IDENTITY

    def test_custom_agent_identity(self, hub):
        assert hub.agent_identity == AGENT

    def test_hub_fallback_pulls_collaborators(self, history, advocacy, resolver_map):
        parent = MagicMock()
        parent.history = history
        parent.advocacy = advocacy
        parent.resolve_fqid = resolver_map
        parent.agent_identity = "capauth:lumina@skworld.io"
        h = AdapterHub(hub=parent)
        assert h._history is history
        assert h._advocacy is advocacy
        assert h._resolve_fqid is resolver_map
        assert h.agent_identity == "capauth:lumina@skworld.io"

    def test_explicit_overrides_hub(self, history, advocacy, resolver_map):
        parent = MagicMock()
        parent.history = MagicMock()
        h = AdapterHub(hub=parent, history=history)
        assert h._history is history

    def test_none_advocacy_allowed(self, history, resolver_map):
        h = AdapterHub(history=history, advocacy=None, resolve_fqid=resolver_map)
        assert h._advocacy is None


# ---------------------------------------------------------------------------
# Sender resolution / trust
# ---------------------------------------------------------------------------


class TestResolveSender:
    def test_resolved_is_verified(self, hub):
        fqid, trust = hub.resolve_sender(_chan_msg())
        assert fqid == "chef@skworld.io"
        assert trust == TRUST_VERIFIED

    def test_unresolved_is_untrusted(self, hub):
        # An identity the resolver_map does not know about.
        msg = _chan_msg(platform=_platform(platform_id="999"))
        fqid, trust = hub.resolve_sender(msg)
        assert trust == TRUST_UNTRUSTED
        assert fqid == "telegram_guest_999@ext"

    def test_none_resolver_is_untrusted(self, history, advocacy):
        h = AdapterHub(history=history, advocacy=advocacy, resolve_fqid=None)
        fqid, trust = h.resolve_sender(_chan_msg())
        assert trust == TRUST_UNTRUSTED
        assert fqid == "telegram_guest_123456789@ext"

    def test_resolver_returning_empty_string_is_untrusted(self, history):
        h = AdapterHub(history=history, resolve_fqid=lambda p: "")
        _fqid, trust = h.resolve_sender(_chan_msg())
        assert trust == TRUST_UNTRUSTED

    def test_resolver_exception_falls_back_untrusted(self, history):
        def boom(_platform):
            raise RuntimeError("resolver exploded")

        h = AdapterHub(history=history, resolve_fqid=boom)
        fqid, trust = h.resolve_sender(_chan_msg())
        assert trust == TRUST_UNTRUSTED
        assert fqid.startswith("telegram_guest_")

    def test_guest_fqid_uses_channel_value(self, history):
        h = AdapterHub(history=history, resolve_fqid=None)
        msg = _chan_msg(channel=ChannelType.SLACK)
        fqid, _trust = h.resolve_sender(msg)
        assert fqid.startswith("slack_guest_")


# ---------------------------------------------------------------------------
# Conversion fidelity
# ---------------------------------------------------------------------------


class TestToChatMessage:
    def test_preserves_text(self, hub):
        msg = _chan_msg(text="the quick brown fox")
        chat = hub.to_chat_message(msg, "chef@skworld.io", TRUST_VERIFIED)
        assert chat.content == "the quick brown fox"

    def test_preserves_sender_fqid(self, hub):
        chat = hub.to_chat_message(_chan_msg(), "chef@skworld.io", TRUST_VERIFIED)
        assert chat.sender == "chef@skworld.io"

    def test_recipient_is_agent(self, hub):
        chat = hub.to_chat_message(_chan_msg(), "chef@skworld.io", TRUST_VERIFIED)
        assert chat.recipient == AGENT

    def test_preserves_timestamp(self, hub):
        ts = datetime(2026, 6, 17, 9, 30, 0, tzinfo=timezone.utc)
        chat = hub.to_chat_message(_chan_msg(timestamp=ts), "chef@skworld.io", TRUST_VERIFIED)
        assert chat.timestamp == ts

    def test_naive_timestamp_coerced_to_utc(self, hub):
        naive = datetime(2026, 6, 17, 9, 30, 0)
        chat = hub.to_chat_message(_chan_msg(timestamp=naive), "x@y", TRUST_VERIFIED)
        assert chat.timestamp.tzinfo is not None
        assert chat.timestamp.utcoffset().total_seconds() == 0

    def test_returns_chat_message_instance(self, hub):
        chat = hub.to_chat_message(_chan_msg(), "chef@skworld.io", TRUST_VERIFIED)
        assert isinstance(chat, ChatMessage)

    def test_content_type_is_plain(self, hub):
        chat = hub.to_chat_message(_chan_msg(), "chef@skworld.io", TRUST_VERIFIED)
        assert chat.content_type == ContentType.PLAIN

    def test_metadata_captures_channel_provenance(self, hub):
        chat = hub.to_chat_message(_chan_msg(), "chef@skworld.io", TRUST_VERIFIED)
        meta = chat.metadata
        assert meta["source"] == "channel_adapter"
        assert meta["channel"] == "telegram"
        assert meta["kind"] == "text"
        assert meta["platform_id"] == "123456789"
        assert meta["platform_name"] == "Chef David"
        assert meta["platform_msg_id"] == "plat-msg-1"

    def test_untrusted_flag_in_metadata(self, hub):
        chat = hub.to_chat_message(_chan_msg(), "telegram_guest_1@ext", TRUST_UNTRUSTED)
        assert chat.metadata["trust"] == TRUST_UNTRUSTED
        assert chat.metadata["untrusted"] is True

    def test_verified_has_no_untrusted_flag(self, hub):
        chat = hub.to_chat_message(_chan_msg(), "chef@skworld.io", TRUST_VERIFIED)
        assert "untrusted" not in chat.metadata

    def test_empty_text_voice_gets_placeholder(self, hub):
        msg = _chan_msg(text="", kind=MessageKind.VOICE)
        chat = hub.to_chat_message(msg, "chef@skworld.io", TRUST_VERIFIED)
        assert chat.content == "[voice]"

    def test_empty_text_image_gets_placeholder(self, hub):
        att = MediaAttachment(filename="pic.png", mime_type="image/png", size_bytes=10)
        msg = _chan_msg(text="", kind=MessageKind.IMAGE, attachments=[att])
        chat = hub.to_chat_message(msg, "chef@skworld.io", TRUST_VERIFIED)
        assert chat.content == "[image]"

    def test_whitespace_only_text_gets_placeholder(self, hub):
        msg = _chan_msg(text="   ", kind=MessageKind.TEXT)
        chat = hub.to_chat_message(msg, "chef@skworld.io", TRUST_VERIFIED)
        assert chat.content == "[text]"

    def test_iso_string_timestamp_parsed(self, hub):
        msg = _chan_msg()
        # ChannelMessage.timestamp is normally a datetime, but the coercer
        # must also accept ISO strings defensively.
        object.__setattr__(msg, "timestamp", "2026-06-17T08:00:00+00:00")
        chat = hub.to_chat_message(msg, "x@y", TRUST_VERIFIED)
        assert chat.timestamp == datetime(2026, 6, 17, 8, 0, 0, tzinfo=timezone.utc)


# ---------------------------------------------------------------------------
# handle_inbound — full pipeline (memory write + advocacy dispatch)
# ---------------------------------------------------------------------------


class TestHandleInbound:
    def test_returns_inbound_result(self, hub):
        result = hub.handle_inbound(_chan_msg())
        assert isinstance(result, InboundResult)

    def test_writes_to_history(self, hub, history):
        hub.handle_inbound(_chan_msg(text="store me"))
        history.save.assert_called_once()
        stored = history.save.call_args.args[0]
        assert isinstance(stored, ChatMessage)
        assert stored.content == "store me"

    def test_history_write_uses_resolved_fqid(self, hub, history):
        hub.handle_inbound(_chan_msg())
        stored = history.save.call_args.args[0]
        assert stored.sender == "chef@skworld.io"

    def test_advocacy_dispatched_with_chat_message(self, hub, advocacy, history):
        hub.handle_inbound(_chan_msg(text="@opus hi"))
        advocacy.process_message.assert_called_once()
        passed = advocacy.process_message.call_args.args[0]
        assert isinstance(passed, ChatMessage)
        # The advocacy engine receives the same object that was stored.
        assert passed is history.save.call_args.args[0]

    def test_advocacy_reply_captured(self, hub, advocacy):
        advocacy.process_message.return_value = "Hello from Opus"
        result = hub.handle_inbound(_chan_msg(text="@opus hi"))
        assert result.reply == "Hello from Opus"

    def test_no_advocacy_reply_when_none(self, hub, advocacy):
        advocacy.process_message.return_value = None
        result = hub.handle_inbound(_chan_msg(text="just chatting"))
        assert result.reply is None

    def test_untrusted_path_end_to_end(self, hub, history):
        msg = _chan_msg(platform=_platform(platform_id="000"))
        result = hub.handle_inbound(msg)
        assert result.trust == TRUST_UNTRUSTED
        assert result.fqid == "telegram_guest_000@ext"
        assert result.message.metadata["untrusted"] is True
        # Still stored despite being untrusted.
        history.save.assert_called_once()

    def test_verified_path_end_to_end(self, hub):
        result = hub.handle_inbound(_chan_msg())
        assert result.trust == TRUST_VERIFIED
        assert result.is_trusted is True
        assert result.fqid == "chef@skworld.io"

    def test_no_advocacy_engine_skips_dispatch(self, history, resolver_map):
        h = AdapterHub(history=history, advocacy=None, resolve_fqid=resolver_map)
        result = h.handle_inbound(_chan_msg(text="@opus hi"))
        assert result.reply is None
        history.save.assert_called_once()

    def test_advocacy_exception_does_not_drop_message(self, history, resolver_map):
        adv = MagicMock()
        adv.process_message.side_effect = RuntimeError("LLM down")
        h = AdapterHub(history=history, advocacy=adv, resolve_fqid=resolver_map)
        result = h.handle_inbound(_chan_msg(text="@opus hi"))
        # Message still stored, reply is None, no exception propagated.
        assert result.reply is None
        history.save.assert_called_once()

    def test_missing_history_is_tolerated(self, advocacy, resolver_map):
        h = AdapterHub(history=None, advocacy=advocacy, resolve_fqid=resolver_map)
        result = h.handle_inbound(_chan_msg(text="@opus hi"))
        # No crash; advocacy still fired.
        assert isinstance(result, InboundResult)
        advocacy.process_message.assert_called_once()

    def test_order_history_before_advocacy(self, resolver_map):
        calls: list[str] = []
        hist = MagicMock()
        hist.save.side_effect = lambda m: calls.append("save")
        adv = MagicMock()
        adv.process_message.side_effect = lambda m: calls.append("advocacy") or None
        h = AdapterHub(history=hist, advocacy=adv, resolve_fqid=resolver_map)
        h.handle_inbound(_chan_msg(text="@opus hi"))
        assert calls == ["save", "advocacy"]


# ---------------------------------------------------------------------------
# InboundResult dataclass
# ---------------------------------------------------------------------------


class TestInboundResult:
    def test_is_trusted_true_for_verified(self):
        r = InboundResult(message=MagicMock(), fqid="x@y", trust=TRUST_VERIFIED)
        assert r.is_trusted is True

    def test_is_trusted_false_for_untrusted(self):
        r = InboundResult(message=MagicMock(), fqid="g@ext", trust=TRUST_UNTRUSTED)
        assert r.is_trusted is False

    def test_default_reply_none(self):
        r = InboundResult(message=MagicMock(), fqid="x@y", trust=TRUST_VERIFIED)
        assert r.reply is None


# ---------------------------------------------------------------------------
# Reply routing — skchat → originating platform (U14 Phase 2)
# ---------------------------------------------------------------------------


class FakeRegistry:
    """A minimal registry exposing the real ``send_to_adapter`` signature."""

    def __init__(self, adapter: FakeAdapter) -> None:
        self._adapter = adapter
        self.calls: list[tuple[str, ChannelMessage]] = []

    def get(self, adapter_name: str):
        return self._adapter if adapter_name == self._adapter.adapter_name else None

    async def send_to_adapter(self, adapter_name: str, message: ChannelMessage) -> str:
        self.calls.append((adapter_name, message))
        return await self._adapter.send(message)


@pytest.fixture
def fake_adapter():
    return FakeAdapter({"adapter_name": "telegram"})


class TestBuildReplyMessage:
    def test_reply_addressed_to_same_channel_and_room(self, hub):
        inbound = _chan_msg(channel=ChannelType.TELEGRAM)
        out = hub.build_reply_message(inbound, "pong")
        assert out.channel == ChannelType.TELEGRAM
        assert out.room_id == inbound.room_id
        assert out.text == "pong"
        assert out.kind == MessageKind.TEXT

    def test_reply_sender_is_agent_identity(self, hub):
        out = hub.build_reply_message(_chan_msg(), "pong")
        assert out.sender.platform_id == AGENT

    def test_reply_threads_to_inbound_platform_msg(self, hub):
        out = hub.build_reply_message(_chan_msg(), "pong")
        assert out.reply_to_platform_id == "plat-msg-1"


class TestRouteReply:
    @pytest.mark.asyncio
    async def test_route_via_send_to_adapter(self, history, advocacy, resolver_map, fake_adapter):
        reg = FakeRegistry(fake_adapter)
        h = AdapterHub(
            history=history,
            advocacy=advocacy,
            resolve_fqid=resolver_map,
            registry=reg,
            outbound_adapter="telegram",
        )
        await h.route_reply(_chan_msg(channel=ChannelType.TELEGRAM), "Hi from Opus")
        assert reg.calls[0][0] == "telegram"
        assert len(fake_adapter.sent) == 1
        assert fake_adapter.sent[0].text == "Hi from Opus"
        assert fake_adapter.sent[0].room_id == "-5134021983"

    @pytest.mark.asyncio
    async def test_route_falls_back_to_channel_name(self, history, resolver_map, fake_adapter):
        # No outbound_adapter configured → derive from the inbound channel.
        reg = FakeRegistry(fake_adapter)
        h = AdapterHub(history=history, resolve_fqid=resolver_map, registry=reg)
        await h.route_reply(_chan_msg(channel=ChannelType.TELEGRAM), "yo")
        assert reg.calls[0][0] == "telegram"

    @pytest.mark.asyncio
    async def test_route_direct_adapter_object(self, history, resolver_map, fake_adapter):
        # outbound_adapter is itself a ChannelAdapter (no registry needed).
        h = AdapterHub(
            history=history, resolve_fqid=resolver_map, outbound_adapter=fake_adapter
        )
        msg_id = await h.route_reply(_chan_msg(), "direct")
        assert isinstance(msg_id, str)
        assert fake_adapter.sent[0].text == "direct"

    @pytest.mark.asyncio
    async def test_route_via_registry_get(self, history, resolver_map, fake_adapter):
        class GetOnlyRegistry:
            def __init__(self, a):
                self._a = a

            def get(self, name):
                return self._a if name == self._a.adapter_name else None

        h = AdapterHub(
            history=history,
            resolve_fqid=resolver_map,
            registry=GetOnlyRegistry(fake_adapter),
            outbound_adapter="telegram",
        )
        await h.route_reply(_chan_msg(channel=ChannelType.TELEGRAM), "via get")
        assert fake_adapter.sent[0].text == "via get"

    @pytest.mark.asyncio
    async def test_empty_reply_is_noop(self, history, resolver_map, fake_adapter):
        reg = FakeRegistry(fake_adapter)
        h = AdapterHub(history=history, resolve_fqid=resolver_map, registry=reg)
        assert await h.route_reply(_chan_msg(), "") is None
        assert await h.route_reply(_chan_msg(), "   ") is None
        assert fake_adapter.sent == []

    @pytest.mark.asyncio
    async def test_no_registry_no_crash(self, history, resolver_map):
        h = AdapterHub(history=history, resolve_fqid=resolver_map)
        assert await h.route_reply(_chan_msg(), "nowhere") is None

    @pytest.mark.asyncio
    async def test_unknown_adapter_name_returns_none(self, history, resolver_map, fake_adapter):
        class GetOnlyRegistry:
            def get(self, name):
                return None

        h = AdapterHub(
            history=history,
            resolve_fqid=resolver_map,
            registry=GetOnlyRegistry(),
            outbound_adapter="nope",
        )
        assert await h.route_reply(_chan_msg(), "x") is None


class TestDispatchInbound:
    @pytest.mark.asyncio
    async def test_reply_routed_back_to_platform(self, history, advocacy, resolver_map, fake_adapter):
        advocacy.process_message.return_value = "Hello from Opus"
        reg = FakeRegistry(fake_adapter)
        h = AdapterHub(
            history=history,
            advocacy=advocacy,
            resolve_fqid=resolver_map,
            registry=reg,
            outbound_adapter="telegram",
        )
        result = await h.dispatch_inbound(_chan_msg(text="@opus hi"))
        assert result.reply == "Hello from Opus"
        # The reply text was sent to the correct channel/room.
        assert len(fake_adapter.sent) == 1
        assert fake_adapter.sent[0].text == "Hello from Opus"
        assert fake_adapter.sent[0].room_id == "-5134021983"
        assert fake_adapter.sent[0].channel == ChannelType.TELEGRAM

    @pytest.mark.asyncio
    async def test_inbound_only_when_no_reply(self, history, advocacy, resolver_map, fake_adapter):
        advocacy.process_message.return_value = None
        reg = FakeRegistry(fake_adapter)
        h = AdapterHub(
            history=history,
            advocacy=advocacy,
            resolve_fqid=resolver_map,
            registry=reg,
            outbound_adapter="telegram",
        )
        result = await h.dispatch_inbound(_chan_msg(text="just chatting"))
        assert result.reply is None
        # Nothing sent back — pure inbound behaviour preserved.
        assert fake_adapter.sent == []
        history.save.assert_called_once()

    @pytest.mark.asyncio
    async def test_dispatch_still_persists_and_returns_result(self, history, advocacy, resolver_map):
        advocacy.process_message.return_value = "reply"
        # No registry → routing is a no-op but the pipeline still completes.
        h = AdapterHub(history=history, advocacy=advocacy, resolve_fqid=resolver_map)
        result = await h.dispatch_inbound(_chan_msg(text="@opus hi"))
        assert isinstance(result, InboundResult)
        history.save.assert_called_once()

    @pytest.mark.asyncio
    async def test_send_failure_does_not_drop_result(self, history, advocacy, resolver_map):
        advocacy.process_message.return_value = "reply"

        class BoomRegistry:
            async def send_to_adapter(self, name, msg):
                raise RuntimeError("platform down")

        h = AdapterHub(
            history=history,
            advocacy=advocacy,
            resolve_fqid=resolver_map,
            registry=BoomRegistry(),
            outbound_adapter="telegram",
        )
        result = await h.dispatch_inbound(_chan_msg(text="@opus hi"))
        # Result intact despite a send failure.
        assert result.reply == "reply"
