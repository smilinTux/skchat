# skreach — Security, RBAC & Audit Specification (F1)

**Date:** 2026-06-13  
**Author:** sentinel agent (security pass)  
**Status:** DRAFT — gates all skreach exec work (Batches F2–F4)  
**Reads:** `2026-06-12-skchat-architecture-reassessment.md` §2.6  
**Depends on:** `2026-06-13-identity-roles-access.md` (P0 identity/roles model — reused, not duplicated)  
**Supersedes:** nothing (greenfield)  
**Feeds into:** F2 (skreachd MVP), F3 (terminal lane), F4 (trustee wrap), MCP tool (agent-drives-agent)

---

## 0. Scope & Gating Statement

skreach is a sovereign remote-control plane: run commands, stream stdout, perform
file operations, and drive deployment ops on fleet nodes and agents over the mesh.
**Remote exec is the highest-value attack target in the entire SKWorld stack.**

This spec is the **gate** for all exec capability. No code in F2–F4 ships until
every acceptance criterion in §8 is met and Chef has reviewed this document. The
implementation plan in §6 is blocked until that review completes.

This spec does **not** duplicate the P0 identity/roles model. It reuses it. Where
this spec says "Tier 1 member", "operator", "guest", "FQID", "capauth", or
"`resolve_speaker_role()`", those terms carry the definitions and implementations
from `2026-06-13-identity-roles-access.md` verbatim.

---

## 1. Trust & Identity: Signed Commands

### 1.1 The invariant

**Every skreach action is a capauth/skos-signed command envelope. skreachd MUST
NOT execute any command that is not accompanied by a valid, fresh, addressed
signature from a known principal. There is no unsigned execution path. There are
no exceptions.**

This is the same signature gate that `/call/incoming` uses today: envelopes
without `_verify.valid == True` are silently dropped. skreach uses the same
mechanism at the exec boundary.

### 1.2 Command envelope schema

```json
{
  "type": "SKREACH_CMD",
  "v":    1,
  "id":   "<random 128-bit hex>",        // command ID (idempotency + audit key)
  "iss":  "<issuer_fqid>",               // signer's capauth FQID
  "sub":  "<target_node_fqid>",          // which skreachd this is addressed to
  "iat":  1718270400,                    // Unix seconds; issued-at
  "exp":  1718270700,                    // Unix seconds; iat + TTL (max 300s)
  "cmd": {
    "class":  "exec",                    // see §2.1 command classes
    "op":     "run",                     // specific operation
    "args":   ["<arg0>", "<arg1>"],      // argv; NO shell interpolation
    "env":    {},                        // additional env vars (scrubbed by daemon)
    "cwd":    "/opt/skworld/app",        // working directory; must be in allowed_cwd
    "stdin":  null                       // optional base64 stdin blob (max 64 KB)
  },
  "confirm_token": null                  // null for normal; required for destructive ops (§3.3)
}
```

The envelope is serialized to JSON, then signed using the issuer's capauth PGP
key (same `sign_message()` / `verify_message()` path as skcomms envelopes). The
PGP signature is the outer wrapper; the JSON above is the cleartext signed body.

### 1.3 Signature verification at skreachd

On receipt of a command envelope (via the WebRTC data channel or direct
skcomms mailbox delivery), skreachd MUST:

1. **Parse the outer PGP wrapper** — call `capauth.verify_message(envelope)`. If
   `verify.valid != True`, drop the envelope immediately, log `[WARN] sig_invalid
   id=<partial-id> iss=<claimed-iss>`, and return without processing.

2. **Verify `sub` == self** — if the `sub` FQID does not match this node's
   registered FQID, drop with `[WARN] misdirected_cmd`. This prevents a stolen
   signed command from being replayed against a different node.

3. **Verify `exp`** — if `now > exp`, drop with `[WARN] expired_cmd id=...
   age=<seconds_past_exp>s`. Reject with zero tolerance; clock skew allowance is
   at most 5 seconds (configured via `SKREACH_MAX_CLOCK_SKEW_S`, default 5).

4. **Check replay** — query the local replay cache (in-memory LRU, 10-minute
   window; keys = `cmd.id`). If the `id` is present, drop with `[WARN]
   replay_cmd id=...`. On first sight, insert `id → exp` into the cache.

5. **Resolve issuer role** — call `resolve_speaker_role(env.iss)` (from the P0
   identity spec). This yields `operator | member | agent | guest`. If the
   resolved role is `guest` or the FQID is unknown, drop with `[WARN]
   unauthorized_iss id=... iss=...`.

6. **RBAC check** — evaluate `_reach_authorized(cmd.class, cmd.op, role,
   target_node)` (§2). If denied, drop with `[WARN] rbac_denied` and emit an
   audit record (§4).

7. **Allowlist check** — if the target node has a command allowlist configured
   (§3.4), verify the resolved `argv[0]` (the binary name, canonicalized) is in
   the allowlist. If not, drop with `[WARN] allowlist_denied`.

8. **Destructive confirm check** — if `cmd.class` is `destructive`, verify the
   `confirm_token` (§3.3). If missing or invalid, reject with a prompt
   response (not silent drop; the caller needs to know a second confirm is
   required).

