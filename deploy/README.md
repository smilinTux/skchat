# skchat docker swarm deployment

> ## B1 — v2 comms-suite images (current)
>
> The three skchat services that run today as `systemd --user` units are
> packaged here as three lean, multi-stage, non-root, healthchecked images for
> the v2 Docker Swarm stack (`deploy/v2/skchat-stack.yml`, composed by B2).
> Secrets are **never** baked in — they arrive at runtime via the stack
> `--env-file` / OpenBao (B3). The legacy single-service `skstack01` notes
> follow below the divider.
>
> ### Images / Dockerfiles
>
> | Image | Dockerfile | systemd unit it replaces | Entry (ExecStart equiv.) | Port | /health | Size |
> |---|---|---|---|---|---|---|
> | `skchat-webui:v2`  | `deploy/Dockerfile.webui`  | `skchat-webui@<agent>.service` | `skchat webui --port 8765 --no-browser` | 8765  | `webui.py:153` | ~436 MB |
> | `skchat-daemon:v2` | `deploy/Dockerfile.daemon` | `skchat-daemon.service`        | `skchat daemon start --foreground --interval N` | 9385 (+9384) | `daemon.py:774` | ~399 MB |
> | `skchat-voice:v2`  | `deploy/Dockerfile.voice`  | (voice WS / ex-skvoice; webui's voice backend) | `skchat-voice` (`transports/serve_ws:main`) | 18800 | `transports/websocket.py:72` | ~759 MB |
>
> > **Voice note:** "voice" here is the in-package WebSocket voice engine
> > (`serve_ws` → `voice_engine`, ex-skvoice :18800) that the v2 stack composes
> > as the webui's voice backend. The `skchat-lumina-call.service` LiveKit
> > *conversational* agent runs `lumina-call.py` from the **separate
> > `lumina-creative` repo** (not part of the skchat package) and is therefore
> > NOT containerised by this skchat-repo B1 work — it belongs to a
> > lumina-creative image. The voice engine does all STT/TTS/LLM over HTTP to
> > external GPU services, so it needs **no torch** (the original
> > `Dockerfile.voice` torch install both bloated the image ~2 GB and broke the
> > build — fixed in B1).
>
> ### Build (all three, tagged :v2)
>
> ```bash
> cd <repo-root>          # build context = repo root for all three
> docker build -f deploy/Dockerfile.webui  -t skchat-webui:v2  .
> docker build -f deploy/Dockerfile.daemon -t skchat-daemon:v2 .
> docker build -f deploy/Dockerfile.voice  -t skchat-voice:v2  .
> ```
>
> **Sovereign sibling deps (NOT on public PyPI).** `skcomms` and `skmemory`
> are required by the daemon (receive loop + history) and the webui (pairing +
> history/FEB). They are installed from their git remotes, pinned via build
> ARGs so B2/CI can override to a vendored path, internal index, or pinned
> digest:
>
> ```bash
> docker build -f deploy/Dockerfile.daemon -t skchat-daemon:v2 \
>   --build-arg SKCOMMS_SPEC=git+https://github.com/smilinTux/skcomms.git@v0.1.6 \
>   --build-arg SKMEMORY_SPEC=git+https://github.com/smilinTux/skmemory.git@v0.10.4 .
> ```
>
> (voice needs neither — it's the leanest of the three minus torch's absence.)
>
> ### Smoke results (2026-06-22, all green)
>
> ```
> webui  /health → {"status":"ok","service":"skchat-webui","agent":"lumina",...}
> daemon /health → {"status":"ok","transport_ok":true,...}   (needs agent-dir volume)
> voice  /health → {"status":"ok","service":"skchat-voice"}
> ```
>
> ### ENV / ports each image expects (B2 composes these)
>
> **webui** (`:8765`, public via Traefik) — bind: `SKCHAT_HOST` (def 0.0.0.0),
> `SKCHAT_PORT` (8765). Identity: `SKAGENT` / `SKCHAT_IDENTITY`,
> `SKCAPSTONE_HOME` (def `/data/skcapstone`). Voice/LLM: `SKCHAT_STT_URL`,
> `SKCHAT_TTS_URL`, `SKCHAT_LLM_URL`, `SKCHAT_LLM_MODEL`, `SKCHAT_SKVOICE_URL`
> (`ws://voice:18800/ws/voice` on the internal overlay). LiveKit (**secrets**):
> `SKCHAT_LIVEKIT_URL/_API_KEY/_API_SECRET/_DEFAULT_ROOM`. TURN (**secret**):
> `SKCHAT_TURN_SECRET/_URLS/_STUN_URLS`. Persistent state: `$HOME/.skchat`
> (= `/home/skchat/.skchat`, pre-created) — **B2 should mount `skchat-data`
> there**; agent identity at `/data/skcapstone`.
>
> **daemon** (`:9385` health, `:9384` skcomms transport; internal only,
> **replicas MUST stay 1**) — `SKAGENT`, `SKCHAT_IDENTITY`,
> `SKCHAT_DAEMON_INTERVAL` (def 5), `SKCHAT_DAEMON_QUIET`,
> `SKCHAT_HOME=/data/skchat`, `SKCOMMS_HOME=/data/skcomms`,
> `SKCAPSTONE_HOME=/data/skcapstone`, plus `SKMEMORY_PG_*` for the skmem-pg
> backend. Writes `~/.skchat/{daemon.pid,daemon.lock,daemon.log}` under
> `/home/skchat/.skchat` (pre-created; not redirected by `SKCHAT_HOME`).
> Volumes: `/data/skchat`, `/data/skcomms`, `/data/capauth`,
> `/data/skcapstone` (agent dir — **required to boot**, supplies
> `~/.skcapstone/agents/<SKAGENT>/config/skmemory.yaml`).
>
> **voice** (`:18800`, internal only) — `SKCHAT_VOICE_HOST` (0.0.0.0),
> `SKCHAT_VOICE_PORT` (18800); `VoiceConfig.from_env()` reads `SKVOICE_*`
> (`SKVOICE_AGENT`, `SKVOICE_LLM_URL`, `SKVOICE_MODEL`, `SKVOICE_STT_URL`,
> `SKVOICE_TTS_URL`, `SKVOICE_TTS_VOICE`, `SKVOICE_STT_MIN_RMS`, …). Optional
> recordings volume at `/data/recordings`.
>
> ### What B2 needs to compose them
>
> - Build/load all three `:v2` images on the placement node(s), OR push to a
>   registry and set `SKCHAT_IMAGE_REGISTRY` + `SKCHAT_VERSION` in the stack.
>   `deploy/v2/skchat-stack.yml` already references
>   `${SKCHAT_IMAGE_REGISTRY:-skchat-webui|voice|daemon}:${SKCHAT_VERSION:-latest}`
>   — set `SKCHAT_VERSION=v2` (or retag) to use these.
> - Provide secrets via `--env-file /var/data/deploy_skchat/skchat.env` (B3).
> - Mount volumes per the stack's volume table: `skchat-data` →
>   webui `/home/skchat/.skchat` **and** daemon `/data/skchat` (note path
>   difference: webui uses `$HOME/.skchat`, daemon uses `SKCHAT_HOME`),
>   `skchat-identity` → `/data/capauth`, `skchat-skcapstone` →
>   `/data/skcapstone` (**daemon won't boot without a populated agent dir**),
>   `skchat-skcomms` → `/data/skcomms`, `skchat-recordings` → `/data/recordings`.
> - Keep `daemon` at `replicas: 1` (singleton inbox poll; lock at
>   `daemon.py:1216`).
> - `version` is reported as `0.0.0+unknown` inside the image because
>   `.dockerignore` excludes `.git` (setuptools-scm can't read tags). If a real
>   version string matters, pass `SETUPTOOLS_SCM_PRETEND_VERSION` as a build arg
>   or include `.git` — cosmetic only, does not affect function.

---

# skchat docker swarm deployment (legacy — skstack01 single-service)

Stack spec for `skchat-prod` running on the `skstack01` swarm cluster
(manager: `norap1001`).

## Files

- `skstack01-stack.yml` — single-service stack spec, reconstructed from
  the live `docker service inspect skchat-prod_skchat` output on
  2026-04-27. Replaces the orphan-state spec that previously lived only
  in Portainer's BoltDB.

## Public URL

<https://skchat.skstack01.douno.it> — Cloudflare DNS, Authentik-protected,
fronted by Traefik on the `cloud-public-prod` overlay network.

Default route `/` redirects to `/voice` (voice-chat.html).

## Redeploy

On the swarm manager:

```bash
ssh norap1001
cd /path/to/skchat/deploy
docker stack deploy -c skstack01-stack.yml skchat-prod
```

Or remotely from a workstation with docker-cli + DOCKER_HOST configured:

```bash
DOCKER_HOST=ssh://norap1001 \
  docker stack deploy -c skstack01-stack.yml skchat-prod
```

## Image distribution

`skchat:0.3.1` is a locally-built image — there is no registry push.
The image must exist on the placement-target node (`norap1001`) before
deploy, or be pre-loaded:

```bash
docker save skchat:0.3.1 | ssh norap1001 docker load
```

The placement constraint `node.hostname==norap1001` keeps the service
pinned to the node that has the image. If you want to allow other nodes
to schedule the task, push to a registry first and update the image
field accordingly.

## External dependencies

The service does not own any state. It is a thin web frontend that talks
to upstream services via their LAN endpoints:

| Env var | Endpoint | What it is |
|---|---|---|
| `SKCHAT_TTS_URL` | `http://127.0.0.1:18793/audio/speech` | VoxCPM TTS |
| `SKCHAT_STT_URL` | `http://127.0.0.1:18794/v1/audio/transcriptions` | faster-whisper STT |
| `SKCHAT_LLM_URL` | `http://127.0.0.1:11434/v1/chat/completions` | Ollama (qwen3.5:9b) |
| `SKCHAT_VOICE_LLM_URL` | `http://gateway:18795/voice-llm` | gateway @ 192.168.0.158 |
| `SKCHAT_SKVOICE_URL` | `ws://192.168.0.158:18800/ws/voice` | skvoice WS |

`gpu100` and `gateway` are resolved via swarm `extra_hosts` entries to
127.0.0.1 and 192.168.0.158 respectively.

## Network

`cloud-public-prod` is an external overlay network shared by every
public-facing stack on `skstack01`. It is **not** created by this stack
— do not remove the `external: true` flag.

## What is NOT captured

- **Volumes** — none; service is stateless.
- **Configs** — none defined.
- **Secrets** — none referenced (all env vars are LAN URLs / model
  names; no credentials inline, so no `.env.example` is needed).
- **Healthcheck** — none defined on the service spec (the image may
  have a `HEALTHCHECK` directive in the Dockerfile, but that is image
  state, not stack state).
- **Image registry digest** — no digest pin; the image is a local
  build, see "Image distribution" above.
