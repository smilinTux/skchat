"""She should be where a human is waiting for her.

Answering was purely INVITE-driven, and an invite is momentary: 120s TTL,
consumed on answer. On 2026-08-13 Chef's phone left (correctly ending her
session), he rejoined later from another device WITHOUT placing a new call, and
nothing on the server had any reason to put her back. He sat alone in the room
while she was healthy and idle. Presence is the durable signal.
"""

import pytest

from skchat.call_answerer import AGENT_IDENTITY_SUFFIX, should_join

AGENT = f"lumina@chef.skworld.io{AGENT_IDENTITY_SUFFIX}"
HUMAN = "lumina@chef.skworld.io"


def test_human_waiting_alone_means_join():
    """The exact situation Chef hit: he is there, she is not."""
    assert should_join([HUMAN]) is True


def test_agent_already_present_means_stay_out():
    """Two sessions in one room is how identity collisions and doubled audio
    pumps happened before. Presence must never cause a second join."""
    assert should_join([HUMAN, AGENT]) is False


def test_empty_room_is_not_an_invitation():
    """Otherwise she would join and hold every watched room forever."""
    assert should_join([]) is False


def test_agent_alone_does_not_retrigger():
    """A lingering agent with nobody to talk to must not look like demand."""
    assert should_join([AGENT]) is False


def test_multiple_humans_still_joins():
    assert should_join(["chef@skworld.io", "guest-1"]) is True


def test_multiple_agents_still_blocks():
    assert should_join([HUMAN, AGENT, f"opus@skworld.io{AGENT_IDENTITY_SUFFIX}"]) is False


@pytest.mark.parametrize("suffix", ["#agent", "#bot"])
def test_suffix_is_configurable_not_hardcoded(suffix):
    assert should_join([f"x{suffix}"], agent_suffix=suffix) is False
    assert should_join(["x"], agent_suffix=suffix) is True
