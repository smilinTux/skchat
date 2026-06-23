I now have authoritative Nextcloud research plus a full read of our code. Let me synthesize the deliverable: the gap analysis mapping each Nextcloud capability to ours, with concrete buildable extensions to our access plane + Flutter browser.

---

# skos Files vs Nextcloud Files — Gap Analysis & "Extend skos Files" Plan

## Where we are today (grounded in the code)

**Backend access plane** (`skcomms/src/skcomms/access/files.py` + `registry.py` + `grants.py`):
- Tools: `file_read`, `file_write`, `file_patch` (unified diff), `file_list`, `file_stat`, `list_roots`, plus `pg_search`/`pg_locate`/`node_info`/`health`.
- Security choke point `_resolve_checked()`: canonicalize → allowlist roots → secrets hard-deny. **This is excellent and must be the gate for every new op.**
- RBAC: per-identity scope grants (`read`/`write`/`exec`, hierarchical) in `grants.yml`; `write` exists but is **off by default**.
- Every mutation is audited (JSON-line append). Reads logged at DEBUG.
- 8 MiB payload cap on read/write (base64-over-tool). Large binaries bypass this via the streaming endpoint.
- `skchat/webui.py`: `GET /media/file?node=&path=` → range-capable `FileResponse` (reuses `_resolve_checked`); `.158` only. `POST /access/tool` = same-origin capauth proxy.

**Flutter browser** (`skchat-app/lib/features/skos/`):
- `AccessClient` seam (mock + `DaemonAccessClient`); one TODO to wire the capauth signer.
- List-only directory view, breadcrumb, swipe-right-to-go-up, node picker (`.158`/`.41`).
- Immersive media viewer: swipeable PageView gallery (image/video/audio), pinch-zoom, drag-to-dismiss, auto-hiding chrome, off-screen controller disposal; PDF via iframe (web); text/markdown via `file_read` with an Edit/Save gated on `canWrite`.
- Long-press media → options sheet (share/open/download/copy-link/send-to-chat).
- Corpus search bar → `pg_search` hits tagged `{node, path}`.
- **Read-only by default.** No create/rename/move/delete/upload, no trash, no versions, no grid, no thumbnails, no favorites/tags, no details sidebar.

## The architectural lesson from Nextcloud

NC's Files app is **a WebDAV client**: browse = PROPFIND, mutations = PUT/MKCOL/MOVE/COPY/DELETE, with three extra DAV trees (`/trashbin/`, `/versions/`, `/uploads/`) each using a sentinel folder (`restore`, `.file`) for the action that has no HTTP verb. Two surfaces are *not* WebDAV: **previews** (`/core/preview?fileId&x&y`) and **zip-of-folder download**. Metadata (tags/comments/shares/activity) rides OCS + DAV properties keyed off `oc:fileid`. Extensibility = **typed objects registered into named registries** (`registerFileAction`, `getNavigation().register`, `getSidebar().registerTab`, `OCA.Viewer.registerHandler`) with context-object args and a proxy pattern for version-safety.

We are **RPC-over-tools**, not WebDAV — and that's fine. We don't need WebDAV-protocol fidelity; we need the *capability set*. We add tools to the access registry and screens/actions to the Flutter app. Below, each NC capability → (a) what NC does, (b) our status, (c) what to build, (d) priority.

---

## Capability-by-capability map

### 1. Create folder / new file — **MUST**
- **(a) NC:** `MKCOL` for folders; the `newMenu` registry for "New folder / New text file" + templates.
- **(b) Ours:** None. `file_write` can create a file (parent auto-`mkdir`), but no folder-create tool and no UI affordance.
- **(c) Build:** Add `file_mkdir(path)` tool (scope=write) — `_resolve_checked(must_exist=False)`, re-validate parent under root, `mkdir(parents=True)`, audit. New-file is already `file_write` with empty content. Flutter: a `+` FAB on the listing → "New folder / New text file" sheet.
- **(d) MUST** — foundational, trivial, unblocks a real file manager.

### 2. Rename / move / copy — **MUST**
- **(a) NC:** `MOVE`/`COPY` with `Destination` + `Overwrite` headers; a FilePicker modal for the destination.
- **(b) Ours:** None. (`file_patch` edits content, not paths.)
- **(c) Build:** `file_move(src, dst)`, `file_copy(src, dst)` tools (scope=write). **Both endpoints validated through `_resolve_checked`** (src must_exist, dst parent under root + not hard-denied). Use `shutil.move`/`shutil.copy2`; reject overwrite unless `overwrite=true`. Audit both paths. Flutter: rename inline-edit on long-press; move/copy → a destination picker (a mini directory-browser modal reusing `_DirListing`).
- **(d) MUST.**

