# skchat docker swarm deployment

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
| `SKCHAT_TTS_URL` | `http://192.168.0.100:18793/audio/speech` | VoxCPM TTS |
| `SKCHAT_STT_URL` | `http://192.168.0.100:18794/v1/audio/transcriptions` | faster-whisper STT |
| `SKCHAT_LLM_URL` | `http://192.168.0.100:11434/v1/chat/completions` | Ollama (qwen3.5:9b) |
| `SKCHAT_VOICE_LLM_URL` | `http://gateway:18795/voice-llm` | gateway @ 192.168.0.158 |
| `SKCHAT_SKVOICE_URL` | `ws://192.168.0.158:18800/ws/voice` | skvoice WS |

`gpu100` and `gateway` are resolved via swarm `extra_hosts` entries to
192.168.0.100 and 192.168.0.158 respectively.

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
