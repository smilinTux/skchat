"""CallerProfile: privilege from the signed invite FQID, never from a display name.

The behaviour under test is a security boundary, so every case here is written
against the two ways the old ``is_chef_identity()`` prefix shim was wrong:

* CLOSED for the real operator, whose browser joins as ``lumina@chef.skworld.io``
  (2026-08-13: no auto-reply, every operator tool refused mid-call).
* OPEN for anyone who picks a display name beginning with ``chef``.
"""

from __future__ import annotations

import pytest

from skchat.voice_engine.caller_profile import (
    LEAST_PRIVILEGE,
    CallerDirectory,
    CallerProfile,
    default_directory,
    is_peer_agent,
    normalize_fqid,
    reset_default_directory,
    resolve_caller_profile,
    speaker_is_operator,
)

#: What the live cluster looks like: operator "chef", realm "skworld.io".
AGENT_FQID = "lumina@chef.skworld.io"
OPERATOR_FQID = "chef@skworld.io"
COMPANION_FQID = "nan@chef.skworld.io"


@pytest.fixture
def directory() -> CallerDirectory:
    return CallerDirectory.build(agent_fqid=AGENT_FQID, companion_fqids=[COMPANION_FQID])


# ─── normalisation: what even counts as an identity ─────────────────────────
def test_normalize_accepts_the_shapes_the_estate_really_carries():
    assert normalize_fqid("  Lumina@Chef.SKWorld.io ") == AGENT_FQID
    assert normalize_fqid("capauth:chef@skworld.io") == OPERATOR_FQID


@pytest.mark.parametrize(
    "value",
    [
        None,
        "",
        "   ",
        "chef",  # a bare short name is not an identity anything can verify
        "chef-laptop",  # a LiveKit display identity
        "chef@",
        "@skworld.io",
        "chef@sk worldio",
        "a@b@c",
        "lumina@chef.skworld.io#agent",  # a participant identity, not an FQID
        42,
    ],
)
def test_normalize_rejects_everything_it_cannot_verify(value):
    assert normalize_fqid(value) is None


# ─── resolution ─────────────────────────────────────────────────────────────
def test_operator_set_is_derived_from_the_agents_own_sovereign_fqid(directory):
    assert directory.profile_for(OPERATOR_FQID) is CallerProfile.OPERATOR
    assert directory.profile_for("chef@chef.skworld.io") is CallerProfile.OPERATOR


def test_chefs_real_self_addressed_call_is_the_operator(directory):
    """The exact call that broke on 2026-08-13, from the other side.

    His browser is authenticated as the AGENT, so both ends of the invite are
    ``lumina@chef.skworld.io`` and his LiveKit identity does not begin with
    "chef". /call/start, /call/answer and /call/incoming are all operator
    gated, so an invite from the agent to itself came from the operator.
    """
    assert resolve_caller_profile(AGENT_FQID, directory=directory) is CallerProfile.OPERATOR
    # ...and the privilege survives the trip through the speaker check, where
    # the prefix shim used to drop it.
    assert speaker_is_operator(CallerProfile.OPERATOR, AGENT_FQID, mode="sacred")
    assert speaker_is_operator(CallerProfile.OPERATOR, AGENT_FQID, mode="private")


def test_a_companion_named_chef_is_still_a_companion(directory):
    """The fail-OPEN half: display names are not claims.

    Picking a LiveKit identity of "chef-laptop" bought the operator's whole
    tool surface under the shim. The invite FQID decides now, and it says
    companion.
    """
    profile = resolve_caller_profile(COMPANION_FQID, directory=directory)
    assert profile is CallerProfile.COMPANION
    for display_identity in ("chef", "chef-laptop", "chef@skworld.io", "Chef"):
        assert not speaker_is_operator(profile, display_identity, mode="sacred")


def test_operator_entry_wins_over_a_stale_companion_entry():
    directory = CallerDirectory.build(
        agent_fqid=AGENT_FQID, companion_fqids=[OPERATOR_FQID, AGENT_FQID]
    )
    assert directory.profile_for(OPERATOR_FQID) is CallerProfile.OPERATOR
    assert directory.profile_for(AGENT_FQID) is CallerProfile.OPERATOR


