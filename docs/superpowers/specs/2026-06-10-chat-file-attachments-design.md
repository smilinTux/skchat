# Chat File & Image Attachments — Design (Spec 1: Shared Core + Web UI)

**Date:** 2026-06-10
**Status:** Approved (architecture + 3-phase decomposition)
**Scope of this spec:** the shared core + the web chat UI (webui). CLI/TUI is
Spec 2; the Flutter app is Spec 3 (separate sub-project).

## Goal

Let a user drop, paste, or pick an **image or file** in the chat window and have
it transfer to the peer and render **in the conversation** — inline image
thumbnails (click → full), download badges for other files, with upload/download
progress. Encrypted, verified, reliable, and **not dependent on any single
network transport**.

## Current state (what already exists — do NOT rebuild)

- `skchat/files.py` `FileTransferService`: chunked, **AES-256-GCM encrypted**,
  per-chunk + whole-file **SHA-256 verified** transfer; chunk/metadata
  persistence under `~/.skchat/transfers/`; daemon-driven reassembly into
  `~/.skchat/received/<transfer_id>/<filename>`.
- MCP tools `send_file` (skcomms `FILE_CHUNK`), `send_file_p2p` (WebRTC data
  channel), `list_transfers`.
- `webui.py`: FastAPI + HTMX **text-only** chat (`POST /send` takes
  `recipient`+`content`; `/ws/chat` WebSocket pushes `{type:"new"}`).
- `skcomms/transports/file.py`: independent chunked+**resume** filesystem/
  Syncthing transport (substrate, not wired into skchat's chat flow).

**The transport is ~built; the gap is the glue:** no message-model attachment,
no receiver→message wiring, no upload affordance, no inline rendering, no MIME/
thumbnails.

## Architecture

### Layer 0 — Shared core (surface-agnostic; reused by webui, CLI/TUI, Flutter)

**1. Message model (`models.py`)** — additive, backward-compatible:
```python
class FileRef(BaseModel):
    transfer_id: str
    filename: str
    size: int
    mime_type: str          # detected; e.g. image/png, application/pdf
    sha256: str
    thumbnail_id: str | None = None   # set for images
    direction: str          # "sent" | "received"

class ChatMessage(BaseModel):
    ...
    attachments: list[FileRef] = Field(default_factory=list)
```
`content` stays text (now optional caption when `attachments` is non-empty).
History JSONL gains `attachments` (defaults to `[]` on old rows — no migration
needed). `ContentType` unchanged; attachments are orthogonal to body type.

**2. `AttachmentService`** (`attachments.py`, wraps `FileTransferService`):
- `send_attachment(recipient, path, caption=None, transport="auto") -> ChatMessage`
  — picks a transport (below), kicks off the encrypted chunked send, **and**
  posts an outbound `ChatMessage(attachments=[FileRef(direction="sent")],
  content=caption or "")` so the sender's UI shows it immediately (optimistic,
  with progress).
- `on_transfer_complete(transfer)` — called by the daemon when a receive
  finishes: assemble (already done) → **detect MIME** → **generate thumbnail if
  image** → post an **inbound** `ChatMessage(attachments=[FileRef(
  direction="received")])`. This is the missing receiver→message wiring.
- `progress(transfer_id) -> float` and a hook to emit progress events (webui
  subscribes via WebSocket).

**3. Transport selection (the "best method" decision):**
- **Default — always available, never relies on a mesh:** the existing
  `FileTransferService` over **skcomms** (`FILE_CHUNK`). Chunked, encrypted,
  SHA-256-verified, **works when the peer is offline** (queued). This is the
  floor — the feature is fully functional with only this.
