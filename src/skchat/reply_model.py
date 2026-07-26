"""The one place every surface resolves an agent's reply backend.

Precedence: per-chat pin (Telegram power-user) > per-agent selection (role via
skos.models, or concrete id via SKGateway) > default ornith-tiny (fast .100).
All roles/models ultimately route through SKGateway, so switching is uniform
across harnesses and surfaces."""

from __future__ import annotations

from .agent_model import default_selection

_ROLE_SET = {
    "sk-default",
    "sk-creative",
    "sk-auto",
    "sk-vision",
    "sk-code",
    "sk-heavy",
    "sk-synth",
    "sk-embed",
}


def resolve_reply_backend(
    agent, chat_context=None, *, selection_fn, role_resolve_fn, chat_pin_fn, gateway_url
):
    if chat_context is not None:
        pinned = chat_pin_fn(chat_context)
        if pinned and pinned[0]:
            return pinned
    selection = selection_fn(agent) or default_selection()
    if selection in _ROLE_SET:
        url, model = role_resolve_fn(selection)
        if url and model:
            return url, model
        selection = default_selection()
    return gateway_url, selection
