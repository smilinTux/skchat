# ~/clawd/skcapstone-repos/skchat/tests/test_operator_auth_wire_compat.py
"""Proves the native Flutter GuestIdentity signs something the server accepts.

Runs the Dart fixture emitter in the skchat-app repo, then feeds its output
through the SAME verify path the operator-auth routes use. If this passes, the
thick client's enrollment/handshake signatures will verify server-side.

Invocation note: the emitter (test/fixtures/emit_device_fixture.dart) imports
guest_identity_io.dart, which pulls in flutter_secure_storage -> package:flutter
-> dart:ui transitively. dart:ui only exists inside Flutter's patched SDK, so a
plain `dart run` fails to compile ("dart:ui is not available on this
platform") no matter how the file itself is written. `flutter test --no-pub`
uses the patched SDK and runs the file's bare main() directly, so that is the
command used here. That runner always reports "No tests were found" (exit
code 79) since the file has no test() blocks, which is expected and ignored;
only the emitted JSON line on stdout matters.
"""
import json
import os
import shutil
import subprocess

import pytest

from skchat.operator_auth import device_fingerprint, verify_device_signature

APP_DIR = os.path.expanduser("~/clawd/skcapstone-repos/skchat-app")
FLUTTER_BIN = "/home/cbrd21/flutter/bin"
FIXTURE_KEYS = {"pubkey_b64", "fingerprint", "payload", "sig_b64"}


def _extract_fixture(stdout: str) -> dict:
    """Find the emitted fixture JSON among the flutter test runner's noise."""
    for line in stdout.splitlines():
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            obj = json.loads(line)
        except ValueError:
            continue
        if FIXTURE_KEYS <= obj.keys():
            return obj
    raise AssertionError(f"no fixture JSON line found in stdout:\n{stdout}")


@pytest.mark.skipif(
    not shutil.which("flutter", path=FLUTTER_BIN + os.pathsep + os.environ.get("PATH", "")),
    reason="Flutter SDK not available",
)
def test_native_guest_identity_signature_verifies_server_side():
    env = dict(os.environ, PATH=FLUTTER_BIN + os.pathsep + os.environ.get("PATH", ""))
    proc = subprocess.run(
        ["flutter", "test", "--no-pub", "test/fixtures/emit_device_fixture.dart"],
        cwd=APP_DIR, env=env, text=True, capture_output=True,
    )
    fx = _extract_fixture(proc.stdout)
    assert verify_device_signature(
        device_pubkey_b64=fx["pubkey_b64"],
        payload=fx["payload"].encode(),
        sig_b64=fx["sig_b64"],
    ) is True
    assert device_fingerprint(fx["pubkey_b64"]) == fx["fingerprint"]
