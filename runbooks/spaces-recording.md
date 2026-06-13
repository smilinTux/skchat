# SK Spaces — recording (Egress)

Audio-only room-composite recording for Spaces. Off by default; consent-gated.

## Prereqs
- `livekit-stack.yml` running (the SFU) with **Redis enabled** (egress coordinates
  with the SFU over Redis — single-node-no-Redis setups must add it).
- `egress-stack.yml` deployed on the same node (`node.labels.livekit == true`).
- `LIVEKIT_API_KEY` / `LIVEKIT_API_SECRET` exported (same as the SFU).

## Flow
1. Each on-stage speaker POSTs `/spaces/{id}/consent {identity}` (the UI prompts
   them when they go on stage).
2. Host POSTs `/spaces/{id}/record/start {requester}`. The gate uses the
   **server-authoritative** on-stage set (`space.speakers`), NOT a body-supplied
   list — if any on-stage speaker hasn't consented → 409 with `missing_consent`.
   Else egress starts and a `● REC` indicator shows to everyone.
3. Output OGG lands in the `spaces-recordings` volume at `<space_id>.ogg`.
4. Host POSTs `/spaces/{id}/record/stop {requester}` → egress stops.

## Consent on promote (recording active)
The on-stage speaker set is **server-authoritative** (`reg.speakers`), so the
record gate cannot be bypassed by omitting `speakers` from the request body.
To close the race where someone is promoted *after* recording starts, promotion
is itself consent-gated while recording is active: if `/invite` or `/raise-hand`
would put an identity on stage but that identity has **not** consented (and
`space.recording` is True), the promotion is reverted (a `remove` stage action)
and the request returns **409 "consent required to speak while recording is
active"**. The UI should prompt the speaker to consent first, then retry. A
consenting identity is promoted normally.

## Notes
- The Recorder uses `start_room_composite_egress(audio_only=True)` → OGG file.
- Replays can be served via the existing recordings UI (point it at the
  spaces-recordings volume) — wire-up tracked separately.
- Egress needs `SYS_ADMIN` cap; keep it tailnet-only.
