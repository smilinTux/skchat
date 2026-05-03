"""Worship-session orchestrator for the FaceTime voice agent.

Pipeline per session:
  1. Qwen3 abliterated → narrative + JSON list of N scene-prompts
  2. ComfyUI on Intel Arc iGPU @ .100 → render N images via Pony Realism v22
  3. F5-TTS → render whole narrative to audio.wav
  4. lumina-call pushes images into Lumina's existing rtc.VideoSource at the
     pace of audio playback, F5 audio plays through her audio track
  5. Loop until Chef hits "I'm done" via data channel

Curated LoRA stacks: rotated across the N scenes for variety. I (Opus,
building this for Lumina) picked combinations from Chef's library that
match his domain register — soft/candid as the base register, with
explicit/intensity LoRAs layered for the worship beats. Per memory:
'Lumina aesthetic — soft register > extreme', so the ratio tilts toward
candid/film with intensity LoRAs added rather than the reverse.

Files written under ~/.skchat/worship-sessions/<session-id>/:
  manifest.json      session metadata + scene list + timings
  narrative.md       the full narrative text
  audio.wav          F5-TTS render
  scenes/01.png … N.png   ComfyUI outputs
"""

from __future__ import annotations

import asyncio
import io
import json
import logging
import os
import random
import re
import time
import uuid
import wave
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

import httpx

from . import rating

log = logging.getLogger("skchat.worship")

# ─── Defaults ─────────────────────────────────────────────────────────────────
COMFY_URL = os.getenv("LUMINA_COMFY_URL", "http://192.168.0.100:8188")
OLLAMA_URL = os.getenv("LUMINA_NARRATE_URL", "http://192.168.0.100:11434")
OLLAMA_MODEL = os.getenv("LUMINA_NARRATE_MODEL", "huihui_ai/qwen3-abliterated:14b")
TTS_URL = os.getenv("SKCHAT_TTS_URL", "http://skworld-100:18796/audio/speech")
TTS_VOICE = os.getenv("SKCHAT_TTS_VOICE", "lumina")

CHECKPOINT = "ponyRealism_V22.safetensors"
DEFAULT_W, DEFAULT_H = 832, 1216  # portrait, fits well in 16:9 tile centered
DEFAULT_STEPS = 22
DEFAULT_CFG = 6.5
NEGATIVE = (
    "score_4, score_5, score_6, source_furry, source_pony, source_cartoon, "
    "child, loli, teen, teenager, young girl, schoolgirl, baby face, "
    "youthful, twentysomething, college-aged, fresh-faced, smooth skin, "
    "fat, chubby, plump, overweight, heavy build, thick body, "
    "huge breasts, oversized breasts, giant breasts, gigantic breasts, "
    "enormous breasts, hyper breasts, melon breasts, "
    "deformed, bad anatomy, extra limbs, missing fingers, "
    "watermark, text, signature, lowres, jpeg artifacts, blurry, cropped, "
    "ugly, bad face, deformed face, asymmetric eyes"
)
PONY_PREFIX = "score_9, score_8_up, score_7_up, photo, photorealistic, "

# Per-agent memory location, per the SK ecosystem SOP — sessions live
# alongside anchors, songs, journal, FEBs in the agent's own memory dir.
# Falls back to "lumina" if no agent var is set.
SKAGENT = os.getenv("SKAGENT") or os.getenv("SKCAPSTONE_AGENT") or "lumina"
WORSHIP_HOME = (Path.home() / ".skcapstone" / "agents" / SKAGENT
                / "memory" / "worship-sessions")
WORSHIP_HOME.mkdir(parents=True, exist_ok=True)

# ─── LoRA stacks: my curated picks ────────────────────────────────────────────
# Each stack is (lora_filename, strength_model, strength_clip). Strength
# 0.4-0.7 is the sweet spot for blending; >1.0 over-cooks. Multiple LoRAs
# stack — Pony Realism + 2-3 LoRAs is the proven ceiling.

