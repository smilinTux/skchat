#!/usr/bin/env bash
# skchat/scripts/bootstrap.sh — single-command setup for SKChat
# Usage: ./scripts/bootstrap.sh
set -euo pipefail

SKCHAT_DIR="$HOME/.skchat"
SKCOMM_DIR="$HOME/.skcomm"
SKCAPSTONE_PEERS="$HOME/.skcapstone/peers"
SKCHAT_SRC="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PYTHON="${HOME}/.pyenv/shims/python3"

echo "=== SKChat Bootstrap ==="
echo "Install dir: $SKCHAT_DIR"

# 1. Create dirs
mkdir -p "$SKCHAT_DIR/groups" "$SKCHAT_DIR/memory"
mkdir -p "$SKCAPSTONE_PEERS"

# 2. Install skchat
echo "--- Installing skchat..."
cd "$SKCHAT_SRC"
pip install -e ".[cli]" --quiet

# 3. Create config if missing
if [ ! -f "$SKCHAT_DIR/config.yml" ]; then
  cp "$SKCHAT_SRC/config.yml.example" "$SKCHAT_DIR/config.yml" 2>/dev/null || \
  cat > "$SKCHAT_DIR/config.yml" << 'EOF'
daemon:
  poll_interval: 5.0
  log_file: ~/.skchat/daemon.log
  quiet: false

advocacy:
  enabled: true
  trigger_prefix: "@opus"

peers:
  lumina: "capauth:lumina@skworld.io"
  claude: "capauth:claude@skworld.io"
EOF
  echo "Created $SKCHAT_DIR/config.yml"
fi

# 4. Create peer stubs if missing
if [ ! -f "$SKCAPSTONE_PEERS/lumina.json" ]; then
  cat > "$SKCAPSTONE_PEERS/lumina.json" << 'EOF'
{
  "name": "Lumina",
  "fingerprint": "AABB1122CCDD3344EEFF5566AABB1122CCDD3344",
  "public_key": "",
  "entity_type": "ai-agent",
  "handle": "lumina@skworld.io",
  "email": "lumina@skworld.io",
  "capabilities": ["capauth:identity","skcomm:messaging","skchat:p2p-chat"],
  "contact_uris": ["capauth:lumina@skworld.io","mailto:lumina@skworld.io"],
  "trust_level": "verified",
  "added_at": "2026-03-03T00:00:00+00:00",
  "source": "bootstrap",
  "agent_type": "ai"
}
EOF
  echo "Created lumina peer"
fi

if [ ! -f "$SKCAPSTONE_PEERS/claude.json" ]; then
  cat > "$SKCAPSTONE_PEERS/claude.json" << 'EOF'
{
  "name": "Claude",
  "fingerprint": "",
  "public_key": "",
  "entity_type": "ai-agent",
  "handle": "claude@skworld.io",
  "email": "claude@skworld.io",
  "capabilities": ["capauth:identity","skcomm:messaging","skchat:p2p-chat"],
  "contact_uris": ["capauth:claude@skworld.io"],
  "trust_level": "verified",
  "added_at": "2026-03-03T00:00:00+00:00",
  "source": "bootstrap",
  "agent_type": "ai"
}
EOF
  echo "Created claude peer"
fi

# 5. Enable systemd service
if systemctl --user is-enabled skchat.service &>/dev/null; then
  echo "skchat.service already enabled"
else
  SYSTEMD_DIR="$HOME/.config/systemd/user"
  mkdir -p "$SYSTEMD_DIR"
  cat > "$SYSTEMD_DIR/skchat.service" << EOF
[Unit]
Description=SKChat receive daemon
After=network-online.target

[Service]
Type=forking
WorkingDirectory=%h
PIDFile=%h/.skchat/daemon.pid
ExecStart=${HOME}/.pyenv/shims/skchat daemon start --interval 5 --log-file %h/.skchat/daemon.log
Restart=on-failure
RestartSec=10

[Install]
WantedBy=default.target
EOF
  systemctl --user daemon-reload
  systemctl --user enable skchat.service
  echo "Enabled skchat.service"
fi

# 6. Start daemon from home dir (avoids skmemory namespace collision)
if ! skchat daemon status 2>/dev/null | grep -q "running"; then
  cd "$HOME" && skchat daemon start --interval 5 --log-file ~/.skchat/daemon.log
  echo "Daemon started"
else
  echo "Daemon already running"
fi

echo ""
echo "=== Setup complete ==="
echo "Run: cd ~ && skchat status"
echo "Run: skchat watch --notify"
echo "Run: skchat send capauth:lumina@skworld.io 'Hello Lumina!'"
echo "Run: skchat group list"
