"""Flatten a FastAPI app's routes the same way on every FastAPI version.

FastAPI 0.136+ no longer copies an included router's routes onto the app: it
keeps one ``_IncludedRouter`` entry per ``include_router`` call, holding the
original router and the include prefix. Those entries have no ``.path``, so a
test that reads ``{r.path for r in app.routes}`` raises on new FastAPI. This
walks them and yields one ``(path, methods, route)`` per served route, with the
include prefixes applied, on old (flat) and new (nested) FastAPI alike. The
production app never walks its own routes, so only tests need this.
"""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any


def walk_routes(routes: Any, prefix: str = "") -> Iterator[tuple[str, set[str], Any]]:
    """Yield ``(path_format, methods, route)`` for every leaf route."""
    for route in routes:
        original = getattr(route, "original_router", None)
        if original is not None:  # FastAPI >= 0.136 nested include
            context = getattr(route, "include_context", None)
            child_prefix = prefix + (getattr(context, "prefix", "") or "")
            yield from walk_routes(original.routes, child_prefix)
            continue
        path = getattr(route, "path_format", None) or getattr(route, "path", None)
        if path is None:
            continue
        yield prefix + path, set(getattr(route, "methods", None) or ()), route


def route_paths(app: Any) -> set[str]:
    """Every served path format on ``app`` (HTTP and websocket)."""
    return {path for path, _methods, _route in walk_routes(app.routes)}
