"""Brief -> premium marketing video (v3).

  1. plan     Claude writes the film: one thing, device + form, worlds, a LOOK built from a
              medium, and a shot list where every scene follows a HyperFrames blueprint.
  2. facts    Claude strikes any claim the brief does not make.
  3. music    HeyGen catalog via `hyperframes media-use resolve`; beats + loudness measured.
  4. time     Cuts snapped to the music's beats; each cut gets a registry transition.
  5. scenes   One Claude call per scene, in parallel, each given its blueprint and the
              matching reference composition from the official launch films.
  6. mix      index.html assembled here: LOOK tokens, scene hosts, injected transitions,
              the music bed when there is one (no sound effects).
  7. check    `hyperframes check` + motion assertions; findings routed to the scene that owns
              them and fixed with find/replace edits (in parallel).
  8. render   `hyperframes render`; with music, loudness normalised to -14 LUFS / -1.5 dBTP.

Usage:
    python pipeline.py "brief" --brand Demlik --colors "#1B1B1B,#F5E9DA,#E07A2E" --format 9:16 --duration 20
"""

import argparse
import base64
import html as htmlesc
import json
import os
import re
import shutil
import subprocess
import tempfile
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from string import Template
from types import SimpleNamespace

import anthropic
from anthropic import AnthropicBedrock

import pricing

ROOT = Path(__file__).parent
DATA = Path(os.environ.get("DATA_DIR", ROOT))   # videos and references live here; a volume on a server
RUNS = DATA / "runs"
LIB = ROOT / "library"
PLANNER_PROMPT = (ROOT / "prompts" / "planner.md").read_text()
SCENE_PROMPT = (ROOT / "prompts" / "scene.md").read_text()


def _remix(prompt, swaps):
    for a, b in swaps:
        assert prompt.count(a) == 1, a[:60]
        prompt = prompt.replace(a, b)
    return prompt


# A remix copies a reference film's look, so the method's own look defaults (light ground, no
# gradients/glow, never centred) must not fight it; the content rules (facts, no people, no
# other brands) stay.
PLANNER_REMIX_PROMPT = _remix(PLANNER_PROMPT, [
    ("Never one dark canvas with small UI floating in it: at most one world may be dark, for at most 30% of the running time, and the film's main ground (`palette.ground`) is light (paper, studio white, a light brand tint). A dark brand colour is a solid plane (`palette.plane`), not the void. ", ""),
    (re.search(r"A dark navy/black void.*?the logo docked in a corner · ", PLANNER_PROMPT).group(0), ""),
    ("Brand colours are used as solid planes and as one accent — not as glows.",
     "(Remix: the reference film's look decides grounds, gradients, glows and layout.)"),
])
SCENE_REMIX_PROMPT = _remix(SCENE_PROMPT, [
    ("Depth comes from real shadows (`0 40px 80px -20px rgb(0 0 0 / .35)` and a tight contact shadow), overlapping planes and scale contrast, not glows.",
     "This film remixes a reference: its frame for your shot is in the request. Reproduce its ground, light, textures, gradients and glows (with CSS gradients from the palette tokens, `var(--spectrum)` for multi-colour fills, `filter: blur()` for light) as faithfully as the tokens allow."),
    (re.search(r"\*\*Never:\*\*.*", SCENE_PROMPT).group(0),
     "**Never:** people or faces; other companies' logos; drop shadows on text; more than two typefaces; text without contrast against its ground."),
])
TRANSITIONS = {t["name"]: t for t in json.loads(
    re.search(r"```json\s*(.*?)```", (LIB / "TRANSITION-REGISTRY.md").read_text(), re.S).group(1))["transitions"]}
FORMATS = {"16:9": (1920, 1080), "9:16": (1080, 1920), "1:1": (1080, 1080)}
FPS = 30
MAX_REPAIRS = 2
PARALLEL = 4            # concurrent Bedrock calls per video (account quota)
REGION = os.environ.get("AWS_REGION", "us-east-1")
FACTCHECK_MODEL = None  # the main model: Haiku 4.5 missed "no credit card" in testing

# Blueprint -> reference composition from the official HyperFrames launch films.
REFERENCES = {
    "kinetic-type-beats": "messaging-multi-phrase.html",
    "logo-assemble-lockup": "brand-reveal-assemble-zoom.html",
    "cta-morph-press": "cta-morph-press.html",
    "dataviz-countup": "hook-counter-burst.html",
    "comparison-split": "comparison-split-cards.html",
    "agent-progress-theater": "workflow-approve-press.html",
    "cursor-ui-demo": "workflow-approve-press.html",
    "spatial-pan-stations": "concept-demo-decode-pan.html",
    "ticker-takeover": "takeover-ticker-displace.html",
    "device-surface-showcase": "demo-page-scroll-spotlight.html",
    "grid-card-assemble": "proof-logo-chain.html",
    "camera-journey": "concept-demo-decode-pan.html",
    "zoom-out-workspace-reveal": "demo-page-scroll-spotlight.html",
    "overwhelm-surround": None, "titlecard-reveal": None, "panel-edit-live-sync": None,
    "typewriter-reveal": None,
}
# Blueprints whose first frame already shows the headline (frame 0 is the social thumbnail).
HOOK_BLUEPRINTS = {"kinetic-type-beats", "typewriter-reveal", "dataviz-countup", "titlecard-reveal",
                   "comparison-split", "device-surface-showcase"}
LUFS = {"bed": -20}   # music bed before the final -14 LUFS normalisation


# ---------------------------------------------------------------- helpers

def sh(*args, timeout=600, check=True):
    return subprocess.run(list(args), capture_output=True, text=True, timeout=timeout, check=check)


def fenced(message, lang):
    text = "".join(b.text for b in message.content if b.type == "text")
    m = re.search(rf"```{lang}\s*(.*?)```", text, re.S)
    return m.group(1).strip() if m else None


