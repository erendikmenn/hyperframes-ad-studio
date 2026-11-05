"""Marketing video platform (POC): reference films from the HyperFrames showcase to remix,
and a job queue that runs the pipeline one video at a time (Bedrock request quota).

    python app.py   # http://localhost:8080

On a server: APP_PASSWORD turns on the login page, ORIGIN_SECRET refuses requests that did not
come through the CloudFront distribution, DATA_DIR holds videos, references and the job history.
/healthz says "sleep" once nobody has used the app for IDLE_MINUTES and no work is running; a
timer on the server then shuts it down, and the wake page in deploy/template.yaml starts it again.
"""

import hashlib
import hmac
import json
import os
import queue
import shutil
import threading
import time
import uuid

from flask import Flask, abort, jsonify, redirect, request, send_from_directory, session

import pipeline
import pricing
import reference

app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = reference.MAX_BYTES
PASSWORD = os.environ.get("APP_PASSWORD", "")
ORIGIN_SECRET = os.environ.get("ORIGIN_SECRET", "")
app.secret_key = hashlib.sha256(f"session:{PASSWORD}".encode()).hexdigest()
app.config.update(SESSION_COOKIE_SECURE=bool(ORIGIN_SECRET), SESSION_COOKIE_SAMESITE="Lax",
                  PERMANENT_SESSION_LIFETIME=30 * 86400)
JOBS_FILE = pipeline.DATA / "jobs.json"
JOBS: dict[str, dict] = {}
QUEUE: queue.Queue = queue.Queue()
SAVING = threading.Lock()
TASKS: dict[str, dict] = {}   # references being added; a slow download must outlive CloudFront's 60 s
IDLE_SECONDS = int(os.environ.get("IDLE_MINUTES", 30)) * 60
LAST_USED = time.time()


def save_jobs():
    with SAVING:
        JOBS_FILE.parent.mkdir(parents=True, exist_ok=True)
        tmp = JOBS_FILE.with_suffix(".tmp")
        tmp.write_text(json.dumps(sorted(list(JOBS.values()), key=lambda j: -j["queued"])[:200], ensure_ascii=False))
        tmp.replace(JOBS_FILE)


def load_jobs():
    """Everyone follows the same history; a job cut off by a restart is reported, not lost."""
    if not JOBS_FILE.exists():
        return
    for job in json.loads(JOBS_FILE.read_text()):
        if job["status"] in ("queued", "running"):
            job["status"], job["error"] = "error", "Sunucu yeniden başladı, işi tekrar başlatın."
        JOBS[job["id"]] = job


@app.get("/healthz")
def healthz():
    busy = (any(j["status"] in ("queued", "running") for j in list(JOBS.values()))
            or any(t["status"] == "running" for t in list(TASKS.values())))
    return "sleep" if not busy and time.time() - LAST_USED > IDLE_SECONDS else "awake"


@app.before_request
def guard():
    global LAST_USED
    if request.endpoint == "healthz":
        return None
    # An open tab polls the queue every few seconds; that alone must not keep the server awake.
    if not (request.method == "GET" and request.path.startswith("/api/jobs")):
        LAST_USED = time.time()
    if ORIGIN_SECRET and not hmac.compare_digest(request.headers.get("X-Origin-Verify", ""), ORIGIN_SECRET):
        abort(403)
    if PASSWORD and not session.get("in") and request.endpoint != "login":
        if request.path.startswith("/api/"):
            return jsonify({"error": "Giriş gerekli."}), 401
        return redirect("/login")


@app.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        if hmac.compare_digest(request.form.get("password", ""), PASSWORD):
            session.permanent, session["in"] = True, True
            return redirect("/")
        time.sleep(1)
        return redirect("/login?hata=1")
    return send_from_directory(pipeline.ROOT / "static", "login.html")


def worker():
    while True:
        key = QUEUE.get()
        job = JOBS[key]
        job["status"], job["started"] = "running", time.time()
        save_jobs()
        try:
            job["result"] = pipeline.run(**job["params"], log=lambda line: job["log"].append(line))
            job["status"] = "done"
        except Exception as e:  # surface any failure to the UI
            job["status"], job["error"] = "error", str(e)
        job["finished"] = time.time()
        save_jobs()



def model_for(key):
    """The subscription route needs the `claude` CLI, which servers do not have."""
    if key not in pricing.MODELS or (key.startswith("sub-") and not shutil.which("claude")):
        return pricing.DEFAULT_MODEL
    return key


