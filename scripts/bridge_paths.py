#!/usr/bin/env python3
"""Portable sys.path setup for the skchat bridge scripts.

Historically ``telegram_bridge.py`` hardcoded absolute ``sys.path.insert``
lines for one specific machine and username, so the bridge could not start
anywhere else. This module resolves the ``skchat`` and ``skcomms`` imports
portably instead. Per package, in order:

  1. Already importable (e.g. ``pip install -e`` into the running venv,
     which is how the live bridges resolve them): nothing to add.
  2. ``SKCHAT_SRC`` / ``SKCOMMS_SRC`` environment variables pointing at a
     checkout's ``src`` directory (developer override). Ignored (with a
     warning) when the directory does not actually contain the package.
  3. Derived from this checkout's layout: ``<repo>/src`` for skchat and the
     sibling ``<repos>/skcomms/src`` for skcomms. This matches the historical
     repo layout the live bridges run from, so behavior on the production
     host is unchanged, just no longer tied to one username.

The pure logic lives in :func:`compute_src_paths` (with injectable
``environ`` / ``importable`` / ``script_path``) so tests can exercise
foreign-home layouts without touching real interpreter state.
:func:`ensure_importable` applies the result to ``sys.path`` idempotently.
"""
from __future__ import annotations

import importlib.util
import logging
import os
import sys
from pathlib import Path
from typing import Callable, Mapping, Optional

log = logging.getLogger("tg-bridge.paths")


def _derive_skchat_src(repo: Path) -> Path:
    """skchat lives in this very checkout: ``<repo>/src``."""
    return repo / "src"


def _derive_skcomms_src(repo: Path) -> Path:
    """skcomms is a sibling checkout: ``<repos>/skcomms/src``."""
    return repo.parent / "skcomms" / "src"


# Order matters: each resolved path is inserted at sys.path position 0 in this
# order (skcomms first, then skchat), so skchat ends up AHEAD of skcomms on
# sys.path, exactly like the old pair of hardcoded inserts.
_PACKAGES: tuple[tuple[str, str, Callable[[Path], Path]], ...] = (
    ("skcomms", "SKCOMMS_SRC", _derive_skcomms_src),
    ("skchat", "SKCHAT_SRC", _derive_skchat_src),
)


def _default_importable(package: str) -> bool:
    """True when *package* already resolves (installed into the venv)."""
    try:
        return importlib.util.find_spec(package) is not None
    except (ImportError, ValueError):
        return False


def repo_root(script_path: Path | str | None = None) -> Path:
    """The skchat checkout root. This file lives in ``<repo>/scripts/``."""
    p = Path(script_path) if script_path is not None else Path(__file__)
    return p.resolve().parent.parent


def _has_package(src_dir: Path, package: str) -> bool:
    """True when *src_dir* actually contains *package* (guards bad overrides)."""
    return (src_dir / package / "__init__.py").is_file()


def compute_src_paths(
    script_path: Path | str | None = None,
    environ: Optional[Mapping[str, str]] = None,
    importable: Optional[Callable[[str], bool]] = None,
) -> list[str]:
    """Resolve the src directories that must be prepended to sys.path.

    Returns the paths in insertion order (each is meant to be inserted at
    position 0, so the LAST entry ends up first on sys.path). Packages that
    are already importable contribute nothing: the installed package wins.
    """
    env = os.environ if environ is None else environ
    can_import = _default_importable if importable is None else importable
    repo = repo_root(script_path)
    out: list[str] = []
    for package, env_var, derive in _PACKAGES:
        if can_import(package):
            continue  # pip-installed into the venv: preferred, nothing to add
        override = (env.get(env_var) or "").strip()
        if override:
            cand = Path(os.path.expanduser(override))
            if _has_package(cand, package):
                out.append(str(cand))
                continue
            log.warning(
                "%s=%s does not contain package %r; falling back to the repo layout",
                env_var, override, package,
            )
        derived = derive(repo)
        if _has_package(derived, package):
            out.append(str(derived))
        else:
            log.warning(
                "cannot locate package %r (not installed, no usable %s, and %s "
                "does not contain it); imports may fail",
                package, env_var, derived,
            )
    return out


def ensure_importable(
    script_path: Path | str | None = None,
    environ: Optional[Mapping[str, str]] = None,
    importable: Optional[Callable[[str], bool]] = None,
    path_list: Optional[list[str]] = None,
) -> list[str]:
    """Prepend any needed src directories to ``sys.path`` (idempotent).

    ``path_list`` defaults to the real ``sys.path``; tests can pass their own
    list. Returns the paths that were actually added (empty when the installed
    packages already cover everything).
    """
    paths = compute_src_paths(
        script_path=script_path, environ=environ, importable=importable
    )
    target = sys.path if path_list is None else path_list
    added: list[str] = []
    for p in paths:
        if p not in target:
            target.insert(0, p)
            added.append(p)
    return added
