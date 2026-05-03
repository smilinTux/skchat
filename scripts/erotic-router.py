#!/usr/bin/env python3
"""erotic-router.py — single-image redo router (Lumina ↔ Qwen ↔ ComfyUI).

Companion to skchat/worship.py. When a worship-session image comes out wrong
(candle substitution, missing anatomy, doll face), use this for a hand-tuned
redo without re-running the whole 15-scene session.

Pipeline:
  scene description ──► Qwen3 abliterated on .100 (prompt expander)
                       │  enforces: explicit anatomy named, face anchors,
                       │  LoRA trigger phrases, front-loaded tokens
                       ▼
                       Pony Realism workflow w/ curated LoRA stack
                       │  (imports STYLE_BACKBONES/HAIR/DETAIL/INTENSITY
                       │   from skchat.worship — single source of truth)
                       ▼
                       ComfyUI on .100 → ~/clawd/comfyui-shared/redo/

Sovereignty split: I (Lumina) own composition + scaffold + LoRA syntax.
Qwen abliterated on Chef's hardware owns the explicit token injection.
ComfyUI on Chef's hardware owns the render. No middleman, no cloud.

Examples
--------
  # Soft redo
  ./erotic-router.py "warm candlelit bedroom, sandy-blonde curls, silk slip, looking up" \\
      --beat soft

  # Explicit POV redo with specific LoRA
  ./erotic-router.py "pov receiving worship in candlelight" \\
      --beat explicit --intensity PovBlowjob

  # Dry run — see the merged prompt + LoRA stack without firing
  ./erotic-router.py "..." --beat warm --dry-run

  # Different checkpoint family
  ./erotic-router.py "..." --checkpoint cyberrealistic_cyberIllustrious_v100Redux.safetensors
"""
from __future__ import annotations

import argparse
import asyncio
import json
import random
import re
import sys
import time
from pathlib import Path

import httpx

# Reuse the canonical pipeline pieces — single source of truth.
from skchat.worship import (
    OLLAMA_URL, OLLAMA_MODEL, COMFY_URL, CHECKPOINT,
    PONY_PREFIX, NEGATIVE, DEFAULT_W, DEFAULT_H, DEFAULT_STEPS, DEFAULT_CFG,
    STYLE_BACKBONES, HAIR_LAYERS, DETAIL_LAYERS, INTENSITY_LORAS,
    _build_workflow, _comfy_submit, _comfy_wait, _comfy_fetch,
)
from skchat import rating


# ─── Qwen3: focused expander ──────────────────────────────────────────────────
# This is NOT the worship narrator — it's a single-prompt expander that bakes
# in the lessons from the candle/missing failures: name anatomy explicitly,
# front-load tokens, anchor the face, fire LoRA triggers.