9. **Execute** in sandbox (§5). Emit audit record (§4) for every executed command
   regardless of outcome.

### 1.4 Reuse of the call-ring signature gate

The implementation in `call_routes.py` lines 150–152 already demonstrates the
pattern:

```python
for env, _verify in _read_inbox():
    if not getattr(_verify, "valid", False):
        continue  # drop unsigned/invalid-signature envelopes
```

skreachd's command loop is the same pattern, extended with the freshness and
replay checks above. The PGP verify path (`capauth.verify_message`) and the
skcomms envelope transport are shared code — no new crypto primitives.

---

## 2. RBAC Tiers

### 2.1 Command classes

skreach commands are grouped into classes by risk level. The class determines
the minimum role required.

| Class | Operations | Min role | Notes |
|---|---|---|---|
| `status` | node health, process list, resource usage | `member` | Read-only, no side effects |
| `log_read` | tail/fetch log files | `member` | Read-only; path-scoped |
| `file_read` | read file content, list directory | `member` | Path-scoped; no traversal |
| `file_write` | write/append file, create directory | `operator` | Destructive-flagged if overwrite |
| `exec` | run an arbitrary command | `operator` | Subject to allowlist + sandbox |
| `deploy` | delegate to `trustee_*` / `run_ansible_playbook` | `operator` | ITIL change wraps all deploys |
| `destructive` | `stop`, `scale-down`, `rm`, `restart`, `drain` | `operator` + confirm | Requires second signed confirm (§3.3) |
| `owner` | node registration/deregistration, skreachd config reload, key rotation | `owner` only | Chef only; not delegatable |

### 2.2 Role mapping (reuse from P0 identity spec)

| Role | Allowed classes | Notes |
|---|---|---|
| `owner` (Chef) | All | No restrictions beyond §3 per-command rules |
| `operator` (Chef today; config-expandable) | `status`, `log_read`, `file_read`, `file_write`, `exec`, `deploy`, `destructive` | Must confirm destructive; subject to node allowlist |
| `member` (Tier-1 capauth peer) | `status`, `log_read`, `file_read` | Read-only; no exec |
| `agent` (Lumina, Opus, etc.) | `status`, `log_read` (scoped) | See §2.3 for agent exec path |
| `guest` | **none** | No skreach access whatsoever |

The `owner` role maps to Chef's FQID (`chef@skworld.io`). The operator set is
configured at `SKREACH_OPERATOR_FQIDS` (comma-separated list; same pattern as
`SKCHAT_OPERATOR_FQIDS` from the P0 spec). No principal may self-elevate at
runtime.

### 2.3 Agent exec path

Agents (Lumina, Opus, Jarvis) operate under their capauth FQIDs (role = `agent`).
By default, agents are restricted to `status` and scoped `log_read`. This is
intentional: an agent whose session is hijacked or whose LLM output is adversarially
manipulated must not be able to run arbitrary commands.

Agents may be granted `exec` access via an **explicit per-node grant**:

```yaml
# skreach node config: per-node grants
grants:
  - fqid: lumina@chef.skworld.io
    classes: [exec]
    allowlist: [skcapstone, skchat, skingest]   # binary names only
    require_confirm: true                         # always require confirm for agent exec
```

Agent grants MUST:
- Be scoped to a specific allowlist (no open exec for agents).
- Require the confirm-on-destructive gate (§3.3) for any op, regardless of
  class — agents must always present a second signed confirm to exec.
- Be configured by an operator or owner, never self-granted.

### 2.4 Per-node scoping

RBAC is evaluated per-node, not fleet-wide. A principal with `exec` rights on
`noroc2027` does not automatically have `exec` rights on `chiap04`. Node
capabilities are configured in each skreachd's node config (`skreach-node.yaml`).
Fleet-wide grants (via a policy file under OpenBao) are a future P1 feature.

---

## 3. Per-Command Authorization

### 3.1 The authorization check

```python
def _reach_authorized(
    cmd_class: str,
    op: str,
    role: str,
    node_config: NodeConfig,
) -> bool:
    """Return True iff this role may issue cmd_class/op on this node."""
    # Guest: never
    if role == "guest":
        return False

    # Owner: always (subject to §3.3 destructive confirm at execution time)
    if role == "owner":
        return True

    # Operator: all classes except owner-only
    if role == "operator":
        return cmd_class != "owner"

    # Agent: status + scoped log_read only (unless node grants extra)
    if role == "agent":
        base_ok = cmd_class in ("status", "log_read")
        grant_ok = node_config.agent_has_grant(role_fqid, cmd_class)
        return base_ok or grant_ok

    # Member: read-only classes
    if role == "member":
        return cmd_class in ("status", "log_read", "file_read")

    return False
```

### 3.2 Allow/deny model

skreachd evaluates commands against the following ordered policy:

1. **Default-deny.** Everything is denied unless explicitly permitted.
2. **Role floor.** Apply §2.2 role-class table.
3. **Node allowlist.** If a per-node `command_allowlist` is configured, the
   resolved binary (`argv[0]` canonicalized to its basename, following no symlinks)
   MUST appear in the allowlist. The allowlist is a whitelist of binary names (not
   full paths) to prevent allowlist bypass via path manipulation.