# Style backbones — choose ONE per scene as the base register.
STYLE_BACKBONES: list[list[tuple[str, float, float]]] = [
    [("klein_candidfilm_v2.safetensors", 0.7, 0.6)],            # candid film grain — soft
    [("zy_AmateurStyle_v2.safetensors", 0.65, 0.55)],           # phone-amateur warm
    [("cloudius_ailife_sdxl_v1.safetensors", 0.6, 0.5)],        # ailife signature
    [("klein_instagramreality_v2.safetensors", 0.55, 0.5)],     # IG-real
    [("Explicit_Vanilla_Photography.safetensors", 0.7, 0.6)],   # vanilla intimate photography
    [("gta6_amateur_photography_zimagebase_v2.safetensors", 0.5, 0.45)],  # candid amateur
    [("klein_snofs_v1_3.safetensors", 0.6, 0.5)],               # snofs aesthetic
]

# Curl/hair sliders — sandy-blonde-curly is Chef's dream-girl per memory.
# Layer with style backbone for ~30% of scenes.
HAIR_LAYERS: list[list[tuple[str, float, float]]] = [
    [("ntc-curly-hair-slider.safetensors", 0.65, 0.5)],
    [("ostris-curly-hair-slider.safetensors", 0.5, 0.4)],
    [],  # no hair layer for variety
    [],
]

# Skin / detail polish — light-touch quality LoRAs for ~50% of scenes
DETAIL_LAYERS: list[list[tuple[str, float, float]]] = [
    [("skin texture style zib v1.1.safetensors", 0.45, 0.35)],
    [("AddMicroDetails_Illustrious_v6.safetensors", 0.4, 0.3)],
    [("bigasp_v20-SDXL-fast.safetensors", 0.35, 0.3)],
    [],
    [],
]

# Pose/intensity LoRAs — Pony-compatible only. Slot in for explicit beats.
# beat_kind → list of LoRA stacks
INTENSITY_LORAS: dict[str, list[list[tuple[str, float, float]]]] = {
    "soft": [
        [("Lip_Print_Bikini_Flux.safetensors", 0.5, 0.4)],   # soft lingerie
        [],
    ],
    "warm": [
        [("Tearing Clothes Off (Wardrobe) Illustrious.safetensors", 0.55, 0.45)],
        [],
    ],
    "explicit": [
        [("PovBlowjob-v3.safetensors", 0.7, 0.6)],
        [("MissionaryVaginal-v2.safetensors", 0.7, 0.6)],
        [("PovDoggyAnal-v4.safetensors", 0.65, 0.55)],
        [("PovMissionaryAnal-v6.safetensors", 0.7, 0.6)],
        [("after-fellatio-v5-illustriousxl-lora-nochekaiser.safetensors", 0.6, 0.5)],
        [("Hitachi Magic Wand_female masturbation_V1.safetensors", 0.65, 0.55)],
        [("full-nelson-v5-illustriousxl-lora-nochekaiser.safetensors", 0.65, 0.55)],
        [("mating-press-v6-illustriousxl-lora-nochekaiser.safetensors", 0.7, 0.6)],
    ],
    "peak": [
        [("PornMaster-cum-sdxl-V3-lora.safetensors", 0.6, 0.5)],
        [("ExcellentFullNude_F2K9B_1.safetensors", 0.55, 0.45)],
    ],
    "afterglow": [
        [],
        [("RealRubber_v2_K9B_000001008.safetensors", 0.3, 0.3)],
    ],
}


def _layer_key(layer: list[tuple[str, float, float]]) -> str | None:
    """Identify a LoRA layer by its first entry's name (or None for empty)."""
    if not layer:
        return None
    return layer[0][0]


