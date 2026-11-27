# Deploying the Flutter web client

Moved here from the old agent instructions file. `AGENTS.md` links to it.

## Why the script, never rsync

`src/skchat/static/app/` is **tracked in git** and the webui serves it directly. An rsync
into it is not a deploy: it leaves the working tree dirty, and the next `git checkout main`
or `git pull` silently reverts every file to the committed bundle. Three consecutive
deploys were undone exactly that way: each looked successful, the operator kept getting an
older build with a feature missing, and nothing reported a problem.

`scripts/deploy-app-web.sh` builds, verifies, stamps and **commits**:

```bash
./scripts/deploy-app-web.sh --check     # deployed bundle vs skworld-app main (touches nothing)
./scripts/deploy-app-web.sh             # build + deploy + stamp + commit
./scripts/deploy-app-web.sh --restart   # ...and restart the webui unit
git push                                # the commit IS the deploy
```

It passes the `--dart-define`s that `lib/core/build_info.dart` reads, so the Me header shows
the real `vX.Y.Z build <app-sha>-<date>`, and writes `src/skchat/static/app/.source_commit`
recording which skworld-app commit produced the bundle. `tests/test_deployed_app_bundle.py`
fails on a bundle with the wrong `<base href>` (blank page) or with no provenance stamp.

## The full procedure

1. **The skchat checkout must be on `main` and clean.** The script refuses any other
   branch, because the bundle commit has to land on `main`. git allows `main` in only one
   worktree, so this runs from the checkout that holds `main`.
   ```bash
   git status --short | grep -v '^??'      # must be empty
   git checkout main && git pull --ff-only
   ```
2. **Pull the skworld-app checkout.** The build reads its working tree, not origin.
   Merging a client PR on GitHub does not move it; skip this and you ship a stale bundle
   that commits and "succeeds" without the fix.
3. **The skworld-app checkout must be clean too.** A dirty tree (another session's
   uncommitted files, even a staged delete) ships into the bundle, and nothing warns:
   `git pull --ff-only` succeeds and `--check` passes, because `.source_commit` records
   HEAD, not whether the tree matches it. If it is dirty, do not stash or reset (that is
   someone's live work); build from a detached clean worktree instead:
   ```bash
   git -C <skworld-app> worktree add --detach <tmp-dir> origin/main
   SKWORLD_APP_DIR=<tmp-dir> ./scripts/deploy-app-web.sh
   ```
4. **Always build from current skworld-app `main`.** Two sessions each deploying from
   their own app commit silently drop each other's client work. Run `--check` before and
   after.
5. Build, commit, push, restart the webui unit.

## Verify by content, not provenance

`.source_commit` and `--check` only prove which commit the build was told to use. Grep the
built bundle for a string unique to the change:

```bash
grep -c '<unique-string>' src/skchat/static/app/main.dart.js   # 0 means it is NOT in there
```

Then grep the **served** file over the real URL, not just the local copy. Confirm
`.source_commit` equals the skworld-app `main` SHA, and tell the operator to hard reload:
the old bundle is cached.