4. **Denylist.** The `command_denylist` (§5.5) is evaluated after the allowlist and
   acts as an override — an explicitly denied command is refused regardless of role.
5. **Destructive confirm gate** (§3.3). Evaluated last; only relevant when the
   above pass.

### 3.3 Confirm-on-destructive

Destructive operations (class `destructive` + high-risk ops in class `exec`: any
invocation of `rm`, `kill`, `pkill`, `docker rm`, `docker stop`, `systemctl stop`,
`systemctl disable`, `kubectl delete`, `scale-down`) require a **second signed
confirm** before execution.

The flow:

1. skreachd receives a command. It passes role + allowlist checks but the op is
   classified as destructive.
2. skreachd does NOT execute. It returns a `SKREACH_CONFIRM_REQUIRED` response:
   ```json
   {
     "type": "SKREACH_CONFIRM_REQUIRED",
     "cmd_id": "<original_cmd.id>",
     "summary": "rm -rf /var/data/skmem_pgdata — irreversible data deletion",
     "expires_at": 1718270760
   }
   ```
3. The caller (operator or MCP tool) presents this response to the user/agent with
   a clear summary. The summary is generated by skreachd from the argv, NOT from
   the original command text (prevents summary injection).
4. The operator/owner issues a **second signed command envelope** of type
   `SKREACH_CONFIRM` referencing the original `cmd_id`:
   ```json
   {
     "type": "SKREACH_CONFIRM",
     "v": 1,
     "id": "<new_confirm_id>",
     "iss": "<issuer_fqid>",
     "sub": "<target_node_fqid>",
     "iat": ..., "exp": ...,
     "confirms": "<original_cmd.id>"
   }
   ```
   The confirm envelope is separately signed and carries its own freshness
   (`exp = iat + 60s`, tight window — the operator is expected to confirm
   immediately or not at all).
5. skreachd verifies the confirm signature (same §1.3 flow), checks `confirms`
   matches the pending `cmd_id`, verifies the confirm arrived within the confirm
   expiry window, and then executes.

**The confirm MUST be signed by the same issuer as the original command.**
A confirm from a different principal is refused.

**For agent exec**: agents always require the confirm gate, even for non-destructive
ops, per §2.3.

### 3.4 Per-node command allowlists

Each skreachd instance is configured with a node-level policy file
(`skreach-node.yaml`) that specifies:

```yaml
node_fqid: noroc2027@chef.skworld.io

# Binary names permitted for exec/deploy class commands (basename only)
command_allowlist:
  - skcapstone
  - skchat
  - skingest
  - skos
  - ansible-playbook
  - docker
  - systemctl

# Binary names always denied, regardless of role or allowlist (§5.5)
command_denylist:
  - bash
  - sh
  - zsh
  - python
  - python3
  - perl
  - ruby
  - node
  - nc
  - netcat
  - curl
  - wget
  - socat
  - tee
  - dd

# Working directory allowlist — exec cwd must be under one of these prefixes
allowed_cwd:
  - /opt/skworld
  - /home/cbrd21/clawd

# Per-FQID grants (see §2.3)
grants: []
```

The policy file is loaded at skreachd startup and on `owner`-class config reload.
It is not hot-reloadable by operator-class commands (prevents privilege escalation
via config manipulation).

---

## 4. Audit Log

### 4.1 Every exec is audited

skreachd emits a structured audit record for **every** command that reaches the
execution stage (whether executed, rejected-by-RBAC, rejected-by-allowlist,
required-confirm, or errored). Records are written to the immutable audit store
**before** the command begins executing (for exec/deploy commands) and again when
the command completes (with outcome + exit code + truncated stdout/stderr).

### 4.2 Audit record schema

```json
{
  "audit_id":   "<random 128-bit hex>",
  "cmd_id":     "<envelope.cmd.id>",
  "node_fqid":  "<this node's FQID>",
  "iss_fqid":   "<issuer FQID>",
  "role":       "operator|member|agent|owner",
  "cmd_class":  "exec|deploy|destructive|...",
  "op":         "run|stop|...",
  "argv":       ["<arg0>", "<arg1>"],       // full argv as presented (pre-sandbox)
  "cwd":        "/opt/skworld/app",
  "env_keys":   ["SKAGENT", "HOME"],        // env key names only; never values
  "outcome":    "executed|rbac_denied|sig_invalid|expired|replay|allowlist_denied|confirm_required|confirm_rejected|error",
  "exit_code":  0,                          // null if not executed
  "stdout_sha": "<sha256 of full stdout>",  // hash only; full output in stdout_store
  "stderr_sha": "<sha256 of full stderr>",
  "started_at": 1718270401.234,
  "ended_at":   1718270402.567,
  "duration_ms": 1333
}
```

The `env_keys` field logs only key names, never values — environment variables
routinely contain secrets; the full env must never be persisted.

### 4.3 Immutability

Audit records are written to:

1. **skmem-pg `skreach_audit` table** — append-only; no `UPDATE` or `DELETE` path
   is provided to the skreachd process. The database role used by skreachd has
   `INSERT` on `skreach_audit` and `SELECT` only; no `UPDATE`, `DELETE`, or
   `TRUNCATE` grants. Schema:
   ```sql
   CREATE TABLE skreach_audit (
     audit_id    TEXT PRIMARY KEY,
     recorded_at TIMESTAMPTZ NOT NULL DEFAULT now(),
     record      JSONB NOT NULL
   );
   -- No DELETE policy; rows are permanent.
   -- Partitioned by month for retention management (operator drops partitions, not rows).
   ```
   Stdout/stderr blobs are stored in `skreach_stdout_store (cmd_id, sha256, data)`
   with a configurable retention (default 30 days; after that the blob is deleted
   but the audit record referencing the `sha256` hash is permanent).

2. **Local audit log file** (`~/.skcapstone/agents/<node>/skreach/audit.jsonl`) —
   append-only JSONL. This is the offline/disaster-recovery copy. The file is not
   truncated by skreachd. Rotation (by logrotate, `copytruncate=false`) creates
   dated archive files, never deletes the current file.

3. **ITIL integration** — every `exec`, `deploy`, and `destructive` command emits
   an ITIL change or incident record via `itil_change_propose()` or
   `itil_incident_create()` as appropriate:
   - Planned deploy ops → `itil_change_propose` (RFC) with `cmd_id` as external
     reference.
   - Interactive exec → `itil_incident_create` with `type=change_record` (a
     non-incident record of ad-hoc ops).
   - Failed destructive ops (confirm rejected, exit code != 0) →
     `itil_incident_create` with `severity=medium`.
   The ITIL record links back to `audit_id` so the full exec trace is reachable
   from the change management view.

### 4.4 Audit review & replay

- **CLI review:** `skcapstone skreach audit --node <fqid> [--since <iso>] [--iss <fqid>] [--outcome executed|denied|...]`
- **MCP tool:** `skreach_audit_query(node, since, iss, outcome)` returns paginated
  audit records.
- **Stdout replay:** `skcapstone skreach replay <cmd_id>` fetches the stdout blob
  by sha256 (if within retention window) and streams it.
- **Tamper detection:** The local JSONL and the skmem-pg record must agree on
  `audit_id` + `record` hash. If they diverge (manual edit of the JSONL), a
  `skcapstone skreach audit --verify` run flags the discrepancy. (P1: add a
  Merkle-chain linking consecutive records for strong tamper evidence.)

---

## 5. Exec Sandbox

### 5.1 User / UID isolation

skreachd runs as a dedicated non-root user (`skreach`, UID ≥ 1000, no sudo
rights, no wheel/sudoers membership). Commands are executed as the same
`skreach` user unless a **per-node `run_as` config** explicitly maps specific
allowlisted commands to a different uid (e.g. `docker` → `cbrd21` if Docker
socket access is needed). The `skreach` user has:

- No shell (`/usr/sbin/nologin`).
- Home dir `~/.skcapstone/agents/<node>/skreach/` (not `/home/skreach`).
- No access to other agents' memory directories.
- Read access to `/opt/skworld` deploy paths; no write access by default (write
  is granted to specific subdirectories by the operator during provisioning).

Commands are **never** run as root. If a command requires root, the correct path
is a systemd unit that can be triggered by the `skreach` user via a tightly
scoped `sudo` rule (specific command + `NOPASSWD`), with the sudo rule itself
reviewed and approved by the owner. Blanket `sudo` is not permitted.

### 5.2 Working directory

The `cwd` field in the command envelope is validated against the node's
`allowed_cwd` prefix list before execution. If the requested `cwd`:

- Does not exist → execution rejected (`outcome=error`, logged).
- Exists but is outside all `allowed_cwd` prefixes → `outcome=allowlist_denied`.
- Is a symlink → the symlink is resolved and the real path is re-checked against
  `allowed_cwd` (prevents `cwd=/safe/path/../../etc` symlink escape).

### 5.3 Environment scrubbing

Before spawning the subprocess, skreachd constructs the child environment from
scratch (does not inherit its own environment). The child env starts empty and
receives only:

```
PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin
HOME=<skreach_home>
SKAGENT=<node_agent_name>
SKREACH_CMD_ID=<cmd.id>       # for process self-identification in logs
```

Any additional `env` keys from the command envelope are then merged in. But the
following keys are **always stripped** from the command envelope's `env`, even if
explicitly provided:

```
SKAGENT_SECRET, SKREACH_NODE_KEY, SKCHAT_GUEST_TOKEN_SECRET, LIVEKIT_API_SECRET,
SKCHAT_TURN_SECRET, DATABASE_URL, POSTGRES_PASSWORD, OPENAI_API_KEY, ANTHROPIC_API_KEY,
AWS_*, GITHUB_TOKEN, FORGEJO_*, NEXTCLOUD_*, SKCAPSTONE_*TOKEN*, *PASSWORD*, *SECRET*,
*KEY*, *CREDENTIAL*
```

The strip list is a regex applied to key names (case-insensitive). Any key name
matching `.*(?:key|secret|token|password|credential).*` is stripped. The audit
log records only the key names (not values) of env keys that were provided and
stripped (for debugging without leaking secrets).

### 5.4 Resource limits

