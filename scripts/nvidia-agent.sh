#!/usr/bin/env bash
# nvidia-agent.sh — Lightweight replacement for `claude -p` that uses NVIDIA API proxy.
# Reads prompt from file passed as argument, calls Kimi K2.5 via NVIDIA proxy.
# Usage: nvidia-agent.sh <prompt-file> [model]
#
# Default model: moonshotai/kimi-k2.5 (via NVIDIA proxy at localhost:18780)

set -euo pipefail

PROMPT_FILE="${1:?Usage: nvidia-agent.sh <prompt-file> [model]}"
MODEL="${2:-moonshotai/kimi-k2-instruct}"
NVIDIA_API="https://integrate.api.nvidia.com/v1/chat/completions"
NVIDIA_API_KEY="${NVIDIA_API_KEY:-$(python3 -c "import json; print(json.load(open('$HOME/.openclaw/openclaw.json'))['env']['NVIDIA_API_KEY'])" 2>/dev/null || echo 'none')}"

if [[ ! -f "$PROMPT_FILE" ]]; then
    echo "ERROR: Prompt file not found: $PROMPT_FILE" >&2
    exit 1
fi

PROMPT=$(cat "$PROMPT_FILE")

# Escape the prompt for JSON (handle newlines, quotes, backslashes)
ESCAPED_PROMPT=$(python3 -c "import json,sys; print(json.dumps(sys.stdin.read()))" <<< "$PROMPT")

RESPONSE=$(curl -sS --max-time 120 "$NVIDIA_API" \
    -H "Content-Type: application/json" \
    -H "Authorization: Bearer $NVIDIA_API_KEY" \
    -d "{
        \"model\": \"$MODEL\",
        \"messages\": [{\"role\": \"user\", \"content\": $ESCAPED_PROMPT}],
        \"max_tokens\": 2048,
        \"temperature\": 0.7,
        \"stream\": false
    }" 2>&1)

# Extract the text response
REPLY=$(echo "$RESPONSE" | python3 -c "
import json, sys
try:
    data = json.load(sys.stdin)
    if 'choices' in data:
        print(data['choices'][0]['message']['content'])
    elif 'error' in data:
        print(f\"API Error: {data['error']}\", file=sys.stderr)
        sys.exit(1)
    else:
        print(f\"Unexpected response: {json.dumps(data)[:200]}\", file=sys.stderr)
        sys.exit(1)
except Exception as e:
    print(f\"Parse error: {e}\", file=sys.stderr)
    sys.exit(1)
")

echo "$REPLY"
