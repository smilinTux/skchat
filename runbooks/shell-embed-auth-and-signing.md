# Runbook: securely re-enable the shell Board/OS panes (embed auth + manifest signing)

No em dashes or en dashes anywhere in this document.

## What this covers

The shell's "Board" (`/skdashboard`) and "OS" (`/skos`) panes were turned off
after a public-leak fix: those reverse proxies now require operator/dataplane
auth (`enforce_dataplane_auth`), but the panes are IFRAMES that cannot set an
`Authorization` header, so they 401. This runbook re-enables them securely, in
order, with no public leak.

Two independent mechanisms have to be turned on:

1. EMBED AUTH (shipped in this PR) lets the authenticated app hand each gated
   iframe a short-lived, module-scoped, read-only token so the pane loads for
   the authenticated user only. Turned on with one flag: `SKCHAT_EMBED_TOKENS`.
2. MANIFEST SIGNING (Fable review A2/A9) makes the shell mount only
   operator-signed, registry-approved modules. Turned on with
   `SKCHAT_SHELL_REQUIRE_SIGNED` + `SKCHAT_SHELL_SIGNER_FPR`, AFTER the operator
   signs and registers the 4 manifests on the box that holds the secret key.

Do steps in the order below. Nothing here is flipped by the PR; every flag is a
deliberate operator action.

---

## Part 0: how embed auth works (context, no action)

- The app calls `POST /api/v1/embed-token` (authenticated, guarded by
  `SKCHAT_EMBED_TOKENS`, default OFF -> 404) with `{"module": "skdashboard"}`
  or `{"module": "skos"}` and gets back `{token, module, expires_at}`.
- The pane appends it to the iframe `src` as `?embed_token=...`. The
  `/skdashboard` and `/skos` proxies accept EITHER a valid `Authorization`
  credential OR a valid embed token scoped to that exact module. On the first
  navigation the proxy also sets a path-scoped, HttpOnly cookie
  (`skc_embed_<module>`) carrying the same token, so the pane's later subresource
  loads (which cannot re-attach the query param) stay authorized.
- Scope + lifetime: tier `embed-token`, `module` claim, `mode: ro`, TTL 120s
  (max 600s). A token minted for one module never authorizes another; a non-GET
  request that presents ONLY an embed token is refused (403, read-only). An
  unauth request with no/invalid token still 401s, so the leak stays closed.
- Signing key: reuses the operator-session HS256 machinery (no new crypto). By
  default the embed key is DERIVED from `SKCHAT_OPERATOR_TOKEN_SECRET`
  (domain-separated), so NO new secret has to be provisioned. To use a distinct
  explicit secret instead, set `SKCHAT_EMBED_TOKEN_SECRET`.

Precondition: `SKCHAT_OPERATOR_TOKEN_SECRET` must already be set on the webui
unit (it is, for operator-session auth). Confirm:

```bash
systemctl --user show skchat-webui@lumina.service -p Environment | tr ' ' '\n' | grep -c SKCHAT_OPERATOR_TOKEN_SECRET
# -> 1  (if the secret is provisioned via EnvironmentFile, check that file instead)
```

---

## Part 1: turn on embed-token minting

On the node serving the funnel (the lumina webui, `skchat-webui@lumina`):

```bash
# Add the flag to the webui env (mirror however the other SKCHAT_* flags are set;
# on .158 they live in ~/.config/skchat/webui-lumina.env).
#   SKCHAT_EMBED_TOKENS=1
systemctl --user restart skchat-webui@lumina.service
```

Verify (off-tailnet or with the operator credential stripped, the leak stays
closed; with the credential, minting works):

```bash
FUNNEL=https://noroc2027.tail204f0c.ts.net

# No credential: proxy still 401s (leak closed).
curl -s -o /dev/null -w '%{http_code}\n' "$FUNNEL/skdashboard/api/board"      # -> 401

# Mint requires auth: no credential -> 401.
curl -s -o /dev/null -w '%{http_code}\n' -X POST "$FUNNEL/api/v1/embed-token" \
     -H 'Content-Type: application/json' -d '{"module":"skdashboard"}'         # -> 401

# With the operator credential (the app's Bearer), mint returns a token, and
# that token loads the proxy read-only:
#   TOKEN=$(curl -s -X POST "$FUNNEL/api/v1/embed-token" \
#            -H "Authorization: Bearer <operator-session>" \
#            -H 'Content-Type: application/json' -d '{"module":"skdashboard"}' | jq -r .token)
#   curl -s -o /dev/null -w '%{http_code}\n' "$FUNNEL/skdashboard/app?embed_token=$TOKEN"  # -> 200
```

The Flutter app needs no rebuild for embed auth alone: the token fetch is always
compiled in and degrades to tokenless when the flag is off. It does need
`USE_SHELL_DYNAMIC_MODULES=true` for the discovered `skdashboard`/`skos` panes to
appear at all (see Part 3).

---