Each subprocess is spawned with hard resource limits (via `resource.setrlimit`
before exec, or a systemd scope if using `systemd-run`):

| Resource | Default limit | Configurable per-node |
|---|---|---|
| CPU time (`RLIMIT_CPU`) | 300 seconds | Yes |
| Wall-clock timeout | 600 seconds | Yes |
| Max open files (`RLIMIT_NOFILE`) | 256 | Yes |
| Max file size (`RLIMIT_FSIZE`) | 512 MB | Yes |
| Max virtual memory (`RLIMIT_AS`) | 2 GB | Yes |
| Max processes/threads (`RLIMIT_NPROC`) | 64 | Yes |

A wall-clock watchdog (separate thread) kills the subprocess group with `SIGKILL`
after the wall-clock timeout. The timeout is logged as `outcome=timeout` and an
ITIL incident is raised.

For streaming commands (e.g. `tail -f`, long-running builds), the issuer sets
`cmd.stream=true` and the wall-clock timeout is extended to `SKREACH_MAX_STREAM_S`
(default 3600s). Streaming commands carry the same RBAC and audit requirements.

### 5.5 Denied command list

The following commands/binaries are **always denied** regardless of role,
allowlist, or node policy. This list is hardcoded in skreachd (not configurable
by node policy, to prevent the owner config being used to unlock them):

```
bash, sh, zsh, fish, dash,          # any interactive shell
python, python2, python3, pypy,     # script interpreters
ruby, perl, lua, node, nodejs,      # more interpreters
nc, ncat, netcat, socat,            # raw network tools
curl, wget, fetch,                   # arbitrary HTTP fetch
tee, dd,                            # raw I/O (used in shell injection chains)
chmod, chown, chgrp,                # permission escalation
mount, umount,                       # filesystem ops
```

**Shell interpretation is explicitly forbidden.** Commands are always passed as
`argv` arrays, never as strings to a shell. The exec call is:

```python
subprocess.Popen(
    args=cmd.args,          # list[str]; NO shell=True
    cwd=validated_cwd,
    env=scrubbed_env,
    shell=False,            # NEVER True
    ...
)
```

`shell=True` is prohibited in the codebase and is a failing test assertion.

**No-shell injection guarantee:** Because `shell=False` + argv list is enforced,
shell metacharacters in arguments (`; | & > < $() `` `) are passed as literal
strings to the process, not interpreted by a shell. A command like
`["ls", "-la", "; rm -rf /"]` runs `ls` with the argument `-la` and the literal
string `; rm -rf /` — harmless.

### 5.6 Blast-radius boundary

In the worst case — a fully compromised `operator` identity + a valid signed
envelope — an attacker can:

- Run any binary in the `command_allowlist`.
- Within the `allowed_cwd` subtrees.
- As the `skreach` user (not root).
- Subject to resource limits.
- With stdout/stderr captured and audited.

They **cannot**:
- Run a shell or interpreter.
- Access secrets in the environment.
- Write outside `allowed_cwd`.
- Escalate to root.
- Affect other nodes (the signed envelope is node-scoped).
- Cover their tracks (audit is written before exec and is append-only).

---

## 6. Network Posture

### 6.1 Tailscale-only default

skreachd exposes no inbound TCP ports. By default, it connects outbound to the
skcomms hub (over the tailnet) and receives command envelopes via the skcomms
mailbox poll loop or a WebRTC data channel. There is no SSH-like listener.

Transport options (same pluggable underlay as skchat §2.6):

| Transport | Status | Notes |
|---|---|---|
| **tsnet (Tailscale)** | Default (P0) | Sovereign, encrypted, mutual-auth; no inbound ports required |
| **NetBird (WireGuard + SSO)** | Tier-2 option | Alternative mesh for non-Tailscale nodes |
| **Cloudflare Tunnel** | Opt-in only | Public-facing nodes only; requires additional gating (§6.2) |
| Raw TCP/UDP | Never | Not implemented; not planned |
| Plain HTTP | Never | All transport is TLS or WireGuard-encrypted |

### 6.2 Public opt-in gating

If a node operator elects to enable public (non-tailnet) access:

1. `SKREACH_PUBLIC_ENABLED=1` must be explicitly set in the node config.
2. The transport MUST be Cloudflare Tunnel or Tailscale Funnel (no raw inbound
   port exposure).
3. The signature verification requirements (§1.3) remain identical — the transport
   encryption does not substitute for application-layer auth.
4. Rate limiting (IP-based, 10 command envelopes / 60s) is applied at the public
   ingress before any processing.
5. Guest-class issuers are rejected immediately at the transport layer (before
   signature verification) for public endpoints.
6. `SKREACH_PUBLIC_ALLOWED_CLASSES` restricts which command classes are reachable
   via the public path (recommended: `status` only; `exec`/`deploy` require
   tailnet).

### 6.3 Transport-adapter trust

Transport encryption (Tailscale WireGuard, Cloudflare Tunnel TLS) provides
**channel integrity** but is not the primary trust mechanism. The application-layer
PGP signature (§1) is the trust root. This means:

- A compromised Tailscale coordination server does not yield exec capability
  (attacker still cannot forge a PGP-signed envelope).
