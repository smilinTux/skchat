"""Tests for scripts/bridge_watchdog.py: sd_notify heartbeat + poll-failure
wedge detection for the Telegram bridge.

sd_notify() and PollFailureTracker are pure/stateful stdlib-only helpers
with no live asyncio loop and no network. sd_notify is exercised against a
real local AF_UNIX datagram socket standing in for systemd's NOTIFY_SOCKET;
sk_alert is exercised against tiny fake shell scripts instead of the real
sk-alert binary.
"""
from __future__ import annotations

import importlib.util
import pathlib
import socket

import pytest

_SCRIPTS = pathlib.Path(__file__).resolve().parent.parent / "scripts"


def _load(name: str, modname: str):
    spec = importlib.util.spec_from_file_location(modname, _SCRIPTS / name)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture()
def bw():
    return _load("bridge_watchdog.py", "bridge_watchdog_under_test")


# ── sd_notify ────────────────────────────────────────────────────────────


class TestSdNotify:
    def test_noop_when_notify_socket_unset(self, bw):
        assert bw.sd_notify("READY=1", environ={}) is False

    def test_noop_never_raises_on_bad_socket(self, bw, tmp_path):
        # A path that exists but is not a listening datagram socket: connect
        # fails with OSError, which must be swallowed, not raised.
        bogus = tmp_path / "not-a-socket"
        bogus.write_text("nope")
        assert bw.sd_notify("READY=1", environ={"NOTIFY_SOCKET": str(bogus)}) is False

    def test_noop_never_raises_on_missing_path(self, bw, tmp_path):
        missing = tmp_path / "does-not-exist.sock"
        assert bw.sd_notify("READY=1", environ={"NOTIFY_SOCKET": str(missing)}) is False

    def test_sends_ready_datagram_to_real_socket(self, bw, tmp_path):
        sock_path = str(tmp_path / "notify.sock")
        srv = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
        srv.bind(sock_path)
        srv.settimeout(2)
        try:
            ok = bw.sd_notify("READY=1", environ={"NOTIFY_SOCKET": sock_path})
            assert ok is True
            data, _ = srv.recvfrom(4096)
            assert data == b"READY=1"
        finally:
            srv.close()

    def test_sends_watchdog_datagram_to_real_socket(self, bw, tmp_path):
        sock_path = str(tmp_path / "wd.sock")
        srv = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
        srv.bind(sock_path)
        srv.settimeout(2)
        try:
            ok = bw.sd_notify("WATCHDOG=1", environ={"NOTIFY_SOCKET": sock_path})
            assert ok is True
            data, _ = srv.recvfrom(4096)
            assert data == b"WATCHDOG=1"
        finally:
            srv.close()

    def test_reads_environ_at_call_time_not_import_time(self, bw, tmp_path):
        # Two calls with different environ dicts must each honor their own
        # NOTIFY_SOCKET -- proves the lookup is per-call, not cached.
        sock_a = str(tmp_path / "a.sock")
        sock_b = str(tmp_path / "b.sock")
        srv_a = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
        srv_a.bind(sock_a)
        srv_a.settimeout(2)
        srv_b = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
        srv_b.bind(sock_b)
        srv_b.settimeout(2)
        try:
            assert bw.sd_notify("READY=1", environ={"NOTIFY_SOCKET": sock_a}) is True
            assert bw.sd_notify("READY=1", environ={"NOTIFY_SOCKET": sock_b}) is True
            assert srv_a.recvfrom(4096)[0] == b"READY=1"
            assert srv_b.recvfrom(4096)[0] == b"READY=1"
        finally:
            srv_a.close()
            srv_b.close()


# ── PollFailureTracker ──────────────────────────────────────────────────


class TestPollFailureTracker:
    def test_does_not_trip_below_threshold(self, bw):
        t = bw.PollFailureTracker(threshold=3)
        assert t.on_failure() is False
        assert t.on_failure() is False
        assert t.consecutive == 2

    def test_trips_at_threshold(self, bw):
        t = bw.PollFailureTracker(threshold=3)
        t.on_failure()
        t.on_failure()
        assert t.on_failure() is True
        assert t.consecutive == 3

    def test_resets_on_success(self, bw):
        t = bw.PollFailureTracker(threshold=3)
        t.on_failure()
        t.on_failure()
        t.on_success()
        assert t.consecutive == 0
        assert t.on_failure() is False
        assert t.on_failure() is False
        assert t.on_failure() is True

    def test_success_before_any_failure_is_a_noop(self, bw):
        t = bw.PollFailureTracker(threshold=3)
        t.on_success()
        assert t.consecutive == 0

    def test_default_threshold_is_three(self, bw):
        t = bw.PollFailureTracker()
        assert t.threshold == 3

    def test_threshold_one_trips_immediately(self, bw):
        t = bw.PollFailureTracker(threshold=1)
        assert t.on_failure() is True

    def test_invalid_threshold_rejected(self, bw):
        with pytest.raises(ValueError):
            bw.PollFailureTracker(threshold=0)
        with pytest.raises(ValueError):
            bw.PollFailureTracker(threshold=-1)

    def test_stays_tripped_while_failures_continue(self, bw):
        t = bw.PollFailureTracker(threshold=2)
        assert t.on_failure() is False
        assert t.on_failure() is True
        assert t.on_failure() is True  # still >= threshold, keeps returning True


# ── sk_alert (best-effort) ───────────────────────────────────────────────


class TestSkAlert:
    def test_missing_binary_returns_false_no_raise(self, bw, tmp_path):
        missing = tmp_path / "no-such-binary"
        assert bw.sk_alert("test message", binary=str(missing)) is False

    def test_success_returns_true(self, bw, tmp_path):
        script = tmp_path / "fake-sk-alert"
        script.write_text("#!/bin/sh\nexit 0\n")
        script.chmod(0o755)
        assert bw.sk_alert("test message", binary=str(script)) is True

    def test_nonzero_exit_returns_false_no_raise(self, bw, tmp_path):
        script = tmp_path / "fake-sk-alert-fail"
        script.write_text("#!/bin/sh\nexit 1\n")
        script.chmod(0o755)
        assert bw.sk_alert("test message", binary=str(script)) is False

    def test_timeout_returns_false_no_raise(self, bw, tmp_path):
        script = tmp_path / "fake-sk-alert-slow"
        script.write_text("#!/bin/sh\nsleep 5\n")
        script.chmod(0o755)
        assert bw.sk_alert("test message", binary=str(script), timeout=0.2) is False
