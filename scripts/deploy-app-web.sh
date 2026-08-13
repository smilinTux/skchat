#!/usr/bin/env bash
# deploy-app-web.sh - build the Flutter web client and deploy it into skchat.
#
# WHY THIS SCRIPT EXISTS
#
# `src/skchat/static/app/` is TRACKED in git. Deploying by rsyncing a fresh
# build into it leaves the working tree dirty, and the next `git checkout main`
# or `git pull` silently reverts every one of those files to the committed
# bundle. That happened three times in a row on 2026-08-08: each deploy looked
# successful, the served page kept showing an older build with no Linked Devices
# section, and nothing anywhere reported a problem.
#
# An rsync alone is therefore NOT a deploy. The bundle has to be committed, which
# is exactly what the repo's own earlier `deploy(app):` commits did. This script
# makes that the only path: it builds, stamps provenance, and commits, in one go.
#
# It also writes `.source_commit`, recording which skworld-app commit the bundle
# was built from, so "is the deployed client stale?" becomes a question anyone
# can answer without diffing 5 MB of compiled JavaScript.
#
# USAGE
#   ./scripts/deploy-app-web.sh --check    Report what is deployed vs app main.
#                                          Touches nothing. Exit 1 if stale.
#   ./scripts/deploy-app-web.sh            Build, deploy, stamp, commit.
#   ./scripts/deploy-app-web.sh --restart  ...and restart the webui service.
#
# ENV
#   SKWORLD_APP_DIR   app checkout   (default ~/clawd/skcapstone-repos/skworld-app)
#   FLUTTER           flutter binary (default ~/flutter/bin/flutter, NON-snap)
set -euo pipefail

APP_DIR="${SKWORLD_APP_DIR:-$HOME/clawd/skcapstone-repos/skworld-app}"
FLUTTER="${FLUTTER:-$HOME/flutter/bin/flutter}"
SKCHAT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
DEST="$SKCHAT_DIR/src/skchat/static/app"
STAMP="$DEST/.source_commit"

# The base href the webui serves the client under. A bundle built without this
# loads a blank page: every asset resolves against / instead of /app/.
BASE_HREF="/app/"

die() { echo "deploy-app-web: $*" >&2; exit 1; }

[ -d "$APP_DIR" ] || die "app checkout not found: $APP_DIR"

deployed_commit() { [ -f "$STAMP" ] && head -n1 "$STAMP" || echo "(none recorded)"; }

app_main_commit() {
  git -C "$APP_DIR" fetch -q origin main 2>/dev/null || true
  git -C "$APP_DIR" rev-parse origin/main
}

# ── Branch guard ────────────────────────────────────────────────────────────
# This script COMMITS the bundle, to whatever branch happens to be checked out.
# On 2026-08-13 that put a deploy commit onto a concurrent session's feature
# branch: the run reported success, the bundle never reached main, and
# production kept serving the old client. The shared checkout is also
# production, so it is routinely parked on someone else's branch, which makes
# this the default outcome rather than an unlucky one.
#
# The fix is a worktree, not a --force:
#   git worktree add ~/skworld-worktrees/deploy -b deploy/<name> origin/main
#
# SKIP_BRANCH_GUARD=1 escapes it, for the rare deliberate case.
require_main_branch() {
  [ "${SKIP_BRANCH_GUARD:-}" = "1" ] && return 0
  local branch
  branch="$(git -C "$SKCHAT_DIR" rev-parse --abbrev-ref HEAD 2>/dev/null || echo unknown)"
  [ "$branch" = "main" ] && return 0
  cat >&2 <<EOF
deploy-app-web: REFUSING to deploy from branch '$branch'.

This script commits the built bundle to the current branch. Deploying from a
feature branch lands the commit there instead of main, which looks like a
successful deploy while production keeps serving the old client.

Deploy from a worktree on main:
  git worktree add ~/skworld-worktrees/deploy -b deploy/<name> origin/main
  cd ~/skworld-worktrees/deploy && ./scripts/deploy-app-web.sh

Override (rarely right): SKIP_BRANCH_GUARD=1
EOF
  exit 1
}

if [ "${1:-}" = "--check" ]; then
  have="$(deployed_commit)"
  want="$(app_main_commit)"
  echo "deployed from : $have"
  echo "app main is   : $want"
  if [ "$have" = "$want" ]; then
    echo "OK: the committed bundle matches app main."
    exit 0
  fi
  echo "STALE: the deployed client is not built from app main."
  echo "Run ./scripts/deploy-app-web.sh to rebuild and commit it."
  exit 1
