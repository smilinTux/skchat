#!/usr/bin/env python3
"""CR-3.4 fixture replay: the Phase-3 sufficiency gate for issuer convergence.

Replays the ENTIRE skchat capability route table against a running server with
BOTH an HS256 operator session and an operator-audience token for the SAME
enrolled device, asserting per-route identical status codes. The server-side
shadow proves convergence on whatever traffic happens to arrive; this proves it
over the whole table, which is the completeness the seat flip actually needs
(spec 2026-08-06-skchat-issuer-convergence.md section 4, R1/R10).

What convergence means here is AGREEMENT, not success: both credentials are sent
the SAME request, so a 404/422 on a placeholder id is a pass as long as both
produce it. A one-sided 401/403 is the issuer divergence the flip must not carry.

Obtain the two tokens from an enrolled operator device's session response
(``POST /api/v1/auth/session`` returns ``session_token`` and, when
``SKCHAT_OPERATOR_AUDIENCE_ISSUE=1``, ``audience_token``). Run against a server
with the enforce PDP on (``SKCHAT_AUTHZ_PDP=enforce``) so the comparison exercises
the real decision path.

    issuer_replay.py --base-url http://127.0.0.1:8790 \
        --hs-token "$SESSION_TOKEN" --aud-token "$AUDIENCE_TOKEN"

Exits 0 if every route converges, 1 on any divergence (or on a usage error).
"""

from __future__ import annotations

import argparse
import json
import sys

from skchat.issuer_replay import RouteResult, iter_capability_probes, replay_one


def _bearer(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def run(base_url: str, hs_token: str, aud_token: str, timeout: float) -> list[RouteResult]:
    import httpx

    hs_headers = _bearer(hs_token)
    aud_headers = _bearer(aud_token)
    results: list[RouteResult] = []
    with httpx.Client(base_url=base_url, timeout=timeout) as client:
        for method, path, _cap in iter_capability_probes():
            results.append(replay_one(client, method, path, hs_headers, aud_headers))
    return results


def _report(results: list[RouteResult], as_json: bool) -> None:
    diverged = [r for r in results if not r.converged]
    if as_json:
        print(
            json.dumps(
                {
                    "total": len(results),
                    "converged": len(results) - len(diverged),
                    "diverged": [
                        {
                            "method": r.method,
                            "path": r.path,
                            "hs_status": r.hs_status,
                            "aud_status": r.aud_status,
                        }
                        for r in diverged
                    ],
                },
                indent=2,
            )
        )
        return
    for r in diverged:
        print(
            f"DIVERGE  {r.method:6} {r.path}  hs={r.hs_status} aud={r.aud_status}",
            file=sys.stderr,
        )
    print(f"{len(results) - len(diverged)}/{len(results)} routes converged")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument(
        "--base-url", required=True, help="running skchat server, e.g. http://127.0.0.1:8790"
    )
    ap.add_argument("--hs-token", required=True, help="HS256 operator session token")
    ap.add_argument(
        "--aud-token", required=True, help="operator-audience token for the SAME device"
    )
    ap.add_argument("--timeout", type=float, default=10.0)
    ap.add_argument("--json", action="store_true", help="machine-readable output")
    args = ap.parse_args(argv)

    results = run(args.base_url, args.hs_token, args.aud_token, args.timeout)
    _report(results, args.json)
    return 1 if any(not r.converged for r in results) else 0


if __name__ == "__main__":
    raise SystemExit(main())