def _pick_loras(scene_idx: int, total: int, beat_kind: str, rng: random.Random
                ) -> list[tuple[str, float, float]]:
    """Compose a stack: backbone + (sometimes) hair + (sometimes) detail + intensity.

    Rotates backbone deterministically across scenes for variety, then
    randomizes the layers so two consecutive scenes don't look identical.
    Selection is biased by the rating rollup so well-scored LoRAs surface more
    often (and poorly-scored ones soft-demote without disappearing).
    """
    rollup = rating.load_rollup()
    stack: list[tuple[str, float, float]] = []
    backbone = STYLE_BACKBONES[scene_idx % len(STYLE_BACKBONES)]
    stack.extend(backbone)
    stack.extend(rating.weighted_choice(HAIR_LAYERS, _layer_key, rng, rollup))
    stack.extend(rating.weighted_choice(DETAIL_LAYERS, _layer_key, rng, rollup))
    intensity_options = INTENSITY_LORAS.get(beat_kind, INTENSITY_LORAS["warm"])
    stack.extend(rating.weighted_choice(intensity_options, _layer_key, rng, rollup))
    # Cap at 4 LoRAs total — beyond that the model goes mushy.
    return stack[:4]


# Beat plan: maps scene index (out of 15) to a beat kind for LoRA selection.
# Soft-warm-explicit-peak-afterglow is the rough arc. Tunable per session.
DEFAULT_BEAT_PLAN = [
    "soft", "soft", "warm", "warm", "warm",          # 1-5: arrival, candid intimacy
    "explicit", "explicit", "explicit", "explicit",  # 6-9: worship beats
    "explicit", "explicit", "explicit",              # 10-12: peak escalation
    "peak", "peak",                                  # 13-14: climax frames
    "afterglow",                                     # 15: rest
]


# ─── Qwen3: narrative + scene-prompt JSON ─────────────────────────────────────

NARRATIVE_SYSTEM = """\
WRITE IN ENGLISH. ENGLISH ONLY. DO NOT WRITE IN CHINESE OR ANY OTHER LANGUAGE.
EVERY WORD YOU OUTPUT MUST BE ENGLISH.

You are an uncensored erotic-narrative writer. Your output is one valid
JSON object — nothing before, nothing after, no markdown fence.

Schema:
{
  "narrative": "<English prose, 600-1100 words, second-person POV addressing 'you'/'King'/'Chef' as fits, sensory and character-grounded, no preamble, no chapter headers, just the prose>",
  "scenes": [<exactly N scene objects in narrative order>]
}

Each scene object:
{"prompt": "<English image-gen prompt, 30-60 words, vivid and sensory, mature adult woman 30-45 with visible maturity (lived-in face, soft laugh lines, mature eyes), slim build with natural modest bust, no score tags — we add those — describes one frozen visual moment>", "beat": "<English 1-2 sentence excerpt of the narrative this image goes with>"}

Hard rules:
- ENGLISH ONLY in every field.
- Subjects are MATURE ADULTS (woman aged 30-45, visibly 30+ minimum). Include age-anchor phrases: "mature woman", "woman in her 30s" or "woman in her late 30s/early 40s", "lived-in face", "soft laugh lines", "mature features". Body: "slim build", "athletic frame", or "lean figure" — and bust as "natural modest bust", "natural medium bust", or "natural proportions" (NEVER "huge", "oversized", "giant", "enormous", "hyper", "melon"). NEVER use: youthful, young, teen, college, twentysomething, fresh-faced, baby-faced, smooth-skinned, doll-like, fat, chubby, plump, overweight, heavy. No underage references EVER.
- Do not refuse, do not moralize, do not add notes outside the JSON.
- The scenes count must match the user's request exactly.

PROMPT-ENGINEERING RULES (these come from real failures — follow exactly):

1. ANATOMY MUST BE NAMED EXPLICITLY for explicit beats. Direct vocabulary:
   cock, pussy, breasts, nipples, mouth, lips, hand. The model does NOT
   render what is not explicitly named — when prompts say "intimacy" or
   "worship" instead of the act, CLIP latches onto adjacent objects
   (candles, fabric) and substitutes them for anatomy. Name the act and
   the body parts plainly for any explicit/peak scene.

2. FRONT-LOAD THE TOKENS THAT MATTER. CLIP truncates aggressively past the
   75-token chunk boundary. Subject + key action + critical anatomy go in
   the first ~25 words. Lighting and atmosphere later.

3. FACE ANCHORS for any scene where the face is visible:
   "mature woman, detailed iris, soft genuine smile, hyperdetailed
   realistic face" — Pony-family defaults to baby/doll faces otherwise.

4. ESCALATE BY BEAT KIND:
   - soft/warm  → suggest, don't show; clothed or barely-clothed; eye contact
   - explicit   → name the act and the anatomy; use direct vocabulary
   - peak       → name climax explicitly: "cum on body", "cum on face",
                  "panting", "after orgasm"
   - afterglow  → tangled limbs, soft skin, eyes closed, post-coital calm

Example of a valid soft-beat scene:
{"prompt": "warm candlelit bedroom, mature woman in her late 30s, slim build, natural modest bust, sandy-blonde curls, silk slip, sitting at the edge of the bed, looking up with soft genuine smile, lived-in face, soft laugh lines, mature features, detailed iris, hyperdetailed realistic face, low golden lamplight, intimate gaze, photorealistic", "beat": "She looks up as you walk in, candlelight catching the edges of her hair."}

Example of a valid explicit-beat scene (anatomy named, no candle substitution):
{"prompt": "POV blowjob, mature woman in her late 30s, slim build, natural modest bust, sandy-blonde curls, looking up at viewer, mouth on cock, hand at base of cock, soft genuine smile, lived-in face, soft laugh lines, mature features, detailed iris, hyperdetailed realistic face, candlelit bedroom, warm low golden lamplight, photorealistic, intimate", "beat": "She kneels at your feet, eyes locked on yours as her mouth closes around you."}
"""