### 3. Delete + Trash/restore — **MUST (delete) / SHOULD (trash)**
- **(a) NC:** `DELETE` routes to `files_trashbin` (not hard delete); trash is a DAV tree; **restore = MOVE to a `restore` sentinel**; retention policy (default 30 days / quota-pressure); empty-trash = DELETE on trash root.
- **(b) Ours:** None.
- **(c) Build:** `file_delete(path, to_trash=true)` (scope=write). Implement a **sovereign trash**: move into a per-root `.skos-trash/` dir (or `~/.skcapstone/agents/<agent>/skos-trash/`) with a sidecar JSON recording `{original_path, deleted_at, sha}`. Tools: `trash_list()`, `trash_restore(id)` (move back to original, recreate parents), `trash_purge(id|all)`. Retention: a sweep (cron or on-list) purging entries older than N days — borrow NC's `'auto'`/`'D, auto'` config shape. **Crucially the trash dir must itself sit under an allowed root and be excluded from normal listings.** Flutter: a "Trash" view (reuse the View pattern) + restore/purge actions.
- **(d) Delete = MUST. Trash = SHOULD** (delete without trash is dangerous; trash is the safety net — do them together if possible).

### 4. Versions / history — **SHOULD**
- **(a) NC:** auto-save prior content on write (≥2 min throttle); DAV versions tree keyed by `fileid`; **restore = MOVE to `restore` sentinel**; tiered thinning expiry; `nc:version-label` PROPPATCH pins a version out of expiry.
- **(b) Ours:** None — `file_write`/`file_patch` overwrite in place. We *do* have sha256 per write in the audit log (a primitive history signal).
- **(c) Build:** On every `file_write`/`file_patch`, **before** overwriting, copy current content into a versions store (`.skos-versions/<relpath>/<timestamp>.<ext>` under the root, or in the agent home). Tools: `versions_list(path)`, `versions_restore(path, version_id)`, `version_label(path, version_id, label)`. Reuse the audit log's sha to dedupe (skip if unchanged). Adopt NC's "labeled version exempt from expiry" primitive. Flutter: a "Versions" tab in the (future) details sidebar; restore/label actions.
- **(d) SHOULD** — high value once write is on; protects against bad agent writes. The write path already runs through one place, so it's a clean hook.

### 5. Chunked / resumable large upload — **SHOULD (upload itself MUST once writeable)**
- **(a) NC:** v2 chunking — `MKCOL` upload session under `/uploads/`, numbered `PUT`s (5MB–5GB), `MOVE .file` to assemble, `OC-Total-Length` for upfront quota, `507` on overflow, 24h expiry, PROPFIND-driven resume.
- **(b) Ours:** Nothing. `file_write` is capped at 8 MiB (base64). The streaming `/media/file` is **download-only**.
- **(c) Build:** Mirror NC's three-step protocol as an **upload counterpart to `/media/file`** — a same-origin `POST /media/upload` that streams the request body directly to disk (range/chunk-aware), reusing `_resolve_checked`. For resumability + the 300+ MB AI-LIFE masters, implement: `upload_begin(path, total) -> upload_id`, chunked `PUT /media/upload/<id>/<n>` (raw bytes, no base64), `upload_commit(id)` (assemble + move into place, audit), `upload_abort(id)`. Cap-free because it streams, not base64-over-tool. Flutter: file picker + drag-drop (web) → progress bar; resume on reconnect by querying which chunks landed.
- **(d) Upload = MUST (once writeable); chunked/resumable = SHOULD** — but design `/media/upload` from the start as the symmetric partner of `/media/file` so large media works day one.

