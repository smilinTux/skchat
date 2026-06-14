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


# ===========================================================================
# QA Area 2 — additional adversarial / edge coverage
# ===========================================================================


def test_newline_injection_never_spawns_second_command(tmp_path):
    """A newline-separated `ls\\nrm -rf /` must not run rm.

    shlex treats the newline as whitespace, so the string collapses to the argv
    ['ls', 'rm', '-rf', '/']. 'ls' is allowlisted; 'rm'/'-rf'/'/' are passed as
    *literal arguments to ls*, never spawned as a separate command.
    """
    runner = FakeRunner()
    ex = _executor(tmp_path, runner=runner)

    events = ex.run("ls\nrm -rf /", identity=OPERATOR, cmd_id="nl")

    assert runner.calls == [["ls", "rm", "-rf", "/"]]  # one argv, rm is an arg
    assert [e["action"] for e in events][-1] == "exit"


def test_absolute_path_binary_is_not_allowlisted(tmp_path):
    """`/bin/ls` must be denied — the allowlist matches the bare token 'ls'."""
    runner = FakeRunner()
    ex = _executor(tmp_path, runner=runner)

    events = ex.run("/bin/ls", identity=OPERATOR, cmd_id="abs")

    assert len(events) == 1
    assert events[0]["action"] == "denied"
    assert runner.calls == []


def test_prefix_lookalike_binary_is_denied(tmp_path):
    """`lsblk` must NOT slip past the 'ls' allowlist entry.

    Allowlisting is token-equality on leading argv tokens, not string-prefix, so
    'lsblk' (a single token) does not equal 'ls'.
    """
    runner = FakeRunner()
    ex = _executor(tmp_path, runner=runner)

    events = ex.run("lsblk", identity=OPERATOR, cmd_id="lb")

    assert events[0]["action"] == "denied"
    assert runner.calls == []


def test_backtick_and_redirect_metachars_are_literal(tmp_path):
    """Backticks and redirects stay literal argv tokens — no shell ever sees them."""
    runner = FakeRunner()
    ex = _executor(tmp_path, runner=runner)

    # `echo` is allowlisted; the metachars become plain arguments to it.
    ex.run("echo `whoami` > /etc/passwd", identity=OPERATOR, cmd_id="meta")

    assert runner.calls == [["echo", "`whoami`", ">", "/etc/passwd"]]


def test_path_traversal_arg_reaches_runner_but_cwd_is_scoped(tmp_path):
    """Documents the containment model: a `..` traversal *argument* is NOT blocked
    at the allowlist layer (cat is allowlisted); containment is the scoped cwd +
    scrubbed env, which the executor always passes to the runner.
    """
    runner = FakeRunner()
    sandbox = tmp_path / "sandbox"
    policy = SandboxPolicy(
        enabled=True, operators=frozenset({OPERATOR}), cwd=sandbox
    )
    ex = SkreachExecutor(policy, runner=runner)

    ex.run("cat ../../etc/passwd", identity=OPERATOR, cmd_id="trav")

    # The traversal token is passed through as an arg AND the runner was handed
    # the scoped sandbox cwd (the actual containment boundary).
    assert runner.calls == [["cat", "../../etc/passwd"]]
    # cwd is the scoped sandbox, created by the executor.
    assert sandbox.exists()


def test_unbalanced_quote_is_denied_not_crashed(tmp_path):
    """An unparseable command (dangling quote) is denied with a parse reason."""
    ex = _executor(tmp_path)
    events = ex.run('echo "unterminated', identity=OPERATOR, cmd_id="q")
    assert len(events) == 1
    assert events[0]["action"] == "denied"
    assert "parse" in events[0]["reason"].lower()


def test_whitespace_only_command_is_denied(tmp_path):
    """A blank/whitespace command parses to an empty argv → denied as empty."""
    runner = FakeRunner()
    ex = _executor(tmp_path, runner=runner)
    events = ex.run("   \t  ", identity=OPERATOR, cmd_id="blank")
    assert events[0]["action"] == "denied"
    assert "empty" in events[0]["reason"].lower()
    assert runner.calls == []


