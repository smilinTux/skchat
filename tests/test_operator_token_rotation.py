"""Generating and rotating the operator token.

The token had no generator and no rotation path: it was created by hand once and
has never changed. If it ever leaked, the only recourse was hand-editing env
files and guessing which services to restart.

Rotation is genuinely dangerous to get wrong, which is what these tests are
about. The token is not just a login secret: it is presented BY services (the
call answerer holds it), and it lives in the same env file as everything else
those units need. A rotation that mangles that file, or that widens its
permissions, or that silently writes to the wrong agent's file, breaks the plane
in a way that is hard to diagnose from the symptom.

So the file rewriting is a pure, tested function and the systemd side is kept
thin on top of it.
"""

from __future__ import annotations

import os
import stat

import pytest

from skchat import operator_token as OT


def _env(tmp_path, name="webui-lumina.env", token="OLD-TOKEN-VALUE"):
    p = tmp_path / name
    p.write_text(
        "# a comment that must survive\n"
        "SKCHAT_DATAPLANE_AUTH=1\n"
        f"SKCHAT_GUEST_OPERATOR_TOKEN={token}\n"
        "SKCHAT_AUTHZ_PDP=enforce\n"
        "SOME_OTHER=value with spaces and an = sign\n"
    )
    p.chmod(0o600)
    return p


def test_a_generated_token_is_high_entropy_and_unique():
    tokens = {OT.generate() for _ in range(20)}
    assert len(tokens) == 20
    assert all(len(t) >= 40 for t in tokens), "a service credential should be long"


def test_read_token_finds_the_value(tmp_path):
    p = _env(tmp_path)
    assert OT.read_token(p) == "OLD-TOKEN-VALUE"


def test_read_token_on_a_file_without_one(tmp_path):
    p = tmp_path / "x.env"
    p.write_text("FOO=bar\n")
    assert OT.read_token(p) is None


def test_writing_a_new_token_leaves_every_other_line_untouched(tmp_path):
    """The token shares its file with everything else those units need."""
    p = _env(tmp_path)
    before = p.read_text().splitlines()

    OT.write_token(p, "NEW-TOKEN-VALUE")

    after = p.read_text().splitlines()
    assert len(after) == len(before), "no line may be added or dropped"
    for b, a in zip(before, after):
        if b.startswith("SKCHAT_GUEST_OPERATOR_TOKEN="):
            assert a == "SKCHAT_GUEST_OPERATOR_TOKEN=NEW-TOKEN-VALUE"
        else:
            assert a == b, f"unrelated line changed: {b!r} -> {a!r}"


def test_writing_preserves_owner_only_permissions(tmp_path):
    """A secrets file must not come back world-readable after a rotation."""
    p = _env(tmp_path)
    OT.write_token(p, "NEW-TOKEN-VALUE")

    mode = stat.S_IMODE(os.stat(p).st_mode)
    assert mode & 0o077 == 0, f"rotation widened permissions to {oct(mode)}"


def test_writing_is_atomic_no_torn_file_left_behind(tmp_path):
    p = _env(tmp_path)
    OT.write_token(p, "NEW-TOKEN-VALUE")

    leftovers = [f for f in os.listdir(tmp_path) if ".tmp" in f]
    assert leftovers == [], f"temp files left behind: {leftovers}"


def test_writing_refuses_a_file_with_no_token_line(tmp_path):
    """Better to refuse than to silently append and half-configure a unit."""
    p = tmp_path / "x.env"
    p.write_text("FOO=bar\n")

    with pytest.raises(ValueError):
        OT.write_token(p, "NEW")


def test_a_backup_is_written_before_the_change(tmp_path):
    """A bad rotation must be recoverable without archaeology."""
    p = _env(tmp_path)
    backup = OT.write_token(p, "NEW-TOKEN-VALUE", backup=True)

    assert backup is not None and backup.exists()
    assert "OLD-TOKEN-VALUE" in backup.read_text()
    assert stat.S_IMODE(os.stat(backup).st_mode) & 0o077 == 0, "backup must not be readable"


def test_fingerprint_identifies_without_revealing(tmp_path):
    fp = OT.fingerprint("SOME-SECRET-VALUE")
    assert "SOME-SECRET-VALUE" not in fp
    assert len(fp) >= 8
    assert OT.fingerprint("SOME-SECRET-VALUE") == fp
    assert OT.fingerprint("OTHER") != fp


def test_env_files_discovers_only_files_that_carry_a_token(tmp_path, monkeypatch):
    _env(tmp_path, "webui-lumina.env")
    _env(tmp_path, "webui-opus.env", token="DIFFERENT")
    (tmp_path / "telegram-opus.env").write_text("TELEGRAM_TOKEN=x\n")
    monkeypatch.setenv("SKCHAT_ENV_DIR", str(tmp_path))

    found = {p.name for p in OT.env_files()}
    assert found == {"webui-lumina.env", "webui-opus.env"}


def test_env_files_can_be_filtered_to_one_agent(tmp_path, monkeypatch):
    _env(tmp_path, "webui-lumina.env")
    _env(tmp_path, "webui-opus.env", token="DIFFERENT")
    monkeypatch.setenv("SKCHAT_ENV_DIR", str(tmp_path))

    assert {p.name for p in OT.env_files(agent="opus")} == {"webui-opus.env"}


def test_agents_can_hold_different_tokens(tmp_path, monkeypatch):
    """They do on the live node, and rotating one must not touch the other."""
    a = _env(tmp_path, "webui-lumina.env", token="LUMINA-TOKEN")
    b = _env(tmp_path, "webui-opus.env", token="OPUS-TOKEN")
    monkeypatch.setenv("SKCHAT_ENV_DIR", str(tmp_path))

    OT.write_token(a, "LUMINA-ROTATED")

    assert OT.read_token(a) == "LUMINA-ROTATED"
    assert OT.read_token(b) == "OPUS-TOKEN", "rotating one agent must not touch another"