- A MITM on the Cloudflare Tunnel terminates at the transport layer but the
  application-layer signature would still be invalid.
- Rotating capauth PGP keys (§7.5) revokes all in-flight exec authority
  immediately, regardless of transport session state.

---

## 7. Threat Model

### 7.1 Stolen `guest` or `member` token

**Threat:** An attacker obtains a valid Tier-3 guest JWT or a Tier-1 member's
capauth session credentials.

**Stolen guest token:**
- Guest class has zero skreach access (§2.2). skreachd drops all envelopes from
  guest-role issuers at step 5 of §1.3.
- Blast radius: none for skreach specifically. Exploit limited to skchat
  permissions (see P0 identity spec §5.1).

**Stolen member session credentials (not the signing key):**
- If the attacker cannot sign envelopes (they have the session cookie but not the
  PGP private key), all skreach commands are rejected at signature verification
  (§1.3 step 1).
- If the attacker also has the private key (full key material theft): blast radius
  is bounded by the member's role (`status`, `log_read`, `file_read` only — no
  exec). The attack surface is read-only.
- **Mitigation:** Key theft is detected when the legitimate key holder next
  connects and observes unexpected audit records (`skcapstone skreach audit
  --iss <fqid> --since <theft_window>`). Rotation via `owner`-class
  `SKREACH_CMD/class=owner/op=rotate_key` immediately revokes the stolen key.

### 7.2 Replay attack

**Threat:** An attacker captures a valid, recently-signed command envelope and
replays it (same command, same signature, same `id`).

**Mitigations:**
- `exp` field: the issuer sets `exp = iat + TTL` (max 300 seconds, §1.3 step 3).
  A captured envelope is useless after 5 minutes.
- Replay cache (§1.3 step 4): the same `cmd.id` is rejected on second sight within
  the 10-minute cache window. Even within the `exp` window, replay is blocked.
- Together: an attacker must replay within both the `exp` window AND before the
  `id` is seen again (which it won't be, since `id` is random 128-bit per command).
  This combination makes replay computationally equivalent to forging a new command.

### 7.3 Privilege escalation

**Threat:** A member or agent tries to call an `exec` or `operator` command
class.

**Mitigations:**
- Role is resolved server-side from the issuer's FQID against the local peer store
  (same `resolve_speaker_role()` from P0 spec). The issuer cannot claim a higher
  role in the envelope; the claim in the envelope (if any) is ignored and the
  server-resolved role is used exclusively.
- The authorization check (§3.1) is a hard gate before any execution.
- No role claims in the envelope are trusted — only the verified signing key maps
  to a role.

**Lateral escalation via agent exec grants:**
- Agent exec grants require a separate per-node config change (§2.3), which itself
  requires an `owner`-class command. No agent can grant itself exec rights.
- Even with an exec grant, the agent's allowlist is narrow and the confirm gate
  is always active.

### 7.4 skreachd daemon compromise

**Threat:** An attacker gains code execution on the host running skreachd (e.g.
via a vulnerability in a skreachd dependency or a supply-chain attack on the
Python package) and can now issue commands without going through the signature gate.

**Mitigations:**
- The `skreach` user has no root access and minimal filesystem permissions (§5.1).
  A compromised skreachd process is contained within the `skreach` user's
  blast radius.
- The audit log (skmem-pg + JSONL) is written by the skreachd process itself, so
  a compromised daemon could omit audit records. Mitigation: skmem-pg is a
  separate process; the `skreach` db role is INSERT-only. A compromised daemon
  cannot delete or modify existing audit records.
- An owner-driven integrity check (`skcapstone skreach --verify-daemon
  --node <fqid>`) compares the running skreachd binary hash against the
  expected hash in the node registry (P1: add to node heartbeat).
- systemd service hardening for the `skreachd.service` unit:
  ```ini
  PrivateTmp=true
  PrivateDevices=true
  ProtectSystem=strict
  ProtectHome=read-only
  NoNewPrivileges=true
  CapabilityBoundingSet=
  RestrictSUIDSGID=true
  ```

### 7.5 Supply-chain attack on the skreachd package

**Threat:** A malicious version of the `skchat` / `skreachd` package is pushed
to the package registry and deployed.

**Mitigations:**
- Package is installed from the **private Forgejo registry** (not PyPI), requiring
  authentication.
- Deployment uses a **pinned SHA256 hash** in the stack YAML / Dockerfile
  (`--hash=sha256:...` pip flag). A changed package will fail the hash check and
  the deploy will fail.
- The Docker image is built in CI (Forgejo Actions) with a reproducible build
  (pinned base image digest + pip hash verification). The resulting image is
  signed (cosign) and the signature is verified at deploy time.
- P1: SBOM generation + Forgejo Dependency Review action flags new transitive
  dependencies.

### 7.6 Compromised confirm flow

**Threat:** An attacker intercepts the `SKREACH_CONFIRM_REQUIRED` response and
either (a) forges a confirm envelope or (b) tricks the operator into confirming
a different command than displayed.

**(a) Forged confirm:**
- The confirm envelope requires the same issuer's PGP signature as the original
  command (§3.3 step 4). A forged confirm from a different key is rejected.
- Forging the issuer's signature requires the private key (same as full identity
  compromise; mitigated by §7.1).

