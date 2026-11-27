# AGENTS.md (skchat)

AI-native P2P encrypted messaging for the SK stack: a receive daemon, a webui, an MCP server
and a CLI over SKComms transports (PyPI: `skchat-sovereign`). Overview: [README.md](./README.md).
Operations: [SOP.md](./SOP.md). This file follows
[AGENT_INSTRUCTIONS_STANDARD](https://github.com/smilinTux/sk-standards/blob/main/standards/AGENT_INSTRUCTIONS_STANDARD.md).
There is no `CLAUDE.md`: Claude Code reads this file, and a `CLAUDE.md` next to it would hide it.

## Source of truth and invariants

- **Identity is not skchat's.** `identity_bridge.py` and `agent_profile.py` are thin delegates to
  `capauth.resolve_agent_identity`, which yields the wire `capauth_uri` and the sovereign `fqid`.
  It resolves from `SKAGENT`; `SKCHAT_IDENTITY` is only an override. Never add identity logic here.
- **Per-agent stores are isolated** (`SKCHAT_HOME`, default `~/.skchat`). Messages live in the
  SQLite history and outbox; peers load from `~/.skcapstone/peers/`.
- **The web client bundle in `src/skchat/static/app/` is generated and tracked.** It is built from
  the skworld-app repo by `scripts/deploy-app-web.sh` and stamped with `.source_commit`. Never
  hand-copy or hand-edit it. Procedure: [docs/WEB_CLIENT_DEPLOY.md](./docs/WEB_CLIENT_DEPLOY.md).
- **Memory reads declare a reader.** Group, DM and unknown surfaces search skmemory through
  `skchat.memory_reader` (`channel:<id>`, `@public`); only a verified operator in a 1:1 voice call
  reads as owner. A skmemory without `search(reader=...)` returns nothing, never an unfiltered
  search. Keep every new memory path fail-closed.
- **A linked device lives in four stores**; `unlink_device()` clears all four. Never re-implement
  it. Detail: [docs/LINKED_DEVICES.md](./docs/LINKED_DEVICES.md).
- **Crypto claims are scoped.** AES-256-GCM is already quantum-resistant; the classical gap is key
  distribution. Cite surface, FIPS number and hybrid-vs-classical; never "quantum-proof", unscoped
  "E2E quantum-resistant" or "CNSA-2.0". See [docs/crypto-architecture.md](./docs/crypto-architecture.md)
  and [docs/quantum-resistance-architecture.md](./docs/quantum-resistance-architecture.md).

## Commands

CI: `ci.yml` (jobs `lint`, `test`), `pytest.yml`, `qa.yml`, `docs-check.yml`, `secret-scan.yml`.

```bash
# install (CI)
pip install -e ".[dev]"
pip install --force-reinstall --no-deps --no-cache-dir \
  "capauth @ git+https://github.com/smilinTux/capauth@d8942ac86d90" \
  "skcomms @ git+https://github.com/smilinTux/skcomms@2152e30f8ce7"
# lint and format (ci.yml lint, ruff pinned to 0.15.4)
ruff check src/ tests/
ruff format --check src/ tests/
# test (ci.yml, pytest.yml and qa.yml; py3.11 and py3.12)
python -m pytest tests/ -q -m "not live and not integration and not e2e_live and not e2e_3way"
# docs gate (docs-check.yml runs tiers 1,2,3 from sk-standards)
python3 <path-to-sk-standards>/scripts/docs_check.py --repo . --tier 1 --tier 2 --tier 3
# secret scan (secret-scan.yml, gitleaks 8.28.0)
gitleaks detect --source . --baseline-path .gitleaks-baseline.json --redact --exit-code 1
```

Locally, SK packages live in `~/.skenv/` (`~/.skenv/bin/python -m pytest ...`).

## Start here

1. [docs/ARCHITECTURE.md](./docs/ARCHITECTURE.md): message lifecycle, advocacy loop, group keys,
   voice pipeline, source map.
2. `src/skchat/daemon.py`: the polling receive daemon (health on `:9385`).
3. `src/skchat/mcp_server.py`: every MCP tool; arguments in [docs/mcp-reference.md](./docs/mcp-reference.md).
4. `src/skchat/cli.py`: the `skchat` CLI (`send`, `inbox`, `group`, `devices`, `operator-token`, ...).
5. [systemd/README.md](./systemd/README.md): every user unit, secret provisioning, `systemd/install.sh`.

## Rules

- **Writing style:** no em dashes (U+2014) and no en dashes (U+2013) anywhere: code, comments,
  docstrings, docs, commits, PR bodies. Use commas, parentheses, colons or a new sentence. Hyphens
  are fine, including ranges (`5-10`).
- **Branch and commit:** never commit to `main`. Branch, commit as soon as a fast check passes and
  before the full suite, then open a PR. Add a `CHANGELOG.md` entry under `[Unreleased]`.
- **Attribution:** a `Co-Authored-By` trailer names the agent that actually did the work, or is left
  out. Never one you cannot evidence.
- **Generated files:** `src/skchat/static/app/` (and its `.source_commit`) comes from
  `scripts/deploy-app-web.sh` only. `tests/test_deployed_app_bundle.py` guards `<base href>` and
  the stamp.
- **Formatting is `ruff format`, not black.** Ruff config is in `pyproject.toml` (line length 99,
  rules `E,W,F,I`, `E501` ignored). Run `ruff format src/ tests/ && ruff check src/ tests/` before
  pushing. Bump the CI ruff pin deliberately, in the same commit as the reformat it implies.
- **Pytest markers:** a command-line `-m` replaces the `addopts` `-m 'not live'`, so always pass
  the full combined expression above. Markers are declared in `pyproject.toml`.
- **New operator-gated routes:** a route in `_ROUTE_CAPABILITY_RULES` (`dataplane_auth.py`) is
  gated and ignores `X-Operator-Token`; its client must attach `buildOperatorAuthInterceptor`.

## Privacy

This repo is public. Never put host names, LAN or tailnet IPs, estate names, personal names or data,
credentials, bot tokens, operator tokens, or any real message, memory or call content in code, docs,
tests, fixtures, commits or the site. Fixtures are invented. Secrets live in env files outside the
repo (`systemd/README.md` names them, never their values). Enforced by `secret-scan.yml` (gitleaks,
full history against `.gitleaks-baseline.json`; never regenerate the baseline to hide a new
finding). Runtime logs mask FQIDs, IPs, emails and tokens through `skchat.redact`
(`tests/test_redact.py`). Doc text is on review.

## Hazards

- **The daemon is systemd-managed.** Use `systemctl --user restart skchat-daemon.service`, never a
  hand-run `skchat daemon start`: it spawns a second daemon polling the same inbox and overwrites the
  pidfile. Spot duplicates with `pgrep -af skchat._daemon_entry`.
- **skmemory namespace collision.** Running from a directory that contains a `skmemory/` checkout
  makes `from skmemory import MemoryStore` pick up the local dir. Run the daemon and tests from the
  repo root or `~`, never from a parent directory holding sibling checkouts.
- **An editable install makes a checkout production.** Where skchat is installed with `pip install -e`,
  every running service imports that checkout's `src/skchat`, and a restart loads whatever is on disk.
  Do feature work in a `git worktree`, keep the installed checkout on `main`, never restart a service
  while another session has uncommitted work there, and push a branch before removing its worktree.
- **Merged is not running.** A service started before your merge does not have your fix. Check
  `systemctl --user show <unit> -p ActiveEnterTimestamp --value` against the merge.
- **Releases come from `v*` tags only.** `publish.yml` publishes to PyPI on a `v*` tag push or a
  manual dispatch. Use a plain `git push origin <branch>`, never `git push --tags`. See SOP.md
  section 5.
- **Published sibling wheels lag their repos.** CI installs capauth and skcomms from pinned git
  commits; a test that passes against local editable siblings can still fail on PyPI wheels.
- **Web client deploys:** an rsync into `static/app/` is reverted by the next checkout, and a dirty
  or unpulled skworld-app checkout ships silently. Follow [docs/WEB_CLIENT_DEPLOY.md](./docs/WEB_CLIENT_DEPLOY.md)
  and verify the served bundle by content.

## See also

- [capauth](https://github.com/smilinTux/capauth): identity resolver, pairing and PDP rules.
- [skcomms](https://github.com/smilinTux/skcomms): the transport layer (`:9384`).
- [skmemory](https://github.com/smilinTux/skmemory): memory store and reader barriers.
- [skcapstone](https://github.com/smilinTux/skcapstone): agent framework and advocacy backend.
- [sk-standards](https://github.com/smilinTux/sk-standards): `SK_REPO_DOC_STANDARD`, `CRYPTOGRAPHY_STANDARD`, `AGENT_INSTRUCTIONS_STANDARD`.
- [docs/SPACES.md](./docs/SPACES.md), [runbooks/](./runbooks/), [QUICKSTART.md](./QUICKSTART.md).
