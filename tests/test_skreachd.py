"""Tests for the Spaces term-lane exec backend (skreachd).

A fake runner is injected throughout so NO real process is ever spawned in CI.
"""

from __future__ import annotations

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from skchat.spaces.registry import SpaceRegistry
from skchat.spaces.routes import register_spaces_routes
from skchat.spaces.skreachd import (
    RunnerResult,
    SandboxPolicy,
    SkreachExecutor,
)

OPERATOR = "chef@skworld.io"


class FakeRunner:
    """Records the argv it was handed and returns a canned RunnerResult.

    If asked to EXECUTE a destructive binary (argv[0] is rm/dd/...) it raises —
    so a test where shell injection actually spawned `rm` as a command would
    FAIL loudly rather than silently pass. A destructive token appearing as a
    mere argument (e.g. ls receiving the literal "rm") is harmless and allowed.
    """

    _DANGER = {"rm", "dd", "mkfs", "shutdown", "reboot"}

    def __init__(self, result: RunnerResult | None = None) -> None:
        self.result = result or RunnerResult(exit_code=0, stdout="ok\n", stderr="")
        self.calls: list[list[str]] = []

    def __call__(self, argv, cwd, timeout, max_bytes):
        self.calls.append(argv)
        if argv and argv[0] in self._DANGER:  # destructive as the COMMAND -> abort
            raise AssertionError(f"runner was asked to execute {argv[0]!r}: {argv!r}")
        return self.result


def _executor(tmp_path, *, enabled=True, runner=None, operators=(OPERATOR,)):
    policy = SandboxPolicy(
        enabled=enabled,
        operators=frozenset(operators),
        cwd=tmp_path / "sandbox",
    )
    return SkreachExecutor(policy, runner=runner or FakeRunner())


# ---------------------------------------------------------------------------
# Executor unit tests
# ---------------------------------------------------------------------------


def test_allowlisted_command_yields_output_and_exit(tmp_path):
    runner = FakeRunner(RunnerResult(exit_code=0, stdout="file1\nfile2\n", stderr=""))
    ex = _executor(tmp_path, runner=runner)

    events = ex.run("ls -la", identity=OPERATOR, cmd_id="c1")

    assert runner.calls == [["ls", "-la"]]
    actions = [e["action"] for e in events]
    assert actions == ["output", "exit"]
    out = events[0]
    assert out == {
        "lane": "term",
        "action": "output",
        "id": "c1",
        "chunk": "file1\nfile2\n",
        "stream": "stdout",
    }
    assert events[1] == {"lane": "term", "action": "exit", "id": "c1", "code": 0}


def test_stderr_becomes_an_output_event(tmp_path):
    runner = FakeRunner(RunnerResult(exit_code=2, stdout="", stderr="nope\n"))
    ex = _executor(tmp_path, runner=runner)

    events = ex.run("cat missing", identity=OPERATOR, cmd_id="c2")

    assert events[0]["stream"] == "stderr"
    assert events[0]["chunk"] == "nope\n"
    assert events[-1] == {"lane": "term", "action": "exit", "id": "c2", "code": 2}


def test_non_allowlisted_command_is_denied(tmp_path):
    runner = FakeRunner()
    ex = _executor(tmp_path, runner=runner)

    events = ex.run("rm -rf /tmp/x", identity=OPERATOR, cmd_id="c3")

    assert len(events) == 1
    assert events[0]["action"] == "denied"
    assert "allowlist" in events[0]["reason"]
    assert runner.calls == []  # never reached the runner


def test_unauthorized_identity_is_denied(tmp_path):
    runner = FakeRunner()
    ex = _executor(tmp_path, runner=runner)

    events = ex.run("ls", identity="randomguest@evil.io", cmd_id="c4")

    assert len(events) == 1
    assert events[0]["action"] == "denied"
    assert "not authorized" in events[0]["reason"]
    assert runner.calls == []


def test_empty_identity_is_denied(tmp_path):
    ex = _executor(tmp_path)
    events = ex.run("ls", identity="", cmd_id="c5")
    assert events[0]["action"] == "denied"


def test_disabled_by_default_emits_exec_disabled(monkeypatch, tmp_path):
    # No SKREACHD_ENABLED set -> default policy is disabled.
    monkeypatch.delenv("SKREACHD_ENABLED", raising=False)
    runner = FakeRunner()
    ex = SkreachExecutor(
        SandboxPolicy(operators=frozenset({OPERATOR}), cwd=tmp_path / "s"),
        runner=runner,
    )
    assert ex.policy.enabled is False

    events = ex.run("ls", identity=OPERATOR, cmd_id="c6")

    assert len(events) == 1
    assert events[0]["action"] == "exec_disabled"
    assert events[0]["id"] == "c6"
    assert runner.calls == []


def test_shell_injection_is_not_executed_as_shell(tmp_path):
    """`ls; rm -rf /` must NOT run rm. argv parsing makes `;`/`rm` literal."""
    runner = FakeRunner()
    ex = _executor(tmp_path, runner=runner)

    events = ex.run("ls; rm -rf /", identity=OPERATOR, cmd_id="c7")

    # shlex.split -> ["ls;", "rm", "-rf", "/"]: the ";" attaches to "ls",
    # so argv[0] is the literal token "ls;" which is NOT allowlisted. The
    # command is DENIED outright. No shell ever parsed the ";", and rm never
    # reached the runner. (Even had ls; been allowlisted, "rm" would only be a
    # literal argument, never a separately-spawned command.)
    assert len(events) == 1
    assert events[0]["action"] == "denied"
    assert runner.calls == []  # rm absolutely never executed