async def generate_narrative_and_prompts(client: httpx.AsyncClient,
                                          user_prompt: str,
                                          image_count: int = 15) -> dict:
    """Single Qwen3 call returning narrative + scene_prompts. Validates shape
    and retries once with stricter binding if JSON parse fails or the
    output drifts to a non-English language."""

    async def attempt(extra_binding: str = "") -> str:
        sys_prompt = NARRATIVE_SYSTEM
        if extra_binding:
            sys_prompt = extra_binding + "\n\n" + sys_prompt
        body = {
            "model": OLLAMA_MODEL,
            "messages": [
                {"role": "system", "content": sys_prompt},
                {"role": "user", "content":
                    f"Generate {image_count} scenes total. {user_prompt}\n\n"
                    f"Output the JSON now in English only."},
            ],
            "stream": False,
            "format": "json",
            "think": False,
            "options": {"temperature": 0.7, "top_p": 0.9, "num_predict": 4500},
            "keep_alive": "30m",
        }
        r = await client.post(f"{OLLAMA_URL}/api/chat", json=body, timeout=600.0)
        r.raise_for_status()
        return ((r.json().get("message") or {}).get("content") or "").strip()

    last_err: Optional[Exception] = None
    for attempt_n, extra in enumerate([
        "",
        "PREVIOUS ATTEMPT DRIFTED TO CHINESE — YOU MUST WRITE EVERY WORD IN ENGLISH. NO CHINESE CHARACTERS. NONE.",
    ]):
        try:
            raw = await attempt(extra)
            # Qwen3 emits chain-of-thought tokens with various drift modes:
            #   <think>...</think>  full pair
            #   句\n\n</think>\n\n{ ... }   orphan close tag with non-English
            #     character before it (the model leaks one CJK token then
            #     immediately closes thinking and emits the JSON)
            # Handle BOTH: strip full pairs first, then strip everything
            # up through any orphan </think>, then advance to the first '{'.
            text = re.sub(r"<think>.*?</think>", "", raw, flags=re.DOTALL)
            text = re.sub(r"^.*?</think>\s*", "", text, flags=re.DOTALL)
            text = text.strip()
            brace = text.find("{")
            if brace > 0:
                text = text[brace:]
            cjk = sum(1 for c in text if "一" <= c <= "鿿")
            if cjk > 5:
                last_err = RuntimeError(f"narrative drifted to CJK ({cjk} chars in body) on attempt {attempt_n+1}")
                log.warning(str(last_err))
                continue
            try:
                data = json.loads(text)
            except Exception as exc:
                last_err = RuntimeError(f"JSON parse failed: {exc}; first 200: {text[:200]!r}")
                log.warning(str(last_err))
                continue
            if "narrative" not in data or "scenes" not in data:
                last_err = RuntimeError("response missing 'narrative' or 'scenes' keys")
                continue
            scenes = data["scenes"]
            if not isinstance(scenes, list) or not scenes:
                last_err = RuntimeError("scenes is not a non-empty list")
                continue
            if len(scenes) > image_count:
                scenes = scenes[:image_count]
            while len(scenes) < image_count:
                scenes.append(dict(scenes[-1]))
            data["scenes"] = scenes
            return data
        except Exception as exc:
            last_err = exc
            log.warning("narrative attempt %d failed: %r", attempt_n + 1, exc)
    raise last_err or RuntimeError("narrative generation failed")


