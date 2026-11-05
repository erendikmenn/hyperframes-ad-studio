"""Marketing video platform (POC): reference films from the HyperFrames showcase to remix,
and a job queue that runs the pipeline one video at a time (Bedrock request quota).

    python app.py   # http://localhost:8080
"""

import json
import queue
import threading
import time
import uuid

from flask import Flask, abort, jsonify, request, send_from_directory

import pipeline
import pricing
import reference

app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = reference.MAX_BYTES
JOBS: dict[str, dict] = {}
QUEUE: queue.Queue = queue.Queue()


def worker():
    while True:
        key = QUEUE.get()
        job = JOBS[key]
        job["status"], job["started"] = "running", time.time()
        try:
            job["result"] = pipeline.run(**job["params"], log=lambda line: job["log"].append(line))
            job["status"] = "done"
        except Exception as e:  # surface any failure to the UI
            job["status"], job["error"] = "error", str(e)
        job["finished"] = time.time()



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
        "model_key": body.get("model") if body.get("model") in pricing.MODELS else pricing.DEFAULT_MODEL,
        "template": body.get("template", ""),
        "reference_id": body.get("reference") if reference.known(body.get("reference")) else "",
    }
    key = uuid.uuid4().hex[:8]
    JOBS[key] = {"id": key, "status": "queued", "log": [], "params": params, "queued": time.time()}
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
    """A user's own reference: a video link (JSON) or an uploaded file (multipart)."""
    try:
        if "file" in request.files:
            return jsonify(reference.add_upload(request.files["file"]))
        return jsonify(reference.add_link((request.get_json(silent=True) or {}).get("url", "")))
    except reference.RefError as e:
        return jsonify({"error": str(e)}), 400


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


threading.Thread(target=worker, daemon=True).start()

if __name__ == "__main__":
    app.run(port=8080, debug=False, threaded=True)