def gain_for(lufs, target):
    return round(min(3.98, max(0.05, 10 ** ((target - lufs) / 20))), 3)


class Bedrock:
    """Claude on Bedrock; records every call's tokens and cost."""

    def __init__(self, model_key, log):
        self.model_key, self.log = model_key, log
        # Retries back off on 429s; a video opens several parallel calls, which can hit the account quota.
        self.client = AnthropicBedrock(aws_region=REGION, max_retries=8)
        self.calls = []

    def ask(self, step, system, messages, max_tokens=16000, model_key=None):
        key = model_key or self.model_key
        t0 = time.time()
        try:
            # Static system prompts carry a cache breakpoint; callers mark the reference
            # block and the newest user turn too (max 4 breakpoints per request).
            with self.client.messages.stream(
                model=pricing.MODELS[key][0],
                max_tokens=max_tokens,
                system=[{"type": "text", "text": system, "cache_control": {"type": "ephemeral"}}],
                messages=messages,
            ) as stream:
                msg = stream.get_final_message()
        except anthropic.APIStatusError as e:
            raise RuntimeError(f"Bedrock hatası {e.status_code}: {e.message}") from e
        usage = msg.usage.model_dump(include={"input_tokens", "output_tokens",
                                              "cache_creation_input_tokens", "cache_read_input_tokens"})
        usage = {k: v or 0 for k, v in usage.items()}
        cost = pricing.llm_cost(key, usage)
        self.calls.append({"step": step, "model": key, "seconds": round(time.time() - t0, 1),
                           "stop_reason": msg.stop_reason, "usage": usage, "cost_usd": round(cost, 5)})
        self.log(f"  {step}: in={usage['input_tokens'] + usage['cache_creation_input_tokens']} "
                 f"cache_r={usage['cache_read_input_tokens']} out={usage['output_tokens']} → ${cost:.4f}")
        return msg


class ClaudeCLI:
    """Claude through the signed-in `claude` CLI (the user's subscription); same `ask` as Bedrock."""

    ENV_DROP = ("ANTHROPIC_API_KEY", "ANTHROPIC_BASE_URL", "ANTHROPIC_AUTH_TOKEN", "ANTHROPIC_DEFAULT_SONNET_MODEL",
                "ANTHROPIC_DEFAULT_HAIKU_MODEL", "ANTHROPIC_DEFAULT_OPUS_MODEL", "CLAUDE_CODE_USE_BEDROCK")

    def __init__(self, model_key, log):
        self.model_key, self.log = model_key, log
        self.calls = []

    @staticmethod
    def flatten(messages):
        """The CLI takes one user turn, so earlier answers travel inside it as quoted text."""
        blocks = []
        for m in messages:
            content = m["content"] if isinstance(m["content"], list) else [{"type": "text", "text": m["content"]}]
            if m["role"] == "assistant":
                text = "".join(getattr(b, "text", "") for b in content)
                blocks.append({"type": "text", "text": f"<your_previous_answer>\n{text}\n</your_previous_answer>"})
            else:
                blocks += [{k: v for k, v in b.items() if k != "cache_control"} for b in content]
        return blocks

    def ask(self, step, system, messages, max_tokens=16000, model_key=None):
        key = model_key or self.model_key
        t0 = time.time()
        env = {k: v for k, v in os.environ.items() if k not in self.ENV_DROP}
        # Thinking counts against this limit and cannot be switched off here, so low effort plus a
        # generous limit keeps a long scene from failing the whole film.
        env["CLAUDE_CODE_MAX_OUTPUT_TOKENS"] = str(max(max_tokens, 32000))
        line = json.dumps({"type": "user", "message": {"role": "user", "content": self.flatten(messages)}})
        res = subprocess.run(
            ["claude", "-p", "--input-format", "stream-json", "--output-format", "stream-json", "--verbose",
             "--model", pricing.MODELS[key][0], "--effort", "low" if "haiku" in key else "medium", "--tools", "", "--system-prompt", system, "--setting-sources", "",
             "--strict-mcp-config", "--no-session-persistence"],
            input=line, capture_output=True, text=True, timeout=900, env=env, cwd=tempfile.gettempdir())
        final = None
        for row in res.stdout.splitlines():
            try:
                event = json.loads(row)
            except json.JSONDecodeError:
                continue
            if event.get("type") == "result":
                final = event
        if not final or final.get("is_error"):
            raise RuntimeError(f"Claude aboneliği hatası: {(final or {}).get('result') or res.stderr[:300]}")
        raw = final.get("usage", {})
        usage = {k: raw.get(k) or 0 for k in ("input_tokens", "output_tokens", "cache_creation_input_tokens",
                                              "cache_read_input_tokens")}
        self.calls.append({"step": step, "model": key, "seconds": round(time.time() - t0, 1),
                           "stop_reason": final.get("stop_reason"), "usage": usage, "cost_usd": 0.0})
        thinking = (raw.get("output_tokens_details") or {}).get("thinking_tokens", 0)
        self.log(f"  {step}: in={usage['input_tokens'] + usage['cache_creation_input_tokens']} "
                 f"cache_r={usage['cache_read_input_tokens']} out={usage['output_tokens']} "
                 f"(thinking {thinking}) {time.time() - t0:.0f} sn → abonelik")
        return SimpleNamespace(content=[SimpleNamespace(type="text", text=final.get("result") or "")],
                               stop_reason=final.get("stop_reason"))


def slug(s, used):
    base = re.sub(r"[^a-z0-9]", "", s.lower())[:10] or "s"
    if base in ("main", "root", "film") or base[0].isdigit():
        base = "s" + base
    out, i = base, 2
    while out in used:
        out, i = f"{base}{i}", i + 1
    used.add(out)
    return out


# ---------------------------------------------------------------- 1-2. plan + facts