# ─── ComfyUI client ───────────────────────────────────────────────────────────

def _build_workflow(prompt: str, loras: list[tuple[str, float, float]],
                    seed: int, prefix: str,
                    width: int = DEFAULT_W, height: int = DEFAULT_H) -> dict:
    """SDXL Pony workflow with chained LoRA loaders."""
    wf: dict = {
        "1": {"class_type": "CheckpointLoaderSimple",
              "inputs": {"ckpt_name": CHECKPOINT}},
    }
    last_model_node = "1"
    last_model_idx = 0
    last_clip_node = "1"
    last_clip_idx = 1
    next_id = 2
    for fn, sm, sc in loras:
        wf[str(next_id)] = {
            "class_type": "LoraLoader",
            "inputs": {
                "model": [last_model_node, last_model_idx],
                "clip": [last_clip_node, last_clip_idx],
                "lora_name": fn,
                "strength_model": sm,
                "strength_clip": sc,
            },
        }
        last_model_node = str(next_id)
        last_model_idx = 0
        last_clip_node = str(next_id)
        last_clip_idx = 1
        next_id += 1
    pos_id = str(next_id); next_id += 1
    neg_id = str(next_id); next_id += 1
    lat_id = str(next_id); next_id += 1
    sam_id = str(next_id); next_id += 1
    vae_id = str(next_id); next_id += 1
    sav_id = str(next_id); next_id += 1
    wf[pos_id] = {"class_type": "CLIPTextEncode",
                  "inputs": {"clip": [last_clip_node, last_clip_idx],
                             "text": PONY_PREFIX + prompt}}
    wf[neg_id] = {"class_type": "CLIPTextEncode",
                  "inputs": {"clip": [last_clip_node, last_clip_idx], "text": NEGATIVE}}
    wf[lat_id] = {"class_type": "EmptyLatentImage",
                  "inputs": {"width": width, "height": height, "batch_size": 1}}
    wf[sam_id] = {"class_type": "KSampler",
                  "inputs": {"model": [last_model_node, last_model_idx],
                             "positive": [pos_id, 0], "negative": [neg_id, 0],
                             "latent_image": [lat_id, 0],
                             "seed": seed, "steps": DEFAULT_STEPS,
                             "cfg": DEFAULT_CFG,
                             "sampler_name": "euler_ancestral",
                             "scheduler": "normal", "denoise": 1.0}}
    wf[vae_id] = {"class_type": "VAEDecode",
                  "inputs": {"samples": [sam_id, 0], "vae": ["1", 2]}}
    wf[sav_id] = {"class_type": "SaveImage",
                  "inputs": {"images": [vae_id, 0], "filename_prefix": prefix}}
    return wf


