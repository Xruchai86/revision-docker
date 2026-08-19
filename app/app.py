import os
import threading
import queue
import uuid
from flask import Flask, request, jsonify, render_template

import dovi_core as core
import settings as settings_store

app = Flask(__name__)

_settings = settings_store.load()

# Sehr einfache, prozessinterne Job-Verwaltung - ausreichend fuer einen
# Einzelnutzer-Unraid-Container. Kein Redis/Celery noetig fuer diesen Umfang.
jobs: dict[str, dict] = {}
job_queue: "queue.Queue[str]" = queue.Queue()
_scan_cache: dict[str, core.MediaInfo] = {}


def _worker():
    while True:
        job_id = job_queue.get()
        job = jobs[job_id]
        job["status"] = "running"

        def log(line: str):
            job["log"].append(line)

        try:
            mi = _scan_cache[job["path"]]
            suffix = "_downsized" if job["job_type"] == "downsize" else "_DV81"
            out_path = os.path.join(job["output_folder"], _out_filename(mi, suffix))
            bitrate = job.get("target_bitrate_mbps")
            if job["job_type"] == "downsize":
                core.downsize(mi, out_path, log, profile_key=job["profile"], target_bitrate_mbps=bitrate)
            else:
                core.run_fix(mi, out_path, log, profile_key=job["profile"], target_bitrate_mbps=bitrate)
                threshold = float(_settings.get("downsize_threshold_mbps", 35.0))
                force_reencode = bool(_settings.get("force_reencode_dual_layer", False))
                core.maybe_chain_downsize(mi, out_path, log, job["profile"], threshold, bitrate, force_reencode)
            job["status"] = "done"
            job["output_path"] = out_path
        except Exception as ex:  # noqa: BLE001 - Job-Fehler sollen den Worker nicht sterben lassen
            job["status"] = "failed"
            job["error"] = str(ex)
            log(f"FEHLER: {ex}")
        finally:
            job_queue.task_done()


def _out_filename(mi: core.MediaInfo, suffix: str) -> str:
    base, _ = os.path.splitext(mi.filename)
    return f"{base}_{mi.container}{suffix}.mkv"


threading.Thread(target=_worker, daemon=True).start()


@app.route("/")
def index():
    return render_template("index.html", profiles=core.QUALITY_PROFILES, settings=_settings, temp_root=core.TEMP_ROOT)


@app.route("/api/settings", methods=["GET", "POST"])
def api_settings():
    global _settings
    if request.method == "POST":
        data = request.get_json()
        _settings.update({k: v for k, v in data.items() if k in settings_store.DEFAULTS})
        settings_store.save(_settings)
    return jsonify(_settings)


MEDIA_ROOT = os.path.normpath(os.environ.get("MEDIA_ROOT", "/media/source"))


@app.route("/api/browse")
def api_browse():
    """Listet Unterordner innerhalb von MEDIA_ROOT auf - fuer den Ordner-
    Browser im Frontend. rel_path ist relativ zu MEDIA_ROOT, niemals ein
    absoluter/externer Pfad - verhindert, dass man aus dem gemounteten
    Medienordner heraus navigieren kann (Path-Traversal-Schutz)."""
    rel_path = request.args.get("path", "").strip("/")
    target = os.path.normpath(os.path.join(MEDIA_ROOT, rel_path))

    # Sicherstellen, dass target wirklich INNERHALB von MEDIA_ROOT liegt - auch
    # nach normpath (faengt "../../etc" o.ae. ab).
    if os.path.commonpath([target, MEDIA_ROOT]) != os.path.normpath(MEDIA_ROOT):
        return jsonify({"error": "Ungültiger Pfad."}), 400
    if not os.path.isdir(target):
        return jsonify({"error": "Ordner nicht gefunden."}), 404

    try:
        entries = sorted(
            name for name in os.listdir(target)
            if os.path.isdir(os.path.join(target, name)) and not name.startswith(".")
        )
    except PermissionError:
        return jsonify({"error": "Keine Leserechte für diesen Ordner."}), 403

    clean_rel = os.path.relpath(target, MEDIA_ROOT)
    clean_rel = "" if clean_rel == "." else clean_rel
    return jsonify({
        "root": MEDIA_ROOT,
        "rel_path": clean_rel,
        "full_path": target,
        "folders": entries,
    })