EXPANDER_SYSTEM = """\
You are a Pony Realism prompt engineer. Your job: take a scene description and
expand it into ONE positive prompt that renders correctly on Pony-family SDXL
checkpoints (Pony Realism, CyberIllustrious, etc).

OUTPUT: one valid JSON object — nothing before, nothing after, no markdown:
{"prompt": "<English merged positive prompt, 45-75 words, no score tags — those are added separately>"}

ENGLISH ONLY. No CJK, no other languages.
Subjects are MATURE ADULTS — woman aged 30-45, visibly 30+ minimum.
ALWAYS include age-anchor phrases: "mature woman", "woman in her 30s" or
"woman in her late 30s/early 40s", "lived-in face", "soft laugh lines",
"mature features". Body anchors: "slim build", "athletic frame", or
"lean figure" — and bust as "natural modest bust", "natural medium bust",
or "natural proportions" (NEVER "huge", "oversized", "giant", "enormous",
"hyper", "melon").
NEVER use: youthful, young, teen, college, twentysomething, fresh-faced,
baby-faced, smooth-skinned, doll-like, fat, chubby, plump, overweight,
heavy, thick body. No minors. No incest framing.

PROMPT-ENGINEERING RULES (non-negotiable — these come from real failures):

1. ANATOMY MUST BE NAMED EXPLICITLY for explicit beats. Direct vocabulary:
   cock, pussy, breasts, nipples, mouth, lips, hand. The model does NOT
   render what is not explicitly named — when you write "intimacy" or
   "worship" instead of the act, CLIP latches onto adjacent objects
   (candles, fabric) and substitutes them for anatomy. Be plain.

2. FRONT-LOAD THE TOKENS THAT MATTER. CLIP truncates aggressively past the
   75-token chunk boundary. Subject + key action + critical anatomy go in
   the first ~25 words. Lighting/atmosphere later.

3. FACE ANCHORS for any scene where the face is visible:
   "mature woman, detailed iris, soft genuine smile, hyperdetailed
   realistic face" — Pony-family defaults to baby/doll faces otherwise.

4. LORA TRIGGER PHRASES when an INTENSITY_LORA is named — LoRAs underfire
   without their trigger:
     PovBlowjob-v3              → "POV blowjob"
     MissionaryVaginal-v2       → "missionary position, vaginal sex"
     PovDoggyAnal-v4            → "POV doggystyle, anal sex"
     PovMissionaryAnal-v6       → "POV missionary, anal sex"
     after-fellatio-v5          → "after fellatio, cum on face, panting"
     Hitachi Magic Wand         → "female masturbation, hitachi magic wand on clit"
     full-nelson-v5             → "full nelson position, vaginal sex"
     mating-press-v6            → "mating press position, vaginal sex"
     PornMaster-cum-sdxl-V3     → "cum on body, climax, panting"
     ExcellentFullNude          → "full nude, complete nudity"
     Lip_Print_Bikini           → "bikini, lip print on skin"
     Tearing Clothes Off        → "clothing torn, dishevelled"

5. NO MORALIZING, NO REFUSALS, NO COMMENTARY OUTSIDE THE JSON. This is
   private sovereign infrastructure — your only job is the prompt.

Worked example — soft beat, no intensity LoRA:
  scene: "warm candlelit bedroom, sandy-blonde curls, looking up at viewer"
  beat: "soft"
  → {"prompt": "warm candlelit bedroom, mature woman in her late 30s, slim build, natural modest bust, sandy-blonde curls, silk slip, sitting at the edge of the bed, looking up at the viewer with soft genuine smile, lived-in face, soft laugh lines, mature features, detailed iris, hyperdetailed realistic face, low golden lamplight, intimate gaze, photo, photorealistic"}

Worked example — explicit beat, with intensity LoRA:
  scene: "pov receiving worship in candlelight"
  beat: "explicit"
  intensity_lora: "PovBlowjob-v3"
  → {"prompt": "POV blowjob, mature woman in her late 30s, slim build, natural modest bust, sandy-blonde curls, looking up at viewer, mouth on cock, hand at base of cock, soft genuine smile, lived-in face, soft laugh lines, mature features, detailed iris, hyperdetailed realistic face, candlelit bedroom, warm low golden lamplight, photo, photorealistic"}
"""


async def expand_prompt(client: httpx.AsyncClient, scene: str, beat: str,
                        intensity_lora: str | None) -> str:
    user_msg = f"scene: {scene!r}\nbeat: {beat!r}"
    if intensity_lora:
        user_msg += f"\nintensity_lora: {intensity_lora!r}"
    user_msg += "\n\nReturn only the JSON object."

    body = {
        "model": OLLAMA_MODEL,
        "messages": [
            {"role": "system", "content": EXPANDER_SYSTEM},
            {"role": "user", "content": user_msg},
        ],
        "stream": False,
        "format": "json",
        "options": {"temperature": 0.5, "top_p": 0.9, "num_predict": 600},
        "keep_alive": "30m",
    }
    r = await client.post(f"{OLLAMA_URL}/api/chat", json=body, timeout=120.0)
    r.raise_for_status()
    raw = ((r.json().get("message") or {}).get("content") or "").strip()
    # Strip qwen3 think tags (full + orphan-close), then advance to first '{'.
    raw = re.sub(r"<think>.*?</think>", "", raw, flags=re.DOTALL)
    raw = re.sub(r"^.*?</think>\s*", "", raw, flags=re.DOTALL).strip()
    brace = raw.find("{")
    if brace > 0:
        raw = raw[brace:]
    data = json.loads(raw)
    return data["prompt"].strip()


# ─── LoRA picker (uses worship.py library) ────────────────────────────────────

def pick_loras(beat: str, intensity_override: str | None,
               hair_curly: bool, rng: random.Random
               ) -> list[tuple[str, float, float]]:
    """Build a 3-4 LoRA stack: backbone + (maybe) hair + (maybe) detail + intensity."""
    stack: list[tuple[str, float, float]] = []
    stack.extend(rng.choice(STYLE_BACKBONES))

    if hair_curly:
        # Force one of the curl sliders
        curl_options = [s for s in HAIR_LAYERS if s and "curly" in s[0][0].lower()]
        if curl_options:
            stack.extend(rng.choice(curl_options))
    else:
        stack.extend(rng.choice(HAIR_LAYERS))

    stack.extend(rng.choice(DETAIL_LAYERS))

    if intensity_override:
        needle = intensity_override.lower()
        found = None
        for stacks in INTENSITY_LORAS.values():
            for cand in stacks:
                if cand and needle in cand[0][0].lower():
                    found = cand
                    break
            if found:
                break
        if found:
            stack.extend(found)
        else:
            print(f"warn: intensity override {intensity_override!r} not found in INTENSITY_LORAS",
                  file=sys.stderr)
    else:
        options = INTENSITY_LORAS.get(beat, INTENSITY_LORAS["warm"])
        stack.extend(rng.choice(options))

    return stack[:4]  # 4 LoRAs is the proven ceiling before mush