@app.post("/api/jobs")
def create_job():
    body = request.get_json()
    if not body.get("brief", "").strip():
        abort(400, "brief gerekli")
    params = {
        "brief": body["brief"].strip(),
        "brand": body.get("brand", "").strip(),
        "colors": body.get("colors", "").strip(),
        "fmt": body.get("format", "16:9") if body.get("format") in pipeline.FORMATS else "16:9",
        "duration": max(12, min(int(body.get("duration", 20)), 45)),
        "model_key": model_for(body.get("model")),
        "template": body.get("template", ""),
        "reference_id": body.get("reference") if reference.known(body.get("reference")) else "",
    }
    key = uuid.uuid4().hex[:8]
    JOBS[key] = {"id": key, "status": "queued", "log": [], "params": params, "queued": time.time()}
    save_jobs()
    QUEUE.put(key)
    return jsonify({"id": key, "position": QUEUE.qsize()})


@app.get("/api/jobs")
def list_jobs():
    return jsonify([{k: v for k, v in j.items() if k != "log"} | {"last": (j["log"] or [""])[-1]}
                    for j in sorted(JOBS.values(), key=lambda j: -j["queued"])])


@app.get("/api/jobs/<key>")
def get_job(key):
    return jsonify(JOBS.get(key) or abort(404))


def scene_frames(run, result):
    """One still from the middle of each scene, cut from the video once and kept with the run."""
    out = {}
    for t in (result.get("timing") or {}).get("scenes", []):
        still = run / "frames" / f"{t['id']}.jpg"
        if not still.exists():
            still.parent.mkdir(exist_ok=True)
            pipeline.sh("ffmpeg", "-loglevel", "error", "-y", "-ss", str((t["cut"] + t["cut_out"]) / 2), "-i",
                        str(run / "video.mp4"), "-frames:v", "1", "-vf", "scale=480:-2", "-q:v", "4", str(still),
                        check=False)
        if still.exists():
            out[t["id"]] = f"runs/{run.name}/frames/{still.name}"
    return out


@app.get("/api/videos/<job_id>")
def video_detail(job_id):
    run = pipeline.RUNS / job_id
    if not (run / "result.json").exists() or "/" in job_id:
        abort(404)
    result = json.loads((run / "result.json").read_text())
    board = json.loads((run / "storyboard.json").read_text()) if (run / "storyboard.json").exists() else {}
    return jsonify({"result": result, "board": board, "frames": scene_frames(run, result)})


def showcase_item(r):
    spec = reference.cached(r["id"])
    edit = spec and {k: spec.get(k) for k in ("summary", "pacing", "look", "signature_moves",
                                              "transitions", "duration", "shot_count", "sound", "sound_measured")}
    if spec:
        edit["acts"] = [a | {"frame": reference.nearest_frame(spec, a)} for a in spec["acts"]]
    return r | {"analyzed": bool(spec), "edit": edit}


@app.get("/api/showcase")
def showcase():
    return jsonify([showcase_item(r) for r in reference.all_refs()])


@app.get("/api/showcase/<ref_id>")
def showcase_one(ref_id):
    if not reference.known(ref_id):
        abort(404)
    return jsonify(showcase_item(reference.entry(ref_id)))


@app.post("/api/references")
def add_reference():
    """A user's own reference: a video link (JSON) or an uploaded file (multipart). Runs in the
    background; the page follows /api/references/<task>."""
    if "file" in request.files:
        d, name = reference.stage_upload(request.files["file"])
        work = lambda: reference.add_upload(d, name)
    else:
        url = (request.get_json(silent=True) or {}).get("url", "")
        try:
            reference.check_url(url)
        except reference.RefError as e:
            return jsonify({"error": str(e)}), 400
        work = lambda: reference.add_link(url)
    key = uuid.uuid4().hex[:8]
    TASKS[key] = {"status": "running"}

    def run():
        try:
            TASKS[key] = {"status": "done", "id": work()["id"]}
        except reference.RefError as e:
            TASKS[key] = {"status": "error", "error": str(e)}
        except Exception:
            TASKS[key] = {"status": "error", "error": "Video eklenemedi."}

    threading.Thread(target=run, daemon=True).start()
    return jsonify({"task": key})


@app.get("/api/references/<key>")
def reference_task(key):
    return jsonify(TASKS.get(key) or abort(404))


@app.errorhandler(413)
def too_large(_):
    return jsonify({"error": "Dosya 300 MB'tan büyük olamaz."}), 413


@app.get("/references/<ref_id>/<any('source.mp4', 'poster.jpg'):name>")
def reference_file(ref_id, name):
    if not ref_id.startswith("u-"):
        abort(404)
    return send_from_directory(reference.CACHE / ref_id, name)


@app.get("/references/<ref_id>/frames/<name>")
def reference_frame(ref_id, name):
    return send_from_directory(reference.CACHE / ref_id / "frames", name)


@app.get("/runs/<path:path>")
def runs(path):
    return send_from_directory(pipeline.RUNS, path)


@app.get("/")
def index():
    return send_from_directory(pipeline.ROOT / "static", "index.html")


load_jobs()
threading.Thread(target=worker, daemon=True).start()

if __name__ == "__main__":
    app.run(port=8080, debug=False, threaded=True)
