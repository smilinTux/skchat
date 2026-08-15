"""The operator-audience mint must not persist a file (card e793b6bc).

An operator-audience token is self-contained (verified by signature, never looked
up in the token store), and the per-request mint path is exactly what flooded the
store. ``mint_operator_audience_token`` therefore mints with ``store=False``.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from skchat import operator_audience as oa


def test_mint_operator_audience_passes_store_false(monkeypatch):
    from capauth.tokens import SignedToken, TokenPayload, TokenType

    captured: dict = {}

    def fake_mint(**kwargs):
        captured.update(kwargs)
        now = datetime.now(timezone.utc)
        return SignedToken(
            payload=TokenPayload(
                token_id="t",
                token_type=TokenType.CAPABILITY,
                issuer="A",
                subject=kwargs["subject"],
                capabilities=list(kwargs["scopes"]),
                expires_at=now + timedelta(hours=12),
                audience=kwargs["audience"],
            )
        )

    monkeypatch.setattr("capauth.mint_audience_token", fake_mint)

    oa.mint_operator_audience_token("abc123deadbeef")

    assert captured.get("store") is False
    assert captured.get("audience") == oa.SKCHAT_AUDIENCE
