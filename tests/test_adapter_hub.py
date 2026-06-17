"""Unit tests for skchat.adapter_hub — AdapterHub inbound bridge.

All collaborators are injected as mocks: no network, no real skcomms adapters,
no skmem-pg.  We use the REAL skcomms ChannelMessage / PlatformIdentity
dataclasses so conversion fidelity is tested against the actual shape.
"""

from __future__ import annotations

from datetime import datetime, timezone
from unittest.mock import MagicMock

import pytest
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
