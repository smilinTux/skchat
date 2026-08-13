"""When does a participant leaving mean the CALL is over?

This one decision failed three separate ways on 2026-08-13, each fix breaking
the previous case, because "someone left" and "nobody has arrived yet" produce
the SAME state (an empty participant map) and mean opposite things. These tests
pin all three failures so the next change has to survive every one of them.
"""

import pytest

from skchat.transports.livekit import should_end_on_leave

GRACE = 20.0


@pytest.mark.parametrize("remaining", [1, 2, 5])
def test_someone_else_is_still_here(remaining):
    """FAILURE 1: cut Chef off mid-call.

    A reconnecting client rejoins with the SAME identity, so the stale
    disconnect must not read as empty while the replacement is present. The
    caller counts the live map WITHOUT filtering, so a present replacement
    keeps the call alive.
    """
    assert should_end_on_leave(remaining, since_start_s=300.0, grace_s=GRACE) is False


def test_empty_after_a_real_call_ends_it():
    """FAILURE 2: she narrated a story at his speakers after he hung up.

    Deferring this to the 60s watchdog let her keep talking into a room he had
    left. Once the grace has passed, empty means everyone hung up: stop.
    """
    assert should_end_on_leave(0, since_start_s=300.0, grace_s=GRACE) is True


@pytest.mark.parametrize("age", [0.0, 1.0, 2.0, 19.9])
def test_empty_during_join_grace_is_not_a_hangup(age):
    """FAILURE 3: she hung up on HERSELF one second after going live.

    The agent joins before the caller finishes arriving, so an empty room is
    normal at the start. A ghost from the previous call disconnecting inside
    that window made 'the room is empty' true at exactly the wrong moment, and
    Chef joined to find only himself.
    """
    assert should_end_on_leave(0, since_start_s=age, grace_s=GRACE) is False


def test_the_boundary_itself_ends_the_call():
    assert should_end_on_leave(0, since_start_s=GRACE, grace_s=GRACE) is True


def test_presence_beats_the_clock_in_both_directions():
    """Someone present never ends the call, at any age; nobody present never
    ends it during grace, at any count of zero. The two facts are independent
    and the rule needs both."""
    assert should_end_on_leave(1, since_start_s=0.0, grace_s=GRACE) is False
    assert should_end_on_leave(1, since_start_s=999.0, grace_s=GRACE) is False
    assert should_end_on_leave(0, since_start_s=0.0, grace_s=GRACE) is False
    assert should_end_on_leave(0, since_start_s=999.0, grace_s=GRACE) is True