def plan(llm, brief, brand, colors, fmt, duration, ref=None):
    shape = {"9:16": "vertical phone", "16:9": "landscape", "1:1": "square"}[fmt]
    lines = [f"Format: {fmt} ({shape}), about {duration} s", f"Brief: {brief}"]
    if brand:
        lines.append(f"Brand name: {brand}")
    if colors:
        lines.append(f"Brand colours: {colors}")
    if ref:
        lines.append("Reference film to remix (its measured edit): " + json.dumps(
            {k: ref.get(k) for k in ("summary", "pacing", "look", "signature_moves", "transitions", "acts", "duration",
                                     "sound")},
            ensure_ascii=False))
    content = [{"type": "text", "text": "\n".join(lines)}]
    if ref:
        import reference
        content += reference.frames_content(ref)
    msg = llm.ask("plan", PLANNER_REMIX_PROMPT if ref else PLANNER_PROMPT, [{"role": "user", "content": content}],
                  max_tokens=12000)
    board = json.loads(fenced(msg, "json") or "".join(b.text for b in msg.content if b.type == "text"))

    scenes = [s for s in board["scenes"] if s.get("beat") != "endcard"][:6]
    # The end card is the host's own template (endcard_html), so the most important frame is
    # always on brand; the planner only supplies its copy in `cta`.
    end = {"id": "end", "beat": "endcard", "blueprint": "endcard", "transition_in": "zoom-through",
           "headline": board["cta"]["wordmark"]}
    used = {"end"}
    for i, s in enumerate(scenes):
        s["id"] = slug(s.get("id", "s"), used)
        if s.get("blueprint") not in REFERENCES or (i == 0 and s["blueprint"] not in HOOK_BLUEPRINTS):
            s["blueprint"] = "kinetic-type-beats"
    allowed = [board["transitions"].get("primary", "blur-crossfade")] + board["transitions"].get("accents", [])
    allowed = [a for a in allowed if a in TRANSITIONS] or ["blur-crossfade"]
    for i, s in enumerate(scenes):
        name, _, direction = (s.get("transition_in") or "").partition(":")
        if i == 0:
            s["transition_in"], s["direction"] = "cut", ""
        else:
            s["transition_in"] = name if name in TRANSITIONS else allowed[0]
            s["direction"] = direction.upper() if direction.upper() in ("LEFT", "RIGHT", "UP", "DOWN") else "LEFT"
    board["scenes"] = scenes + [end]
    return board


FACTCHECK_PROMPT = """You check marketing copy against its brief. List every on-screen string that states a fact the brief does not state (payment terms such as "no credit card", guarantees, numbers, durations, prices, integrations, customers, awards, URLs). Illustrative values inside drawn UI are fine. Rephrasing the brief's own facts is fine.
Return only a ```json block: [{"text": "<the exact offending string>", "fix": "<the same string with the unsupported claim removed, still natural; may be empty>"}], or [] if all copy is supported."""


def factcheck(llm, board, brief):
    strings = []
    for s in board["scenes"]:
        strings += [s.get("headline", ""), s.get("support", "")]
    strings += [board["cta"].get("action_line", ""), board["cta"].get("button_label", "")]
    strings = [x for x in strings if x]
    msg = llm.ask("facts", FACTCHECK_PROMPT, [{"role": "user", "content":
                  f"Brief:\n{brief}\n\nOn-screen copy:\n" + "\n".join(f"- {x}" for x in strings)}],
                  max_tokens=1500, model_key=FACTCHECK_MODEL)
    try:
        fixes = json.loads(fenced(msg, "json") or "[]")
    except json.JSONDecodeError:
        return []
    applied = []
    for f in fixes if isinstance(fixes, list) else []:
        bad, good = f.get("text"), f.get("fix", "")
        for obj in board["scenes"] + [board["cta"]]:
            for k in ("headline", "support", "action_line", "button_label"):
                if bad and obj.get(k) == bad:
                    obj[k] = good
                    applied.append(f)
    return applied


# ---------------------------------------------------------------- 3. music

def fetch_music(project, query, log):
    """HeyGen catalog via media-use. Returns a local path, or None (the film then plays without a bed)."""
    out = sh("npx", "hyperframes", "media-use", "resolve", "--type", "bgm", "--intent", query,
             "--project", str(project), "--json", check=False, timeout=300)
    path = None
    try:
        data = json.loads(out.stdout)
        path = data.get("path") or (data.get("asset") or {}).get("path") or (data.get("data") or {}).get("path")
    except json.JSONDecodeError:
        m = re.search(r"→\s*(\S+\.(?:mp3|wav|m4a))", out.stdout + out.stderr)
        path = m.group(1) if m else None
    if not path:
        log(f"  müzik yok: {(out.stdout + out.stderr).strip()[:160]}")
        return None
    p = Path(path)
    return p if p.is_absolute() else project / p


def pick_entry(beat_times, env_db, hop, film_len):
    """Start on the downbeat whose window is loudest, favouring an energy jump (an entrance)."""
    def energy(a, b):
        seg = env_db[int(a / hop):max(int(b / hop), int(a / hop) + 1)] or [-120]
        return sum(10 ** (v / 20) for v in seg) / len(seg)

    best, best_score = 0, -1
    for k in range(0, len(beat_times), 4):
        t0 = beat_times[k]
        if t0 + film_len + 0.5 > beat_times[-1]:
            break
        score = energy(t0, t0 + film_len) + 0.5 * max(0, energy(t0, t0 + 2) - energy(max(0, t0 - 2), t0))
        if score > best_score:
            best, best_score = k, score
    return best


# ---------------------------------------------------------------- 4. timing