**(b) Summary injection / confused deputy:**
- The `summary` field in `SKREACH_CONFIRM_REQUIRED` is **generated by skreachd**
  from the verified argv array, not from user-supplied content. Even if the `cmd`
  payload contained a crafted `summary` field, skreachd ignores it and generates
  its own description.
- The operator's UI presents the skreachd-generated summary, not any field from
  the original command request. An attacker who controls the command content cannot
  make the summary say something different from what the command actually does.

---

## 8. Acceptance Criteria (Security Gates)

These are hard gates. **No code in F2–F4 ships unless every item below passes.**
Each item maps to a required automated test (unit or integration).

### Signature & freshness

- [ ] **SIG-1** An envelope with an invalid PGP signature is dropped by skreachd
  before any processing. Confirmed by unit test: forge a signature → no execution,
  `sig_invalid` audit record.
- [ ] **SIG-2** An envelope with `exp < now` (even by 1 second, ignoring clock skew
  allowance) is rejected. Confirmed by unit test: set `exp = iat - 1` → rejected,
  `expired_cmd` audit record.
- [ ] **SIG-3** An envelope replayed with the same `cmd.id` within the 10-minute
  cache window is rejected on second delivery. Confirmed by unit test.
- [ ] **SIG-4** An envelope with `sub` != the receiving node's FQID is dropped.
  Confirmed by unit test: send a valid signed envelope to the wrong node → rejected.

### RBAC

- [ ] **RBAC-1** A `guest`-role issuer's command is rejected regardless of command
  class. Confirmed by unit test.
- [ ] **RBAC-2** A `member`-role issuer cannot execute `exec`, `deploy`, or
  `destructive` class commands. Confirmed by unit test with a valid signed member
  envelope targeting `exec` → `rbac_denied`.
- [ ] **RBAC-3** An `agent`-role issuer cannot execute `exec` class commands unless
  an explicit per-node grant exists. Confirmed by unit test (no grant → denied;
  grant present → allowed).
- [ ] **RBAC-4** No principal may self-elevate via a claim in the envelope. The role
  is resolved from the local peer store, not from any field in the envelope.
  Confirmed by unit test: member envelope with fabricated `role=operator` claim in
  body → still resolves as `member`.

### Exec sandbox

- [ ] **SAND-1** `shell=True` is never passed to `subprocess.Popen`. Confirmed by
  static analysis (grep/ast scan in CI that fails if `shell=True` appears in
  `skreachd` execution paths).
- [ ] **SAND-2** Shell metacharacters in argv are passed as literals, not
  interpreted. Confirmed by unit test: cmd `["echo", "; id"]` → stdout is
  literally `"; id"`, not the output of `id`.
- [ ] **SAND-3** A command with `cwd` outside `allowed_cwd` is rejected.
  Confirmed by unit test.
- [ ] **SAND-4** A `cwd` that is a symlink pointing outside `allowed_cwd` is
  rejected after symlink resolution. Confirmed by unit test.
- [ ] **SAND-5** A denied-command-list binary (e.g. `bash`) is rejected even if
  present in the `command_allowlist`. Confirmed by unit test.
- [ ] **SAND-6** A command that exceeds the wall-clock timeout is killed and logged
  with `outcome=timeout`. Confirmed by integration test (command that sleeps longer
  than the timeout).
- [ ] **SAND-7** Secret-named env keys (`*SECRET*`, `*TOKEN*`, `*PASSWORD*`,
  `*KEY*`) provided in the command envelope's `env` field are stripped before
  subprocess spawn. Confirmed by unit test: `env={"MY_SECRET": "x"}` → stripped,
  audit record shows key name was provided and stripped, child process does not
  receive it.

### Confirm-on-destructive

- [ ] **CONF-1** A destructive command (e.g. `rm`, `stop`, `scale-down`) is NOT
  executed without a second signed confirm. skreachd returns
  `SKREACH_CONFIRM_REQUIRED`. Confirmed by integration test.
- [ ] **CONF-2** A confirm envelope signed by a different FQID than the original
  command issuer is rejected. Confirmed by unit test.
- [ ] **CONF-3** A confirm envelope for an already-executed command is rejected
  (confirm IDs are single-use). Confirmed by unit test.

### Audit