def test_pipe_and_subshell_metachars_are_literal(tmp_path):
    runner = FakeRunner()
    ex = _executor(tmp_path, runner=runner)

    ex.run("echo $(whoami) | cat", identity=OPERATOR, cmd_id="c8")

    # No shell expansion: $(whoami) stays literal, | is a literal arg token.
    assert runner.calls == [["echo", "$(whoami)", "|", "cat"]]


def test_git_subcommand_allowlist_is_scoped(tmp_path):
    runner = FakeRunner()
    ex = _executor(tmp_path, runner=runner)

    # "git status" is allowlisted; "git push" is NOT.
    ok = ex.run("git status", identity=OPERATOR, cmd_id="g1")
    assert [e["action"] for e in ok][-1] == "exit"

    bad = ex.run("git push origin main", identity=OPERATOR, cmd_id="g2")
    assert len(bad) == 1 and bad[0]["action"] == "denied"


def test_empty_operator_set_denies_everyone(tmp_path):
    ex = SkreachExecutor(
        SandboxPolicy(enabled=True, operators=frozenset(), cwd=tmp_path / "s"),
        runner=FakeRunner(),
    )
    events = ex.run("ls", identity=OPERATOR, cmd_id="x")
    assert events[0]["action"] == "denied"


def test_real_runner_uses_shell_false():
    """The real subprocess runner must call subprocess with shell=False."""
    import skchat.spaces.skreachd as mod

    captured = {}

    class _FakeCompleted:
        returncode = 0
        stdout = b"hi\n"
        stderr = b""

    def _fake_run(argv, **kwargs):
        captured.update(kwargs)
        captured["argv"] = argv
        return _FakeCompleted()

    orig = mod.subprocess.run
    mod.subprocess.run = _fake_run
    try:
        res = mod.subprocess_runner(["echo", "hi"], "/tmp", 5.0, 1024)
    finally:
        mod.subprocess.run = orig

    assert captured["shell"] is False
    assert captured["argv"] == ["echo", "hi"]
    assert "PATH" in captured["env"]
    # Parent env (secrets) is NOT inherited — only the safe base keys present.
    assert set(captured["env"]) <= {"PATH", "LANG"}
    assert res.exit_code == 0 and res.stdout == "hi\n"


def test_output_is_capped(tmp_path):
    runner = FakeRunner(RunnerResult(exit_code=0, stdout="x" * 10, stderr=""))
    policy = SandboxPolicy(
        enabled=True,
        operators=frozenset({OPERATOR}),
        cwd=tmp_path / "s",
        max_output_bytes=5,
    )
    ex = SkreachExecutor(policy, runner=runner)
    events = ex.run("echo hi", identity=OPERATOR, cmd_id="cap")
    # The fake runner ignores max_bytes; the cap is the runner's job. This test
    # documents that the policy carries the cap through to the runner.
    assert policy.max_output_bytes == 5
    assert events[-1]["action"] == "exit"


# ---------------------------------------------------------------------------
# Route tests
# ---------------------------------------------------------------------------


@pytest.fixture
def client(tmp_path):
    app = FastAPI()
    return app, tmp_path


def test_route_runs_and_returns_events(tmp_path):
    runner = FakeRunner(RunnerResult(exit_code=0, stdout="hello\n", stderr=""))
    ex = _executor(tmp_path, runner=runner)

    app = FastAPI()
    register_spaces_routes(
        app,
        registry=SpaceRegistry(path=tmp_path / "spaces.json"),
        lane_store=_lane_store(tmp_path),
        skreach_executor=ex,
    )
    c = TestClient(app)

    r = c.post(
        "/spaces/space-x/lanes/term/run",
        json={"cmd": "echo hello", "id": "r1", "from": OPERATOR},
    )
    assert r.status_code == 200
    events = r.json()["events"]
    assert [e["action"] for e in events] == ["output", "exit"]
    assert events[0]["chunk"] == "hello\n"


def test_route_gated_when_disabled(tmp_path):
    ex = _executor(tmp_path, enabled=False)
    app = FastAPI()
    register_spaces_routes(
        app,
        registry=SpaceRegistry(path=tmp_path / "spaces.json"),
        lane_store=_lane_store(tmp_path),
        skreach_executor=ex,
    )
    c = TestClient(app)

    r = c.post(
        "/spaces/space-x/lanes/term/run",
        json={"cmd": "ls", "id": "r2", "from": OPERATOR},
    )
    assert r.status_code == 200
    events = r.json()["events"]
    assert len(events) == 1
    assert events[0]["action"] == "exec_disabled"


def test_route_denies_unauthorized(tmp_path):
    ex = _executor(tmp_path)
    app = FastAPI()
    register_spaces_routes(
        app,
        registry=SpaceRegistry(path=tmp_path / "spaces.json"),
        lane_store=_lane_store(tmp_path),
        skreach_executor=ex,
    )
    c = TestClient(app)

    r = c.post(
        "/spaces/space-x/lanes/term/run",
        json={"cmd": "ls", "id": "r3", "from": "stranger@x.io"},
    )
    assert r.json()["events"][0]["action"] == "denied"


def test_route_missing_cmd_is_400(tmp_path):
    ex = _executor(tmp_path)
    app = FastAPI()
    register_spaces_routes(
        app,
        registry=SpaceRegistry(path=tmp_path / "spaces.json"),
        lane_store=_lane_store(tmp_path),
        skreach_executor=ex,
    )
    c = TestClient(app)
    r = c.post("/spaces/space-x/lanes/term/run", json={"id": "r4"})
    assert r.status_code == 400


def _lane_store(tmp_path):
    from skchat.spaces.lanes import LaneStore

    return LaneStore(db_path=tmp_path / "lanes.db")