def make_timing(board, duration, bpm, beat_times=None, entry=0):
    beat = 60 / bpm
    total = max(16, round(duration / beat / 2) * 2)
    grid = [t - beat_times[entry] for t in beat_times[entry:]] if beat_times else []
    if len(grid) <= total:
        grid = [i * beat for i in range(total + 1)]
    end_beats = max(6, round(3.6 / beat / 2) * 2)
    body = board["scenes"][:-1][:max(3, (total - end_beats) // 4)]
    board["scenes"] = body + board["scenes"][-1:]
    avail = (total - end_beats) // 2                      # half bars for the body scenes
    weights = [max(1, int(s.get("weight", 2))) for s in body]
    raw = [w / sum(weights) * avail for w in weights]
    alloc = [max(2, int(r)) for r in raw]                 # at least one bar each
    order = sorted(range(len(raw)), key=lambda i: raw[i] - int(raw[i]), reverse=True)
    while sum(alloc) < avail:
        alloc[order[0]] += 1
        order = order[1:] + order[:1]
    while sum(alloc) > avail:
        alloc[alloc.index(max(alloc))] -= 1
    beats = [a * 2 for a in alloc] + [total - sum(a * 2 for a in alloc)]

    t = lambda k: round(grid[k], 3)
    cuts, k = [0], 0
    for n in beats:
        k += n
        cuts.append(k)
    film_end = t(total)
    scenes = []
    for i, s in enumerate(board["scenes"]):
        length = t(cuts[i + 1]) - t(cuts[i])
        tr = s["transition_in"]
        d_in = 0 if i == 0 else min(TRANSITIONS[tr]["default_duration_s"], 0.6, length * 0.3)
        scenes.append({"id": s["id"], "cut": t(cuts[i]), "cut_out": t(cuts[i + 1]), "d_in": round(d_in, 3),
                       "transition": tr, "direction": s.get("direction", "")})
    for i, s in enumerate(scenes):
        s["d_out"] = scenes[i + 1]["d_in"] if i + 1 < len(scenes) else 0
        s["start"] = round(s["cut"] - s["d_in"] / 2, 3)
        s["end"] = round(min(film_end, s["cut_out"] + s["d_out"] / 2), 3)
        s["duration"] = round(s["end"] - s["start"], 3)
        s["beats"] = [round(grid[j] - s["start"], 3) for j in range(len(grid))
                      if s["start"] <= grid[j] < s["end"]]
    return {"bpm": bpm, "beat": round(beat, 4), "scenes": scenes, "end": film_end}


def apply_sound(tm, ref):
    """Scene arrivals follow the reference's measured sound hits, scaled to this film's length."""
    hits = (ref.get("sound_measured") or {}).get("hits") or []
    if not hits:
        return
    scale = tm["end"] / ref["duration"]
    for s in tm["scenes"]:
        s["beats"] = [round(h * scale - s["start"], 3) for h in hits if s["start"] <= h * scale < s["end"]]


# ---------------------------------------------------------------- 5. scenes

def reference_block(blueprint):
    parts = [f"## Blueprint `{blueprint}`\n\n" + (LIB / "blueprints" / f"{blueprint}.md").read_text()]
    ref = REFERENCES.get(blueprint)
    if ref:
        parts.append(f"## Reference composition from the official launch films (`{ref}`)\n\n```html\n"
                     + (LIB / "blueprints" / ref).read_text() + "\n```")
    return "\n\n".join(parts)


def scene_request(board, tm, i, fmt):
    w, h = FORMATS[fmt]
    s, t = board["scenes"][i], tm["scenes"][i]
    world = next((x for x in board["worlds"] if x["id"] == s.get("world")), board["worlds"][0])
    prev_end = board["scenes"][i - 1].get("ends_on", "") if i else ""
    nxt = board["scenes"][i + 1] if i + 1 < len(board["scenes"]) else None
    spec = {k: s.get(k, "") for k in ("beat", "headline", "highlight", "support", "visual", "action",
                                      "enters_from", "ends_on")}
    if s["beat"] == "endcard":
        spec.update({"wordmark": board["cta"]["wordmark"], "action_line": board["cta"]["action_line"],
                     "button_label": board["cta"]["button_label"]})
    return "\n".join([
        f"SCENE_ID = {s['id']}   W = {w}   H = {h}   DURATION = {t['duration']} s",
        f"T_IN = {t['d_in']} s (host transition: {t['transition']})   T_OUT = {t['d_out']} s",
        f"Music beats (s from scene start): {t['beats']}",
        f"Scene {i + 1} of {len(board['scenes'])}. Blueprint: {s['blueprint']}.",
        "",
        "Film plan:",
        json.dumps({k: board[k] for k in ("one_thing", "task", "device", "form", "look")}, ensure_ascii=False),
        f"This scene's world: {json.dumps(world, ensure_ascii=False)}",
        f"The previous scene ends on: {prev_end or '(none — this is the first scene)'}",
        f"The next scene opens on: {(nxt or {}).get('enters_from') or '(none — this is the last scene)'}",
        "",
        "Scene spec:",
        json.dumps(spec, ensure_ascii=False, indent=1),
    ])


def build_scene(llm, board, tm, i, fmt, ref=None):
    s = board["scenes"][i]
    messages = [{"role": "user", "content": [
        {"type": "text", "text": reference_block(s["blueprint"]), "cache_control": {"type": "ephemeral"}},
        {"type": "text", "text": scene_request(board, tm, i, fmt), "cache_control": {"type": "ephemeral"}},
    ]}]
    act = ref and isinstance(s.get("ref_act"), int) and 0 < s["ref_act"] <= len(ref["acts"]) and ref["acts"][s["ref_act"] - 1]
    if act:
        import reference
        messages[0]["content"] += [
            {"type": "text", "text": "## The reference film's shot this scene remixes\n\nMatch its layout, type scale, "
             "density and motion; use your scene's content, the LOOK's palette and fonts, never its brand or copy.\n"
             + json.dumps({"look": ref["look"], "act": act}, ensure_ascii=False)},
            reference.act_frame(ref["id"], ref, act)]
    msg = llm.ask(f"scene:{s['id']}", SCENE_REMIX_PROMPT if act else SCENE_PROMPT, messages)
    return {"messages": messages, "last": msg, "html": fenced(msg, "html") or "",
            "system": SCENE_REMIX_PROMPT if act else SCENE_PROMPT}



def endcard_html(board, t, fmt):
    """The fixed end card: accent flood over paper, the wordmark cascading in at 50-80% of the
    width, the action line, and a cursor pressing the CTA button. Returns (html, press time)."""
    w, h = FORMATS[fmt]
    cta = board["cta"]
    word = cta.get("wordmark", "")
    word_px = int(min(w * (0.80 if w < h else 0.56) / max(1, len(word) * 0.5), h * 0.26))
    action_px = int(w * (0.056 if w < h else 0.036))
    btn_h, btn_px = int(h * (0.07 if w < h else 0.12)), int(w * (0.045 if w < h else 0.027))
    gap = int(h * 0.035)
    text_max = int(w * 0.8)
    lines = max(1, round(len(cta.get("action_line", "")) * action_px * 0.52 / text_max + 0.4))
    stack = word_px * 0.9 + gap + lines * action_px * 1.15 + gap + btn_h
    btn_cy = stack / 2 - btn_h / 2
    label_w = len(cta.get("button_label", "")) * btn_px * 0.58 + 2 * btn_px * 1.4
    press = round(min(t["duration"] - 1.6, 1.95), 2)
    vals = {
        "W": w, "H": h, "word_px": f"{word_px}px", "action_px": f"{action_px}px", "gap": f"{gap}px",
        "text_max": f"{text_max}px", "btn_h": f"{btn_h}px", "btn_px": f"{btn_px}px",
        "btn_pad": f"{int(btn_px * 1.4)}px", "btn_r": f"{int(btn_h * 0.22)}px", "band_h": f"{int(h * 0.08)}px",
        "cur_w": f"{int(h * 0.045)}px", "cur_x0": int(w * 0.32), "cur_y0": int(h * 0.42),
        "cur_x1": int(label_w * 0.18), "cur_y1": int(btn_cy + btn_h * 0.1),
        "press": press, "press_up": round(press + 0.08, 2),
        "letters": "".join(f'<span class="end-l"><span class="end-li">{htmlesc.escape(c) if c != " " else "&nbsp;"}</span></span>'
                           for c in word),
        "action": htmlesc.escape(cta.get("action_line", "")), "button": htmlesc.escape(cta.get("button_label", "")),
    }
    return Template((LIB / "blocks" / "endcard.html").read_text()).substitute(vals), press


REPAIR_PROMPT = (
    "`hyperframes check` found the problems below in your scene file (your previous answer plus any edits "
    "already applied). Fix them without changing the design. Return ONLY a ```json block holding a list of "
    'edits: [{"find": "<exact substring of the current file, unique, a few lines>", "replace": "<new text>"}]. '
    "Do not return the whole file.\n"
)


def apply_edits(src, message):
    if fenced(message, "html"):
        return fenced(message, "html")
    try:
        edits = json.loads(fenced(message, "json") or "[]")
    except json.JSONDecodeError:
        return src
    for e in edits if isinstance(edits, list) else []:
        if isinstance(e, dict) and e.get("find") and e["find"] in src:
            src = src.replace(e["find"], e.get("replace", ""), 1)
    return src


# ---------------------------------------------------------------- 6. assemble + mix

def transition_lines(t, prev_id, w, h):
    tr = TRANSITIONS[t["transition"]]
    if t["transition"] == "push-slide":
        dx = {"LEFT": -w, "RIGHT": w}.get(t["direction"], 0)
        dy = {"UP": -h, "DOWN": h}.get(t["direction"], 0)
        lines = tr["gsap_template_horizontal"] if dx else tr["gsap_template_vertical"]
    else:
        dx = dy = 0
        lines = tr["gsap_template"]
    subs = {"__OLD__": f'"#tx-{prev_id}"', "__NEW__": f'"#tx-{t["id"]}"', "__T__": str(t["start"]),
            "__DUR__": str(t["d_in"]), "__DXIN__": str(-dx), "__DYIN__": str(-dy), "__DX__": str(dx),
            "__DY__": str(dy), "__ORIGIN_OUT__": '"left center"', "__ORIGIN_IN__": '"right center"'}
    out = []
    for line in lines:
        for k, v in subs.items():
            line = line.replace(k, v)
        out.append("  " + line)
    return out



GRAIN = ("<div id=\"grain\" class=\"clip\" data-start=\"0\" data-duration=\"{end}\" data-track-index=\"9\" "
         "data-layout-ignore style=\"position:absolute;inset:0;pointer-events:none;z-index:50;opacity:.06;"
         "mix-blend-mode:overlay;background:url(&quot;data:image/svg+xml,%3Csvg viewBox='0 0 256 256' "
         "xmlns='http://www.w3.org/2000/svg'%3E%3Cfilter id='n'%3E%3CfeTurbulence type='fractalNoise' "
         "baseFrequency='0.8' numOctaves='3'/%3E%3C/filter%3E%3Crect width='100%25' height='100%25' "
         "filter='url(%23n)'/%3E%3C/svg%3E&quot;)\"></div>")


def assemble(project, board, tm, fmt, music):
    w, h = FORMATS[fmt]
    look, end = board["look"], tm["end"]
    pal, fonts = look["palette"], look["fonts"]

    hosts, tx = [], []
    for i, t in enumerate(tm["scenes"]):
        hosts.append(
            f'<div id="el-{t["id"]}" class="clip" data-start="{t["start"]}" data-duration="{t["duration"]}" '
            f'data-track-index="{i % 2}" data-track-kind="graphics" style="position:absolute;inset:0;z-index:{i + 1}">'
            f'<div id="tx-{t["id"]}" class="tx">'
            f'<div id="{t["id"]}" data-composition-id="{t["id"]}" data-composition-src="compositions/{t["id"]}.html" '
            f'data-start="{t["start"]}" data-duration="{t["duration"]}" data-width="{w}" data-height="{h}" '
            f'style="position:absolute;inset:0"></div></div></div>')
        if i:
            tx += transition_lines(t, tm["scenes"][i - 1]["id"], w, h)


    music_tag = ""
    if music:
        music_tag = (f'<audio id="music" class="clip" src="assets/music/{music["file"]}" data-start="0" '
                     f'data-duration="{end}" data-media-start="{music["entry"]:.3f}" data-track-index="20" '
                     f'data-volume="{music["volume"]}" data-fade-in="0.08" data-fade-out="1.4"></audio>')
    grain = GRAIN.format(end=end) if look.get("grain") else ""
    variables = json.dumps([
        {"id": "ground", "type": "color", "label": "Ground", "default": pal["ground"]},
        {"id": "ink", "type": "color", "label": "Ink", "default": pal["ink"]},
        {"id": "accent", "type": "color", "label": "Accent", "default": pal["accent"]},
        {"id": "plane", "type": "color", "label": "Plane", "default": pal.get("plane", pal["accent"])},
        {"id": "paper", "type": "color", "label": "Paper", "default": pal.get("paper", "#F4F1EA")}])

    nl = "\n  "
    doc = f"""<!doctype html>
<html lang="{board.get('language', 'tr')}" data-composition-variables='{variables}'>
<head>
<meta charset="UTF-8" />
<meta name="viewport" content="width={w}, height={h}" />
<script src="https://cdn.jsdelivr.net/npm/gsap@3.15.0/dist/gsap.min.js"></script>
<style>
  :root {{
    --ground: {pal['ground']}; --ink: {pal['ink']}; --muted: {pal['muted']}; --accent: {pal['accent']};
    --plane: {pal.get('plane', pal['accent'])}; --paper: {pal.get('paper', '#F4F1EA')}; --paper-ink: {pal.get('paper_ink', '#111111')};
    --spectrum: linear-gradient(90deg, {', '.join(pal.get('spectrum') or [pal['accent'], pal.get('plane', pal['accent'])])});
    --field: var({'--ground' if board['cta'].get('field') == 'ground' else '--accent'});
    --field-ink: var({'--ink' if board['cta'].get('field') == 'ground' else '--paper-ink'});
    --btn-bg: var({'--accent' if board['cta'].get('field') == 'ground' else '--plane'});
    --btn-ink: var({'--ground' if board['cta'].get('field') == 'ground' else '--paper'});
    --font-display: "{fonts['display']}", sans-serif; --display-weight: {fonts.get('display_weight', 800)};
    --font-body: "{fonts['body']}", sans-serif;
  }}
  * {{ margin: 0; padding: 0; box-sizing: border-box; }}
  html, body {{ width: {w}px; height: {h}px; overflow: hidden; background: var(--ground); }}
  .tx {{ position: absolute; inset: 0; }}
</style>
</head>
<body>
<div id="root" data-composition-id="main" data-start="0" data-duration="{end}" data-width="{w}" data-height="{h}" data-fps="{FPS}" data-motion-blur="on">
  {nl.join(hosts)}
  {grain}
  {music_tag}
</div>
<script>
  const tl = gsap.timeline({{ paused: true }});
{chr(10).join(tx)}
  window.__timelines["main"] = tl;
</script>
</body>
</html>
"""
    (project / "index.html").write_text(doc)
    # Motion assertions `check` verifies on the seeked timeline: every scene title has landed
    # shortly after its cut.
    assertions = [{"kind": "appearsBy", "selector": f"#{t['id']}-title",
                   "bySec": round(t["cut"] + t["d_in"] / 2 + 0.6, 2)} for t in tm["scenes"]]
    (project / "index.motion.json").write_text(json.dumps({"assertions": assertions}, indent=1))
    return {"transitions": [t["transition"] for t in tm["scenes"][1:]]}


# ---------------------------------------------------------------- 7-8. check, render

REPAIR_WARNINGS = {"content_overlap", "text_overlap", "contrast_aa_failure", "line_unread", "text_below_floor",
                   "first_frame_empty", "text_clipped", "text_overflow", "overlapping_gsap_tweens",
                   "motion_appears_late", "motion_selector_missing", "motion_frozen", "motion_off_frame"}


def findings_by_scene(report, tm):
    """Errors plus the warnings that visibly hurt a promo, keyed by the scene that owns them."""
    ids, out = [t["id"] for t in tm["scenes"]], {}
    for section in ("lint", "runtime", "layout", "motion", "contrast", "craft"):
        for f in report.get(section, {}).get("findings", []):
            if f.get("severity") != "error" and f.get("code") not in REPAIR_WARNINGS:
                continue
            ft = f.get("time")
            if (f.get("code") in ("content_overlap", "text_overlap") and isinstance(ft, (int, float))
                    and any(t["start"] - 0.05 <= ft <= t["start"] + t["d_in"] + 0.05 for t in tm["scenes"][1:])):
                continue  # two scenes overlap by design while the host transition runs
            src, sel = f.get("sourceFile") or "", f.get("selector") or ""
            owner = next((sid for sid in ids if src.endswith(f"compositions/{sid}.html")
                          or re.search(rf"#{sid}-", sel)), None)
            if owner is None and isinstance(f.get("time"), (int, float)):
                owner = next((t["id"] for t in tm["scenes"] if t["cut"] <= f["time"] < t["cut_out"]), None)
            if owner:
                out.setdefault(owner, []).append({k: f.get(k) for k in ("code", "selector", "time", "message", "fixHint")})
    return out


REVIEW_PROMPT = """These are rendered frames of YOUR scene at the times shown (seconds in the film). Review them as a harsh motion director at a top studio, then fix what you see.
P0 (fix all): text hidden behind or touching an object, text cut off or leaving the frame, a headline that is small or not readable, an empty or half-empty frame, a hero smaller than the spec (45–75% of the width in 16:9, 85–95% in 9:16), anything the LOOK or the BANNED list forbids, an end card that does not fill the frame with the wordmark at 45–60% width.
P1 (fix the ones worth it): weak hierarchy, everything centred, cramped spacing, a seam that would jump, a dead stretch.
Return ONLY a ```json block of edits: [{"find": "<exact substring of the current file, unique>", "replace": "<new text>"}]. Edit sizes, layout and positions; keep the copy, the timing and the choreography. Return [] if the frames are already premium."""


def snapshot_scenes(project, tm):
    """Three frames per scene (after its entrance, its middle, before it leaves) as small JPEGs."""
    shots = {}
    for t in tm["scenes"][:-1]:
        a = t["cut"] + t["d_in"] / 2 + 0.45
        b = t["cut_out"] - t["d_out"] / 2 - 0.15
        times = sorted({round(x, 2) for x in (a, (a + b) / 2, b) if t["start"] <= x < tm["end"]})
        out_dir = project / ".review" / t["id"]
        res = sh("npx", "hyperframes", "snapshot", str(project), "--at", ",".join(map(str, times)), "--no-end",
                 "--output", str(out_dir), "--json", check=False, timeout=300)
        try:
            frames = json.loads(res.stdout)["frames"]
        except (json.JSONDecodeError, KeyError):
            continue
        shots[t["id"]] = []
        for f in frames:
            jpg = Path(f["path"]).with_suffix(".jpg")
            sh("ffmpeg", "-loglevel", "error", "-y", "-i", f["path"], "-vf", "scale=960:-1", "-q:v", "4", str(jpg))
            shots[t["id"]].append((f["time"], base64.b64encode(jpg.read_bytes()).decode()))
    return shots


REVIEW_REMIX_NOTE = ("\n\nThis scene remixes the reference shot you were given (frame above). The reference decides the look: "
                     "its ground, centring, gradients, glows and type scale are correct, not faults. Fix what drifts from "
                     "the reference shot first, then P0 issues. Ignore the hero-width rule when the reference shot has no hero.")


def review_content(frames, findings, remix=False):
    content = []
    for t, data in frames:
        content.append({"type": "text", "text": f"t = {t} s"})
        content.append({"type": "image", "source": {"type": "base64", "media_type": "image/jpeg", "data": data}})
    text = REVIEW_PROMPT
    if findings:
        text += "\n\nThe checker also measured these:\n" + json.dumps(findings[:10], ensure_ascii=False, indent=1)
    if remix:
        text += REVIEW_REMIX_NOTE
    content.append({"type": "text", "text": text})
    return content


def run_check(project):
    return json.loads(sh("npx", "hyperframes", "check", str(project), "--json", check=False, timeout=900).stdout)


def poster(video, out, at=1.0):
    """Gallery thumbnail: a frame from the hook, where the headline is already on screen."""
    sh("ffmpeg", "-loglevel", "error", "-y", "-ss", str(at), "-i", str(video), "-frames:v", "1",
       "-vf", "scale='min(960,iw)':-2", "-q:v", "3", str(out))


def render(project, has_audio):
    raw, out = project / "render-raw.mp4", project / "video.mp4"
    t0 = time.time()
    sh("npx", "hyperframes", "render", str(project), "--output", str(raw), "--quality", "delivery", "--quiet",
       timeout=1800)
    render_s = time.time() - t0
    loud = None
    if has_audio:
        sh("ffmpeg", "-loglevel", "error", "-y", "-i", str(raw), "-c:v", "copy",
           "-af", "loudnorm=I=-14:TP=-1.5:LRA=11", "-c:a", "aac", "-b:a", "192k", str(out))
        raw.unlink()
        loud = json.loads(sh("npx", "hyperframes", "audio", str(out), "--json").stdout)["items"][0]["loudness"]
    else:
        raw.replace(out)
    poster(out, project / "poster.jpg")
    return out, render_s, loud


# ---------------------------------------------------------------- run

def run(brief, brand="", colors="", fmt="16:9", duration=20, model_key=pricing.DEFAULT_MODEL, log=print,
        template="", reference_id=""):
    job_id = time.strftime("%Y%m%d-%H%M%S-") + uuid.uuid4().hex[:6]
    project = RUNS / job_id
    (project / "compositions").mkdir(parents=True)
    say = lambda line: log(f"[{job_id}] {line}")
    llm = (ClaudeCLI if model_key.startswith("sub-") else Bedrock)(model_key, say)

    ref = None
    if reference_id:
        import reference
        say("0/8 referans film (HyperFrames showcase)")
        ref = reference.ensure(reference_id, llm, say)
    say("1/8 film planı (Claude)")
    board = plan(llm, brief, brand, colors, fmt, duration, ref)
    say(f"  {board['device']} · {board['form']} · {board['look']['medium']}")
    say("2/8 olgu kontrolü (Claude)")
    fixes = factcheck(llm, board, brief)
    if fixes:
        say(f"  {len(fixes)} desteksiz ifade düzeltildi: {[f['text'] for f in fixes]}")

    say("3/8 müzik (HeyGen kataloğu)")
    music = None
    track = fetch_music(project, board["music"]["query"], say)
    if track and track.exists():
        dest = project / "assets" / "music" / track.name
        dest.parent.mkdir(parents=True, exist_ok=True)
        if track.resolve() != dest.resolve():
            shutil.copy(track, dest)
        beats = json.loads(sh("npx", "hyperframes", "beats", str(dest), "--json").stdout)
        info = json.loads(sh("npx", "hyperframes", "audio", str(dest), "--json").stdout)["items"][0]
        entry = pick_entry(beats["beatTimes"], info["envelope"]["values"], info["envelope"]["hopMs"] / 1000,
                           duration + 1)
        tm = make_timing(board, duration, beats["bpm"], beats["beatTimes"], entry)
        music = {"file": dest.name, "bpm": beats["bpm"], "entry": beats["beatTimes"][entry],
                 "volume": gain_for(info["loudness"]["lufsIntegrated"], LUFS["bed"])}
        say(f"  {dest.name}: {beats['bpm']:.0f} BPM, giriş {music['entry']:.1f} sn")
    else:
        tm = make_timing(board, duration, 120)
    if ref:
        apply_sound(tm, ref)
    (project / "storyboard.json").write_text(json.dumps(board, ensure_ascii=False, indent=2))
    say(f"4/8 zamanlama: {tm['end']} sn · " + " → ".join(f"{t['id']}@{t['cut']}" for t in tm["scenes"]))

    say(f"5/8 {len(tm['scenes'])} sahne paralel (Claude)")
    with ThreadPoolExecutor(max_workers=PARALLEL) as pool:
        built = list(pool.map(lambda i: build_scene(llm, board, tm, i, fmt, ref), range(len(tm["scenes"]) - 1)))
    end_t = tm["scenes"][-1]
    end_src, _ = endcard_html(board, end_t, fmt)
    (project / "compositions" / "end.html").write_text(end_src)
    for t, b in zip(tm["scenes"], built):
        (project / "compositions" / f"{t['id']}.html").write_text(b["html"])

    say("6/8 montaj")
    mix = assemble(project, board, tm, fmt, music)

    say("7/8 check + görsel review")
    ids = [t["id"] for t in tm["scenes"][:-1]]   # the scenes Claude wrote (the end card is a template)

    def revise(step, sid, content):
        """Append one turn to the scene's conversation and apply the edits it returns."""
        b = built[ids.index(sid)]
        b["messages"].append({"role": "assistant", "content": b["last"].content})
        content[-1]["cache_control"] = {"type": "ephemeral"}
        b["messages"].append({"role": "user", "content": content})
        for m in b["messages"][1:-1]:  # keep breakpoints on the reference block + the newest turn
            if m["role"] == "user" and isinstance(m["content"], list):
                for block in m["content"]:
                    block.pop("cache_control", None)
        b["messages"][0]["content"][1].pop("cache_control", None)
        b["last"] = llm.ask(f"{step}:{sid}", b["system"], b["messages"], max_tokens=8000)
        b["html"] = apply_edits(b["html"], b["last"])
        (project / "compositions" / f"{sid}.html").write_text(b["html"])

    def repair_round(step, found):
        say(f"  {step}: " + ", ".join(f"{k}={len(v)}" for k, v in found.items()))
        jobs = {sid: [{"type": "text", "text": REPAIR_PROMPT + json.dumps(v[:15], ensure_ascii=False, indent=1)}]
                for sid, v in found.items()}
        with ThreadPoolExecutor(max_workers=PARALLEL) as pool:
            list(pool.map(lambda sid: revise(step, sid, jobs[sid]), jobs))

    # Measured findings first, then every scene looks at its own frames, then errors until clean.
    report = run_check(project)
    found = {k: v for k, v in findings_by_scene(report, tm).items() if k in ids}
    if found:
        repair_round("onarım", found)
        report = run_check(project)
    frames = {k: v for k, v in snapshot_scenes(project, tm).items() if k in ids}
    found = findings_by_scene(report, tm)
    say(f"  görsel review: {sum(len(v) for v in frames.values())} kare")
    with ThreadPoolExecutor(max_workers=PARALLEL) as pool:
        list(pool.map(lambda sid: revise("review", sid, review_content(
            frames[sid], found.get(sid, []), built[ids.index(sid)]["system"] is SCENE_REMIX_PROMPT)), frames))
    report = run_check(project)
    for attempt in range(1, MAX_REPAIRS + 1):
        found = {k: v for k, v in findings_by_scene(report, tm).items() if k in ids}
        if not found:
            break
        repair_round(f"onarım-{attempt + 1}", found)
        report = run_check(project)
    left = findings_by_scene(report, tm)

    say("8/8 render")
    video, render_s, loud = render(project, bool(music))

    llm_total = sum(c["cost_usd"] for c in llm.calls)
    result = {
        "job_id": job_id, "version": 3, "brief": brief, "brand": brand, "colors": colors, "format": fmt,
        "template": template, "reference": reference_id, "created": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "duration_s": tm["end"], "model": model_key, "bedrock_model_id": pricing.MODELS[model_key][0],
        "film": {"one_thing": board["one_thing"], "device": board["device"], "form": board["form"],
                 "look": board["look"]["medium"],
                 "scenes": [f"{s['id']} ({s['blueprint']})" for s in board["scenes"]]},
        "facts_fixed": fixes,
        "music": music and {**music, "query": board["music"]["query"]},
        "timing": tm, "mix": mix, "loudness": loud, "calls": llm.calls,
        "check_findings_left": sum(len(v) for v in left.values()),
        "check_errors_left": sum(report.get(s, {}).get("errorCount", 0) for s in ("lint", "runtime", "layout", "motion")),
        "render_seconds": round(render_s, 1),
        "cost": {"bedrock_usd": round(llm_total, 4),
                 "render_fargate_usd_est": round(pricing.render_cost(render_s), 5),
                 "total_usd": round(llm_total + pricing.render_cost(render_s), 4)},
        "video": str(video.relative_to(ROOT)),
    }
    (project / "result.json").write_text(json.dumps(result, ensure_ascii=False, indent=2))
    say(f"bitti → {result['video']} · Bedrock ${result['cost']['bedrock_usd']}")
    return result


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("brief")
    ap.add_argument("--brand", default="")
    ap.add_argument("--colors", default="")
    ap.add_argument("--format", default="16:9", choices=FORMATS)
    ap.add_argument("--duration", type=int, default=20)
    ap.add_argument("--model", default=pricing.DEFAULT_MODEL, choices=pricing.MODELS)
    ap.add_argument("--reference", default="", help="showcase reference id (library/showcase.json)")
    a = ap.parse_args()
    print(json.dumps(run(a.brief, a.brand, a.colors, a.format, a.duration, a.model, reference_id=a.reference),
                     indent=2, ensure_ascii=False))
