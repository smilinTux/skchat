"""Aggregate every discoverable SKWorld subapp manifest for the shell.

The SKWorld shell (Flutter) needs ONE same-origin endpoint, reachable over the
443 funnel, to learn about all subapps at once instead of probing each daemon
port itself. ``webui.py`` exposes that as the public route
``GET /api/v1/shell/modules`` (no bearer, like ``/.well-known/skworld-module.json``);
this module does the aggregation behind it.

Design (umbrella shell design 5.3, "Registry"): the v1 registry is a static set
of manifest locations on each node under ``~/.skcapstone/shell/modules/``. We
combine those static, statically-emitted manifests with the manifests a few
sibling daemons serve live, so the shell sees the union.

Sources aggregated, all best-effort (any unreachable/missing source is logged
and skipped, it never fails the whole response):

1. skchat's OWN manifest, built in-process (``skworld_manifest.skchat_module_manifest``).
2. skcode's manifest, fetched from skcode-hostd's ``/.well-known/skworld-module.json``
   (``SKCODE_HOSTD_URL``, default ``http://100.108.59.57:9394``). Its URLs are
   rewritten onto the same-origin ``/skcode`` reverse-proxy path (webui.py's
   ``skcode_proxy``) so the browser reaches skcode over the funnel, not the raw
   tailnet daemon port.
3. skdashboard's manifest, fetched from the skcapstone dashboard's
   ``/.well-known/skworld-module.json`` (``SKDASHBOARD_URL``, default
   ``http://127.0.0.1:7778``).
4. Every ``*.skworld-module.json`` file in the shell registry dir
   ``$SKCAPSTONE_HOME/shell/modules/`` (default ``~/.skcapstone/shell/modules/``).
   This picks up skos (which emits ``skos.skworld-module.json`` via
   ``skos manifest emit``) and any future statically-emitted subapp automatically.

Dedupe is by manifest ``id``: a live-served manifest wins over a static file for
the same id (live sources are merged first, static files only fill in ids not
already present).
"""

from __future__ import annotations

import json
import logging
import os
from pathlib import Path

from .skworld_manifest import skchat_module_manifest

logger = logging.getLogger(__name__)

#: Default upstream for skcode-hostd (matches webui.skcode_proxy).
DEFAULT_SKCODE_HOSTD_URL = "http://100.108.59.57:9394"
#: Default upstream for the skcapstone dashboard.
DEFAULT_SKDASHBOARD_URL = "http://127.0.0.1:7778"
#: Short per-source fetch timeout (seconds), so one dead source can't stall the aggregate.
FETCH_TIMEOUT = 2.5


def _shell_modules_dir() -> Path:
    """The node's shell registry dir: ``$SKCAPSTONE_HOME/shell/modules/``."""
    home = os.environ.get("SKCAPSTONE_HOME") or os.path.expanduser("~/.skcapstone")
    return Path(home) / "shell" / "modules"


def _fetch_json(url: str, timeout: float = FETCH_TIMEOUT) -> dict | None:
    """GET ``url`` and parse JSON, best-effort.

    Returns the parsed dict, or ``None`` on any error (unreachable, timeout,
    non-JSON, non-dict body). Never raises: the caller skips a ``None`` source.
    """
    import urllib.error
    import urllib.request

    try:
        with urllib.request.urlopen(url, timeout=timeout) as r:  # noqa: S310 (fixed internal hosts)
            data = json.loads(r.read())
    except Exception as exc:  # noqa: BLE001 - best-effort discovery, log + skip
        logger.info("shell_modules: skipping %s (%s)", url, exc)
        return None
    if not isinstance(data, dict):
        logger.info("shell_modules: skipping %s (not a JSON object)", url)
        return None
    return data