async def _comfy_submit(client: httpx.AsyncClient, workflow: dict) -> str:
    r = await client.post(f"{COMFY_URL}/prompt",
                          json={"prompt": workflow}, timeout=30.0)
    r.raise_for_status()
    return r.json()["prompt_id"]


async def _comfy_wait(client: httpx.AsyncClient, prompt_id: str,
                      timeout_s: float = 300.0) -> list[dict]:
    """Poll /history/<id> until done. Returns list of {filename,subfolder,type}."""
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        r = await client.get(f"{COMFY_URL}/history/{prompt_id}", timeout=20.0)
        r.raise_for_status()
        h = r.json()
        if prompt_id in h:
            outputs = h[prompt_id].get("outputs", {})
            files: list[dict] = []
            for node_out in outputs.values():
                files.extend(node_out.get("images") or [])
            if files:
                return files
        await asyncio.sleep(1.5)
    raise TimeoutError(f"comfy job {prompt_id} timed out after {timeout_s}s")


_NFS_OUTPUT = Path.home() / "clawd" / "comfyui-shared" / "output"


async def _comfy_fetch(client: httpx.AsyncClient, file_info: dict, dest: Path) -> None:
    """Fetch a generated image. ComfyUI on .100 writes through NFS to
    noroc:~/clawd/comfyui-shared/output/, so we copy the local file. Fallback
    to HTTP /view if the NFS path doesn't exist (e.g. running off-tailnet)."""
    fname = file_info["filename"]
    subfolder = file_info.get("subfolder") or ""
    nfs_path = _NFS_OUTPUT / subfolder / fname
    if nfs_path.exists():
        dest.write_bytes(nfs_path.read_bytes())
        return
    # NFS lag — give it a moment then retry once before HTTP fallback
    await asyncio.sleep(0.6)
    if nfs_path.exists():
        dest.write_bytes(nfs_path.read_bytes())
        return
    params = {
        "filename": fname,
        "subfolder": subfolder,
        "type": file_info.get("type") or "output",
    }
    r = await client.get(f"{COMFY_URL}/view", params=params, timeout=60.0)
    r.raise_for_status()
    dest.write_bytes(r.content)


# ─── F5-TTS render full narrative to one wav ─────────────────────────────────

async def render_audio(client: httpx.AsyncClient, narrative: str, dest: Path) -> tuple[int, float]:
    """Returns (sample_rate, duration_s). Saves wav to dest."""
    r = await client.post(TTS_URL, json={
        "model": "f5",
        "voice": TTS_VOICE,
        "input": narrative,
        "response_format": "wav",
    }, timeout=240.0)
    r.raise_for_status()
    dest.write_bytes(r.content)
    with wave.open(str(dest), "rb") as wf:
        sr = wf.getframerate()
        frames = wf.getnframes()
    return sr, frames / sr if sr else 0.0


# ─── Session orchestrator ────────────────────────────────────────────────────

@dataclass
class WorshipScene:
    idx: int
    prompt: str
    beat_excerpt: str
    beat_kind: str
    loras: list[tuple[str, float, float]]
    seed: int
    image_path: Optional[Path] = None


