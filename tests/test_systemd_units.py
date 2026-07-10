"""Lint tests for the shipped systemd units (coord e0161de7).

Guards against the two fresh-install landmines that were fixed:
- WatchdogSec on a daemon that never calls sd_notify (watchdog kill loop)
- ExecStart pointing at dead or machine-specific absolute paths

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

UNIT_FILES = sorted(
    p
    for p in SYSTEMD_DIR.iterdir()
    if p.suffix in (".service", ".target", ".timer", ".socket")
)

EXPECTED_UNITS = {"skchat-daemon.service", "skchat-telegram@.service"}

RETIRED_UNITS = {
    "skchat-opus-bridge.service",
    "skchat-lumina-bridge.service",
    "skchat-bridges.target",
}


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
    """Units must be portable: no dead paths, no absolute /home/<user>."""
    text = unit.read_text()
    assert "dkloud.douno.it" not in text
    assert not re.search(r"/home/\w+", text), (
        f"{unit.name} hardcodes an absolute /home path; use %h instead"
    )


def test_daemon_unit_type_matches_daemonization() -> None:
    """`skchat daemon start` forks a detached child and writes a PID file,
    so the unit must be Type=forking with a matching PIDFile."""
    lines = _directives((SYSTEMD_DIR / "skchat-daemon.service").read_text())
    assert "Type=forking" in lines
    assert "PIDFile=%h/.skchat/daemon.pid" in lines


def test_bridge_template_runs_current_bridge() -> None:
    """The template must exec the current bridge script with a repo path
    that is overridable through the named environment file."""
    text = (SYSTEMD_DIR / "skchat-telegram@.service").read_text()
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


def test_bridge_script_exists() -> None:
    """The ExecStart target actually exists in the repo."""
    repo = SYSTEMD_DIR.parent
    assert (repo / "scripts" / "telegram_bridge.py").is_file()


def test_install_sh_installs_current_units_and_retires_old() -> None:
    """install.sh must install the current units and clean up retired ones."""
    text = (SYSTEMD_DIR / "install.sh").read_text()
    for unit in EXPECTED_UNITS:
        assert unit in text
    for unit in RETIRED_UNITS:
        assert unit in text, f"install.sh should retire {unit} on upgrade"
    assert "systemd-analyze --user verify" in text


@pytest.mark.skipif(
    shutil.which("systemd-analyze") is None, reason="systemd-analyze not available"
)
@pytest.mark.parametrize("unit", UNIT_FILES, ids=lambda p: p.name)
def test_systemd_analyze_verify(unit: Path, tmp_path: Path) -> None:
    """Each shipped unit passes systemd-analyze verify at user scope."""
    name = unit.name.replace("@.", "@verifyinstance.")
    staged = tmp_path / name
    staged.write_text(unit.read_text())
    proc = subprocess.run(
        ["systemd-analyze", "--user", "verify", str(staged)],
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert proc.returncode == 0, proc.stderr or proc.stdout