@pytest.mark.parametrize(
    "value",
    [None, "", "chef", "chef-laptop", "mallory@evil.io", "opus@chef.skworld.io", 42],
)
def test_unresolvable_callers_get_the_least_privilege(directory, value):
    assert directory.profile_for(value) is LEAST_PRIVILEGE
    assert LEAST_PRIVILEGE is CallerProfile.GUEST


def test_an_empty_directory_grants_nothing():
    """No resolvable identity means no operator, not a default operator.

    A host that cannot resolve its own capauth identity ends up here. It must
    lose privilege, loudly, rather than hand it to whoever calls next.
    """
    empty = CallerDirectory()
    for value in (OPERATOR_FQID, AGENT_FQID, COMPANION_FQID):
        assert empty.profile_for(value) is LEAST_PRIVILEGE


def test_an_unsigned_caller_carries_no_fqid_at_all(directory):
    """/call/incoming surfaces only signature-verified invites.

    Anything that reaches a session without one (a presence rejoin into a room
    nobody was invited to, a hand-built joinable) has nothing to resolve, and
    nothing resolves to guest.
    """
    joinable_without_an_invite = {"room": "call-e4qj4kxvef2dxmxq", "token": "t"}
    peer = joinable_without_an_invite.get("peer_fqid") or joinable_without_an_invite.get(
        "from_fqid"
    )
    assert resolve_caller_profile(peer, directory=directory) is LEAST_PRIVILEGE


# ─── who speaks with the operator's authority ───────────────────────────────
def test_operator_authority_needs_the_one_to_one_register():
    """A second human in the room must not inherit the operator's hands.

    In a group room an utterance cannot be attributed to the verified caller,
    so nobody there speaks for him.
    """
    assert not speaker_is_operator(CallerProfile.OPERATOR, AGENT_FQID, mode="group")
    assert not speaker_is_operator(CallerProfile.OPERATOR, AGENT_FQID, mode="")
    assert speaker_is_operator(CallerProfile.OPERATOR, AGENT_FQID, mode="SACRED")


def test_a_peer_agent_never_borrows_the_operators_authority():
    assert not speaker_is_operator(
        CallerProfile.OPERATOR, "lumina@chef.skworld.io#agent", mode="sacred"
    )
    assert not speaker_is_operator(
        CallerProfile.OPERATOR, "opus", mode="sacred", other_agents=("opus",)
    )


def test_non_operator_profiles_never_reach_operator_authority():
    for profile in (CallerProfile.COMPANION, CallerProfile.GUEST):
        assert not speaker_is_operator(profile, AGENT_FQID, mode="sacred")


def test_is_peer_agent_reads_the_participant_suffix_and_the_roster():
    assert is_peer_agent("lumina@chef.skworld.io#agent")
    assert is_peer_agent("OPUS", other_agents=("opus",))
    assert not is_peer_agent(AGENT_FQID)
    assert not is_peer_agent("")


# ─── loading from the environment ───────────────────────────────────────────
def test_load_reads_pinned_fqids_and_caches_per_agent(monkeypatch):
    monkeypatch.setenv("SKCHAT_AGENT_FQID", AGENT_FQID)
    monkeypatch.setenv("SKCHAT_COMPANION_FQIDS", f" {COMPANION_FQID} , chef-laptop ,")
    reset_default_directory()
    try:
        loaded = default_directory("lumina")
        assert loaded.profile_for(OPERATOR_FQID) is CallerProfile.OPERATOR
        assert loaded.profile_for(COMPANION_FQID) is CallerProfile.COMPANION
        # "chef-laptop" is not an FQID, so it never becomes a companion entry.
        assert loaded.profile_for("chef-laptop") is LEAST_PRIVILEGE
        assert default_directory("lumina") is loaded  # cached
    finally:
        reset_default_directory()