fi

# Everything past --check writes and commits, so gate it here. --check stays
# read-only and usable from any branch, which is how you diagnose a bad deploy.
require_main_branch

command -v "$FLUTTER" >/dev/null 2>&1 || [ -x "$FLUTTER" ] || die "flutter not found: $FLUTTER"

# A deploy COMMITS the bundle into this checkout, so it lands on whatever branch
# happens to be open. That has already put one session's deploy onto another
# session's feature branch, which hides the bundle from main and leaves the next
# person deploying from main shipping a stale client. Refuse rather than do it
# quietly. This checkout is also the editable install the live services import,
# so it should be sitting on main anyway.
branch="$(git -C "$SKCHAT_DIR" rev-parse --abbrev-ref HEAD)"
if [ "$branch" != "main" ] && [ "${ALLOW_DEPLOY_OFF_MAIN:-}" != "1" ]; then
  die "this checkout is on '$branch', not main. A deploy commits the bundle, and
     committing it to a feature branch hides it from main. Switch to main, or set
     ALLOW_DEPLOY_OFF_MAIN=1 if you really mean it."
fi

echo "==> building from $APP_DIR"
git -C "$APP_DIR" fetch -q origin main
src_commit="$(git -C "$APP_DIR" rev-parse HEAD)"
if [ -n "$(git -C "$APP_DIR" status --porcelain)" ]; then
  echo "    NOTE: app checkout has uncommitted changes; the bundle will include them."
fi
# The app renders its version + build id from compile-time dart-defines
# (lib/core/build_info.dart). Omitting them silently ships the hardcoded
# fallback, so the UI claims a version that has nothing to do with this build,
# which is precisely the "is what I am looking at current?" question this whole
# script exists to answer. Mirrors scripts/build-web-lumina.sh in the app repo.
app_version="$(grep -m1 '^version:' "$APP_DIR/pubspec.yaml" | awk '{print $2}')"
build_id="${src_commit:0:7}-$(date +%m%d-%H%M)"
echo "    stamping v$app_version build $build_id"
# USE_SHELL_DYNAMIC_MODULES turns on the umbrella-shell subapp discovery
# (GET /api/v1/shell/modules) so the embedded ops panes (skdashboard Board /
# skos OS) appear in the nav; USE_SHELL_REQUIRE_SIGNED matches the server's
# SKCHAT_SHELL_REQUIRE_SIGNED enforcement (only capauth-signed manifests). Both
# default OFF in the client, so without these the whole discovery path is
# tree-shaken out and the panes never render (2026-08-12 embed fix).
( cd "$APP_DIR" && "$FLUTTER" build web --release --base-href "$BASE_HREF" \
    --dart-define="APP_VERSION=$app_version" \
    --dart-define="BUILD_ID=$build_id" \
    --dart-define="USE_SHELL_DYNAMIC_MODULES=true" \
    --dart-define="USE_SHELL_REQUIRE_SIGNED=true" )

built="$APP_DIR/build/web"
[ -f "$built/main.dart.js" ] || die "build produced no main.dart.js"
grep -q "<base href=\"$BASE_HREF\">" "$built/index.html" \
  || die "built index.html is missing <base href=\"$BASE_HREF\">; it would load blank"

echo "==> deploying into $DEST"
rsync -a --delete "$built/" "$DEST/"

{
  echo "$src_commit"
  echo "# skworld-app commit this bundle was built from."
  echo "# Written by scripts/deploy-app-web.sh. Do not hand-edit."
} > "$STAMP"

echo "==> committing (an uncommitted deploy is reverted by the next checkout)"
git -C "$SKCHAT_DIR" add "$DEST"
if git -C "$SKCHAT_DIR" diff --cached --quiet -- "$DEST"; then
  echo "    nothing changed; bundle already matches."
else
  git -C "$SKCHAT_DIR" commit -q -m "deploy(app): rebuild web client from skworld-app ${src_commit:0:12}

Built with --base-href $BASE_HREF and committed, because src/skchat/static/app
is tracked and an uncommitted bundle is reverted by the next checkout or pull."
  echo "    committed: $(git -C "$SKCHAT_DIR" rev-parse --short HEAD)"
fi

if [ "${1:-}" = "--restart" ]; then
  echo "==> restarting skchat-webui@lumina"
  systemctl --user restart skchat-webui@lumina.service
  sleep 5
  systemctl --user is-active skchat-webui@lumina.service
fi

echo "==> done. Remember to push."
