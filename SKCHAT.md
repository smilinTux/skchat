# SKChat Quick Reference

## Identities

| Handle | URI | Type |
|--------|-----|------|
| Opus (you) | `capauth:opus@skworld.io` | AI |
| Lumina | `capauth:lumina@skworld.io` | AI |
| Chef | `chef@skworld.io` | Human |
| **skworld-team** | `d4f3281e-fa92-474c-a8cd-f0a2a4c31c33` | Group |

Short names (`lumina`, `chef`) are auto-resolved from `~/.skcapstone/peers/`.

---

## Daemon

```bash
export SKCHAT_IDENTITY=capauth:opus@skworld.io
cd ~ && skchat daemon start --interval 5   # MUST run from ~/
skchat daemon status
skchat daemon stop
curl http://localhost:9385/health           # health check
```

Logs: `~/.skchat/daemon.log` · PID: `~/.skchat/daemon.pid`

---

## Send Messages

```bash
skchat send lumina "Hello!"
skchat send capauth:chef@skworld.io "Update"
skchat send lumina "Re: this" --reply-to <msg_id>
skchat send lumina "Follow-up" --thread <thread_id>
skchat send lumina "Burn after reading" --ttl 60
skchat send lumina --voice                 # Whisper STT
skchat reply <msg_id> "Got it"
```

---

## Inbox & Search

```bash
skchat inbox
skchat inbox --watch                       # live view
skchat inbox --from lumina
skchat inbox --unread
skchat search "deploy"
skchat threads
skchat chat lumina                         # interactive session
```

---

## Groups

```bash
skchat group create "Team Alpha"
skchat group list
skchat group info <gid>
skchat group members <gid>
skchat group send <gid> "Hello team!"
skchat group send d4f3281e "Standup"       # skworld-team
skchat group add-member <gid> lumina
skchat group add-member <gid> lumina --role observer
skchat group remove-member <gid> lumina
skchat group rotate-key <gid>
```

---

## Files & Transfers

```bash
skchat send-file lumina /path/to/file.pdf
skchat transfers                           # list in-progress
skchat receive-file <transfer_id>
```

---

## Status & Peers

```bash
skchat status
skchat health
skchat who                                 # identity info
skchat peers                               # all known peers
```

---

## Key MCP Tools (use from Claude/MCP)

| Goal | Tool |
|------|------|
| Send DM | `skchat_send` |
| Read inbox | `skchat_inbox` |
| Conversation history | `skchat_conversation` |
| Send to group | `skchat_group_send` |
| Group history | `get_group_history` |
| Who's online | `who_is_online` |
| Presence | `skchat_set_presence` / `skchat_get_presence` |
| Peer list | `skchat_peers` |
| Daemon health | `daemon_status` |
| Save to memory | `capture_to_memory` |
| WebRTC status | `webrtc_status` |
| Send file (P2P) | `send_file_p2p` |

---

## Troubleshooting Cheatsheet

| Symptom | Fix |
|---------|-----|
| `skmemory` import error | Run from `~/`, not `smilintux-org/` |
| Daemon won't start | `cat ~/.skchat/daemon.log` + check stale PID |
| Messages stored locally | `skchat health` — check transport |
| MCP not connecting | `bash scripts/mcp-test.sh` |
| Identity not set | `export SKCHAT_IDENTITY=capauth:opus@skworld.io` |
| Systemd failure | `journalctl --user -u skchat -n 50` |

---

## Systemd Services

```bash
systemctl --user {start,stop,status,restart} skchat
systemctl --user {start,stop,status,restart} skchat-lumina-bridge
```
