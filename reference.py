"""Reference films: the HyperFrames showcase, plus any video a user adds by link or upload.
Download once, measure, and have Claude write the film's edit (shots, rhythm, type, look) so a
new brief can be remixed onto it. Only the edit is reused, never the original's footage.

Cached per reference in library/references/<id>/: source.mp4, frames/, spec.json; a user's
reference also keeps meta.json and poster.jpg and has an id starting with "u-".
"""

import base64
import ipaddress
import json
import re
import shutil
import socket
import threading
import urllib.request
import uuid
from math import gcd
from urllib.parse import urlparse

from pipeline import DATA, LIB, fenced, sh

SHOWCASE = {r["id"]: r for r in json.loads((LIB / "showcase.json").read_text())}
CACHE = DATA / "library" / "references"
MAX_FRAMES = 12
MAX_SECONDS = 300          # a reference is an ad, not a long video; also bounds download and measuring
MAX_BYTES = 300 * 1024 * 1024
PLATFORMS = ("youtube.com", "youtu.be", "tiktok.com", "instagram.com", "x.com", "twitter.com")
VIDEO_FILES = (".mp4", ".webm", ".mov")
_locks: dict[str, threading.Lock] = {}

ANALYZE_PROMPT = """You are an editor breaking down a finished promo film so a motion team can remix it for another brand: same cuts, rhythm, layouts, type treatment and signature moves; new words, product and colours.
You get the film's measured shots (start/end seconds), its measured sound when it has any, and one frame from the middle of each shot (labelled with its shot number). Describe what is there, concretely and in numbers; never name or describe the original brand, its logo or its copy beyond their role ("the product name", "a 3-word claim").

Return only a ```json block:
{
  "summary": "one sentence: what kind of film this is and its structure",
  "pacing": "the rhythm in numbers: average shot length, where it slows down, where it cuts fast",
  "look": {"medium": "the visual medium in one line (e.g. 'flat colour fields + giant grotesk type')", "ground": "background treatment", "palette_roles": "how many colours and what each does (not hex of the brand)", "type": "type treatment: family style (grotesk/serif/mono), weight, case, size share of the frame, tracking", "rules": ["3-5 measurable rules"]},
  "signature_moves": ["2-4 moves that make this film recognisable, each with timing"],
  "transitions": "how shots hand over (hard cut, match cut, push, zoom, mask wipe) with durations",
  "sound": {"role": "what the soundtrack does: music bed, voice-over, or both, and how it drives the edit", "mood": "the feel in a few words", "arc": "how energy moves across the film, with seconds", "music_query": "English stock-music search query for a similar bed, without any brand or track name"},
  "acts": [
    {"shots": [1, 3], "start": 0.0, "end": 2.9, "role": "hook | tension | reveal | proof | payoff | endcard", "layout": "what is where in the frame and how big", "type": "what text does (count of words, size, animation)", "motion": "camera and element motion with eases/durations", "hand_off": "how it leads into the next act", "sound": "what the sound does in this act"}
  ]
}
Group the shots into 4-7 acts (a fast run of cuts on one idea is one act). `shots` is the first and last shot number of the act. With no measured sound, `sound` is null and the acts have no `sound`."""


class RefError(ValueError):
    """A problem to show the user as is."""


def user_refs():
    metas = sorted(CACHE.glob("u-*/meta.json"), key=lambda p: p.stat().st_mtime, reverse=True)
    return [json.loads(p.read_text()) for p in metas]


def all_refs():
    return user_refs() + list(SHOWCASE.values())


def known(ref_id):
    return ref_id in SHOWCASE or bool(re.fullmatch(r"u-[0-9a-f]{8}", str(ref_id))
                                      and (CACHE / ref_id / "meta.json").is_file())


def entry(ref_id):
    if ref_id in SHOWCASE:
        return SHOWCASE[ref_id]
    if known(ref_id):
        return json.loads((CACHE / ref_id / "meta.json").read_text())
    raise KeyError(f"bilinmeyen referans: {ref_id}")


def check_url(url):
    """Return the url or refuse: the server fetches this address, so only public video
    platforms and direct https video files on public hosts are allowed."""
    u = urlparse(url.strip())
    host = (u.hostname or "").lower()
    if u.scheme != "https" or not host:
        raise RefError("Link https:// ile başlamalı.")
    if any(host == d or host.endswith("." + d) for d in PLATFORMS):
        return url.strip()
    if not u.path.lower().endswith(VIDEO_FILES):
        raise RefError("Bu link desteklenmiyor. YouTube, TikTok, Instagram, X linki ya da .mp4 dosya linki yapıştırın.")
    try:
        addrs = {a[4][0] for a in socket.getaddrinfo(host, 443)}
    except OSError:
        raise RefError("Bu adrese ulaşılamadı.") from None
    if not all(ipaddress.ip_address(a).is_global for a in addrs):
        raise RefError("Yerel ağ adresleri kullanılamaz.")
    return url.strip()