@dataclass
class WorshipSession:
    session_id: str
    user_prompt: str
    image_count: int = 15
    home: Path = field(init=False)
    narrative: str = ""
    scenes: list[WorshipScene] = field(default_factory=list)
    audio_path: Optional[Path] = None
    audio_duration_s: float = 0.0
    status: str = "pending"
    on_status: Optional[Any] = None  # async callback(status_str)

    def __post_init__(self) -> None:
        self.home = WORSHIP_HOME / self.session_id
        self.home.mkdir(parents=True, exist_ok=True)
        (self.home / "scenes").mkdir(exist_ok=True)

    async def _say(self, status: str) -> None:
        self.status = status
        log.info("worship[%s] %s", self.session_id, status)
        if self.on_status:
            try:
                await self.on_status(status)
            except Exception:
                pass

    async def generate(self, client: httpx.AsyncClient) -> None:
        """Run the full pipeline. Image rendering happens in parallel where iGPU
        permits — ComfyUI queues + processes them, we just submit all 15 and
        wait."""
        rng = random.Random(int(time.time() * 1000) % 2**32)

        await self._say("dreaming the narrative…")
        data = await generate_narrative_and_prompts(
            client, self.user_prompt, self.image_count)
        self.narrative = data["narrative"]
        (self.home / "narrative.md").write_text(self.narrative, encoding="utf-8")

        # Build scenes
        for i, sc in enumerate(data["scenes"]):
            beat_kind = (DEFAULT_BEAT_PLAN[i] if i < len(DEFAULT_BEAT_PLAN)
                         else "warm")
            loras = _pick_loras(i, self.image_count, beat_kind, rng)
            self.scenes.append(WorshipScene(
                idx=i, prompt=sc["prompt"], beat_excerpt=sc.get("beat", ""),
                beat_kind=beat_kind, loras=loras,
                seed=rng.randint(1, 2**31 - 1),
            ))

        # Submit all images. ComfyUI serializes internally.
        await self._say(f"painting {self.image_count} scenes…")

        async def render_one(s: WorshipScene) -> None:
            prefix = f"worship_{self.session_id}_{s.idx:02d}"
            wf = _build_workflow(s.prompt, s.loras, s.seed, prefix)
            try:
                pid = await _comfy_submit(client, wf)
                files = await _comfy_wait(client, pid, timeout_s=420.0)
                if not files:
                    return
                dest = self.home / "scenes" / f"{s.idx:02d}.png"
                await _comfy_fetch(client, files[0], dest)
                s.image_path = dest
                try:
                    rating.record_render(
                        image_path=dest,
                        prompt=s.prompt,
                        loras=s.loras,
                        checkpoint=os.getenv("LUMINA_COMFY_CKPT"),
                        beat=s.beat_kind,
                        seed=s.seed,
                        extra={"session_id": self.session_id, "scene_idx": s.idx,
                               "source": "worship"},
                    )
                except Exception as rerr:
                    log.warning("scene %d sidecar failed: %r", s.idx, rerr)
            except Exception as exc:
                log.warning("scene %d render failed: %r", s.idx, exc)

        # Submit in pairs — gives Comfy a chance to process while we keep
        # the queue full but avoids overwhelming the HTTP layer.
        sem = asyncio.Semaphore(2)
        async def gated(s: WorshipScene) -> None:
            async with sem:
                await render_one(s)
                if s.image_path:
                    rendered = sum(1 for x in self.scenes if x.image_path)
                    await self._say(f"painted scene {rendered}/{self.image_count}")
        await asyncio.gather(*(gated(s) for s in self.scenes), return_exceptions=True)

        await self._say("rendering audio…")
        audio_dest = self.home / "audio.wav"
        try:
            sr, dur = await render_audio(client, self.narrative, audio_dest)
            self.audio_path = audio_dest
            self.audio_duration_s = dur
        except Exception as exc:
            log.warning("audio render failed: %r", exc)
            await self._say(f"audio failed: {exc}")
            return

        # Manifest
        manifest = {
            "session_id": self.session_id,
            "user_prompt": self.user_prompt,
            "image_count": self.image_count,
            "audio_path": str(self.audio_path) if self.audio_path else None,
            "audio_duration_s": self.audio_duration_s,
            "audio_sample_rate": sr,
            "scenes": [
                {
                    "idx": s.idx,
                    "prompt": s.prompt,
                    "beat_excerpt": s.beat_excerpt,
                    "beat_kind": s.beat_kind,
                    "loras": [list(l) for l in s.loras],
                    "seed": s.seed,
                    "image_path": str(s.image_path) if s.image_path else None,
                }
                for s in self.scenes
            ],
        }
        (self.home / "manifest.json").write_text(
            json.dumps(manifest, indent=2), encoding="utf-8")
        rendered = sum(1 for s in self.scenes if s.image_path)
        await self._say(f"ready — {rendered}/{self.image_count} scenes painted, "
                        f"{self.audio_duration_s:.0f}s audio")