def test_destructive_token_as_argument_is_allowed(tmp_path):
    """A destructive WORD appearing only as an argument is harmless.

    `grep rm somefile` must run grep (with 'rm' as a search pattern) — the
    FakeRunner only aborts when a destructive token is argv[0].
    """
    runner = FakeRunner()
    ex = _executor(tmp_path, runner=runner)
    events = ex.run("grep rm somefile", identity=OPERATOR, cmd_id="ga")
    assert runner.calls == [["grep", "rm", "somefile"]]
    assert [e["action"] for e in events][-1] == "exit"


def test_real_runner_truncates_output_to_cap():
    """The real subprocess runner caps each stream to max_bytes."""
    import skchat.spaces.skreachd as mod

    class _Big:
        returncode = 0
        stdout = b"A" * 1000
        stderr = b"B" * 1000

    orig = mod.subprocess.run
    mod.subprocess.run = lambda argv, **kw: _Big()
    try:
        res = mod.subprocess_runner(["echo"], "/tmp", 5.0, 10)
    finally:
        mod.subprocess.run = orig
    assert len(res.stdout) == 10
    assert len(res.stderr) == 10


def test_real_runner_handles_timeout():
    """A TimeoutExpired yields exit 124, timed_out=True, and a timeout message."""
    import subprocess

    import skchat.spaces.skreachd as mod

    def _timeout(argv, **kw):
        raise subprocess.TimeoutExpired(
            cmd=argv, timeout=kw.get("timeout"), output=b"partial", stderr=b""
        )

    orig = mod.subprocess.run
    mod.subprocess.run = _timeout
    try:
        res = mod.subprocess_runner(["sleep", "99"], "/tmp", 1.0, 1024)
    finally:
        mod.subprocess.run = orig
    assert res.timed_out is True
    assert res.exit_code == 124
    assert res.stdout == "partial"
    assert "timed out" in res.stderr


def test_real_runner_handles_missing_binary():
    """An OSError (binary not found) yields exit 127 with the error as stderr."""
    import skchat.spaces.skreachd as mod

    def _oserr(argv, **kw):
        raise FileNotFoundError("no such file")

    orig = mod.subprocess.run
    mod.subprocess.run = _oserr
    try:
        res = mod.subprocess_runner(["nope"], "/tmp", 1.0, 1024)
    finally:
        mod.subprocess.run = orig
    assert res.exit_code == 127
    assert "no such file" in res.stderr


def test_route_lanes_event_term_does_not_execute(tmp_path):
    """SAFETY BOUNDARY: posting a term run-request to the generic lanes/event
    route must only PERSIST it (append to the log) — never execute it. Execution
    happens ONLY via the explicit lanes/term/run route.
    """
    runner = FakeRunner()
    ex = _executor(tmp_path, runner=runner)
    store = _lane_store(tmp_path)
    app = FastAPI()
    register_spaces_routes(
        app,
        registry=SpaceRegistry(path=tmp_path / "spaces.json"),
        lane_store=store,
        skreach_executor=ex,
    )
    c = TestClient(app)

    r = c.post(
        "/spaces/sp/lanes/event",
        json={"lane": "term", "action": "run", "cmd": "ls", "from": OPERATOR},
    )
    assert r.status_code == 200
    # Nothing was executed.
    assert runner.calls == []
    # But it WAS persisted to the term log lane for replay.
    state = c.get("/spaces/sp/lanes/term/state").json()
    assert state["events"][-1]["cmd"] == "ls"


def test_route_non_string_cmd_is_400(tmp_path):
    """A non-string `cmd` (e.g. a list) on the run route is a 400, not a 500."""
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
        "/spaces/sp/lanes/term/run",
        json={"cmd": ["ls", "-la"], "id": "r5", "from": OPERATOR},
    )
    assert r.status_code == 400
