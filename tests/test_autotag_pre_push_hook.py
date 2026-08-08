"""The auto-tag pre-push hook only cuts a release tag from the release branch.

Card 0c3668a6. The hook bumps a vX.Y.Z patch tag at HEAD on push, so
setuptools-scm's derived version stays in lockstep with what is deployed. It
used to fire on ANY branch push, so pushing a feature branch cut a release tag
at that branch's tip and pushed it, which triggers publish.yml and ships it to
PyPI. Five of six consecutive tags were cut off unmerged branches that way, and
skchat-sovereign was published straight from an in-review branch: peer review
stopped being a gate on what got released.

These drive the real script against throwaway git repos, feeding it the same
stdin format git uses (``<local-ref> <local-sha> <remote-ref> <remote-sha>``),
so they exercise the actual shell logic rather than a reimplementation of it.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

HOOK = Path(__file__).resolve().parents[1] / "scripts" / "hooks" / "pre-push"


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args],
        cwd=repo,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


@pytest.fixture()
def repo(tmp_path: Path) -> Path:
    """An origin plus a clone with one commit on main and a v1.2.3 tag."""
    origin = tmp_path / "origin.git"
    subprocess.run(
        ["git", "init", "--bare", "-b", "main", str(origin)], check=True, capture_output=True
    )

    work = tmp_path / "work"
    subprocess.run(["git", "clone", str(origin), str(work)], check=True, capture_output=True)
    _git(work, "config", "user.email", "t@example.com")
    _git(work, "config", "user.name", "T")
    (work / "f.txt").write_text("one\n")
    _git(work, "add", "f.txt")
    _git(work, "commit", "-m", "one")
    _git(work, "tag", "-a", "v1.2.3", "-m", "base")
    _git(work, "push", "origin", "main")
    _git(work, "push", "origin", "v1.2.3")
    return work


def _run_hook(repo: Path, ref: str) -> subprocess.CompletedProcess:
    """Invoke the hook exactly as git does: ref list on stdin, remote as argv[1]."""
    sha = _git(repo, "rev-parse", "HEAD")
    return subprocess.run(
        ["bash", str(HOOK), "origin", "unused-url"],
        cwd=repo,
        input=f"{ref} {sha} {ref} 0000000000000000000000000000000000000000\n",
        capture_output=True,
        text=True,
    )


def _tags(repo: Path) -> list[str]:
    out = _git(repo, "tag", "--list")
    return sorted(t for t in out.splitlines() if t)


# --------------------------------------------------------------------------- #
# the regression: a feature branch must NOT be tagged
# --------------------------------------------------------------------------- #


def test_feature_branch_push_cuts_no_tag(repo):
    """THE bug. Before the fix this tagged v1.2.4 at the branch tip and pushed it."""
    _git(repo, "checkout", "-b", "feat/something")
    (repo / "f.txt").write_text("two\n")
    _git(repo, "commit", "-am", "two")

    proc = _run_hook(repo, "refs/heads/feat/something")

    assert proc.returncode == 0, proc.stderr
    assert _tags(repo) == ["v1.2.3"], (
        "a feature-branch push cut a release tag; it would publish to PyPI"
    )
    assert "auto-tag" not in proc.stderr


@pytest.mark.parametrize(
    "ref",
    ["refs/heads/fix/x", "refs/heads/release/v2", "refs/heads/mainline", "refs/heads/main-ish"],
)
def test_only_the_exact_release_branch_qualifies(repo, ref):
    """Near-miss names must not slip through a prefix match."""
    _git(repo, "checkout", "-b", ref.removeprefix("refs/heads/"))
    (repo / "f.txt").write_text("x\n")
    _git(repo, "commit", "-am", "x")

    _run_hook(repo, ref)

    assert _tags(repo) == ["v1.2.3"]


def test_tag_push_is_still_skipped(repo):
    """Loop guard: pushing a tag must never cut another tag."""
    proc = _run_hook(repo, "refs/tags/v1.2.3")

    assert proc.returncode == 0
    assert _tags(repo) == ["v1.2.3"]


# --------------------------------------------------------------------------- #
# the legitimate path must still work
# --------------------------------------------------------------------------- #


def test_main_push_still_bumps_and_tags(repo):
    """The hook's whole purpose: a real main push keeps the version in lockstep."""
    (repo / "f.txt").write_text("three\n")
    _git(repo, "commit", "-am", "three")

    proc = _run_hook(repo, "refs/heads/main")

    assert proc.returncode == 0, proc.stderr
    assert "v1.2.4" in _tags(repo), f"expected a bump to v1.2.4, got {_tags(repo)}"
    assert "auto-tag" in proc.stderr


def test_main_push_with_head_already_tagged_is_a_noop(repo):
    """Nothing new to release means no tag."""
    proc = _run_hook(repo, "refs/heads/main")

    assert proc.returncode == 0
    assert _tags(repo) == ["v1.2.3"]


def test_skip_autotag_env_still_wins(repo, monkeypatch):
    (repo / "f.txt").write_text("four\n")
    _git(repo, "commit", "-am", "four")
    sha = _git(repo, "rev-parse", "HEAD")

    proc = subprocess.run(
        ["bash", str(HOOK), "origin", "unused-url"],
        cwd=repo,
        input=f"refs/heads/main {sha} refs/heads/main 0\n",
        capture_output=True,
        text=True,
        env={"PATH": "/usr/bin:/bin", "HOME": str(repo), "SKIP_AUTOTAG": "1"},
    )

    assert proc.returncode == 0
    assert _tags(repo) == ["v1.2.3"]
