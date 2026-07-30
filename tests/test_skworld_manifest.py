"""skchat's SKWorld module manifest: grade-A shape, operator facet, served unauth."""

from __future__ import annotations

from skchat.skworld_manifest import AUDIENCE, SCHEMA_VERSION, skchat_module_manifest


def test_manifest_ui_facet_is_grade_a_flutter_package():
    m = skchat_module_manifest("http://localhost:8765/")
    assert m["schemaVersion"] == SCHEMA_VERSION
    assert m["id"] == "skchat"
    assert m["grade"] == "A"
    assert m["entry"]["flutter_package"] == {
        "path": "packages/skchat_ui",
        "package": "skchat_ui",
    }
    assert m["nav"] == {"icon": "chat", "order": 20, "label": "Chats"}
    assert m["deeplinkPrefix"] == "skworld://skchat/"
    assert m["memory"] == {"opt_in": True, "scope": "skchat"}


def test_auth_facet_declares_audience_and_scopes():
    m = skchat_module_manifest("http://host/")
    assert m["auth"]["audience"] == AUDIENCE == "skchat"
    assert m["auth"]["scopes"] == ["chat.read", "chat.send", "calls.join", "spaces.join"]


def test_health_is_origin_relative():
    assert skchat_module_manifest("http://host:8765/")["health"] == "http://host:8765/health"
    # No trailing-slash base yields the same (no double/missing slash).
    assert skchat_module_manifest("http://host:8765")["health"] == "http://host:8765/health"


def test_operator_facet_matches_the_skchat_adapter_contract():
    op = skchat_module_manifest("http://host/")["operator"]
    assert op["contractVersion"] == 1
    assert op["cli"] == "skchat operator"
    assert op["repos"] == ["skchat"]
    # Mirrors operator_seat/skchat_adapter.py CONDITIONS and its standard actions.
    assert op["conditions"] == ["DaemonReady", "BridgeAlive", "OutboxBounded", "AuthEnforced"]
    assert op["proposedStandardActions"] == ["restart-daemon", "restart-telegram-bridge"]


def test_manifest_served_unauthenticated_from_webui():
    # The /.well-known route is public (no dataplane gate): a fresh TestClient with
    # no credential gets the grade-A manifest.
    from fastapi.testclient import TestClient

    from skchat import webui

    client = TestClient(webui.app)
    r = client.get("/.well-known/skworld-module.json")
    assert r.status_code == 200
    body = r.json()
    assert body["id"] == "skchat"
    assert body["grade"] == "A"
    assert body["operator"]["proposedStandardActions"] == [
        "restart-daemon",
        "restart-telegram-bridge",
    ]
    assert body["health"].endswith("/health")