### 6. Grid view + thumbnails/previews — **MUST (grid) / SHOULD (server previews)**
- **(a) NC:** list↔grid toggle; previews from `/core/preview?fileId=&x=&y=&a=` with a server-side cache (`appdata/preview/...`), powers-of-4 size snapping, ffmpeg for video frames, Imagick for PDF; lazy on first hit + optional pre-generation cron.
- **(b) Ours:** List only. Media tiles use a generic icon. The viewer streams full-resolution originals via `/media/file` (fine for viewing, wasteful for a grid of 300 MB masters).
- **(c) Build:**
  - **Grid (cheap, MUST):** add a list/grid toggle in `_Header`; a `FileEntryGrid` tile rendering `Image.network(mediaStreamUrl(...))` for images. Works immediately with the existing endpoint.
  - **Previews (SHOULD):** add a `GET /media/preview?node=&path=&w=&h=` endpoint that generates + caches thumbnails (Pillow for images/PDF-first-page, `ffmpeg -ss 1 -frames:v 1` for video) into a cache dir keyed by `sha+size` (steal NC's sharded layout). Return the generic icon as fallback. This is what makes a grid of large masters fast and cheap.
- **(d) Grid = MUST (low effort, big UX win). Server-side preview cache = SHOULD** (needed before grid-of-huge-videos is usable).

### 7. Details / info sidebar — **SHOULD**
- **(a) NC:** right-hand panel with tabs (Details/Sharing/Versions/Activity/Comments); `getSidebar().registerTab()` extensibility.
- **(b) Ours:** None. The viewer top-bar shows `node · path`. `file_stat` returns size/mtime/mode/type/sha.
- **(c) Build:** A details panel (bottom-sheet on phone, side-panel on wide) driven by `file_stat` + (future) versions/tags. Make it **tabbed and pluggable from the start** — this is where our module/plugin story lands (see §12). Tabs: Details (stat + sha + preview), Versions, Tags, (later) Sharing.
- **(d) SHOULD** — the natural home for versions/tags; build the shell when those land.

### 8. Favorites / starred — **SHOULD**
- **(a) NC:** stored as reserved system tag `_$!<Favorite>!$_`, exposed as `oc:favorite`; `PROPPATCH` to set; `REPORT oc:filter-files` to list.
- **(b) Ours:** None.
- **(c) Build:** Lightweight + client-first: a per-identity favorites store. Simplest is a tool pair `favorite_add(node, path)`/`favorite_remove`/`favorite_list` writing to `~/.skcapstone/agents/<agent>/skos-favorites.json` (no schema migration, syncs via Syncthing). Flutter: a star on each tile + a "Favorites" view. (Could later fold into a unified tags store — see §9.)
- **(d) SHOULD** — cheap, high daily-use value, no security surface (pure metadata).

### 9. Tags + comments — **NICE (tags) / NICE (comments)**
- **(a) NC:** systemtags app (collaborative/personal tags, assign via `PUT systemtags-relations/files/<fileid>/<tagid>`, filter via `REPORT oc:systemtag`); comments via DAV `comments/files/<fileid>`.
- **(b) Ours:** None. We *do* have a richer adjacent capability: **`pg_search` corpus search** (hybrid vector+BM25 over `docs`), which NC only gets via the heavy external Full-Text-Search stack.
- **(c) Build:** Tags = generalize the favorites store into `skos-tags.json` (`{path: [tags]}`) with `tag_add/remove/list` + a tag-filter view. Comments are lower value for a single-operator sovereign tool — skip unless multi-agent annotation becomes a need; if so, a `comments.jsonl` sidecar keyed by sha/path.
- **(d) Tags = NICE. Comments = NICE (probably skip).** Our corpus search already beats NC's default content-search story.

### 10. Sharing (links, internal, permissions) — **NICE / mostly N/A**
- **(a) NC:** OCS Share API — public links (password, expiry, hide-download, file-drop), internal user/group shares with a permission bitmask, federated shares.
- **(b) Ours:** Different model entirely. "Sharing" in our world = **send-to-chat / copy-link via the options sheet**, and access is **capauth-gated per identity** (the grants system), not per-file ACLs. The federation primitive is skcomms/skfed, not OCM.
- **(c) Build (if wanted):** A "public link" could be a Funnel-exposed, signed, expiring URL to `/media/file` (token-bound, time-boxed) — useful for the conf-calls/file-drop direction. Internal "sharing" is better expressed as **granting another agent identity a read scope on a path subtree** (extend `grants.yml` to support path-scoped grants, not just global read/write). That's the sovereign-native analog of NC's per-file share.
- **(d) NICE** — and reframe it: *path-scoped capauth grants* (an extension to `grants.py`) is the feature worth borrowing, not NC's ACL bitmask. Expiring signed `/media/file` links are a good fit for the file-drop use case.

### 11. Search — **HAVE (and ahead)**
- **(a) NC:** filename via DAV `SEARCH d:basicsearch`; metadata via `REPORT`; content only via the external FTS/Elasticsearch stack.
- **(b) Ours:** `pg_search` = hybrid vector+BM25 over the corpus, tagged `{node, path}`. We are **ahead of stock NC** on content search.
- **(c) Build (gaps):** We have *content/corpus* search but no fast *filename/in-folder* filter. Add (i) a client-side filename filter box on the current listing (instant, no backend), and (ii) optionally a `file_find(root, glob, name_contains)` tool for recursive filename search (validated, depth-capped). Keep `pg_search` as the corpus/semantic lane.
- **(d) Filename filter = SHOULD (trivial, client-side). Recursive `file_find` = NICE.** Corpus search: already done.

### 12. Extensibility / plugins — **SHOULD (the strategic borrow)**
- **(a) NC:** the highest-value pattern to copy. Typed objects into named registries: `registerFileAction({id, displayName, iconSvgInline, enabled(ctx), exec/execBatch(ctx)})`, `getNavigation().register(View)` (custom left-rail views that supply their own contents), `getSidebar().registerTab({tagName web-component})`, `OCA.Viewer.registerHandler({mimes, component, group})`. Two design wins: **context-object args** (not positional, so the contract grows without breaking) and a **proxy/registry pattern** for version-safety. PHP side wires in via `IBootstrap` + injects JS via `LoadAdditionalScriptsEvent`.
- **(b) Ours:** Actions are hard-coded in the options sheet and viewer dispatch (`switch(MediaKind)`). The backend already has a clean registry analog: `AccessRegistry.register(name, fn, scope=...)` — that's our `registerFileAction` equivalent **on the tool side**.
- **(c) Build:** Introduce a Flutter-side **`FileAction` registry** mirroring NC: `registerFileAction(FileAction(id, label, icon, enabled: (ctx) => bool, exec: (ctx) => Future))` where `ctx = {nodes, node, currentDir}`. Refactor the existing options-sheet entries (share/open/download/copy-link/send-to-chat) and the future create/rename/move/delete into registered actions. Mirror NC's **View** concept for the left-rail/tab navigation (Files / Favorites / Trash / Tags / Search are all "views" supplying contents). Keep the **context-object** convention. This makes "skos Files is plugin-extensible" true by construction and aligns with the SKWorld host-plugin pattern.
- **(d) SHOULD** — do it as the *refactor* that lands alongside create/rename/move (so the new ops are the registry's first consumers, not a retrofit).

### 13. Media gallery / Photos-Memories — **PARTIAL (have viewer) / NICE (timeline)**
- **(a) NC:** Photos (timeline via DAV SEARCH ordered by EXIF date; albums; places; on-this-day) and Memories (day-index `/api/days` scrubber, faces via recognize, places via OSM, go-vod HLS transcoding for scale).
- **(b) Ours:** A strong **media viewer** (swipe gallery, zoom, video/audio playback, drag-dismiss) — better than a plain file list, but **folder-scoped**, not a timeline. No EXIF/date index, no faces/places, no transcoding.
- **(c) Build:** The buildable slice is a **timeline View**: a tool `media_index(root)` that walks for image/video, reads mtime (and EXIF date when cheap via Pillow), returns a date-bucketed list → a Flutter timeline/scrubber reusing the existing gallery viewer. Faces/places/transcoding are big external-dependency phases (we already have ComfyUI/ffmpeg infra on .100 if ever wanted) — defer. Pairs with the preview-cache (§6) for cheap thumbnails.
- **(d) Timeline view = NICE. Faces/places/transcoding = NICE/defer.** The viewer is already our differentiator; a timeline is the next increment.

### 14. Conflict handling — **SHOULD (small but real)**
- **(a) NC:** optimistic concurrency — `If-Match: <etag>` on PUT/MOVE → `412` on mismatch; desktop client writes a "conflicted copy" rather than clobbering.
- **(b) Ours:** `file_write` blindly overwrites. We compute a sha on write but don't check it against an expected prior sha.
- **(c) Build:** Add optional `expected_sha` to `file_write`/`file_patch`; if the current file's sha ≠ expected, return a `409`-style conflict instead of overwriting. The Flutter editor already reads then writes — capture the read sha and pass it back. Cheap insurance against two agents stomping a file.
- **(d) SHOULD** — small change, prevents silent data loss in a multi-agent fleet.

### 15. WebDAV / external storage / sync clients — **N/A (don't borrow the protocol)**
- We deliberately aren't WebDAV. External-storage mounts (SMB/S3/SFTP) and desktop-sync-client compatibility aren't goals — Syncthing already handles fleet sync, and the access plane is the deliberate single gate. **Skip.** (If desktop-client compat ever mattered, a thin SabreDAV-shape adapter over the same tools is conceivable, but it's not on the path.)

---

## Prioritized "Extend skos Files" feature list (fold into the comms-suite plan)

**Must (the read-only → real-file-manager jump):**
1. **Write ops on the access plane:** `file_mkdir`, `file_move`, `file_copy`, `file_delete` — all through `_resolve_checked` + audited + scope=write. (New-file = existing `file_write`.)
2. **Flutter mutation UI:** `+` FAB (new folder/file), long-press rename, move/copy destination picker, delete-with-confirm — built as the **first consumers of a new `FileAction` registry** (§12 refactor lands here).
3. **Upload — symmetric `/media/upload`** (streaming, no 8 MiB cap), so large AI-LIFE masters can be added, with drag-drop/file-picker + progress in Flutter.
4. **Grid view + tiles** (list/grid toggle; image tiles via existing `/media/file`).

**Should (safety, polish, the differentiators):**
5. **Trash + restore** (sovereign `.skos-trash/` + sidecar + retention sweep; `trash_list/restore/purge`). Ship with delete.
6. **Versions** (pre-overwrite snapshot on every write/patch; `versions_list/restore/label`; labeled = exempt from expiry).
7. **Server-side preview cache** (`/media/preview?w&h`, Pillow/ffmpeg, sharded cache) — makes the grid cheap for big media.
8. **Favorites** (per-agent JSON store + star + Favorites view).
9. **Details sidebar shell** (tabbed, pluggable: Details/Versions/Tags) — driven by `file_stat`.
10. **Conflict guard** (`expected_sha` on write/patch → conflict instead of clobber).
11. **Filename filter** (instant client-side filter of the current listing).
12. **Resumable chunked upload** (extend §3 with `upload_begin/chunk/commit/abort` + resume).

**Nice (later increments):**
13. **Tags** (generalize favorites store; tag-filter view).
14. **Path-scoped capauth grants** in `grants.py` (the sovereign analog of NC sharing) + **expiring signed `/media/file` links** for file-drop.
15. **Recursive `file_find`** (glob/name search tool).
16. **Media timeline view** (`media_index` + date scrubber reusing the gallery viewer).

**Skip:** WebDAV protocol fidelity, external-storage mounts, desktop-sync-client compat, per-file ACL bitmask, comments, Photos/Memories faces+places+transcoding (huge external deps; defer).

## Two design principles to carry over from Nextcloud
- **Context-object actions + named registries** (`FileAction`/`View`) on both tiers: our `AccessRegistry` already does this for tools — mirror it in Flutter so every file operation is a registered, plugin-addable action. This makes "skos Files is extensible" true by construction and matches the SKWorld host-plugin pattern.
- **"Restore = move to a sentinel"** for trash and versions is a clean, low-machinery pattern worth copying directly. And **every** new path that touches the filesystem must go through `_resolve_checked` — it's the single security property that makes the whole plane safe; new tools (move/copy/upload/preview) must validate **both** endpoints.

**Key files for whoever picks this up:**
- Backend tools + security gate: `/home/cbrd21/clawd/skcapstone-repos/skcomms/src/skcomms/access/files.py`
- Tool registry + scopes: `/home/cbrd21/clawd/skcapstone-repos/skcomms/src/skcomms/access/registry.py`
- Scope grants/RBAC: `/home/cbrd21/clawd/skcapstone-repos/skcomms/src/skcomms/access/grants.py`
- Streaming + proxy endpoints: `/home/cbrd21/clawd/skcapstone-repos/skchat/src/skchat/webui.py` (`/media/file`, `/access/tool`)
- Flutter browser + viewer: `/home/cbrd21/clawd/skcapstone-repos/skchat-app/lib/features/skos/skos_files_screen.dart`
- Flutter access seam: `/home/cbrd21/clawd/skcapstone-repos/skchat-app/lib/features/skos/access_client.dart`