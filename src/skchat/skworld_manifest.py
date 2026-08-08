"""skchat's SKWorld module manifest (spec 3.1), served from the webui.

skchat is a first-class SKWorld subapp: it declares ONE capauth-signed
skworld.module.json with two facets (the UI module + the operator adapter). The
webui serves it unauthenticated at /.well-known/skworld-module.json (public
discovery metadata, no secrets: the shell reads it to learn skchat's entry, nav,
and required auth audience/scopes before it has a token).

Grade A: the UI facet points at the packages/skchat_ui Flutter module (which now
renders the real chats surface). Per spec 2.3 a grade promotion is a manifest edit
plus a package, never a contract change. URLs are origin-relative to the serving
request, so health resolves against wherever the webui actually answers.

The operator block mirrors operator_seat/skchat_adapter.py in skcapstone; the
shared sk-standards manifest schema is the source of truth. (The adapter emits
five conditions: the fifth, CallingReady, landed once the daemon's WebRTC
signaling-health probe made a real calling-health signal available, per spec
2.3. The order here MUST match the adapter's CONDITIONS exactly, the
manifest-adapter drift-guard test asserts it.)
"""

from __future__ import annotations

#: sk-standards manifest schema version (v1.1, with the operator block).
SCHEMA_VERSION = "1.1"
#: The audience skchat tokens are minted for.
AUDIENCE = "skchat"


def skchat_module_manifest(base_url: str) -> dict:
    """Build skchat's skworld.module.json for a given serving origin.

    Args:
        base_url: The origin the webui answers on (e.g. the request base URL).
            The health URL is built relative to it so it never hardcodes a host.

    Returns:
        The manifest dict (UI facet + operator facet).
    """
    base = base_url.rstrip("/")
    return {
        "schemaVersion": SCHEMA_VERSION,
        "id": "skchat",
        "name": "Chats",
        # Grade A: a native Flutter module the shell mounts directly.
        "grade": "A",
        "entry": {"flutter_package": {"path": "packages/skchat_ui", "package": "skchat_ui"}},
        "nav": {"icon": "chat", "order": 20, "label": "Chats"},
        "deeplinkPrefix": "skworld://skchat/",
        "auth": {
            "audience": AUDIENCE,
            "scopes": ["chat.read", "chat.send", "calls.join", "spaces.join"],
        },
        "memory": {"opt_in": True, "scope": "skchat"},
        "health": f"{base}/health",
        # Operator facet: what Atlas's skchat adapter observes and may act on.
        "operator": {
            "contractVersion": 1,
            "cli": "skchat operator",
            "repos": ["skchat"],
            "conditions": [
                "DaemonReady",
                "BridgeAlive",
                "OutboxBounded",
                "AuthEnforced",
                "CallingReady",
            ],
            "proposedStandardActions": ["restart-daemon", "restart-telegram-bridge"],
        },
    }


__all__ = ["skchat_module_manifest", "SCHEMA_VERSION", "AUDIENCE"]
