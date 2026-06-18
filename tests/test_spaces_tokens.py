import jwt  # PyJWT (already used by guest.py)
import pytest

from skchat.spaces.roles import ConfRole, Role
from skchat.spaces.tokens import mint_conf_token, mint_space_token

_KEY, _SECRET = "test-key", "test-secret-0123456789"


def _claims(token):
    return jwt.decode(token, _SECRET, algorithms=["HS256"], options={"verify_aud": False})


def test_listener_token_is_subscribe_only():
    tok = mint_space_token(
        "guest:abc", "Guest", Role.LISTENER, "space-x", 3600, api_key=_KEY, api_secret=_SECRET
    )
    v = _claims(tok)["video"]
    assert v["room"] == "space-x"
    assert v["roomJoin"] is True
    assert v.get("canPublish", False) is False
    assert v["canSubscribe"] is True
    assert v["canPublishData"] is True


def test_host_token_is_admin_publisher():
    tok = mint_space_token(
        "lumina@chef.skworld",
        "Lumina",
        Role.HOST,
        "space-x",
        3600,
        api_key=_KEY,
        api_secret=_SECRET,
    )
    v = _claims(tok)["video"]
    assert v["canPublish"] is True
    assert v["roomAdmin"] is True


def test_speaker_token_is_mic_only():
    tok = mint_space_token(
        "dave@chef.skworld",
        "Dave",
        Role.SPEAKER,
        "space-x",
        3600,
        api_key=_KEY,
        api_secret=_SECRET,
    )
    v = _claims(tok)["video"]
    assert v["canPublish"] is True
    assert v["canPublishSources"] == ["microphone"]


def test_identity_and_ttl_round_trip():
    tok = mint_space_token(
        "x@y.z", "X", Role.LISTENER, "space-x", 120, api_key=_KEY, api_secret=_SECRET
    )
    c = _claims(tok)
    assert c["sub"] == "x@y.z"  # LiveKit puts identity in sub
    # Installed livekit-api emits nbf (not iat) as the issue baseline; ttl round-trips.
    issued = c.get("iat", c["nbf"])
    assert c["exp"] - issued == 120


# ── Conference video tokens ───────────────────────────────────────────────────

_SCREENSHARE = {"screen_share", "screen_share_audio"}


@pytest.mark.parametrize(
    "role",
    [ConfRole.PARTICIPANT, ConfRole.PRESENTER, ConfRole.SOVEREIGN, ConfRole.GUEST_CONF],
)
def test_conf_token_includes_screenshare_sources(role):
    tok = mint_conf_token(
        "p@chef.skworld", "P", role, "conf-1", 3600, api_key=_KEY, api_secret=_SECRET
    )
    v = _claims(tok)["video"]
    assert v["canPublish"] is True
    srcs = set(v["canPublishSources"])
    assert "camera" in srcs
    assert "microphone" in srcs
    assert _SCREENSHARE.issubset(srcs)


def test_conf_guest_token_is_never_admin_or_recorder():
    tok = mint_conf_token(
        "guest:abc",
        "Guest",
        ConfRole.GUEST_CONF,
        "conf-1",
        3600,
        sovereign_admin=True,  # must be ignored for a guest
        api_key=_KEY,
        api_secret=_SECRET,
    )
    v = _claims(tok)["video"]
    assert _SCREENSHARE.issubset(set(v["canPublishSources"]))
    assert v.get("roomAdmin", False) is False
    assert v.get("roomRecord", False) is False
    assert v.get("roomCreate", False) is False


def test_conf_agent_token_is_mic_and_data_only():
    tok = mint_conf_token(
        "capauth:lumina@skworld.io",
        "Lumina",
        ConfRole.AGENT,
        "conf-1",
        3600,
        api_key=_KEY,
        api_secret=_SECRET,
    )
    v = _claims(tok)["video"]
    assert v["canPublishSources"] == ["microphone"]
    assert v["canPublishData"] is True
    assert v.get("roomAdmin", False) is False


def test_conf_sovereign_admin_flag_sets_room_admin():
    tok_plain = mint_conf_token(
        "owner@chef.skworld", "Owner", ConfRole.SOVEREIGN, "conf-1", 3600,
        api_key=_KEY, api_secret=_SECRET,
    )
    tok_admin = mint_conf_token(
        "owner@chef.skworld", "Owner", ConfRole.SOVEREIGN, "conf-1", 3600,
        sovereign_admin=True, api_key=_KEY, api_secret=_SECRET,
    )
    assert _claims(tok_plain)["video"].get("roomAdmin", False) is False
    assert _claims(tok_admin)["video"]["roomAdmin"] is True