## Part 2: sign + register the 4 manifests (on the KEY-HOLDING box)

The operator secret key (fpr `D8920EA8...`) is NOT on the funnel box. Run this on
the node that holds the operator gpg secret key. This box only has agent keys
(`gpg --list-secret-keys` shows `opus@skworld.io`, `jarvis@skworld.io`, ...), so
do NOT run the signing here.

### 2a. Get each manifest as a canonical file

The shell registry `~/.skcapstone/shell/modules/modules.json` records, per module,
a manifest file path + a detached signature. Each of the 4 subapps needs its
manifest saved as a canonical (sorted-key) JSON file:

```bash
mkdir -p ~/.skcapstone/shell/modules
cd ~/.skcapstone/shell/modules

# skos already ships a static file here: skos.skworld-module.json
# (re-emit it canonically if needed):
skos manifest emit --output skos.skworld-module.json

# skchat / skcode / skdashboard serve their manifest live; save each to a file.
# Use the SAME canonical bytes the aggregator will hash. Example (adjust hosts):
curl -s http://127.0.0.1:8765/.well-known/skworld-module.json \
  | python3 -c 'import sys,json;print(json.dumps(json.load(sys.stdin),sort_keys=True,separators=(",",":")))' \
  > skchat.skworld-module.json
curl -s http://100.108.59.57:9394/.well-known/skworld-module.json \
  | python3 -c 'import sys,json;print(json.dumps(json.load(sys.stdin),sort_keys=True,separators=(",",":")))' \
  > skcode.skworld-module.json
curl -s http://127.0.0.1:7778/.well-known/skworld-module.json \
  | python3 -c 'import sys,json;print(json.dumps(json.load(sys.stdin),sort_keys=True,separators=(",",":")))' \
  > skdashboard.skworld-module.json
```

`capauth manifest sign` refuses non-canonical bytes by default, which is why the
files are normalized above.

### 2b. Sign each with the operator key

Replace `<OPERATOR_FPR>` with the operator fingerprint (`D8920EA8...`); pass
`--passphrase` if the key is protected.

```bash
for m in skchat skcode skdashboard skos; do
  capauth manifest sign "$m.skworld-module.json" --signer <OPERATOR_FPR>
done
# writes skchat.skworld-module.json.sig, ... one detached signature each.
```

Verify each before registering:

```bash
for m in skchat skcode skdashboard skos; do
  capauth manifest verify "$m.skworld-module.json" --expected-signer <OPERATOR_FPR>
done
# each must print VALID.
```

### 2c. Register each into the operator-approved registry

```bash
for m in skchat skcode skdashboard skos; do
  capauth manifest register "$m.skworld-module.json"
done
capauth manifest list        # all 4 -> signature=ok, enabled=true
```

`register` records the entries in `~/.skcapstone/shell/modules.json`. Syncthing
then distributes the signed manifests + registry to every node.

---

## Part 3: flip signing enforcement + re-enable dynamic modules

Only AFTER Part 2 shows all 4 `ok`.

On the webui unit (funnel node):

```bash
# ~/.config/skchat/webui-lumina.env
#   SKCHAT_SHELL_REQUIRE_SIGNED=1
#   SKCHAT_SHELL_SIGNER_FPR=<OPERATOR_FPR>     # pin the signer (D8920EA8...)
systemctl --user restart skchat-webui@lumina.service
```

Verify the public aggregate now emits ONLY verified modules (each tagged
`"verified": true`), and fails closed to `[]` if capauth/registry is unreachable:

```bash
curl -s "$FUNNEL/api/v1/shell/modules" | jq '.modules[] | {id, verified}'
```

Rebuild + redeploy the Flutter app with dynamic modules on, and (belt +
suspenders) client-side signature enforcement on, so the shell mounts the
`skdashboard`/`skos` panes and refuses any unverified entry:

```bash
cd skchat-app
flutter build web --release \
  --dart-define=USE_SHELL_DYNAMIC_MODULES=true \
  --dart-define=USE_SHELL_REQUIRE_SIGNED=true
# deploy the build output to skchat/src/skchat/static/app/ (the funnel serves /app)
```

---

## Rollback

- Embed auth: unset `SKCHAT_EMBED_TOKENS` and restart the webui. The mint route
  goes inert (404), no new tokens are issued, and the proxies fall back to
  requiring a full operator credential. Outstanding tokens expire within 600s.
- Signing: unset `SKCHAT_SHELL_REQUIRE_SIGNED`. The aggregate returns to
  unenforced discovery (operator facet still stripped). Rebuild the app with the
  dart-defines off to hide the discovered panes again.

## Done looks like

- Off-tailnet, no funnel path reaches the Board / ITIL / skos surface without an
  operator credential OR a valid, unexpired, correctly-scoped embed token.
- The authenticated app loads the Board and OS panes; a foreign-scope or expired
  token, or a write attempt through an embed token, is refused.
- `GET /api/v1/shell/modules` returns only signed, registry-approved modules.