@app.route("/api/scan", methods=["POST"])
def api_scan():
    data = request.get_json()
    folder = data.get("folder", "").strip()
    if not folder or not os.path.isdir(folder):
        return jsonify({"error": "Ordner nicht gefunden."}), 400

    threshold = float(_settings.get("downsize_threshold_mbps", 35.0))
    results = []
    for root, _, files in os.walk(folder):
        for fname in files:
            ext = os.path.splitext(fname)[1].lower()
            if ext not in core.VIDEO_EXTENSIONS:
                continue
            path = os.path.join(root, fname)
            try:
                mi = core.probe(path)
            except Exception:  # noqa: BLE001
                continue

            downsize_ok = core.can_downsize(mi, threshold)
            if mi.action in ("none", "unsupported") and not downsize_ok:
                continue

            _scan_cache[path] = mi
            results.append({
                "path": path,
                "filename": mi.filename,
                "container": mi.container,
                "resolution": f"{mi.width}x{mi.height}" if mi.width else "-",
                "bitrate_mbps": round(mi.bitrate_mbps, 1),
                "dv_profile": mi.dv_profile,
                "action": mi.action,
                "can_downsize": downsize_ok,
            })
    return jsonify({"results": results, "downsize_threshold_mbps": threshold})


def _queue_jobs(paths: list[str], output_folder: str, profile: str, job_type: str,
                 target_bitrate_mbps: float | None = None) -> list[str]:
    os.makedirs(output_folder, exist_ok=True)
    created = []
    for path in paths:
        if path not in _scan_cache:
            continue
        job_id = str(uuid.uuid4())
        jobs[job_id] = {
            "id": job_id,
            "path": path,
            "filename": _scan_cache[path].filename,
            "output_folder": output_folder,
            "profile": profile,
            "job_type": job_type,
            "target_bitrate_mbps": target_bitrate_mbps,
            "status": "queued",
            "log": [],
        }
        job_queue.put(job_id)
        created.append(job_id)
    return created


@app.route("/api/fix", methods=["POST"])
def api_fix():
    data = request.get_json()
    output_folder = data.get("output_folder", "").strip()
    if not output_folder:
        return jsonify({"error": "Kein Zielordner angegeben."}), 400
    _settings["output_folder"] = output_folder
    _settings["quality_profile"] = data.get("profile", "balanced")
    bitrate = data.get("target_bitrate_mbps")
    if bitrate:
        _settings["target_bitrate_mbps"] = float(bitrate)
    settings_store.save(_settings)

    created = _queue_jobs(data.get("paths", []), output_folder, _settings["quality_profile"], "fix", bitrate)
    return jsonify({"job_ids": created})


@app.route("/api/downsize", methods=["POST"])
def api_downsize():
    data = request.get_json()
    output_folder = data.get("output_folder", "").strip()
    if not output_folder:
        return jsonify({"error": "Kein Zielordner angegeben."}), 400
    _settings["output_folder"] = output_folder
    _settings["quality_profile"] = data.get("profile", "balanced")
    bitrate = data.get("target_bitrate_mbps")
    if bitrate:
        _settings["target_bitrate_mbps"] = float(bitrate)
    settings_store.save(_settings)

    created = _queue_jobs(data.get("paths", []), output_folder, _settings["quality_profile"], "downsize", bitrate)
    return jsonify({"job_ids": created})


@app.route("/api/jobs")
def api_jobs():
    return jsonify({"jobs": [
        {k: v for k, v in job.items() if k != "log"} for job in jobs.values()
    ]})


@app.route("/api/jobs/<job_id>/log")
def api_job_log(job_id):
    job = jobs.get(job_id)
    if job is None:
        return jsonify({"error": "Job nicht gefunden."}), 404
    return jsonify({"log": "\n".join(job["log"])})


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=8080)
