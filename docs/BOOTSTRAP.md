# BOOTSTRAP: blank machine to a live skchat node

This is the ordered dependency chain to bring a genuinely blank machine into
the skchat plane deterministically, plus the script that runs it with
preflight checks (`scripts/bootstrap-node.sh`). It exists because the chain
has a real chicken-and-egg in the middle (identity vs skvault, see Step 2)
that is easy to get backwards on a real rebuild.

Companion docs: `scripts/backup-skchat.sh` / `scripts/restore-skchat.sh`
(Task 1, what gets backed up and how it's restored), `systemd/README.md`
(what `install.sh` installs and the secret files it needs),
`systemd/TAILSCALE-INGRESS.md` (Task 4, the public ingress).

## The ordered chain

```
1. ~/.skenv packages
        |
2. CapAuth identity / PGP key import   <-- chicken-and-egg is broken HERE
        |
3. skvault unlock
        |
4. ~/.skchat restore (or fresh provision) + identity.json/peers manual step
        |
5. systemd/install.sh --enable
        |
6. tailscale ingress (systemd/tailscale-ingress.sh)
```

Every step assumes the previous one is satisfied. Steps 1-4 are per-node
identity/data bootstrap; steps 5-6 are "now make it actually serve traffic."

### Step 1: install `~/.skenv` packages

The shared venv every SK* CLI lives in (`skchat`, `skcapstone`, `capauth`,
`skvault`, `skcomms`, `skmemory`, all at `~/.skenv/bin/`).

```bash
git clone <skcapstone-repo-url> ~/clawd/skcapstone-repos/skcapstone
bash ~/clawd/skcapstone-repos/skcapstone/scripts/install.sh
export PATH="$HOME/.skenv/bin:$PATH"   # add to shell profile
```

Verified command (`scripts/install.sh` read directly, see
`skcapstone/README.md:41,170-176`): creates `~/.skenv` via `python3 -m venv`,
installs the SK* packages into it, and instructs adding
`~/.skenv/bin` to `PATH`. This repo's own `pip install -e ".[cli]"` (from
`scripts/bootstrap.sh`) additionally installs `skchat` itself into the same
venv, editable.

Nothing here needs an identity or a secret. This step can always run first
on a genuinely blank machine.

### Step 2: provision the CapAuth identity / import the agent PGP key -- THE CHICKEN-AND-EGG BREAKER

This is the step the brief is pointing at. Read it carefully before doing
anything else on a rebuild.

