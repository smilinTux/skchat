"""Tri-mode tests for the skchat ⇄ skcapstone integration adapter.

Contract per skcapstone/docs/ADR-optional-integration-backbone.md:
  * standalone  (SK_STANDALONE=1)           → native fallback (log only)
  * absent      (_sdk = None)               → native fallback (log only)
  * integrated  (skcapstone present,
                 SKCAPSTONE_HOME sandboxed) → sk-alert / skscheduler / registry

skchat already had ``integration.py`` absent — this adapter is the ADR's
optional-skcapstone backbone for skchat.  It mirrors the reference adapter
(``skmemory/integration.py``) and skcomms' exactly: alerts route to the shared
sk-alert PubSub bus by severity topic, scheduled work registers a ``jobs.d``
drop-in, and the service advertises itself to the discovery registry — every
call degrading to skchat's native behaviour (structured log / native daemon)
when skcapstone is absent.

skcapstone is installed in the dev venv, so "integrated" mode is exercised
against a sandboxed temp SKCAPSTONE_HOME — writes never leak to
~/.skcapstone/config/jobs.d/ or ~/.skcapstone/registry/.
"""

from __future__ import annotations

import json

import pytest

from skchat import integration


@pytest.fixture
def home(tmp_path, monkeypatch):
    """Sandbox skcapstone's shared home at a temp dir for each test.

    Both SKCAPSTONE_HOME (used by the scheduler_jobs writer + pubsub) and the
    skcapstone.AGENT_HOME module attribute (captured at import-time) are
    redirected to tmp_path so no fragment ever escapes to the real home.
    """
    monkeypatch.setenv("SKCAPSTONE_HOME", str(tmp_path))
    monkeypatch.delenv("SK_STANDALONE", raising=False)
    import skcapstone

    monkeypatch.setattr(skcapstone, "AGENT_HOME", str(tmp_path))
    return tmp_path


# ---------------------------------------------------------------------------
# Standalone mode — SK_STANDALONE=1
# ---------------------------------------------------------------------------


def test_standalone_flag_disables_integration(monkeypatch):
    """SK_STANDALONE=1 forces native mode regardless of skcapstone presence."""
    monkeypatch.setenv("SK_STANDALONE", "1")
    assert integration.is_present() is False
    assert integration.alert("delivery_failed", {"recipient": "peer@test"}, level="warn") is False
    assert integration.ensure_schedule() is False
    assert integration.register_self() is False
    assert integration.unregister_schedule() is False


# ---------------------------------------------------------------------------
# Absent mode — skcapstone package not importable
# ---------------------------------------------------------------------------


def test_absent_skcapstone_falls_back_to_log(monkeypatch):
    """When _sdk is None (skcapstone absent), every call returns False gracefully."""
    monkeypatch.delenv("SK_STANDALONE", raising=False)
    monkeypatch.setattr(integration, "_sdk", None)
    assert integration.is_present() is False
    assert integration.alert("message_delivery_failed", {"id": "x", "error": "oops"}) is False
    assert integration.ensure_schedule() is False
    assert integration.register_self() is False
    assert integration.unregister_schedule() is False


def test_absent_sdk_alert_returns_false_for_all_levels(monkeypatch):
    """Native fallback: alert() always returns False (no pubsub path)."""
    monkeypatch.setattr(integration, "_sdk", None)
    for level in ("info", "warn", "error", "critical"):
        assert integration.alert("test_event", {"k": "v"}, level=level) is False


def test_is_present_false_when_sdk_raises(monkeypatch):
    """A broken/partial skcapstone (is_available raises) is treated as absent."""
    monkeypatch.delenv("SK_STANDALONE", raising=False)

    class _Boom:
        @staticmethod
        def is_available():
            raise RuntimeError("partial install")

    monkeypatch.setattr(integration, "_sdk", _Boom)
    assert integration.is_present() is False


# ---------------------------------------------------------------------------
# Integrated mode — skcapstone present, SKCAPSTONE_HOME sandboxed
# ---------------------------------------------------------------------------


def test_is_present_true_when_skcapstone_available(home):
    """With skcapstone installed and no SK_STANDALONE, is_present() is True."""
    assert integration.is_present() is True


def test_alert_publishes_to_correct_severity_topic(home):
    """alert() writes a pubsub message at topic skchat.<level>."""
    assert integration.alert("delivery_failed", {"recipient": "peer@realm"}, level="warn") is True
    topic_dir = home / "pubsub" / "topics" / "skchat.warn"
    assert topic_dir.is_dir(), f"expected topic dir {topic_dir} to exist"
    msg_files = list(topic_dir.glob("msg-*.json"))
    assert msg_files, "expected at least one pubsub message file"
    data = json.loads(msg_files[0].read_text())
    assert data["topic"] == "skchat.warn"
    # CRITICAL: event name must be in payload, NOT in topic suffix
    assert data["payload"]["event"] == "delivery_failed"
    assert data["payload"]["recipient"] == "peer@realm"


