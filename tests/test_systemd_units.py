"""Lint tests for the shipped systemd units (coord e0161de7).

Guards against the two fresh-install landmines that were fixed:
- WatchdogSec on a daemon that never calls sd_notify (watchdog kill loop)
- ExecStart pointing at dead or machine-specific absolute paths

Tracks the Phase 1 reconcile (6ba43ea), which moved the shipped unit files
into systemd/units/ while install.sh stayed at the systemd/ root.

These are static checks on the repo files only; nothing is installed
or started.
"""

from __future__ import annotations

import re
import shutil
import subprocess
from pathlib import Path

import pytest

SYSTEMD_DIR = Path(__file__).resolve().parent.parent / "systemd"
# Phase 1 reconcile (6ba43ea) moved the shipped unit files out of the top level
# of systemd/ into systemd/units/ (top level now holds install.sh, README.md,
# and the dropins/, examples/, coturn/ subdirs). install.sh still lives at the
# systemd/ root.
UNITS_DIR = SYSTEMD_DIR / "units"

UNIT_FILES = sorted(
    p
    for p in UNITS_DIR.iterdir()
    if p.suffix in (".service", ".target", ".timer", ".socket")
)

# The full reconciled .158 skchat plane installed by systemd/install.sh.
EXPECTED_UNITS = {
    "skchat-daemon.service",
    "skchat-daemon-opus.service",
    "skchat-daemon-chef.service",
    "skchat-app-web.service",
    "skchat-telegram-opus.service",
    "skchat-telegram-lumina.service",
    "skchat-telegram@.service",
    "skchat-lumina-call.service",
    "skchat-nostr-relay.service",
    "skchat-piper-tts.service",
    "skchat-webui@.service",
    "livekit-server.service",
    "jarvis-heartbeat.service",
    "skchat-coturn.service",
    "telegram-catchup.service",
    "telegram-catchup.timer",
    "skchat-backup.service",
    "skchat-backup.timer",
    "skchat-health-probe.service",
    "skchat-health-probe.timer",
}

# Old bridge units replaced by the skchat-telegram-*/skchat-telegram@ template.
# Their retirement is documented in the go-forward template header.
RETIRED_BRIDGE_UNITS = {
    "skchat-opus-bridge.service",
    "skchat-lumina-bridge.service",
    "skchat-bridges.target",
}

# Legacy uvicorn TTS unit (:15090), superseded by skchat-piper-tts.service
# (:18797); install.sh explicitly does NOT install it.
LEGACY_UNIT = "piper-tts.service"

# Everything the reconcile killed; none of these may ship in units/.
RETIRED_UNITS = RETIRED_BRIDGE_UNITS | {LEGACY_UNIT}


def _directives(text: str) -> list[str]:
    """Return non-comment, non-blank unit file lines."""
    return [
        line.strip()
        for line in text.splitlines()
        if line.strip() and not line.strip().startswith(("#", ";"))
    ]


def test_expected_unit_set() -> None:
    """The repo ships exactly the current units, none of the retired ones."""
    names = {p.name for p in UNIT_FILES}
    assert names == EXPECTED_UNITS
    assert not names & RETIRED_UNITS


@pytest.mark.parametrize("unit", UNIT_FILES, ids=lambda p: p.name)
def test_no_watchdog_without_sd_notify(unit: Path) -> None:
    """No unit may set WatchdogSec until the code sends sd_notify pings."""
    for line in _directives(unit.read_text()):
        assert not line.startswith("WatchdogSec"), (
            f"{unit.name} sets WatchdogSec but the daemon never calls "
            "sd_notify, so systemd would kill it in a loop"
        )


@pytest.mark.parametrize("unit", UNIT_FILES, ids=lambda p: p.name)
def test_no_dead_or_machine_specific_paths(unit: Path) -> None:
    """Units must be portable: no dead paths, no absolute /home/<user>.

    The dead host must not appear anywhere. The /home check is applied to
    actual directives only: a comment noting that a hardcoded path *was*
    replaced by %h is documentation, not a portability regression.
    """
    text = unit.read_text()
    assert "dkloud.douno.it" not in text
    for line in _directives(text):
        assert not re.search(r"/home/\w+", line), (
            f"{unit.name} hardcodes an absolute /home path; use %h instead"
        )


