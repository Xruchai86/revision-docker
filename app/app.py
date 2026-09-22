import os
from datetime import datetime
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

            # Kalibrierung schreibt keine Ausgabedatei - sie misst nur und legt
            # das Ergebnis am Job ab, damit es die Oberflaeche anzeigen kann.
            if job["job_type"] == "calibrate":
                rows = core.calibrate_quality(
                    job["path"], job["profile"], job["quality_values"], log,
                    sample_seconds=job.get("sample_seconds", 120))
                job["calibration"] = rows
                # Unplausible Messungen nicht in Stufen verwandeln - sonst
                # wuerden aus einem Messfehler dauerhaft gespeicherte Werte.
                warning = core.calibration_plausibility(rows)
                job["calibration_warning"] = warning
                # Reagieren die schwaechsten 5 % nicht auf die Bitrate, sind sie
                # als Kriterium unbrauchbar - dann nur nach Durchschnitt waehlen
                # und das offen sagen, statt jeden Wert durchfallen zu lassen.
                p5_ok = core.calibration_p5_reliable(rows)
                job["calibration_note"] = None if p5_ok else (
                    "Die Werte der schwächsten 5 % reagieren kaum auf die Bitrate und "
                    "messen daher vermutlich keinen Encode-Verlust. Die Stufen wurden "
                    "deshalb nur nach dem Durchschnitt gewählt.")
                job["tiers"] = {} if warning else core.tiers_from_calibration(rows, use_p5=p5_ok)
                if warning:
                    log("WARNUNG: " + warning)
                if not p5_ok:
                    log("HINWEIS: " + job["calibration_note"])
                for t, e in job["tiers"].items():
                    log(f"Stufe {t}: Qualität {e['quality']} (VMAF {e['vmaf']}), "
                        f"gemessen {e['measured_mbps']} Mbit/s - Obergrenze beim "
                        "Encode ist die Bitrate der jeweiligen Originaldatei")
                job["status"] = "done"
                continue

            suffix = "_downsized" if job["job_type"] == "downsize" else "_DV81"
            out_path = os.path.join(job["output_folder"], _out_filename(mi, suffix))
            bitrate = job.get("target_bitrate_mbps")
            quality = job.get("quality")
            if job["job_type"] == "downsize":
                core.downsize(mi, out_path, log, profile_key=job["profile"],
                              target_bitrate_mbps=bitrate, quality=quality)
            else:
                core.run_fix(mi, out_path, log, profile_key=job["profile"],
                             target_bitrate_mbps=bitrate, quality=quality)
                threshold = float(_settings.get("downsize_threshold_mbps", 35.0))
                force_reencode = bool(_settings.get("force_reencode_dual_layer", False))
                core.maybe_chain_downsize(mi, out_path, log, job["profile"], threshold,
                                          bitrate, force_reencode, quality)
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
    return render_template("index.html", profiles=core.QUALITY_PROFILES,
                           categories=core.CATEGORIES, settings=_settings,
                           temp_root=core.TEMP_ROOT)


@app.route("/einstellungen")
def settings_page():
    """Eigene Seite fuer alles Dauerhafte (Zielordner, Ordner-Zuordnung,
    Qualitaetswert, Schwellen). Haelt die Hauptseite frei von Feldern, die man
    einmal einstellt und danach nicht mehr anfasst."""
    return render_template("settings.html", profiles=core.QUALITY_PROFILES,
                           categories=core.CATEGORIES, settings=_settings,
                           temp_root=core.TEMP_ROOT)


@app.route("/api/settings", methods=["GET", "POST"])
def api_settings():
    global _settings
    if request.method == "POST":
        data = request.get_json()
        _settings.update({k: v for k, v in data.items() if k in settings_store.DEFAULTS})
        settings_store.save(_settings)
    return jsonify(_settings)


MEDIA_ROOT = os.path.normpath(os.environ.get("MEDIA_ROOT", "/media/source"))
# Zweite Wurzel fuer den Ordner-Browser: der Ausgabeordner. Getrennt gehalten,
# damit der Browser nie zwischen Quell- und Zielbaum wechseln kann - der
# Path-Traversal-Schutz prueft immer gegen genau eine feste Wurzel.
OUTPUT_ROOT = os.path.normpath(os.environ.get("OUTPUT_ROOT", "/media/output"))
BROWSE_ROOTS = {"source": MEDIA_ROOT, "output": OUTPUT_ROOT}