def test_alert_critical_level_publishes(home):
    """critical-level alert lands on skchat.critical topic."""
    assert integration.alert("daemon_crash", {"detail": "segfault"}, level="critical") is True
    topic_dir = home / "pubsub" / "topics" / "skchat.critical"
    assert topic_dir.is_dir()
    data = json.loads(next(topic_dir.glob("msg-*.json")).read_text())
    assert data["payload"]["event"] == "daemon_crash"


def test_alert_unknown_level_does_not_crash(home):
    """An unrecognised severity degrades to a published info-ish alert."""
    # SDK coerces unknown level to "info"; adapter still returns published=True
    assert integration.alert("weird", {"k": "v"}, level="bogus") is True


def test_ensure_schedule_registers_outbox_sweep(home):
    """ensure_schedule() writes a jobs.d drop-in for the outbox flush sweep."""
    assert integration.ensure_schedule(interval_minutes=10) is True
    from skcapstone.scheduler_jobs import load_jobs_with_dropins

    jobs = {j.name: j for j in load_jobs_with_dropins(home / "config" / "jobs.yaml")}
    assert integration.OUTBOX_JOB in jobs, f"expected {integration.OUTBOX_JOB} in {list(jobs)}"
    assert jobs[integration.OUTBOX_JOB].command == "skchat outbox flush"
    assert jobs[integration.OUTBOX_JOB].every_seconds == 10 * 60


def test_ensure_schedule_idempotent(home):
    """Calling ensure_schedule() twice does not raise."""
    assert integration.ensure_schedule() is True
    assert integration.ensure_schedule() is True


def test_unregister_schedule_removes_job(home):
    """unregister_schedule() removes the outbox-sweep drop-in."""
    integration.ensure_schedule()
    assert integration.unregister_schedule() is True
    from skcapstone.scheduler_jobs import load_jobs_with_dropins

    jobs = {j.name: j for j in load_jobs_with_dropins(home / "config" / "jobs.yaml")}
    assert integration.OUTBOX_JOB not in jobs


def test_register_self_writes_registry_entry(home):
    """register_self() writes a service registry JSON file."""
    assert integration.register_self(pid_file="/tmp/skchat-test.pid") is True
    registry_file = home / "registry" / "skchat.json"
    assert registry_file.exists(), f"expected registry file {registry_file}"
    entry = json.loads(registry_file.read_text())
    assert entry["name"] == "skchat"
    assert entry["health_url"]  # adapter supplies a default health URL


def test_register_self_uses_default_pid_file(home):
    """register_self() defaults to skchat's native pid-file path when not given."""
    assert integration.register_self() is True
    entry = json.loads((home / "registry" / "skchat.json").read_text())
    assert entry["pid_file"].endswith("daemon.pid")


# ---------------------------------------------------------------------------
# Capability / feature detection
# ---------------------------------------------------------------------------


def test_capabilities_reports_integrated(home):
    """capabilities() advertises the integrated backbone when present."""
    caps = integration.capabilities()
    assert caps["service"] == "skchat"
    assert caps["integrated"] is True
    assert "sk-alert" in caps["features"]
    assert "skscheduler" in caps["features"]


def test_capabilities_reports_standalone(monkeypatch):
    """capabilities() reports standalone with empty backbone features when absent."""
    monkeypatch.setattr(integration, "_sdk", None)
    caps = integration.capabilities()
    assert caps["service"] == "skchat"
    assert caps["integrated"] is False
    assert caps["features"] == []


# ---------------------------------------------------------------------------
# Sandboxing — integrated writes stay in the temp home
# ---------------------------------------------------------------------------


def test_no_leak_to_real_home(home):
    """All integrated operations use the sandboxed home, not ~/.skcapstone."""
    integration.ensure_schedule()
    integration.register_self(pid_file="/tmp/skchat-leak-test.pid")
    assert (home / "registry" / "skchat.json").exists()


# ---------------------------------------------------------------------------
# Wiring smoke: package import does not hard-depend on skcapstone
# ---------------------------------------------------------------------------


def test_integration_importable_from_package():
    """skchat re-exports the integration adapter (safe optional import)."""
    from skchat import integration as _integ  # noqa: F401

    assert hasattr(_integ, "is_present")
    assert hasattr(_integ, "alert")
    assert hasattr(_integ, "capabilities")
