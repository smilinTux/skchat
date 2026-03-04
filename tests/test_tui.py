"""Tests for skchat.tui — imports and basic app initialisation.

Neither test is marked as integration so both run in CI without a real
terminal or running event loop.
"""

from __future__ import annotations


def test_tui_imports() -> None:
    """All public symbols in tui.py import without error."""
    import skchat.tui  # noqa: F401

    from skchat.tui import (
        GROUP_ID,
        GROUP_NAME,
        KNOWN_PEERS,
        SELF_IDENTITY,
        SKChatTUI,
        _common_prefix,
        _fetch_recent_messages,
        _peer_css_class,
        _short_sender,
        main,
    )

    assert GROUP_ID == "d4f3281e-fa92-474c-a8cd-f0a2a4c31c33"
    assert GROUP_NAME == "skworld-team"
    assert isinstance(KNOWN_PEERS, list)
    assert len(KNOWN_PEERS) >= 2
    assert SELF_IDENTITY.startswith("capauth:")


def test_tui_app_init() -> None:
    """SKChatTUI() instantiates with expected initial state and widgets."""
    from skchat.tui import SKChatTUI

    app = SKChatTUI()

    # Initial instance state
    assert app._seen_ids == set()
    assert app._group_mode is False

    # CSS declares the expected widget IDs
    assert "#messages" in SKChatTUI.CSS
    assert "#input-bar" in SKChatTUI.CSS
    assert "#status" in SKChatTUI.CSS

    # BINDINGS: ctrl+c, ctrl+g, tab all present
    keys = [b.key for b in SKChatTUI.BINDINGS]
    assert "ctrl+c" in keys
    assert "ctrl+g" in keys
    assert "tab" in keys