@app.route("/api/browse")
def api_browse():
    """Listet Unterordner innerhalb von MEDIA_ROOT auf - fuer den Ordner-
    Browser im Frontend. rel_path ist relativ zu MEDIA_ROOT, niemals ein
    absoluter/externer Pfad - verhindert, dass man aus dem gemounteten
    Medienordner heraus navigieren kann (Path-Traversal-Schutz)."""
    rel_path = request.args.get("path", "").strip("/")
    root_key = request.args.get("root", "source")
    root = BROWSE_ROOTS.get(root_key)
    if root is None:
        return jsonify({"error": "Unbekannte Wurzel."}), 400

    target = os.path.normpath(os.path.join(root, rel_path))

    # Sicherstellen, dass target wirklich INNERHALB der gewaehlten Wurzel liegt -
    # auch nach normpath (faengt "../../etc" o.ae. ab).
    if os.path.commonpath([target, root]) != root:
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

    clean_rel = os.path.relpath(target, root)
    clean_rel = "" if clean_rel == "." else clean_rel
    return jsonify({
        "root": root,
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
                "is_sdr": mi.is_sdr,
                "action": mi.action,
                "can_downsize": downsize_ok,
            })
    return jsonify({
        "results": results,
        "downsize_threshold_mbps": threshold,
        # Ueber die Ordnerregeln erkannte Kategorie (oder None) - das Frontend
        # filtert damit die Profilliste, ohne dass manuell umgestellt werden muss.
        "category": category_for_path(folder),
    })


def _quality_override() -> int | None:
    """Globaler Qualitaetswert - 0/leer bedeutet "Profil-Standard verwenden",
    nicht "Qualitaet 0" (das waere nahezu verlustfrei und riesig)."""
    try:
        q = int(_settings.get("quality_override", 0) or 0)
    except (TypeError, ValueError):
        return None
    return q if q > 0 else None


def is_calibrated(path: str, category: str | None = None,
                  tier: str | None = None) -> bool:
    """True, wenn fuer diese Datei ein gemessener Qualitaetswert vorliegt."""
    cat = category or category_for_path(path)
    if not cat:
        return False
    per_cat = (_settings.get("quality_by_category") or {}).get(cat) or {}
    entry = per_cat.get(tier or "empfohlen") or per_cat.get("empfohlen") or {}
    try:
        return int(entry.get("quality", 0) or 0) > 0
    except (TypeError, ValueError):
        return False


def quality_for_path(path: str, category: str | None = None,
                     tier: str | None = None) -> int | None:
    """Qualitaetswert fuer eine konkrete Datei.

    Vorrang: gemessener Kategorie-Wert (aus der VMAF-Kalibrierung) > globaler
    Wert > None (= Preset-Standard). Damit wirkt eine Kalibrierung sofort und
    nur fuer ihre Kategorie - Anime und Realfilm koennen unterschiedliche Werte
    haben, ohne sich gegenseitig zu ueberschreiben."""
    # Eine in der Oberflaeche GEWAEHLTE Kategorie schlaegt die Ordnerregel.
    # Wichtig z.B. bei Animationsfilmen, die im selben Ordner wie Realfilme
    # liegen - dort kann keine Pfadregel unterscheiden, der Nutzer aber schon.
    cat = category or category_for_path(path)
    if cat:
        # Gemessene Werte liegen je Kategorie UND Stufe vor: dieselbe Messung
        # liefert "sparsam"/"empfohlen"/"max" - welcher davon gilt, haengt am
        # gewaehlten Profil.
        per_cat = (_settings.get("quality_by_category") or {}).get(cat) or {}
        entry = per_cat.get(tier or "empfohlen") or per_cat.get("empfohlen") or {}
        try:
            q = int(entry.get("quality", 0) or 0)
            if q > 0:
                return q
        except (TypeError, ValueError):
            pass
    return _quality_override()


def category_for_path(path: str) -> str | None:
    """Ordnet einen Quellpfad ueber die konfigurierten Regeln einer Kategorie zu.
    Erste Uebereinstimmung gewinnt, Gross-/Kleinschreibung egal - bei strikter
    Ordnerstruktur muss damit pro Aufgabe nichts mehr manuell umgestellt werden."""
    low = path.lower()
    for rule in _settings.get("category_rules") or []:
        frag = (rule.get("pfad") or "").strip().lower()
        cat = (rule.get("kategorie") or "").strip()
        if frag and cat and frag in low:
            return cat
    return None


