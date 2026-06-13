import jwt  # PyJWT (already used by guest.py)

from skchat.spaces.roles import Role
from skchat.spaces.tokens import mint_space_token

_KEY, _SECRET = "test-key", "test-secret-0123456789"


def _claims(token):
    return jwt.decode(token, _SECRET, algorithms=["HS256"],
                      options={"verify_aud": False})


def test_listener_token_is_subscribe_only():
    tok = mint_space_token("guest:abc", "Guest", Role.LISTENER, "space-x", 3600,
                           api_key=_KEY, api_secret=_SECRET)
    v = _claims(tok)["video"]
    assert v["room"] == "space-x"
    assert v["roomJoin"] is True
    assert v.get("canPublish", False) is False
    assert v["canSubscribe"] is True
    assert v["canPublishData"] is True


def test_host_token_is_admin_publisher():
    tok = mint_space_token("lumina@chef.skworld", "Lumina", Role.HOST, "space-x",
                           3600, api_key=_KEY, api_secret=_SECRET)
    v = _claims(tok)["video"]
    assert v["canPublish"] is True
    assert v["roomAdmin"] is True


def test_speaker_token_is_mic_only():
    tok = mint_space_token("dave@chef.skworld", "Dave", Role.SPEAKER, "space-x",
                           3600, api_key=_KEY, api_secret=_SECRET)
    v = _claims(tok)["video"]
    assert v["canPublish"] is True
    assert v["canPublishSources"] == ["microphone"]


def test_identity_and_ttl_round_trip():
    tok = mint_space_token("x@y.z", "X", Role.LISTENER, "space-x", 120,
                           api_key=_KEY, api_secret=_SECRET)
    c = _claims(tok)
    assert c["sub"] == "x@y.z"        # LiveKit puts identity in sub
    # Installed livekit-api emits nbf (not iat) as the issue baseline; ttl round-trips.
    issued = c.get("iat", c["nbf"])
    assert c["exp"] - issued == 120
