"""The committed web bundle has to be a real, deployable build.

``src/skchat/static/app/`` is TRACKED, and the webui serves it directly. That
makes it the one artifact where "it works on my machine" is meaningless: what
ships is whatever got committed.

On 2026-08-08 three consecutive deploys were silently undone. Each rsynced a
fresh build into the working tree without committing it, so the next
``git checkout main`` reverted the tracked files and the operator kept being
served an older bundle with no Linked Devices section. Nothing failed. The only
signal was a human eventually noticing the UI looked wrong.

These tests catch the two ways that bundle can be broken in a way no other test
would notice: a wrong ``<base href>`` (loads a blank page, since every asset
resolves against ``/`` instead of ``/app/``) and a bundle deployed with no record
of which app commit produced it (so nobody can tell whether it is stale).

They deliberately do NOT try to verify the bundle is up to date with
skworld-app's main. That needs the other repo and a Flutter toolchain, neither of
which CI has. ``scripts/deploy-app-web.sh --check`` answers that question where
both checkouts exist.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

_APP = Path(__file__).resolve().parent.parent / "src" / "skchat" / "static" / "app"

pytestmark = pytest.mark.skipif(
    not (_APP / "index.html").exists(),
    reason="no web bundle committed in this checkout",
)


def test_the_bundle_is_served_under_the_app_base_href():
    """A bundle built without --base-href /app/ renders a blank page.

    Every asset request resolves against / instead of /app/, so index.html loads
    and then nothing else does. It is invisible in any Python-level test and
    looks like a broken deploy rather than a wrong build flag.
    """
    html = (_APP / "index.html").read_text(errors="ignore")
    assert '<base href="/app/">' in html, (
        'committed bundle is missing <base href="/app/">; rebuild with: scripts/deploy-app-web.sh'
    )


def test_the_bundle_records_which_app_commit_built_it():
    """Without provenance, "is the deployed client stale?" is unanswerable.

    Diffing a 5 MB compiled main.dart.js tells you nothing. The stamp is what
    makes `scripts/deploy-app-web.sh --check` possible.
    """
    stamp = _APP / ".source_commit"
    assert stamp.exists(), (
        "committed bundle has no .source_commit; deploy with scripts/deploy-app-web.sh "
        "rather than a bare rsync"
    )
    first = stamp.read_text().splitlines()[0].strip()
    assert re.fullmatch(r"[0-9a-f]{40}", first), (
        f".source_commit should start with a full 40-hex skworld-app commit, got {first!r}"
    )


def test_the_bundle_has_its_entrypoint_and_manifest():
    """A truncated or partial rsync is otherwise silent until the page is opened."""
    main_js = _APP / "main.dart.js"
    assert main_js.exists(), "committed bundle has no main.dart.js"
    assert main_js.stat().st_size > 1_000_000, (
        "main.dart.js is implausibly small for a release build; partial deploy?"
    )
    version = _APP / "version.json"
    assert version.exists(), "committed bundle has no version.json"
    meta = json.loads(version.read_text())
    assert meta.get("app_name"), "version.json carries no app_name"
