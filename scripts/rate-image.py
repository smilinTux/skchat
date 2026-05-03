#!/usr/bin/env python3
"""rate-image — record/inspect ComfyUI render ratings.

Usage:
  rate-image rate <image_id> <score> [--note "..."] [--via cli|telegram]
  rate-image rollup                              # recompute aggregate
  rate-image weights                             # show LoRA/ckpt/beat weights
  rate-image report [--top N] [--bottom N]       # best/worst performers
  rate-image show <image_id>                     # dump one sidecar

Score: 1..5  (1=hate, 3=neutral, 5=love)
Telegram reaction map: 👎=1 🤷=2 👍=3 ❤️=4 🔥=5
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

# Allow running from source tree without install.
HERE = Path(__file__).resolve()
SRC = HERE.parent.parent / "src"
if SRC.exists():
    sys.path.insert(0, str(SRC))

from skchat import rating  # noqa: E402


def cmd_rate(args: argparse.Namespace) -> int:
    rec = rating.record_score(args.image_id, args.score, note=args.note, via=args.via)
    if rec is None:
        print(f"no record found for {args.image_id}", file=sys.stderr)
        return 1
    rating.write_rollup()
    print(f"rated {rec.image_id} → {rec.score}/5 (rollup updated)")
    return 0


def cmd_rollup(_: argparse.Namespace) -> int:
    path = rating.write_rollup()
    data = json.loads(path.read_text())
    print(f"rollup → {path}")
    print(f"  total_rated: {data['total_rated']}")
    print(f"  loras tracked:       {len(data['loras'])}")
    print(f"  checkpoints tracked: {len(data['checkpoints'])}")
    print(f"  beats tracked:       {len(data['beats'])}")
    return 0


def cmd_weights(_: argparse.Namespace) -> int:
    rollup = rating.load_rollup()
    for bucket in ("loras", "checkpoints", "beats"):
        items = rollup.get(bucket, {})
        if not items:
            continue
        print(f"\n{bucket}:")
        rows = sorted(items.items(), key=lambda kv: -kv[1].get("weight", 1.0))
        for name, stats in rows:
            print(f"  {stats['weight']:>5.2f}x  n={stats['n']:>3}  μ={stats['mean']:.2f}  {name}")
    return 0


def cmd_report(args: argparse.Namespace) -> int:
    rollup = rating.load_rollup()
    for bucket in ("loras", "checkpoints", "beats"):
        items = list(rollup.get(bucket, {}).items())
        if not items:
            continue
        items.sort(key=lambda kv: -kv[1]["mean"])
        print(f"\n[{bucket}] top {args.top}:")
        for name, stats in items[: args.top]:
            print(f"  μ={stats['mean']:.2f}  n={stats['n']:>3}  {name}")
        if len(items) > args.top:
            print(f"\n[{bucket}] bottom {args.bottom}:")
            for name, stats in items[-args.bottom :]:
                print(f"  μ={stats['mean']:.2f}  n={stats['n']:>3}  {name}")
    return 0


def cmd_show(args: argparse.Namespace) -> int:
    path = rating.RATINGS_DIR / f"{args.image_id}.json"
    if not path.exists():
        candidates = list(rating.RATINGS_DIR.glob(f"{args.image_id}*.json"))
        if len(candidates) != 1:
            print(f"no record found for {args.image_id}", file=sys.stderr)
            return 1
        path = candidates[0]
    print(path.read_text())
    return 0


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="rate-image", description=__doc__)
    sub = p.add_subparsers(dest="cmd", required=True)

    r = sub.add_parser("rate")
    r.add_argument("image_id")
    r.add_argument("score", type=int, choices=range(1, 6))
    r.add_argument("--note", default=None)
    r.add_argument("--via", default="cli")
    r.set_defaults(func=cmd_rate)

    sub.add_parser("rollup").set_defaults(func=cmd_rollup)
    sub.add_parser("weights").set_defaults(func=cmd_weights)

    rep = sub.add_parser("report")
    rep.add_argument("--top", type=int, default=5)
    rep.add_argument("--bottom", type=int, default=3)
    rep.set_defaults(func=cmd_report)

    s = sub.add_parser("show")
    s.add_argument("image_id")
    s.set_defaults(func=cmd_show)

    args = p.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
