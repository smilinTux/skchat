"""Shell subapp-manifest discovery aggregate: shape, dedupe, best-effort, unauth.

The aggregate fetches are monkeypatched so the test never touches the network or
a real registry dir. Run from ~ per skchat/CLAUDE.md (skmemory namespace collision).
"""

from __future__ import annotations

import json

from skchat import shell_modules


def _fake_fetch(mapping):
    """Return a _fetch_json stand-in that serves ``mapping`` by URL, else None."""

    def _fetch(url, timeout=shell_modules.FETCH_TIMEOUT):
        return mapping.get(url)

    return _fetch


def test_aggregate_shape_and_own_manifest(monkeypatch, tmp_path):
    # No sibling daemons, no static files: still returns skchat's own manifest.
    monkeypatch.setattr(shell_modules, "_fetch_json", _fake_fetch({}))
    monkeypatch.setattr(shell_modules, "_shell_modules_dir", lambda: tmp_path)

    mods = shell_modules.aggregate_shell_modules("http://host:8765/")
    assert isinstance(mods, list)
    ids = {m["id"] for m in mods}
    assert ids == {"skchat"}
    own = next(m for m in mods if m["id"] == "skchat")
    assert own["grade"] == "A"
    assert own["health"] == "http://host:8765/health"


def test_aggregate_all_sources_and_skcode_url_rewrite(monkeypatch, tmp_path):
    skcode = {
        "id": "skcode",
        "name": "Code",
        "grade": "B",
        "entry": {"url": "http://100.108.59.57:9394/app"},
        "health": "http://100.108.59.57:9394/api/v1/hosts/self",
    }
    dashboard = {"id": "skdashboard", "name": "Dashboard", "grade": "C"}
    monkeypatch.setattr(
        shell_modules,
        "_fetch_json",
        _fake_fetch(
            {
                "http://100.108.59.57:9394/.well-known/skworld-module.json": skcode,
                "http://127.0.0.1:7778/.well-known/skworld-module.json": dashboard,
            }
        ),
    )
    # A static skos manifest in the registry dir.
    (tmp_path / "skos.skworld-module.json").write_text(
        json.dumps({"id": "skos", "name": "OS", "grade": "B"})
    )
    monkeypatch.setattr(shell_modules, "_shell_modules_dir", lambda: tmp_path)

    mods = shell_modules.aggregate_shell_modules("https://chat.skworld.io/")
    by_id = {m["id"]: m for m in mods}
    assert set(by_id) == {"skchat", "skcode", "skdashboard", "skos"}

    # skcode entry + health are rewritten onto the same-origin /skcode proxy path.
    assert by_id["skcode"]["entry"]["url"] == "https://chat.skworld.io/skcode/app"
    assert by_id["skcode"]["health"] == "https://chat.skworld.io/skcode/api/v1/hosts/self"
    # skdashboard is served same-origin already: left untouched.
    assert by_id["skdashboard"]["grade"] == "C"


def test_static_file_picked_up_automatically(monkeypatch, tmp_path):
    # A future statically-emitted subapp is discovered with no code change.
    monkeypatch.setattr(shell_modules, "_fetch_json", _fake_fetch({}))
    (tmp_path / "skfuture.skworld-module.json").write_text(
        json.dumps({"id": "skfuture", "name": "Future", "grade": "C"})
    )
    monkeypatch.setattr(shell_modules, "_shell_modules_dir", lambda: tmp_path)

    ids = {m["id"] for m in shell_modules.aggregate_shell_modules("http://host/")}
    assert ids == {"skchat", "skfuture"}


def test_dedupe_live_wins_over_static(monkeypatch, tmp_path):
    live_skcode = {
        "id": "skcode",
        "grade": "B",
        "source": "live",
        "entry": {"url": "http://100.108.59.57:9394/app"},
    }
    monkeypatch.setattr(
        shell_modules,
        "_fetch_json",
        _fake_fetch(
            {"http://100.108.59.57:9394/.well-known/skworld-module.json": live_skcode}
        ),
    )
    # A stale static file for the SAME id must lose to the live-served one.
    (tmp_path / "skcode.skworld-module.json").write_text(
        json.dumps({"id": "skcode", "grade": "A", "source": "static"})
    )
    monkeypatch.setattr(shell_modules, "_shell_modules_dir", lambda: tmp_path)

    by_id = {m["id"]: m for m in shell_modules.aggregate_shell_modules("http://host/")}
    assert by_id["skcode"]["source"] == "live"
    assert by_id["skcode"]["grade"] == "B"