- [ ] **AUDIT-1** Every executed command has an audit record in skmem-pg with
  `outcome=executed` before `subprocess.wait()` returns. The record is written
  *before* the process exits (so a crash doesn't lose the record). Confirmed by
  integration test: run a command, query audit table, verify record exists.
- [ ] **AUDIT-2** Every rejected command (any reason) has an audit record with the
  appropriate `outcome`. Confirmed by integration tests for each rejection path.
- [ ] **AUDIT-3** The skreachd db role cannot `DELETE` or `UPDATE` rows in
  `skreach_audit`. Confirmed by integration test: attempt `DELETE FROM
  skreach_audit` as the skreachd role → `pg_exception_code=42501` (insufficient
  privilege).
- [ ] **AUDIT-4** Audit record `env_keys` never contains env values. Confirmed by
  unit test: inspect audit record `env_keys` field → contains key names only.

### Network posture

- [ ] **NET-1** skreachd binds no inbound TCP/UDP ports by default (tsnet transport
  only). Confirmed by integration test: `ss -tlnp` on the node → no skreachd port.
- [ ] **NET-2** `SKREACH_PUBLIC_ENABLED=0` (default) causes all commands received
  via the Cloudflare Tunnel adapter to be silently dropped. Confirmed by unit test.

---

## 9. Open Questions (require Chef decision before F2)

**OQ-1 — Owner vs operator exec distinction for day-to-day ops:**
The spec defines `owner` as Chef-only for `owner`-class commands (node
registration, key rotation, skreachd config reload). Is there a near-term need
for a second operator identity (e.g. a CI service account) to issue `exec` and
`deploy` commands autonomously? If yes, the `SKREACH_OPERATOR_FQIDS` list should
be configured from day one; if no, single-owner is simpler and safer for now.

**OQ-2 — Agent exec authority by default:**
Currently, agents (Lumina, Opus) have `status`/`log_read` only with opt-in exec
grants. The MCP tool (F4, agent-drives-agent) implies agents will need to run
`deploy`-class commands via trustee wrap. What is the intended scope? Options:
  (a) Agent gets `deploy` class (delegates to trustee), no raw `exec` ever.
  (b) Agent gets scoped `exec` with tight allowlist for the MCP use case.
  (c) All agent actions are mediated by a human-in-the-loop confirm gate.
Recommendation: (a) is the safest default — agents drive deployment via the
trustee wrapper, never raw exec. Raw exec for agents is a separate, later decision.

**OQ-3 — Confirm UX for non-interactive agent confirmations:**
The confirm-on-destructive flow (§3.3) requires a second signed command from
the issuer. For interactive human operators this is a second button press. For
agents confirming their own commands in the MCP tool, this creates a loop — the
agent issues a command, gets a confirm request, and must issue a second command.
Is a "pre-confirmed" destructive command pattern acceptable for specific, well-
scoped agent workflows (e.g. `scale-down N to 0` as part of a managed deploy
runbook), or must the human always be in the confirm loop?

**OQ-4 — Audit retention & compliance posture:**
How long must audit records be retained? The spec defaults stdout/stderr blobs to
30 days with permanent metadata. If skreach is used for regulated workloads (ITIL
mandates, SOC-2 scoping, etc.), a longer retention or a WORM-backed audit store
may be required. Define the retention policy before F2 ships.

**OQ-5 — Key rotation cadence:**
The spec allows `owner`-class `rotate_key` to revoke stolen keys immediately. But
what is the standard (non-incident) rotation cadence for capauth PGP keys used for
skreach command signing? Recommendation: 90-day rotation for operator keys; agent
keys rotate on agent identity refresh. This should be codified in the key
management runbook before exec is live.

**OQ-6 — Node registry and fleet-wide policy:**
The spec defines per-node `skreach-node.yaml` policy files. For a growing fleet,
per-node config becomes operational burden. The P1 design should include a fleet-
wide policy layer (OpenBao-backed, signed policy bundles) that pushes allowlist/
denylist updates to all nodes via the `owner`-class config reload command. This is
deferred but the per-node schema should be designed to be composable with a fleet
policy (local overrides fleet-policy defaults, not the other way around).

**OQ-7 — WebRTC data-channel vs mailbox delivery:**
The architecture says skreach rides the WebRTC data channel as a `{lane:"term"}`
envelope (§2.6 of the reassessment spec). The signature + freshness model works
for both transports. But the WebRTC data channel is session-scoped (both peers
must be in the same LiveKit room). For non-interactive, async ops (e.g. a
scheduled deploy at 3am), the skcomms mailbox is the right transport. skreachd
should accept commands via both paths, with identical security treatment. Confirm
this is the intended design before F2 implementation begins.

---

## 10. Implementation Notes for F2 (skreachd MVP)

This section is guidance, not spec. The spec is §§1–9.

- `src/skchat/skreach/` — new module dir, parallel to `voice_engine/`.
- `skreachd.py` — the daemon; receives envelopes, runs §1.3 verification pipeline,
  dispatches to sandbox executor.
- `cmd_verifier.py` — §1.3 steps 1–8; thin, heavily unit-tested.
- `sandbox.py` — §5 exec sandbox; `subprocess.Popen` with `shell=False` enforced.
- `audit.py` — §4 audit writer; skmem-pg INSERT + JSONL append; idempotent on
  `audit_id` (use `ON CONFLICT DO NOTHING` for crash-recovery replay safety).
- `node_policy.py` — §3.4 policy loader; validates `skreach-node.yaml` at startup.
- `confirm_store.py` — pending confirm tracking (in-memory dict `cmd_id →
  (envelope, expires_at)`).
- `replay_cache.py` — LRU replay cache (10-minute window, max 10,000 entries).
- The `skreachd.service` systemd unit template (per-node, `skreachd@<node>`) with
  the §7.4 hardening directives.
- The `skreach_audit` table migration lives in `src/skchat/migrations/`.
- All tests in `tests/skreach/` — unit tests run without a running daemon; integration
  tests require `skmem-pg` (skip with `-m 'not integration'`).
