# skchat v2 — Docker Swarm deployment guide

## Prerequisites

1. **Docker Swarm** initialised (`docker swarm init` on the manager node).
2. **External overlay network** created once per cluster:
   ```bash
   docker network create --driver overlay --attachable cloud-public-prod
   ```
3. **Tailscale** installed on each Swarm worker node that will run skchat services.
4. **Tailscale serve** configured on the webui node (and optionally on the daemon node):
   ```bash
   # Expose webui over the tailnet (HTTPS, auto-TLS from Tailscale's MagicDNS cert)
   tailscale serve --bg https+insecure://localhost:8765
   # → reachable at https://noroc2027.tail204f0c.ts.net  (replace with your node's name)

   # Optional: expose daemon health over the tailnet for monitoring
   tailscale serve --bg --https 9385 https+insecure://localhost:9385
   ```
   voice (:18800) is internal-overlay-only and is NOT served via Tailscale.
5. **Images built and pushed** to your registry:
   ```bash
   TAG=v0.2.0
   REGISTRY=registry.skworld.io/skchat
   docker build -f deploy/Dockerfile.webui  -t ${REGISTRY}-webui:${TAG}  .
   docker build -f deploy/Dockerfile.voice  -t ${REGISTRY}-voice:${TAG}  .
   docker build -f deploy/Dockerfile.daemon -t ${REGISTRY}-daemon:${TAG} .
   docker push ${REGISTRY}-webui:${TAG}
   docker push ${REGISTRY}-voice:${TAG}
   docker push ${REGISTRY}-daemon:${TAG}
   ```
6. **Host env file** created from the template and populated:
   ```bash
   sudo mkdir -p /var/data/deploy_skchat
   sudo cp deploy/v2/skchat-prod.env.example /var/data/deploy_skchat/skchat.env
   sudo chmod 600 /var/data/deploy_skchat/skchat.env
   sudo $EDITOR /var/data/deploy_skchat/skchat.env    # fill in every required value
   ```
   Secrets (marked `SECRET`) should be injected from OpenBao in prod — see `deploy/SECRETS.md`.
7. **Media plane** (LiveKit + coturn) already deployed if WebRTC calls are needed:
   - `deploy/v2/livekit-stack.yml` (B4)
   - `deploy/v2/coturn-stack.yml` (B4)

## Deploy

```bash
# Source the env file so variable expansion works for the command field
set -a && source /var/data/deploy_skchat/skchat.env && set +a

docker stack deploy \
  --env-file /var/data/deploy_skchat/skchat.env \
  -c deploy/v2/skchat-stack.yml \
  skchat
```

Verify services came up:
```bash
docker stack ps skchat
docker service ls --filter label=com.docker.stack.namespace=skchat
```

## Update (rolling)

```bash
# After pushing a new image tag:
SKCHAT_VERSION=v0.2.1
docker service update --image registry.skworld.io/skchat-webui:${SKCHAT_VERSION}  skchat_webui
docker service update --image registry.skworld.io/skchat-voice:${SKCHAT_VERSION}  skchat_voice
docker service update --image registry.skworld.io/skchat-daemon:${SKCHAT_VERSION} skchat_daemon
```

Or redeploy the full stack (re-reads the stack file + env):
```bash
set -a && source /var/data/deploy_skchat/skchat.env && set +a
docker stack deploy --env-file /var/data/deploy_skchat/skchat.env \
  -c deploy/v2/skchat-stack.yml skchat
```

## Health checks

```bash
# From the Swarm manager (tailnet access assumed):
curl http://<node-tailnet-ip>:8765/health    # webui
curl http://<node-tailnet-ip>:9385/health    # daemon
curl http://<node-tailnet-ip>:18800/health   # voice

# Or via tailscale serve (webui only, after step 4 above):
curl https://noroc2027.tail204f0c.ts.net/health
```

## Teardown

```bash
docker stack rm skchat
# Named volumes persist by default — remove manually only if wiping state:
# docker volume rm skchat_skchat-data skchat_skchat-skcomms skchat_skchat-identity skchat_skchat-recordings
```

## Reachability summary

| Service | Internal overlay (service-to-service) | Tailnet (direct) | Public (Traefik, opt-in) |
|---------|--------------------------------------|------------------|--------------------------|
| webui   | `http://webui:8765` | `https://<node>.ts.net` via `tailscale serve` | `https://skchat.skstack01.douno.it` (Traefik labels) |
| voice   | `ws://voice:18800/ws/voice` (webui→voice) | Not served | Not exposed |
| daemon  | `http://daemon:9385/health` (internal) | Optional `tailscale serve` on :9385 | Not exposed |

To make fully private (tailnet-only, no Cloudflare ingress): remove or set
`traefik.enable: "false"` on the webui service deploy labels.

## Singleton daemon constraint

The daemon MUST run as a single replica.  Two daemon instances polling the same
SKComms inbox cause duplicate message deliveries.  The `run_daemon()` function
holds a process-level lock, but the real enforcement is Swarm's `replicas: 1`.
For hard pinning to a single node, add a node label and uncomment the constraint
in `skchat-stack.yml`:
```bash
docker node update --label-add skchat.daemon=true <node-id>
# then uncomment in skchat-stack.yml:
#   - node.labels.skchat.daemon == true
```

## Secret management

See `deploy/SECRETS.md` for:
- Full secret inventory and OpenBao path mapping
- TURN credential derivation model
- LiveKit key rotation procedure
- What must NEVER be committed to git
