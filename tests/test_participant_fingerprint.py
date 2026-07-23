"""M1b participant trust badges: the server embeds each participant's real
capauth fingerprint in the LiveKit token metadata (unspoofable channel that
reaches every conf/Space/call snapshot), and the Space moderation layer must
round-trip it instead of clobbering it on a hand-raise/invite."""
import json

import jwt

from skchat.spaces import tokens
from skchat.spaces.moderation import StageState, parse_meta, dump_meta, apply_action

_KEY = "test-key"
_SECRET = "test-secret-0123456789"
_FP = "4E06A71935D1DF1FB9848112D8634AB3E7B55236"


def _claims(token: str) -> dict:
    return jwt.decode(token, _SECRET, algorithms=["HS256"], options={"verify_aud": False})


def test_conf_token_carries_metadata():
    tok = tokens.mint_conf_token(
        "capauth:steward@skworld.io", "Steward", "sovereign", "room1", 3600,
        api_key=_KEY, api_secret=_SECRET,
        metadata=json.dumps({"soul_fingerprint": _FP}),
    )
    meta = json.loads(_claims(tok)["metadata"])
    assert meta["soul_fingerprint"] == _FP


def test_space_token_carries_metadata():
    tok = tokens.mint_space_token(
        "capauth:steward@skworld.io", "Steward", "listener", "space1", 3600,
        api_key=_KEY, api_secret=_SECRET,
        metadata=json.dumps({"soul_fingerprint": _FP}),
    )
    meta = json.loads(_claims(tok)["metadata"])
    assert meta["soul_fingerprint"] == _FP


def test_token_without_metadata_omits_it():
    # Back-compat: no metadata arg -> no metadata claim (or empty), never a crash.
    tok = tokens.mint_space_token(
        "guest", "Guest", "listener", "space1", 3600,
        api_key=_KEY, api_secret=_SECRET,
    )
    claims = _claims(tok)
    assert not claims.get("metadata")


def test_moderation_round_trip_preserves_soul_fingerprint():
    """The LOAD-BEARING fix: a stage action (parse -> apply -> dump) must keep
    the mint-time soul_fingerprint, else the badge vanishes after first
    hand-raise/invite."""
    meta_in = json.dumps(
        {"hand_raised": True, "invited_to_stage": False, "soul_fingerprint": _FP}
    )
    state = parse_meta(meta_in)
    assert state.soul_fingerprint == _FP

    new_state, _ = apply_action(state, "invite")
    out = json.loads(dump_meta(new_state))
    assert out["soul_fingerprint"] == _FP  # survived the read-modify-write
    assert out["invited_to_stage"] is True
    assert out["hand_raised"] is True


def test_stagestate_defaults_keyless():
    assert StageState().soul_fingerprint == ""
    assert json.loads(dump_meta(StageState()))["soul_fingerprint"] == ""


# --- proven-path stamping + strict resolution (security-model tests) ---------
from skchat.daemon_proxy import LUMINA_FINGERPRINT, soul_metadata_for


def test_soul_metadata_for_strict_resolution():
    # Lumina (special-case) resolves; an unknown/cross-realm identity is keyless.
    assert json.loads(soul_metadata_for("lumina@chef.skworld"))[
        "soul_fingerprint"] == LUMINA_FINGERPRINT
    assert json.loads(soul_metadata_for("ghost@otherrealm.io"))[
        "soul_fingerprint"] == ""


def test_proven_sovereign_join_stamps_fingerprint(monkeypatch):
    """The PROVEN /join/sovereign mint (verify_signed fqid) DOES stamp the badge."""
    monkeypatch.setenv("SKCHAT_LIVEKIT_API_KEY", _KEY)
    monkeypatch.setenv("SKCHAT_LIVEKIT_API_SECRET", _SECRET)
    from skchat import join_routes

    tok = join_routes._default_mint("lumina@chef.skworld", "room1",
                                    sovereign_admin=True)
    assert json.loads(_claims(tok)["metadata"])["soul_fingerprint"] == \
        LUMINA_FINGERPRINT


def test_proven_space_federation_stamps_fingerprint(monkeypatch):
    """The PROVEN federation authd mint DOES stamp the badge."""
    monkeypatch.setenv("SKCHAT_LIVEKIT_API_KEY", _KEY)
    monkeypatch.setenv("SKCHAT_LIVEKIT_API_SECRET", _SECRET)
    from skchat.spaces.federation import authd
    from skchat.spaces.roles import Role

    tok = authd._default_mint("lumina@chef.skworld", Role.SPEAKER, "space1")
    assert json.loads(_claims(tok)["metadata"])["soul_fingerprint"] == \
        LUMINA_FINGERPRINT