def output_folder_for(path: str, fallback: str, category: str | None = None) -> str:
    """Waehlt den Ausgabeordner anhand der Kategorie des QUELLpfades. Ohne
    passende Regel oder ohne konfigurierten Kategorie-Ordner bleibt es beim
    allgemeinen Zielordner - so bleibt alles wie bisher, solange nichts
    eingerichtet ist."""
    cat = category or category_for_path(path)
    if cat:
        per_cat = (_settings.get("output_folders") or {}).get(cat, "")
        if per_cat and per_cat.strip():
            return per_cat.strip()
    return fallback


def _queue_jobs(paths: list[str], output_folder: str, profile: str, job_type: str,
                 target_bitrate_mbps: float | None = None,
                 quality: int | None = None,
                 category: str | None = None,
                 profile_map: dict | None = None) -> list[str]:
    created = []
    for path in paths:
        if path not in _scan_cache:
            continue
        # Zielordner pro Datei bestimmen - eine Auswahl kann Dateien aus
        # mehreren Kategorien enthalten, wenn ueber einen Sammelordner gescannt
        # wurde. Deshalb hier und nicht einmal vorab.
        target_folder = output_folder_for(path, output_folder, category)
        # Qualitaet ebenfalls pro Datei - eine Auswahl kann Dateien aus
        # mehreren Kategorien enthalten, die unterschiedlich kalibriert sind.
        # Profil pro Datei: SDR und HDR liegen bei vielen Sammlungen im selben
        # Ordner, deshalb muss die Wahl je Datei moeglich sein und nicht nur
        # pauschal fuer den ganzen Durchlauf.
        file_profile = (profile_map or {}).get(path) or profile
        file_tier = core.QUALITY_PROFILES.get(file_profile, {}).get("tier")
        file_quality = quality if quality is not None else quality_for_path(
            path, category, file_tier)

        # Obergrenze fuer kalibrierte Dateien: die Bitrate des ORIGINALS.
        # Bei QVBR ist die Qualitaet das Ziel, die Bitrate greift nur als
        # Grenze, wenn ein Frame das Ziel sonst nicht erreichen koennte - ein
        # hoher Deckel blaeht also nichts auf. Der gemessene Qualitaetswert
        # entscheidet allein; die Grenze verhindert nur, dass eine Datei
        # groesser wird als ihr Original.
        #
        # Frueher wurde aus der Kalibrierung ein Deckel von 1,5 x Durchschnitt
        # eines kurzen Ausschnitts abgeleitet - ein nicht recherchierter Faktor,
        # der in anspruchsvollen Szenen die Qualitaet gedrueckt haette. Und er
        # wurde zwar angezeigt, beim Encode aber nie verwendet.
        file_bitrate = target_bitrate_mbps
        if quality is None and is_calibrated(path, category, file_tier):
            src_mbps = getattr(_scan_cache[path], "bitrate_mbps", 0) or 0
            if src_mbps > 0:
                file_bitrate = round(src_mbps, 1)
        os.makedirs(target_folder, exist_ok=True)
        job_id = str(uuid.uuid4())
        jobs[job_id] = {
            "id": job_id,
            "path": path,
            "filename": _scan_cache[path].filename,
            "output_folder": target_folder,
            "profile": file_profile,
            "job_type": job_type,
            "target_bitrate_mbps": file_bitrate,
            "quality": file_quality,
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
    _settings["quality_profile"] = data.get("profile", "qvbr_film")
    bitrate = data.get("target_bitrate_mbps")
    if bitrate:
        _settings["target_bitrate_mbps"] = float(bitrate)
    settings_store.save(_settings)

    created = _queue_jobs(data.get("paths", []), output_folder, _settings["quality_profile"],
                          "fix", bitrate, category=data.get("category") or None,
                          profile_map=data.get("profile_map") or {})
    return jsonify({"job_ids": created})


@app.route("/api/downsize", methods=["POST"])
def api_downsize():
    data = request.get_json()
    output_folder = data.get("output_folder", "").strip()
    if not output_folder:
        return jsonify({"error": "Kein Zielordner angegeben."}), 400
    _settings["output_folder"] = output_folder
    _settings["quality_profile"] = data.get("profile", "qvbr_film")
    bitrate = data.get("target_bitrate_mbps")
    if bitrate:
        _settings["target_bitrate_mbps"] = float(bitrate)
    settings_store.save(_settings)

    created = _queue_jobs(data.get("paths", []), output_folder, _settings["quality_profile"],
                          "downsize", bitrate, category=data.get("category") or None,
                          profile_map=data.get("profile_map") or {})
    return jsonify({"job_ids": created})


@app.route("/api/vmaf/status")
def api_vmaf_status():
    """Sagt der Oberflaeche, ob gemessen werden kann - statt einen Knopf
    anzubieten, der dann an einem fehlenden Binary scheitert."""
    return jsonify({"available": core.vmaf_available()})


@app.route("/api/calibrate", methods=["POST"])
def api_calibrate():
    """Startet eine Qualitaets-Kalibrierung als Hintergrund-Job. Laeuft je nach
    Ausschnittlaenge und Anzahl der Werte etliche Minuten, deshalb kein
    blockierender Aufruf."""
    if not core.vmaf_available():
        return jsonify({"error": "Kein VMAF-fähiges ffmpeg im Image - Kalibrierung nicht möglich."}), 400

    data = request.get_json() or {}
    path = data.get("path", "")
    if path not in _scan_cache:
        return jsonify({"error": "Datei nicht im letzten Scan enthalten."}), 400

    values = data.get("quality_values") or [17, 19, 21, 23]
    try:
        values = sorted({int(v) for v in values})
    except (TypeError, ValueError):
        return jsonify({"error": "Ungültige Qualitätswerte."}), 400

    profile = data.get("profile") or _settings["quality_profile"]
    job_id = str(uuid.uuid4())
    jobs[job_id] = {
        "id": job_id,
        "path": path,
        "filename": _scan_cache[path].filename,
        "output_folder": "",
        "profile": profile,
        "job_type": "calibrate",
        "quality_values": values,
        "sample_seconds": int(data.get("sample_seconds") or 120),
        "status": "queued",
        "log": [],
    }
    job_queue.put(job_id)
    return jsonify({"job_id": job_id, "quality_values": values})


@app.route("/api/calibrate/apply", methods=["POST"])
def api_calibrate_apply():
    """Uebernimmt einen gemessenen Qualitaetswert fuer eine Kategorie. Ab dann
    verwendet jeder Job dieser Kategorie automatisch diesen Wert."""
    data = request.get_json() or {}
    cat = (data.get("category") or "").strip()
    if cat not in core.CATEGORIES:
        return jsonify({"error": "Unbekannte Kategorie."}), 400
    try:
        quality = int(data.get("quality")) if data.get("quality") is not None else 0
    except (TypeError, ValueError):
        return jsonify({"error": "Ungültiger Qualitätswert."}), 400
    if not quality and not data.get("tiers"):
        return jsonify({"error": "Weder Qualitätswert noch Stufen übergeben."}), 400

    tiers = data.get("tiers") or {}
    stamp = datetime.now().strftime("%Y-%m-%d %H:%M")
    source = data.get("source", "")

    entry = {}
    if tiers:
        # Alle gemessenen Stufen auf einmal - sie stammen aus derselben Messung
        # und gehoeren zusammen.
        for tier, e in tiers.items():
            if tier not in core.TIER_VMAF_TARGETS:
                continue
            entry[tier] = {
                "quality": int(e.get("quality")),
                "vmaf": e.get("vmaf"),
                "vmaf_p5": e.get("vmaf_p5"),
                "cambi": e.get("cambi"),
                "measured_mbps": e.get("measured_mbps"),
                "measured_at": stamp,
                "source": source,
            }
    else:
        # Einzelwert (aus einer Tabellenzeile) - gilt als "empfohlen".
        entry["empfohlen"] = {
            "quality": quality, "vmaf": data.get("vmaf"),
            "measured_at": stamp, "source": source,
        }

    by_cat = dict(_settings.get("quality_by_category") or {})
    # merge=True: nur die uebergebenen Stufen setzen, die anderen behalten.
    # Noetig fuer die Einzelauswahl pro Tabellenzeile - sonst wuerde das
    # Uebernehmen EINER Stufe die beiden anderen stillschweigend loeschen.
    if data.get("merge"):
        merged = dict(by_cat.get(cat) or {})
        merged.update(entry)
        entry = merged
    by_cat[cat] = entry
    _settings["quality_by_category"] = by_cat
    settings_store.save(_settings)
    return jsonify({"ok": True, "category": cat, "entry": entry})


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
