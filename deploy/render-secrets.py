#!/usr/bin/env python3
"""render-secrets.py: resolve ${skvault:...} tokens in a template into a target
file with 0600 permissions, without ever printing a secret value.

Engine behind deploy/provision-secrets.sh. Do not call directly for a full run;
use the shell script (it owns the template -> target mapping). Exposed as a
separate file so the substitution logic is testable in isolation.

Token grammar (inside any template, env-file or YAML):
    ${skvault:<KeePass entry title>}       -> the entry's password, verbatim
    ${skvault-dsn:<KeePass entry title>}   -> a full Postgres DSN built from the
                                              entry's password, URL-encoded, using
                                              SKCHAT_BRIDGE_DSN_TEMPLATE (a format
                                              string with a single {pw} field;
                                              default points at the scoped
                                              skchat_bridge role on localhost).

Guarantees:
  * Secret values never touch argv, stdout, stderr, or any log line. Only the
    target path and a redacted token count are printed.
  * Fails closed: an unresolved token, a locked vault, a missing/ambiguous entry,
    or any error aborts with a nonzero exit and writes nothing.
  * Atomic + private: renders to a temp file in the target directory, chmods it
    0600 before it holds any secret content is moot (we chmod the temp file to
    0600 immediately after creation, then write, then os.replace).

Usage:
    render-secrets.py --template T --target P [--mode 0600]
                      [--strip-final-newline] [--check] [--dry-run]

Modes:
  (default)             resolve + write the target
  --check               resolve every token (proving the vault has them) but do
                        NOT write anything; prints "OK <path> (<n> secrets)"
  --dry-run            do NOT touch the vault; just list the tokens the template
                        references and the target path
"""
from __future__ import annotations

import argparse
import os
import re
import sys
import tempfile
from urllib.parse import quote

TOKEN_RE = re.compile(r"\$\{skvault(?P<dsn>-dsn)?:(?P<title>[^}]+)\}")

DEFAULT_DSN_TEMPLATE = os.environ.get(
    "SKCHAT_BRIDGE_DSN_TEMPLATE",
    "postgresql://skchat_bridge:{pw}@localhost:5432/skmemory",
)


def _fail(msg: str) -> "None":
    sys.stderr.write(f"render-secrets: {msg}\n")
    sys.exit(2)


def _skvault_password(title: str) -> str:
    """Return the password for the KeePass entry EXACTLY titled *title*.

    Fails closed on locked vault, no match, or ambiguous match. Never logs the
    value.
    """
    try:
        from skvault import vault_creds as vc
    except Exception as e:  # noqa: BLE001
        _fail(f"skvault not importable ({e}); is the ~/.skenv venv active?")
    matches, err = vc.get(title)
    if err:
        _fail(f"skvault: {err} (run `skvault unlock` first)")
    exact = [m for m in (matches or []) if (m.get("title") or "") == title]
    if not exact:
        _fail(f"no skvault entry titled exactly {title!r}")
    if len(exact) > 1:
        _fail(f"ambiguous: {len(exact)} skvault entries titled {title!r}")
    pw = exact[0].get("password")
    if not pw:
        _fail(f"skvault entry {title!r} has an empty password")
    return pw


def _resolve_token(m: "re.Match[str]", *, fetch: bool) -> str:
    title = m.group("title").strip()
    is_dsn = bool(m.group("dsn"))
    if not fetch:
        # dry-run: return an obvious, value-free placeholder.
        return f"<{'dsn:' if is_dsn else ''}{title}>"
    pw = _skvault_password(title)
    if is_dsn:
        return DEFAULT_DSN_TEMPLATE.format(pw=quote(pw, safe=""))
    return pw


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--template", required=True)
    ap.add_argument("--target", required=True)
    ap.add_argument("--mode", default="0600")
    ap.add_argument("--strip-final-newline", action="store_true")
    ap.add_argument("--check", action="store_true")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    try:
        with open(args.template, encoding="utf-8") as fh:
            content = fh.read()
    except OSError as e:
        _fail(f"cannot read template {args.template}: {e}")

    tokens = list(TOKEN_RE.finditer(content))

    if args.dry_run:
        titles = [
            f"{'skvault-dsn' if m.group('dsn') else 'skvault'}:{m.group('title').strip()}"
            for m in tokens
        ]
        print(f"DRY-RUN {args.target} <- {args.template}  ({len(tokens)} tokens)")
        for t in titles:
            print(f"    token: {t}")
        return 0

    fetch = True
    rendered = TOKEN_RE.sub(lambda m: _resolve_token(m, fetch=fetch), content)

    # Nothing unresolved should remain.
    leftover = TOKEN_RE.search(rendered)
    if leftover:
        _fail(f"unresolved token after render: {leftover.group(0)}")

    if args.strip_final_newline:
        rendered = rendered.rstrip("\n")

    if args.check:
        print(f"OK {args.target}  ({len(tokens)} secrets resolve)")
        return 0

    mode = int(args.mode, 8)
    target_dir = os.path.dirname(os.path.abspath(args.target))
    os.makedirs(target_dir, exist_ok=True)

    # Atomic + private write: temp file in the same dir, chmod 0600 up front,
    # write, fsync, os.replace.
    fd, tmp = tempfile.mkstemp(dir=target_dir, prefix=".render-", suffix=".tmp")
    try:
        os.fchmod(fd, mode)
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(rendered)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, args.target)
        os.chmod(args.target, mode)
    except Exception as e:  # noqa: BLE001
        try:
            os.unlink(tmp)
        except OSError:
            pass
        _fail(f"write failed for {args.target}: {e}")

    print(f"wrote {args.target}  (mode {args.mode}, {len(tokens)} secrets)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
