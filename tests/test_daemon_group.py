"""Tests for the daemon's group-message routing contract.

These are focused unit tests around the GroupResponder contract the daemon's
receive loop relies on (see daemon.py: subsystem init builds a GroupResponder
when SKCHAT_GROUPS is set; the receive loop routes an @-mentioned group
message through it and back into the group via GroupChat.send). We don't boot
a full daemon here — that would require a live SKComms/ChatHistory stack —
we just lock the contract daemon.py's wiring depends on (Task 5's
GroupResponder.respond()), plus the daemon's own `_is_group_message` helper.
"""

from __future__ import annotations

from skchat.daemon import _is_group_message
from skchat.group_responder import GroupResponder, load_group_config
from skchat.models import ChatMessage


class _Builder:
    def build(self):
        return "You are Lumina."


class _Resp:
    status_code = 200

    def json(self):
        return {"choices": [{"message": {"content": "hi from lumina"}}]}


class _Http:
    def __init__(self):
        self.calls = []

    def post(self, url, json=None, timeout=None):
        self.calls.append(json)
        return _Resp()


def test_group_message_routed_and_replied():
    # A group message mentioning lumina produces a reply the daemon would send.
    cfg = load_group_config("lumina", env={"SKCHAT_GROUPS": "group:room1"})
    r = GroupResponder(cfg, prompt_builder=_Builder(), http=_Http(), store=None)
    msg = ChatMessage(
        sender="chef@skworld.io",
        recipient="group:room1",
        content="@lumina hi",
        thread_id="room1",
    )
    reply = r.respond(msg)
    assert reply == "hi from lumina"


def test_dm_or_unmentioned_group_no_reply():
    cfg = load_group_config("lumina", env={"SKCHAT_GROUPS": "group:room1"})
    r = GroupResponder(cfg, prompt_builder=_Builder(), http=_Http(), store=None)
    msg = ChatMessage(
        sender="chef@skworld.io",
        recipient="group:room1",
        content="@opus hi",
        thread_id="room1",
    )
    assert r.respond(msg) is None


def test_is_group_message_true_for_group_recipient():
    msg = ChatMessage(
        sender="chef@skworld.io",
        recipient="group:room1",
        content="@lumina hi",
        thread_id="room1",
    )
    assert _is_group_message(msg, ["group:room1"]) is True


def test_is_group_message_false_for_dm():
    msg = ChatMessage(
        sender="chef@skworld.io",
        recipient="capauth:lumina@skworld.io",
        content="hi",
    )
    assert _is_group_message(msg, ["group:room1"]) is False


def test_is_group_message_false_when_not_in_configured_groups():
    msg = ChatMessage(
        sender="chef@skworld.io",
        recipient="group:other-room",
        content="@lumina hi",
        thread_id="other-room",
    )
    assert _is_group_message(msg, ["group:room1"]) is False