def finish(d, name, source="", original=""):
    """Validate a downloaded video and register it as a reference."""
    video = d / "source.mp4"
    res = sh("npx", "hyperframes", "measure", "probe", str(video), check=False)
    try:
        probe = json.loads(res.stdout)
        w, h, seconds = probe["width"], probe["height"], probe["duration"]
    except (json.JSONDecodeError, KeyError):
        raise RefError("Bu dosya video olarak okunamadı.") from None
    if seconds > MAX_SECONDS:
        raise RefError(f"Video {MAX_SECONDS // 60} dakikadan uzun olamaz.")
    sh("ffmpeg", "-loglevel", "error", "-y", "-ss", str(round(min(1.0, seconds / 3), 2)), "-i", str(video),
       "-frames:v", "1", "-vf", "scale='min(960,iw)':-2", "-q:v", "3", str(d / "poster.jpg"))
    k = gcd(w, h)
    meta = {"id": d.name, "name": name[:90] or "Referans video", "category": "Eklediklerim", "kind": "Eklenen video",
            "ratio": f"{w // k}/{h // k}", "seconds": seconds, "video": f"/references/{d.name}/source.mp4",
            "poster": f"/references/{d.name}/poster.jpg", "credit": source, "original": original}
    (d / "meta.json").write_text(json.dumps(meta, ensure_ascii=False, indent=1))
    return meta


def _new_dir():
    d = CACHE / f"u-{uuid.uuid4().hex[:8]}"
    d.mkdir(parents=True)
    return d


def add_link(url):
    url = check_url(url)
    source = urlparse(url).hostname.removeprefix("www.")
    d = _new_dir()
    try:
        res = sh("yt-dlp", "--no-playlist", "--no-simulate", "--no-warnings", "--print", "%(title)s",
                 "--match-filter", f"duration<=?{MAX_SECONDS}", "--max-filesize", str(MAX_BYTES),
                 "-f", "bv*[height<=720][ext=mp4]+ba[ext=m4a]/b[height<=720][ext=mp4]/bv*[height<=720]+ba/b",
                 "--merge-output-format", "mp4", "-o", str(d / "source.mp4"), "--", url, timeout=300, check=False)
        if not (d / "source.mp4").exists():
            why = (res.stderr or "").lower()
            if "login" in why or "cookies" in why or "private" in why:
                raise RefError("Bu video herkese açık değil ya da platform giriş istiyor. Videoyu dosya olarak yükleyin.")
            raise RefError(f"Video indirilemedi. {MAX_SECONDS // 60} dakikadan kısa, herkese açık bir video olmalı.")
        title = res.stdout.strip().splitlines()[0] if res.stdout.strip() else ""
        return finish(d, "" if title in ("NA", "") else title, source, url)
    except Exception:
        shutil.rmtree(d, ignore_errors=True)
        raise


def stage_upload(file):
    """Save the request's file; `add_upload` then processes it outside the request."""
    d = _new_dir()
    file.save(d / "upload")
    return d, file.filename or ""


def add_upload(d, filename):
    raw = d / "upload"
    try:
        # Re-encoded so every browser plays it; also rejects anything that is not a video.
        res = sh("ffmpeg", "-loglevel", "error", "-y", "-i", str(raw), "-t", str(MAX_SECONDS + 1),
                 "-vf", "scale=-2:'min(720,ih)'", "-c:v", "libx264", "-preset", "veryfast", "-pix_fmt", "yuv420p",
                 "-c:a", "aac", "-movflags", "+faststart", str(d / "source.mp4"), timeout=300, check=False)
        raw.unlink()
        if res.returncode:
            raise RefError("Bu dosya video olarak okunamadı.")
        return finish(d, re.sub(r"\.\w+$", "", filename))
    except Exception:
        shutil.rmtree(d, ignore_errors=True)
        raise


def measure_sound(video):
    """The soundtrack's structure, from `hyperframes audio`: never the audio itself."""
    res = sh("npx", "hyperframes", "audio", str(video), "--only",
             "kind,onsets,loudness,silence,envelope,bpm,sections,describe", "--json", check=False, timeout=300)
    try:
        a = json.loads(res.stdout)["items"][0]
    except (json.JSONDecodeError, KeyError, IndexError):
        return None
    hits = []
    for e in sorted(a["onsets"]["events"], key=lambda e: -e["strength"]):
        if len(hits) < 40 and all(abs(e["t"] - h) >= 0.4 for h in hits):
            hits.append(e["t"])
    env = a["envelope"]
    step = max(1, round(1000 / env["hopMs"]))
    per_second = [max(env["values"][i:i + step]) for i in range(0, len(env["values"]), step)]
    return {"kind": a["kind"]["value"], "tags": [t["label"] for t in (a.get("describe") or {}).get("tags", [])[:6]],
            "bpm": (a.get("bpm") or {}).get("value"), "lufs": a["loudness"]["lufsIntegrated"],
            "sections": (a.get("sections") or {}).get("spans", []), "hits": sorted(hits),
            "silences": a.get("silence", []), "energy_db_per_second": [round(v, 1) for v in per_second]}