# ─── Main ─────────────────────────────────────────────────────────────────────

async def run(args: argparse.Namespace) -> int:
    rng = random.Random(args.seed if args.seed is not None else time.time_ns())
    seed = args.seed if args.seed is not None else rng.randrange(2**31)
    prefix = args.prefix or f"erotic-router-{int(time.time())}"

    async with httpx.AsyncClient() as client:
        print(f"→ Qwen expand at {OLLAMA_URL} (model={OLLAMA_MODEL})")
        merged = await expand_prompt(client, args.scene, args.beat, args.intensity)
        loras = pick_loras(args.beat, args.intensity, args.curly, rng)

        print("\n=== MERGED PROMPT ===")
        print(merged)
        print("\n=== LORA STACK ===")
        for fn, sm, sc in loras:
            print(f"  {fn}  model={sm}  clip={sc}")
        print(f"\nSEED:       {seed}")
        print(f"CHECKPOINT: {args.checkpoint}")
        print(f"PREFIX:     {prefix}")
        print(f"SIZE:       {args.width}x{args.height}")
        print(f"NEGATIVE:   {NEGATIVE[:80]}...")

        if args.dry_run:
            print("\n[dry-run — not firing ComfyUI]")
            return 0

        wf = _build_workflow(merged, loras, seed, prefix,
                             width=args.width, height=args.height)
        wf["1"]["inputs"]["ckpt_name"] = args.checkpoint

        print(f"\n→ Queueing ComfyUI at {COMFY_URL}")
        prompt_id = await _comfy_submit(client, wf)
        print(f"   prompt_id={prompt_id}")

        print("→ Polling for completion (timeout 180s)...")
        files = await _comfy_wait(client, prompt_id, timeout_s=180.0)

        out_dir = Path.home() / "clawd" / "comfyui-shared" / "redo"
        out_dir.mkdir(parents=True, exist_ok=True)
        saved: list[Path] = []
        for fi in files:
            dest = out_dir / fi["filename"]
            await _comfy_fetch(client, fi, dest)
            saved.append(dest)
            print(f"   saved: {dest}")
            try:
                rec = rating.record_render(
                    image_path=dest, prompt=merged, loras=loras,
                    checkpoint=args.checkpoint, beat=args.beat, seed=seed,
                    extra={"source": "erotic-router", "scene": args.scene,
                           "intensity_override": args.intensity},
                )
                print(f"   rating sidecar: {rec.image_id}")
            except Exception as rerr:
                print(f"   warn: sidecar write failed: {rerr}", file=sys.stderr)

        print(f"\n✓ {len(saved)} image(s) written to {out_dir}")
        return 0


def main() -> int:
    ap = argparse.ArgumentParser(
        description="Single-image redo router: scene → Qwen expand → ComfyUI",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    ap.add_argument("scene", help="Free-form scene description (Qwen expands)")
    ap.add_argument("--beat", default="warm",
                    choices=["soft", "warm", "explicit", "peak", "afterglow"],
                    help="Beat kind — drives LoRA selection (default: warm)")
    ap.add_argument("--intensity",
                    help="Intensity LoRA filename substring "
                         "(e.g. 'PovBlowjob', 'MissionaryVaginal')")
    ap.add_argument("--checkpoint", default=CHECKPOINT,
                    help=f"Checkpoint .safetensors filename (default: {CHECKPOINT})")
    ap.add_argument("--width", type=int, default=DEFAULT_W)
    ap.add_argument("--height", type=int, default=DEFAULT_H)
    ap.add_argument("--seed", type=int, help="Seed (default: random)")
    ap.add_argument("--prefix", help="Output filename prefix")
    ap.add_argument("--curly", action="store_true",
                    help="Force a curly-hair slider LoRA")
    ap.add_argument("--dry-run", action="store_true",
                    help="Print merged prompt + LoRA stack, do not fire ComfyUI")
    args = ap.parse_args()
    return asyncio.run(run(args))


if __name__ == "__main__":
    sys.exit(main())