def test_best_effort_skips_unreachable_sources(monkeypatch, tmp_path):
    # _fetch_json returns None for every URL (simulates all siblings down).
    monkeypatch.setattr(shell_modules, "_fetch_json", lambda url, timeout=2.5: None)
    monkeypatch.setattr(shell_modules, "_shell_modules_dir", lambda: tmp_path)

    ids = {m["id"] for m in shell_modules.aggregate_shell_modules("http://host/")}
    # Own manifest survives even when every remote source fails.
    assert ids == {"skchat"}


def test_bad_static_file_is_skipped_not_fatal(monkeypatch, tmp_path):
    monkeypatch.setattr(shell_modules, "_fetch_json", _fake_fetch({}))
    (tmp_path / "broken.skworld-module.json").write_text("{ not valid json")
    (tmp_path / "ok.skworld-module.json").write_text(json.dumps({"id": "skok"}))
    monkeypatch.setattr(shell_modules, "_shell_modules_dir", lambda: tmp_path)

    ids = {m["id"] for m in shell_modules.aggregate_shell_modules("http://host/")}
    assert ids == {"skchat", "skok"}  # broken file skipped, rest survive


def test_env_overrides_upstreams(monkeypatch, tmp_path):
    monkeypatch.setenv("SKCODE_HOSTD_URL", "http://code.local:9999")
    monkeypatch.setenv("SKDASHBOARD_URL", "http://dash.local:7000")
    seen = {}

    def _fetch(url, timeout=2.5):
        seen[url] = True
        return None

    monkeypatch.setattr(shell_modules, "_fetch_json", _fetch)
    monkeypatch.setattr(shell_modules, "_shell_modules_dir", lambda: tmp_path)

    shell_modules.aggregate_shell_modules("http://host/")
    assert "http://code.local:9999/.well-known/skworld-module.json" in seen
    assert "http://dash.local:7000/.well-known/skworld-module.json" in seen


def test_route_is_public_and_returns_modules(monkeypatch, tmp_path):
    # The /api/v1/shell/modules route is public (no dataplane gate): a fresh
    # TestClient with no credential gets the aggregate wrapped as {"modules": [...]}.
    from fastapi.testclient import TestClient

    from skchat import webui

    monkeypatch.setattr(shell_modules, "_fetch_json", _fake_fetch({}))
    monkeypatch.setattr(shell_modules, "_shell_modules_dir", lambda: tmp_path)

    client = TestClient(webui.app)
    r = client.get("/api/v1/shell/modules")
    assert r.status_code == 200
    body = r.json()
    assert "modules" in body and isinstance(body["modules"], list)
    assert any(m["id"] == "skchat" for m in body["modules"])


def test_route_exempt_from_dataplane_gate():
    # Belt-and-suspenders: even with the operator-auth flag ON, the path is exempt.
    from skchat.dataplane_paths import is_gated

    assert is_gated("GET", "/api/v1/shell/modules") is False


# ---------------------------------------------------------------------------
# A5: operator-facet strip (always on, independent of signature enforcement)
# ---------------------------------------------------------------------------


def test_operator_block_stripped_from_public_aggregate(monkeypatch, tmp_path):
    # skchat's own manifest carries an operator facet; a sibling manifest too.
    # Neither may leak on the PUBLIC aggregate.
    skcode = {
        "id": "skcode",
        "name": "Code",
        "grade": "B",
        "entry": {"url": "http://100.108.59.57:9394/app"},
        "operator": {"cli": "skcode-hostd operator", "port": 9394},
    }
    monkeypatch.setattr(
        shell_modules,
        "_fetch_json",
        _fake_fetch(
            {"http://100.108.59.57:9394/.well-known/skworld-module.json": skcode}
        ),
    )
    monkeypatch.setattr(shell_modules, "_shell_modules_dir", lambda: tmp_path)

    mods = shell_modules.aggregate_shell_modules("http://host:8765/")
    assert mods, "expected at least skchat + skcode"
    for m in mods:
        assert "operator" not in m, f"operator facet leaked on {m['id']!r}"
    # The UI facet survives the strip.
    by_id = {m["id"]: m for m in mods}
    assert by_id["skcode"]["entry"]["url"] == "http://host:8765/skcode/app"


# ---------------------------------------------------------------------------
# A2/A9: signature enforcement gated by SKCHAT_SHELL_REQUIRE_SIGNED
# ---------------------------------------------------------------------------