**Why there is a chicken-and-egg at all.** `skvault unlock` (see
`skvault/src/skvault/vault.py:unlock()`) works by presetting a passphrase
into `gpg-agent` for every recipient in `capauth.seal.recipients()`
(`CAPAUTH_PGP_RECIPIENT`, falling back to the legacy `SKINGEST_PGP_RECIPIENT`
-- on this host both resolve to `chef@skworld.io`, verified in
`~/.config/skmemory/skingest.env`). Before it can preset anything, it needs
`gpg --list-secret-keys <recipient>` to already succeed
(`skvault/src/skvault/vault.py:_has_secret_key`) -- i.e. the **private key
material must already be sitting in the local gpg keyring**. skvault has no
code path that materializes a private key from anywhere; it only unlocks a
key that is already there. So: skvault cannot hand you the identity key
(that's the "egg"), because unlocking skvault itself requires the identity
key to already be present (that's the "chicken"). The break has to happen
**outside** skvault, before it is ever invoked:

```bash
# On the SOURCE (already-provisioned) node, export the operator's private key
# to a file, move it to the new machine over an out-of-band secure channel
# (scp over tailnet SSH, a hardware token, a sealed USB drive -- NOT through
# skvault, NOT through anything skvault-sealed, since none of that is
# readable yet):
gpg --export-secret-keys --armor chef@skworld.io > chef-private.asc   # on source
# ... move chef-private.asc to the new machine out of band ...
gpg --import chef-private.asc                                        # on new machine
shred -u chef-private.asc                                            # don't leave it lying around
gpg --list-secret-keys chef@skworld.io                               # confirms it's importable
```

This host (verified 2026-07-16): `gpg --list-secret-keys` shows a real
operator secret key (`Chef (SK Sovereign Root) <chef@skworld.io>`,
fingerprint `BD7EEECA23D90A594400751CFDB582D9CB7272A6`). That is exactly the
key that had to be imported first, on this host, before skvault ever worked
here. **Verify on real rebuild**: the precise export/import invocation above
is standard `gpg` and was verified as syntax (`gpg --export-secret-keys`
round-trips with `gpg --import`), but was NOT exercised end-to-end against a
second machine as part of this task (no second host was provisioned).
Passphrase handling on export/import is interactive, matching
`restore-skchat.sh`'s documented assumption that a human/gpg-agent is
present for key material operations.

**Separately, the per-agent CapAuth profile.** Each agent (e.g. `lumina`)
also carries its own PGPy-backend keypair at
`~/.skcapstone/agents/<agent>/capauth/identity/{private.asc,public.asc,profile.json}`
(confirmed present on this host for `lumina`). This is a *different* key
from the operator's gpg key above -- it is what
`capauth.resolve_agent_identity()` uses to populate `AgentIdentity.fingerprint`
for that agent (`capauth/src/capauth/agent_identity.py:_load_fingerprint`),
and it is read directly off disk (PGPy), not via the gpg keyring, so it does
not participate in the gpg chicken-and-egg above. On a rebuild it needs to
arrive by one of:

- **Syncthing replication**, if `capauth init --sync` was used when the
  agent identity was first created (`capauth/src/capauth/cli.py:_offer_sync`)
  -- the whole `~/.skcapstone/agents/<agent>/capauth/` dir replicates
  automatically once Syncthing is configured on the new node. **Verify on
  real rebuild**: confirming Syncthing is actually configured for this path
  on the live fleet was out of scope here (read-only check only); the CLI
  code path exists and was read, not exercised.
- A manual copy from a trusted, already-provisioned node (same
  out-of-band-channel rule as the operator key above -- this is private key
  material).
- `capauth init --name "<Agent>" --email "<agent>@skworld.io"` **only** if
  this is genuinely a brand-new agent with no prior identity anywhere --
  this command *generates a new keypair*, so running it for an agent that
  already has an established identity elsewhere creates a second, different
  identity and is almost certainly the wrong choice on a rejoin/rebuild.

Only after both pieces above are in place (operator gpg key imported +
per-agent CapAuth profile present) does Step 3 become possible.

### Step 3: unlock skvault with that key

```bash
skvault unlock          # prompts for the operator's gpg key passphrase
skvault status           # confirm: unlocked
```

Verified against the real shim chain on this host:
`~/.skenv/bin/skvault` (stable entry point) delegates to
`~/.skenv/bin/skvault-backend` (the `skvault` package's click CLI,
`skvault/src/skvault/cli.py:cmd_unlock`/`cmd_vault_status`), which presets
the passphrase into `gpg-agent` and re-probes `vault_unlocked()`. Now that
Step 2 has put the private key in the gpg keyring, this succeeds; secrets
sealed with `capauth.seal` (KeePass master, bot tokens, LiveKit/NVIDIA keys
provisioned via the vault, etc.) become readable for the rest of the
bootstrap and for the running daemons.

### Step 4: restore `~/.skchat` (or provision fresh) + the identity.json/peers gap

**Restoring from the Task-1 backup** (preferred on a real rebuild of an
existing node):

```bash
ls -t ~/.skchat-backups/skchat-*.tar.gz.pgp | head -1     # find the latest archive
bash scripts/restore-skchat.sh --target "$HOME" <archive-path>
```

`scripts/restore-skchat.sh` decrypts (gpg first, `sq` fallback) into
`--target` -- decryption needs the private key from Step 2 unlocked via
Step 3 (gpg-agent cache), which is exactly why restore has to come after
skvault unlock, not before. Verified: this is the same script shipped by
Task 1, read directly (`scripts/restore-skchat.sh`), including its
member-path traversal guard.

**Provisioning fresh** (a genuinely new node, no prior `~/.skchat` to
restore): `bash scripts/bootstrap.sh` -- creates `~/.skchat/{groups,memory}`,
installs skchat editable, writes a default `config.yml`, seeds placeholder
peer stubs under `~/.skcapstone/peers/`, and can register a (legacy,
non-systemd-managed-by-this-repo) unit. On a node that will run the
`systemd/` unit set from this repo, skip its own service-registration step
and use Step 5 (`systemd/install.sh --enable`) instead -- do not run both.

**The identity.json / peers gap (manual step -- not covered by Task 1's
backup).** Flagged during Task 1 review and confirmed here by reading
`scripts/backup-skchat.sh`'s source list directly: it tars
`~/.skchat`, `~/.skcomms/outbox`, and `~/.config/skchat/*.env` only. Two
files a full restore also needs live **outside** all three of those paths
and are silently absent from every Task-1 backup:

- `~/.skcapstone/identity/identity.json` -- the **operator's** shared
  identity file (confirmed on this host: role `operator`, name `Chef`,
  `fingerprint: D8920EA8...`). `skchat`'s `encrypted_store.py._get_fingerprint()`
  reads this exact path directly (`src/skchat/encrypted_store.py:355`) to
  derive the at-rest DEK for encrypted message history. Without it, message
  history restored from a backup cannot be decrypted (or the daemon falls
  back to a hostname/username-derived key, `encrypted_store.py`'s fallback
  branch, which will not match the key the original data was encrypted
  under on a different machine).
- `~/.skcomms/peers` -- the peer registry (fingerprints, contact URIs,
  trust levels for every known agent/human). Without it, `skchat` cannot
  resolve short handles (`lumina`, `chef`) to full identities and every
  send/verify falls back to whatever `scripts/seed-peers.py` /
  `scripts/generate-peers-from-agents.py` regenerate from scratch (weaker
  trust state than what was actually live).

Until these two get their own backup coverage (a follow-up, not part of
this task), the manual step on a rebuild is:

```bash
# From an already-provisioned node, over the same out-of-band channel used
# for the PGP key in Step 2 (this is also sensitive: it's the operator's
# fingerprint file and the full peer trust registry):
rsync -av <known-good-host>:~/.skcapstone/identity/ ~/.skcapstone/identity/
rsync -av <known-good-host>:~/.skcomms/peers/       ~/.skcomms/peers/
```

`bootstrap-node.sh` (below) checks for both paths and prints this exact
gap as a loud warning rather than silently proceeding; it does not attempt
to auto-copy them (no source host is knowable generically).

### Step 5: `systemd/install.sh --enable`

```bash
cd systemd && ./install.sh --diff        # see what would change first
./install.sh --enable                     # copy units/drop-ins + enable the live-set
# --start is a deliberate, separate operator step (see systemd/README.md)
```

Verified directly from `systemd/install.sh`: idempotent (copies only on
content change, never restarts a running unit), reconciles the full unit +
drop-in + helper set documented in `systemd/README.md`, runs a secret
preflight (warns, does not fail, on missing `EnvironmentFile`s -- most of
which are exactly the secrets that came alive in Step 3), and verifies every
rendered unit with `systemd-analyze --user verify` before `daemon-reload`.
`--enable` additionally enables the live-set (`ENABLE_UNITS`); it does not
start anything unless `--start` is also given.

### Step 6: apply the tailscale ingress

```bash
bash systemd/tailscale-ingress.sh --dry-run     # always look first
bash systemd/tailscale-ingress.sh                # apply (see caveat below)
```

Verified from `systemd/tailscale-ingress.sh` and `systemd/TAILSCALE-INGRESS.md`
(Task 4): read-and-skip idempotency (reads `tailscale serve status --json`,
only issues a `tailscale funnel` command for a mapping that does not already
match). **Caveat carried forward from Task 4, repeated here because it is
directly relevant to an automated bootstrap**: this ingress is shared, live
infrastructure (skchat web/daemon, skfed directory, LiveKit signaling,
coturn TURNS all ride the same Funnel-enabled `:443` listener), so
`tailscale-ingress.sh`'s own safety banner says not to run it for real
inside an automated task. `scripts/bootstrap-node.sh` therefore defaults
this step to `--dry-run` and only applies for real when the operator passes
`--apply-ingress` explicitly.

## Summary table

| # | Step | Breaks on | Verified vs assumed |
|---|------|-----------|----------------------|
| 1 | `~/.skenv` install | nothing (first step) | Verified (`scripts/install.sh` read) |
| 2 | Identity / PGP import | **the chicken-and-egg lives here** | gpg export/import verified as syntax; not run end-to-end cross-host; Syncthing agent-profile sync path read, not exercised |
| 3 | `skvault unlock` | needs Step 2's key in the gpg keyring | Verified (`skvault/vault.py` read + real `skvault`/`skvault-backend` shim chain confirmed on this host) |
| 4 | `~/.skchat` restore + identity.json/peers manual step | needs Step 3 unlocked (decrypt) | Verified (`restore-skchat.sh` read; identity.json/peers gap confirmed by reading `backup-skchat.sh`'s source list + `encrypted_store.py`) |
| 5 | `systemd/install.sh --enable` | needs Step 4's `~/.skchat` + Step 3's secrets | Verified (script read directly; not re-run for real against a live host here) |
| 6 | tailscale ingress | needs Step 5's units running for the proxied ports to answer | Verified (`--dry-run` re-confirmed live-parity in Task 4); real apply intentionally gated behind `--apply-ingress` |

## Related gaps for follow-up (not fixed by this task)

- `~/.skcapstone/identity/identity.json` and `~/.skcomms/peers` have no
  backup coverage of their own (see Step 4). A follow-up could extend
  `scripts/backup-skchat.sh` with an `--include-identity` flag, or these
  could get a small dedicated backup script since they are cluster-wide
  (not skchat-specific) state.
- Syncthing sync of `~/.skcapstone/agents/<agent>/capauth/` (Step 2) was
  read in code but not confirmed live on the fleet for every agent; worth a
  `skcapstone doctor` pass (`identity:*` checks, see
  `skcapstone/src/skcapstone/doctor.py`) on a real second node once one
  exists.