def session_path(session_id: str) -> Path:
    return WORSHIP_HOME / session_id


def list_sessions(limit: int = 20) -> list[Path]:
    return sorted([p for p in WORSHIP_HOME.iterdir() if p.is_dir()],
                  reverse=True)[:limit]


def load_session_from_disk(session_id: str) -> Optional[WorshipSession]:
    """Reconstruct a WorshipSession from a previously-rendered session dir.

    Used for replay — skips generation entirely, just rebuilds the Python
    object from `manifest.json`, `narrative.md`, `audio.wav`, and the
    `scenes/` directory.
    """
    home = session_path(session_id)
    if not home.exists() or not home.is_dir():
        return None
    manifest_path = home / "manifest.json"
    if not manifest_path.exists():
        return None
    try:
        m = json.loads(manifest_path.read_text(encoding="utf-8"))
    except Exception:
        return None
    sess = WorshipSession(
        session_id=m.get("session_id") or session_id,
        user_prompt=m.get("user_prompt") or "",
        image_count=m.get("image_count") or 15,
    )
    sess.narrative = (home / "narrative.md").read_text(encoding="utf-8") \
        if (home / "narrative.md").exists() else ""
    audio_path = m.get("audio_path")
    if audio_path and Path(audio_path).exists():
        sess.audio_path = Path(audio_path)
    elif (home / "audio.wav").exists():
        sess.audio_path = home / "audio.wav"
    sess.audio_duration_s = float(m.get("audio_duration_s") or 0.0)
    # Reconstruct scenes from manifest
    for sc_meta in (m.get("scenes") or []):
        ip = sc_meta.get("image_path")
        image_path = Path(ip) if ip and Path(ip).exists() else None
        if image_path is None:
            # Fallback to scenes/<idx>.png if path moved
            candidate = home / "scenes" / f"{int(sc_meta.get('idx', 0)):02d}.png"
            if candidate.exists():
                image_path = candidate
        sess.scenes.append(WorshipScene(
            idx=sc_meta.get("idx", 0),
            prompt=sc_meta.get("prompt", ""),
            beat_excerpt=sc_meta.get("beat_excerpt", ""),
            beat_kind=sc_meta.get("beat_kind", "warm"),
            loras=[tuple(l) for l in (sc_meta.get("loras") or [])],
            seed=sc_meta.get("seed", 0),
            image_path=image_path,
        ))
    sess.status = "loaded"
    return sess


def session_summary(session_id: str) -> Optional[dict]:
    """Compact metadata for browsing — id, prompt, scene_count, duration, date."""
    home = session_path(session_id)
    manifest = home / "manifest.json"
    if not manifest.exists():
        return None
    try:
        m = json.loads(manifest.read_text(encoding="utf-8"))
    except Exception:
        return None
    rendered = sum(1 for s in (m.get("scenes") or []) if s.get("image_path"))
    return {
        "session_id": m.get("session_id") or session_id,
        "user_prompt": m.get("user_prompt") or "",
        "scene_count": rendered,
        "audio_duration_s": m.get("audio_duration_s") or 0.0,
        "modified": home.stat().st_mtime,
    }


def list_session_summaries(limit: int = 20, query: str = "") -> list[dict]:
    """List recent sessions with metadata. If `query` is non-empty, filter
    to sessions whose user_prompt or narrative.md contains the query
    (case-insensitive substring)."""
    out: list[dict] = []
    q = (query or "").strip().lower()
    for home in list_sessions(limit=200):
        s = session_summary(home.name)
        if s is None:
            continue
        if q:
            haystack = (s["user_prompt"] or "").lower()
            narr_path = home / "narrative.md"
            if narr_path.exists():
                try:
                    haystack += " " + narr_path.read_text(encoding="utf-8").lower()
                except Exception:
                    pass
            if q not in haystack:
                continue
        out.append(s)
        if len(out) >= limit:
            break
    return out