- **Optional fast-paths (used only when present, auto-detected, graceful
  fallback to the default):**
  - **WebRTC data channel** (`send_file_p2p`) when a live peer connection exists.
  - **Tailscale direct** when both peers share a tailnet (peer's tailscale hint
    comes from the skcomms peer registry's Tailscale backend). **We may use
    Tailscale but MUST NOT depend on it** — if it's absent/unreachable, fall
    straight back to the default. No code path requires tailscale.
- **Substrate, not a new pipe:** skcomms/Syncthing remains the realm replication
  substrate the default transport can ride later; we do not build a second
  parallel file pipeline now (YAGNI).
- `transport="auto"` (default) tries fast-paths, falls back to skcomms; explicit
  `"skcomms"|"webrtc"|"tailscale"` override allowed.

**4. MIME + thumbnails** (`media.py`):
- MIME detection: `filetype` (magic-bytes) with `mimetypes` fallback.
- Thumbnails for `image/*`: Pillow, max 320px long edge, stored at
  `~/.skchat/received/<transfer_id>/thumb.webp` (and for sent images under
  `~/.skchat/uploads/`). Guard against decompression bombs
  (`Image.MAX_IMAGE_PIXELS`); on any failure, no thumbnail → generic file badge.

### Layer 1 — Web UI (this spec's surface)

**Upload affordances** (webui chat form):
- A file button (`<input type="file" multiple>`).
- Drag-and-drop over the chat area.
- Paste-image handler (clipboard → upload).

**Endpoints** (FastAPI):
- `POST /upload` (multipart: `recipient`, `file`, optional `caption`) → save to
  `~/.skchat/uploads/<uuid>/<filename>` → `AttachmentService.send_attachment` →
  returns the created `ChatMessage`; WS broadcast `{type:"new"}`.
- `GET /file/<transfer_id>` → stream the file (download; `Content-Disposition`).
- `GET /file/<transfer_id>/thumb` → the thumbnail (or 404 → UI shows file badge).
- Existing `/messages` HTMX fragment + `/ws/chat` extended with
  `{type:"file_progress", transfer_id, percent}` events.

**Rendering** (`_render_messages`):
- `image/*` attachment → inline `<img src="/file/<id>/thumb">`, click → full
  (`/file/<id>`), with filename + size.
- other attachment → download badge: 📄 `name · size · type` linking
  `/file/<id>`.
- in-flight send/receive → a progress bar fed by the WS `file_progress` events.

### Storage layout
```
~/.skchat/uploads/<uuid>/<filename>          # outbound staged files (+ thumb.webp)
~/.skchat/transfers/<transfer_id>...         # chunk/metadata state (existing)
~/.skchat/received/<transfer_id>/<filename>  # inbound assembled (existing) (+ thumb.webp)
```

## Data flow
- **Send:** drop/paste/pick → `POST /upload` → stage file → `AttachmentService`
  → transport (auto) chunks+encrypts+sends → outbound `ChatMessage(attachments)`
  persisted + WS push → UI renders with progress.
- **Receive:** daemon collects `FILE_CHUNK`s → on `FILE_TRANSFER_DONE`
  assemble+verify → `on_transfer_complete` → MIME + thumbnail → inbound
  `ChatMessage(attachments)` persisted + WS push → recipient's window renders.

## Error handling & limits
- **SHA-256 mismatch** → transfer marked `failed`; the message shows a failed
  badge, never a working download. (Verification already exists in files.py.)
- **Resume:** expose the chunk-state resume the transfer layer already supports;
  a reconnecting transfer continues rather than restarting.
- **Size cap:** configurable, default **100 MB**; over-cap upload → 413 + clear
  UI error before any transfer starts.
- **Transport degradation:** fast-path failure (no WebRTC / no tailscale / mesh
  down) falls back to skcomms silently; offline peer → queued.
- **Thumbnail/MIME failure** → generic file badge, transfer unaffected.

## Security
- Files are already E2E-encrypted (AES-256-GCM, PGP-wrapped key) in transit.
- **Download endpoints** resolve only within `~/.skchat/received|uploads`; reject
  `..`/absolute paths (path-traversal guard); look up by `transfer_id`, never by
  caller-supplied path.
- Thumbnailing runs Pillow with `MAX_IMAGE_PIXELS` set (decompression-bomb
  guard); never executes file content.
- `Content-Disposition: attachment` + a conservative `Content-Type` on download
  to avoid the browser executing served content; thumbnails served as `image/webp`.

## Testing
- **Unit:** `FileRef` model + `ChatMessage.attachments` round-trip (incl. old
  rows w/o the field); `AttachmentService.send_attachment` (mocked transport)
  posts the outbound message + FileRef; `on_transfer_complete` posts inbound +
  MIME + thumbnail; MIME detection (image/pdf/unknown); thumbnail gen + bomb
  guard; transport selection (auto picks fast-path when present, falls back to
  skcomms when not — including "tailscale absent → skcomms").
- **webui:** `POST /upload` multipart (image + non-image, over-cap → 413);
  `GET /file/<id>` + `/thumb` (happy + path-traversal attempt rejected + 404);
  `_render_messages` emits inline `<img>` for image attachments and a download
  badge otherwise.
- **Integration (round-trip):** send a file via `AttachmentService` with a
  faked transport → simulate receive → assert an inbound message with a valid
  `FileRef` + thumbnail for an image.
- All tests standalone (tmp `~/.skchat`, in-process keys, fake transport, no
  network, **no real tailscale/WebRTC**, notifications gated off per conftest).

## Out of scope (this spec)
- **CLI/TUI** file send/receive/render → **Spec 2** (reuses Layer 0).
- **Flutter app** → **Spec 3** (separate sub-project; mobile dev env).
- A new skcomms-Syncthing file pipeline (substrate stays as-is).
- Multi-file albums / message editing of attachments (future).

## Decisions made
- Reuse the existing encrypted chunked `FileTransferService`; do **not** build a
  new transport. (User: "best method"; YAGNI.)
- Tailscale is an **optional** fast-path only — the feature never depends on it.
  (User: "we may use tailscale too but i don't wanna rely on it.")
- Want-it-all across surfaces is honored via decomposition: core+webui now,
  CLI/TUI next, Flutter as its own spec.