def test_daemon_unit_type_matches_daemonization() -> None:
    """`skchat daemon start` forks a detached child and writes a PID file,
    so the unit must be Type=forking with a matching PIDFile."""
    lines = _directives((UNITS_DIR / "skchat-daemon.service").read_text())
    assert "Type=forking" in lines
    assert "PIDFile=%h/.skchat/daemon.pid" in lines


def test_bridge_template_runs_current_bridge() -> None:
    """The template must exec the current bridge script with a repo path
    that is overridable through the named environment file."""
    text = (UNITS_DIR / "skchat-telegram@.service").read_text()
    lines = _directives(text)
    exec_lines = [ln for ln in lines if ln.startswith("ExecStart=")]
    assert len(exec_lines) == 1
    assert exec_lines[0] == (
        "ExecStart=%h/.skenv/bin/python ${SKCHAT_REPO}/scripts/telegram_bridge.py"
    )
    assert "EnvironmentFile=%h/.config/skchat/telegram-%i.env" in lines
    assert "Environment=SKCHAT_REPO=%h/clawd/skcapstone-repos/skchat" in lines
    # The env-file contract (bot token key) is documented in the unit.
    assert "SKC_BRIDGE_TOKEN" in text
    # The template is the go-forward replacement for the retired bridge units,
    # and records that retirement in its header so the migration is traceable.
    for retired in RETIRED_BRIDGE_UNITS:
        assert retired in text, (
            f"telegram@ template should document that it retires {retired}"
        )


def test_bridge_script_exists() -> None:
    """The ExecStart target actually exists in the repo."""
    repo = SYSTEMD_DIR.parent
    assert (repo / "scripts" / "telegram_bridge.py").is_file()


def test_install_sh_installs_current_units_and_retires_old() -> None:
    """install.sh must install every current unit, verify them, and document
    that it deliberately does NOT install the retired legacy TTS unit."""
    text = (SYSTEMD_DIR / "install.sh").read_text()
    for unit in EXPECTED_UNITS:
        assert unit in text, f"install.sh must install {unit}"
    # The legacy uvicorn TTS unit is retired: install.sh names it in the
    # "NOT installed (by design)" block as deprecated, superseded by
    # skchat-piper-tts.service. Match on the port-qualified deprecation note so
    # this is not trivially satisfied by the skchat-piper-tts.service substring.
    assert f"{LEGACY_UNIT} (:15090" in text, (
        "install.sh should document retiring the legacy piper-tts.service"
    )
    assert "DEPRECATED" in text
    assert "skchat-piper-tts.service" in text
    assert "systemd-analyze --user verify" in text


# systemd-analyze verify resolves %h to $HOME and checks that ExecStart binaries
# exist and are executable. A few units (e.g. skchat-coturn.service) ExecStart a
# helper script that install.sh materializes at runtime under ~/.skchat/, so on a
# box where install.sh has not been run that path is absent. That is a
# provisioning artifact, not a unit-file defect: dead/absolute paths are already
# caught by test_no_dead_or_machine_specific_paths, and the helper ships in the
# repo under systemd/coturn/. Tolerate ONLY that benign diagnostic class.
_BENIGN_VERIFY = re.compile(r"is not executable|No such file or directory")
_PROVISIONED_HELPER = "/.skchat/"


@pytest.mark.skipif(
    shutil.which("systemd-analyze") is None, reason="systemd-analyze not available"
)
@pytest.mark.parametrize("unit", UNIT_FILES, ids=lambda p: p.name)
def test_systemd_analyze_verify(unit: Path, tmp_path: Path) -> None:
    """Each shipped unit passes systemd-analyze verify at user scope.

    A verify failure is real unless every diagnostic is the benign
    "install.sh-provisioned helper not present yet" case (a missing/non-executable
    ExecStart target under ~/.skchat/).
    """
    name = unit.name.replace("@.", "@verifyinstance.")
    staged = tmp_path / name
    staged.write_text(unit.read_text())
    proc = subprocess.run(
        ["systemd-analyze", "--user", "verify", str(staged)],
        capture_output=True,
        text=True,
        timeout=60,
    )
    if proc.returncode == 0:
        return
    out = (proc.stderr or "") + (proc.stdout or "")
    problems = [ln.strip() for ln in out.splitlines() if ln.strip()]
    unresolved = [
        ln
        for ln in problems
        if not (_BENIGN_VERIFY.search(ln) and _PROVISIONED_HELPER in ln)
    ]
    assert not unresolved, out