def cached(ref_id):
    p = CACHE / ref_id / "spec.json"
    return json.loads(p.read_text()) if p.exists() else None


def ensure(ref_id, llm, log=print):
    """Return the reference's edit spec, analysing it the first time (one Claude call with frames)."""
    with _locks.setdefault(ref_id, threading.Lock()):
        spec = cached(ref_id)
        if spec:
            log(f"  referans önbellekten: {ref_id}")
            return spec
        ref, d = entry(ref_id), CACHE / ref_id
        (d / "frames").mkdir(parents=True, exist_ok=True)
        video = d / "source.mp4"
        if not video.exists():
            log(f"  referans indiriliyor: {ref['video']}")
            urllib.request.urlretrieve(ref["video"], video)
        probe = json.loads(sh("npx", "hyperframes", "measure", "probe", str(video)).stdout)
        cuts = json.loads(sh("npx", "hyperframes", "measure", "cuts", str(video)).stdout)
        shots = cuts["shots"] or [{"start": 0, "end": probe["duration"], "seconds": probe["duration"]}]
        # One frame per shot, thinned evenly when the film has more shots than we send.
        pick = list(range(len(shots)))
        if len(pick) > MAX_FRAMES:
            pick = sorted({round(i * (len(shots) - 1) / (MAX_FRAMES - 1)) for i in range(MAX_FRAMES)})
        sound = measure_sound(video) if probe.get("audio") else None
        content = [{"type": "text", "text": json.dumps({
            "size": f"{probe['width']}x{probe['height']}", "duration": probe["duration"], "sound": sound,
            "shots": [{"n": i + 1, "start": s["start"], "end": s["end"]} for i, s in enumerate(shots)]})}]
        for i in pick:
            s = shots[i]
            at = round((s["start"] + s["end"]) / 2, 3)
            out = d / "frames" / f"shot{i + 1:02d}.jpg"
            sh("ffmpeg", "-loglevel", "error", "-y", "-ss", str(at), "-i", str(video), "-frames:v", "1",
               "-vf", "scale=640:-2", "-q:v", "4", str(out))
            content += [{"type": "text", "text": f"Shot {i + 1} ({s['start']:.2f}-{s['end']:.2f} s)"},
                        {"type": "image", "source": {"type": "base64", "media_type": "image/jpeg",
                                                     "data": base64.b64encode(out.read_bytes()).decode()}}]
        log(f"  referans analizi: {len(shots)} çekim, {len(pick)} kare (Claude)")
        msg = llm.ask(f"reference:{ref_id}", ANALYZE_PROMPT, [{"role": "user", "content": content}], max_tokens=12000)
        spec = json.loads(fenced(msg, "json") or "".join(b.text for b in msg.content if b.type == "text"))
        spec.update(id=ref_id, name=ref["name"], width=probe["width"], height=probe["height"],
                    duration=probe["duration"], shot_count=len(shots), shots=shots,
                    frames=[f"shot{i + 1:02d}.jpg" for i in pick], sound_measured=sound,
                    analysis_cost_usd=llm.calls[-1]["cost_usd"])
        (d / "spec.json").write_text(json.dumps(spec, ensure_ascii=False, indent=1))
        return spec


def _image(ref_id, name):
    data = (CACHE / ref_id / "frames" / name).read_bytes()
    return {"type": "image", "source": {"type": "base64", "media_type": "image/jpeg",
                                        "data": base64.b64encode(data).decode()}}


def frames_content(spec):
    """The reference's frames for the planner, each labelled with its shot number."""
    blocks = [{"type": "text", "text": "The reference film's frames (one per shot):"}]
    for name in spec["frames"]:
        shot = int(re.search(r"\d+", name).group())
        blocks += [{"type": "text", "text": f"Shot {shot}"}, _image(spec["id"], name)]
    return blocks


def nearest_frame(spec, act):
    """The cached frame closest to the middle of an act."""
    lo, hi = (act.get("shots") or [1, 1])[0], (act.get("shots") or [1, 1])[-1]
    return min(spec["frames"], key=lambda f: abs(int(re.search(r"\d+", f).group()) - (lo + hi) / 2))


def act_frame(ref_id, spec, act):
    """An act's frame as an image block for the scene builder."""
    return _image(ref_id, nearest_frame(spec, act))