def _fake_registry(entries):
    """Return a list_registered stand-in that ignores kwargs and yields ``entries``."""

    def _list_registered(*args, **kwargs):
        return list(entries)

    return _list_registered


def test_enforce_off_is_default_and_unchanged(monkeypatch, tmp_path):
    # Flag unset: behavior is exactly the pre-enforcement aggregate (minus the
    # always-on operator strip), and NO verified marker is added.
    monkeypatch.delenv(shell_modules.REQUIRE_SIGNED_ENV, raising=False)
    monkeypatch.setattr(shell_modules, "_fetch_json", _fake_fetch({}))
    (tmp_path / "skos.skworld-module.json").write_text(
        json.dumps({"id": "skos", "name": "OS", "grade": "B"})
    )
    monkeypatch.setattr(shell_modules, "_shell_modules_dir", lambda: tmp_path)

    mods = shell_modules.aggregate_shell_modules("http://host/")
    ids = {m["id"] for m in mods}
    assert ids == {"skchat", "skos"}
    for m in mods:
        assert "verified" not in m


def test_enforce_on_keeps_only_signed_and_tags_verified(monkeypatch, tmp_path):
    # skchat (own) + skos (static) present in the aggregate. Registry marks skchat
    # signed+enabled ("ok"); skos is NOT registered. Only skchat survives, tagged.
    monkeypatch.setenv(shell_modules.REQUIRE_SIGNED_ENV, "1")
    monkeypatch.setattr(shell_modules, "_fetch_json", _fake_fetch({}))
    (tmp_path / "skos.skworld-module.json").write_text(
        json.dumps({"id": "skos", "name": "OS", "grade": "B"})
    )
    monkeypatch.setattr(shell_modules, "_shell_modules_dir", lambda: tmp_path)

    import capauth.manifest as capmanifest

    monkeypatch.setattr(
        capmanifest,
        "list_registered",
        _fake_registry(
            [
                {"id": "skchat", "signature": "ok", "enabled": True},
                # skos absent from the registry entirely -> unsigned -> dropped.
            ]
        ),
    )

    mods = shell_modules.aggregate_shell_modules("http://host/")
    by_id = {m["id"]: m for m in mods}
    assert set(by_id) == {"skchat"}
    assert by_id["skchat"]["verified"] is True


def test_enforce_on_drops_failed_and_disabled(monkeypatch, tmp_path):
    # A bad-signature ("failed") entry and a signed-but-disabled entry are BOTH
    # dropped even though they are registered.
    monkeypatch.setenv(shell_modules.REQUIRE_SIGNED_ENV, "1")
    dashboard = {"id": "skdashboard", "name": "Dashboard", "grade": "B"}
    monkeypatch.setattr(
        shell_modules,
        "_fetch_json",
        _fake_fetch(
            {"http://127.0.0.1:7778/.well-known/skworld-module.json": dashboard}
        ),
    )
    (tmp_path / "skos.skworld-module.json").write_text(
        json.dumps({"id": "skos", "name": "OS", "grade": "B"})
    )
    monkeypatch.setattr(shell_modules, "_shell_modules_dir", lambda: tmp_path)

    import capauth.manifest as capmanifest

    monkeypatch.setattr(
        capmanifest,
        "list_registered",
        _fake_registry(
            [
                {"id": "skchat", "signature": "ok", "enabled": True},
                {"id": "skdashboard", "signature": "failed", "enabled": True},
                {"id": "skos", "signature": "ok", "enabled": False},
            ]
        ),
    )

    ids = {m["id"] for m in shell_modules.aggregate_shell_modules("http://host/")}
    assert ids == {"skchat"}


def test_enforce_on_fails_closed_when_capauth_unavailable(monkeypatch, tmp_path):
    # Enforcement ON but the registry read raises: emit NOTHING (fail closed),
    # never fall back to trusting the unsigned aggregate.
    monkeypatch.setenv(shell_modules.REQUIRE_SIGNED_ENV, "1")
    monkeypatch.setattr(shell_modules, "_fetch_json", _fake_fetch({}))
    monkeypatch.setattr(shell_modules, "_shell_modules_dir", lambda: tmp_path)

    def _boom():
        return None

    monkeypatch.setattr(shell_modules, "_verified_module_ids", _boom)

    mods = shell_modules.aggregate_shell_modules("http://host/")
    assert mods == []
