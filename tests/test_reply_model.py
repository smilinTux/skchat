from skchat.reply_model import resolve_reply_backend

GW = "http://localhost:18780/v1"


def test_concrete_selection_routes_to_gateway():
    url, model = resolve_reply_backend(
        "lumina",
        selection_fn=lambda a: "claude-opus-4-8",
        role_resolve_fn=lambda r: ("http://skos", "role-model"),
        chat_pin_fn=lambda c: None,
        gateway_url=GW,
    )
    assert url == GW and model == "claude-opus-4-8"


def test_role_selection_resolves_via_skos():
    url, model = resolve_reply_backend(
        "lumina",
        selection_fn=lambda a: "sk-creative",
        role_resolve_fn=lambda r: ("http://skos/v1", "ornith-tiny"),
        chat_pin_fn=lambda c: None,
        gateway_url=GW,
    )
    assert url == "http://skos/v1" and model == "ornith-tiny"


def test_chat_pin_wins():
    url, model = resolve_reply_backend(
        "lumina", chat_context="chat:42",
        selection_fn=lambda a: "claude-opus-4-8",
        role_resolve_fn=lambda r: ("http://skos", "x"),
        chat_pin_fn=lambda c: ("http://pinned", "pinned-model"),
        gateway_url=GW,
    )
    assert url == "http://pinned" and model == "pinned-model"


def test_default_when_selection_empty():
    url, model = resolve_reply_backend(
        "lumina",
        selection_fn=lambda a: None,
        role_resolve_fn=lambda r: (None, None),
        chat_pin_fn=lambda c: None,
        gateway_url=GW,
    )
    assert url == GW and model == "ornith-tiny"
