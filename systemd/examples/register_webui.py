"""ExecStartPost hook: advertise this agent's webui in the skcapstone
service-discovery registry (~/.skcapstone/registry/<name>.json).

Reads SKAGENT + SKCHAT_PORT from the systemd service environment.
Degrades silently if skcapstone isn't importable (optional dependency).

Installed to ~/.config/skchat/register_webui.py by systemd/install.sh
(referenced by skchat-webui@.service ExecStartPost). The installer never
clobbers an existing copy.
"""
import os
import sys

agent = os.environ.get("SKAGENT") or os.environ.get("SKCAPSTONE_AGENT") or "unknown"
port = os.environ.get("SKCHAT_PORT", "")

try:
    from skcapstone.sdk import register_service
except Exception as exc:  # skcapstone optional --- never block webui startup
    sys.stderr.write(f"register_webui: skcapstone unavailable, skipping ({exc})\n")
    sys.exit(0)

try:
    path = register_service(
        f"skchat-webui-{agent}",
        health_url=f"http://localhost:{port}/" if port else None,
    )
    print(f"register_webui: registered skchat-webui-{agent} -> {path}")
except Exception as exc:
    sys.stderr.write(f"register_webui: registration failed (non-fatal): {exc}\n")
    sys.exit(0)
