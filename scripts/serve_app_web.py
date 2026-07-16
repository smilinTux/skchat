#!/usr/bin/env python3
"""serve_app_web.py - hardened static file server for the built Flutter web
client (coord b5078963, Task 7 of skchat-resilience-v1).

Replaces the stopgap `python3 -m http.server` that served
skchat-app/build/web on :8088 with a still-stdlib-only server that:

  - Sets correct Content-Type for .wasm/.js/.json/.css (Flutter web needs
    application/wasm and application/javascript, not the interpreter's
    guessed defaults).
  - Sends sensible Cache-Control: no-cache for index.html (so a fresh
    deploy is always picked up), a long immutable cache for filenames that
    contain a content hash, a short cache for everything else.
  - Disables directory listing (autoindex) entirely; a directory request
    with no index.html in it returns 403, never a file list.
  - Uses ThreadingHTTPServer so one slow client cannot stall the rest.

No third-party dependencies: stdlib http.server + socketserver only, so it
drops into the same venv the rest of skchat uses with nothing extra to
install.
"""

from __future__ import annotations

import argparse
import functools
import http.server
import os
import re
import sys

# Extensions the interpreter's built-in mimetypes DB gets wrong or leaves
# unmapped often enough to matter for a Flutter web build. Checked before
# falling back to the parent class's guess.
CONTENT_TYPES = {
    ".wasm": "application/wasm",
    ".js": "application/javascript",
    ".mjs": "application/javascript",
    ".json": "application/json",
    ".css": "text/css",
    ".html": "text/html",
    ".htm": "text/html",
    ".map": "application/json",
    ".ico": "image/x-icon",
}

# A filename is treated as content-hashed (safe to cache forever) if it
# contains a run of 8+ hex characters, e.g. main.a1b2c3d4.js or
# app.9f8e7d6c5b4a.wasm. index.html is excluded from this regardless (see
# _cache_control_for), since Flutter build output does not hash it and it
# is exactly the file a fresh deploy needs picked up immediately.
_HASH_RE = re.compile(r"[0-9a-f]{8,}", re.IGNORECASE)

_NO_CACHE = "no-cache"
_LONG_CACHE = "public, max-age=31536000, immutable"
_SHORT_CACHE = "public, max-age=3600"


class HardenedStaticHandler(http.server.SimpleHTTPRequestHandler):
    """SimpleHTTPRequestHandler with correct MIME types, cache headers, and
    directory listing disabled."""

    # Silence the default banner; overridden per-instance by functools.partial
    # binding `directory=` in main().
    server_version = "skchat-app-web/1.0"

    def guess_type(self, path: str) -> str:
        ext = os.path.splitext(path)[1].lower()
        ctype = CONTENT_TYPES.get(ext)
        if ctype:
            return ctype
        return super().guess_type(path)

    def list_directory(self, path):  # noqa: ANN001 - matches base signature
        """Never render an autoindex; a directory with no index.html is a
        403, not a file listing."""
        self.send_error(403, "Directory listing is disabled")
        return None

    @staticmethod
    def _cache_control_for(request_path: str) -> str:
        clean = request_path.split("?", 1)[0].split("#", 1)[0]
        name = clean.rsplit("/", 1)[-1]
        if name in ("", "index.html", "index.htm"):
            return _NO_CACHE
        if _HASH_RE.search(name):
            return _LONG_CACHE
        return _SHORT_CACHE

    def end_headers(self) -> None:
        try:
            self.send_header("Cache-Control", self._cache_control_for(self.path))
            self.send_header("X-Content-Type-Options", "nosniff")
        except Exception:
            # Never let a header-computation bug turn into a hung response;
            # fall through to the base behavior with no extra headers.
            pass
        super().end_headers()

    def log_message(self, fmt: str, *args) -> None:  # noqa: ANN002
        # Route through stderr like the stdlib default; journald captures it
        # via StandardError=journal on the systemd unit.
        sys.stderr.write("%s - - [%s] %s\n" % (self.address_string(), self.log_date_time_string(), fmt % args))


def build_server(root: str, bind: str, port: int) -> http.server.ThreadingHTTPServer:
    if not os.path.isdir(root):
        raise SystemExit(f"serve_app_web: web root does not exist: {root}")
    handler = functools.partial(HardenedStaticHandler, directory=root)
    server = http.server.ThreadingHTTPServer((bind, port), handler)
    server.daemon_threads = True
    return server


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--root",
        required=True,
        help="Directory to serve (skchat-app/build/web)",
    )
    parser.add_argument("--port", type=int, default=8088)
    parser.add_argument(
        "--bind",
        default="127.0.0.1",
        help="Bind address (loopback by default; ingress fronts this port, see Task 4)",
    )
    args = parser.parse_args(argv)

    server = build_server(os.path.abspath(args.root), args.bind, args.port)
    print(f"serve_app_web: serving {args.root} on {args.bind}:{args.port}", file=sys.stderr)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