def _rewrite_prefix(manifest: dict, from_prefix: str, to_prefix: str) -> None:
    """Rewrite URL fields that start with ``from_prefix`` onto ``to_prefix``, in place.

    Used to point a sibling daemon's raw entry/health URLs at the webui's
    same-origin reverse-proxy path (e.g. skcode's ``http://<host>:9394/app`` ->
    ``<origin>/skcode/app``) so the browser reaches it over the 443 funnel. Only
    the ``entry`` (its string values) and ``health`` fields are rewritten; any
    other field is left untouched.
    """
    from_prefix = from_prefix.rstrip("/")

    def _swap(value):
        if isinstance(value, str) and value.startswith(from_prefix):
            return to_prefix.rstrip("/") + value[len(from_prefix):]
        return value

    if "health" in manifest:
        manifest["health"] = _swap(manifest["health"])
    entry = manifest.get("entry")
    if isinstance(entry, dict):
        for key, value in list(entry.items()):
            entry[key] = _swap(value)


def aggregate_shell_modules(base_url: str) -> list[dict]:
    """Aggregate every discoverable SKWorld subapp manifest for a serving origin.

    Args:
        base_url: The origin the webui answers on (the request base URL). skchat's
            own manifest is built against it, and sibling URLs are rewritten onto
            same-origin proxy paths under it where sensible.

    Returns:
        A list of manifest dicts, deduped by ``id`` (live-served wins over static).
    """
    base = base_url.rstrip("/")
    by_id: dict[str, dict] = {}

    # 1. skchat's own manifest (always available, built in-process).
    try:
        own = skchat_module_manifest(base_url)
        if own.get("id"):
            by_id[own["id"]] = own
    except Exception as exc:  # noqa: BLE001 - never let one source fail the whole response
        logger.warning("shell_modules: own manifest failed (%s)", exc)

    # 2. skcode: fetch its live manifest and rewrite URLs onto the /skcode proxy path.
    skcode_upstream = os.environ.get("SKCODE_HOSTD_URL", DEFAULT_SKCODE_HOSTD_URL).rstrip("/")
    skcode = _fetch_json(f"{skcode_upstream}/.well-known/skworld-module.json")
    if skcode and skcode.get("id"):
        _rewrite_prefix(skcode, skcode_upstream, f"{base}/skcode")
        by_id[skcode["id"]] = skcode

    # 3. skdashboard: fetch its live manifest and rewrite URLs onto /skdashboard
    #    (it serves on a loopback port that the browser cannot reach; the webui
    #    skdashboard_proxy bridges it onto the 443 funnel).
    dashboard_upstream = os.environ.get("SKDASHBOARD_URL", DEFAULT_SKDASHBOARD_URL).rstrip("/")
    dashboard = _fetch_json(f"{dashboard_upstream}/.well-known/skworld-module.json")
    if dashboard and dashboard.get("id"):
        _rewrite_prefix(dashboard, dashboard_upstream, f"{base}/skdashboard")
        by_id[dashboard["id"]] = dashboard

    # 4. Static registry files (skos + any future statically-emitted subapp).
    #    Live-served ids already win; static files only fill in ids not yet seen.
    modules_dir = _shell_modules_dir()
    try:
        static_files = sorted(modules_dir.glob("*.skworld-module.json"))
    except Exception as exc:  # noqa: BLE001 - missing/unreadable dir is fine, skip
        logger.info("shell_modules: no static registry dir (%s)", exc)
        static_files = []
    for path in static_files:
        try:
            manifest = json.loads(path.read_text())
        except Exception as exc:  # noqa: BLE001 - skip a bad file, keep the rest
            logger.info("shell_modules: skipping static %s (%s)", path, exc)
            continue
        if not isinstance(manifest, dict):
            logger.info("shell_modules: skipping static %s (not a JSON object)", path)
            continue
        mid = manifest.get("id")
        if mid and mid not in by_id:
            if mid == "skos":
                # skos emits a static manifest pointing at its own loopback web
                # surface; point its Grade B pane at the /skos same-origin proxy
                # (webui skos_proxy) so it loads over the 443 funnel.
                manifest["entry"] = {"url": f"{base}/skos/app"}
                manifest["health"] = f"{base}/skos/health"
            by_id[mid] = manifest

    return list(by_id.values())


__all__ = ["aggregate_shell_modules"]
