"""
Kernlogik von ReVision, portiert nach Python fuer die Docker/Unraid-Version.
Gleiche Prinzipien wie in der Windows-App (DoviConverter.cs/MediaScanner.cs):

- Dual-Layer-Quellen (Profile 7, 4, ...) -> verlustfrei, EL wird verworfen.
- Single-Layer ohne Base-Layer (Profile 5, 9, ...) -> muss reencodiert werden.
- Single-Layer MIT Base-Layer, falsch markiert (z.B. 8.2/8.4) -> verlustfrei
  relabeln, nur die RPU-Kennung wird auf 8.1 umgestellt.

Encoder-Backend hier: direktes VAAPI (hevc_vaapi), passend zur Intel-iGPU auf
Unraid-Boxen (z.B. Core Ultra 5/Arrow Lake) - kein NVENC, da Unraid-Server
typischerweise keine dedizierte NVIDIA-GPU haben (waere aber als zweites
Backend nachruestbar, analog zur Windows-App). QSV/oneVPL wurde bewusst NICHT
verwendet - siehe Bugfix-Historie im README, die komplette oneVPL-Geraete-
Verkettung scheiterte auf getesteter Hardware zuverlaessig, waehrend direktes
VAAPI sofort funktionierte.
"""
import json
import os
import subprocess
import tempfile
import shutil
import uuid
from dataclasses import dataclass, field
from typing import Optional

FFMPEG = shutil.which("ffmpeg") or "/usr/bin/ffmpeg"
DOVI_TOOL = shutil.which("dovi_tool") or "/usr/local/bin/dovi_tool"
MKVMERGE = shutil.which("mkvmerge") or "/usr/bin/mkvmerge"
MEDIAINFO = shutil.which("mediainfo") or "/usr/bin/mediainfo"

VIDEO_EXTENSIONS = {".mkv", ".mp4", ".m2ts"}

# Explizit selbst ausgelesen statt uns blind auf Pythons implizite TMPDIR-
# Erkennung (tempfile.gettempdir()) zu verlassen - falls die Umgebungsvariable
# aus irgendeinem Grund nicht durchgereicht wird (Container nicht neu gestartet,
# Docker-Eigenheit, o.ae.), soll das hier klar sichtbar sein statt still auf
# das volle System-/tmp zurueckzufallen. Wird bei jedem Scan-Aufruf geloggt
# (siehe app.py), damit man den tatsaechlich verwendeten Pfad sofort sieht.
TEMP_ROOT = os.environ.get("TMPDIR") or os.environ.get("TEMP_ROOT") or "/tmp"
os.makedirs(TEMP_ROOT, exist_ok=True)
print(f"[ReVision] Zwischendateien-Pfad (TEMP_ROOT): {TEMP_ROOT}", flush=True)

# Direktes VAAPI statt QSV/oneVPL - siehe Bugfix-Historie im README: die ganze
# oneVPL-Geraete-Verkettung ("-init_hw_device qsv=hw@va") scheiterte auf diesem
# System zuverlaessig mit "Error setting child device handle: -17", trotz
# mehrerer verschiedener, community-dokumentierter Loesungsversuche. Ein
# direkter VAAPI-Testencode (ohne jede QSV/oneVPL-Beteiligung) lief dagegen auf
# demselben System sofort fehlerfrei durch - GPU und Treiber sind also in
# Ordnung, das Problem sass ausschliesslich in der oneVPL-Softwareschicht.
# Ueber Umgebungsvariable VAAPI_DEVICE anpassbar, falls der Render-Node anders
# heisst (mehrere GPUs im System o.ae.).
VAAPI_DEVICE = os.environ.get("VAAPI_DEVICE", os.environ.get("QSV_DEVICE", "/dev/dri/renderD128"))


def _temp_dir(prefix: str) -> tempfile.TemporaryDirectory:
    """Wie tempfile.TemporaryDirectory(), aber mit explizit erzwungenem TEMP_ROOT
    statt Pythons eigener (evtl. fehlerhafter) TMPDIR-Herleitung zu vertrauen.
    ignore_cleanup_errors=True: ein Aufräumfehler beim Loeschen (z.B. Rest-
    Datei-Handle einer abgebrochenen Festplatte-voll-Situation) soll den JOB
    nicht zum Scheitern bringen, nur die Bereinigung selbst darf leise scheitern."""
    return tempfile.TemporaryDirectory(prefix=prefix, dir=TEMP_ROOT, ignore_cleanup_errors=True)


# ---------------------------------------------------------------------------
# Qualitätsprofile - dieselben vier wie in ReVision (Ausgewogen/Maximale
# Qualität/Kleinere Dateien/Schnell), mit den frueheren QSV-Werten (jetzt fuer VAAPI weiterverwendet)
# uebernommen (inkl. der nachtraeglich ergaenzten extbrc/rdo/mbbrc/b_strategy-
# Kopplung an Lookahead).
# ---------------------------------------------------------------------------
QUALITY_PROFILES = {
    "balanced": dict(
        name="Ausgewogen (Standard)", preset="medium",
        quality_fix=20, quality_downsize=20, quality_sdr=23,
        bframes=3, lookahead=True, lookahead_depth=32,
    ),
    "max": dict(
        name="Maximale Qualität (langsam)", preset="veryslow",
        quality_fix=18, quality_downsize=18, quality_sdr=20,
        bframes=4, lookahead=True, lookahead_depth=40,
    ),
    "smaller": dict(
        name="Kleinere Dateien (schneller)", preset="fast",
        quality_fix=23, quality_downsize=24, quality_sdr=26,
        bframes=3, lookahead=True, lookahead_depth=20,
    ),
    "fast": dict(
        name="Schnell (Entwurf/Test)", preset="veryfast",
        quality_fix=24, quality_downsize=25, quality_sdr=27,
        bframes=2, lookahead=False, lookahead_depth=8,
    ),
}


def build_vaapi_args(profile: dict, qp: int) -> list[str]:
    """Baut die hevc_vaapi-Argumentliste. CQP-Modus mit -qp (0-52, niedriger=
    besser) statt -global_quality/ICQ - letzteres zeigte in mehreren Community-
    Tests inkonsistente Skalierung je nach Treiber-Version, waehrend -qp direkt
    aus 'ffmpeg -h encoder=hevc_vaapi' mit klarer, eindeutiger Bedeutung
    dokumentiert ist. Kein Aequivalent zu QSVs extbrc/rdo/mbbrc/look_ahead -
    das waren MediaSDK/oneVPL-spezifische Erweiterungen, VAAPIs Rate-Control
    ist bewusst einfacher gehalten."""
    return [
        "-rc_mode", "CQP",
        "-qp", str(qp),
        "-bf", str(profile["bframes"]),
    ]


# ---------------------------------------------------------------------------
# Profilerkennung (Port von MediaScanner.cs)
# ---------------------------------------------------------------------------
@dataclass
class MediaInfo:
    path: str
    filename: str
    container: str
    width: int = 0
    height: int = 0
    duration_sec: Optional[float] = None
    bitrate_mbps: float = 0.0
    dv_profile: Optional[str] = None
    is_hdr10: bool = False
    action: str = "none"  # "dual_layer" | "reencode" | "relabel" | "none" | "unsupported"


def probe(path: str) -> MediaInfo:
    result = subprocess.run(
        [MEDIAINFO, "--Output=JSON", path],
        capture_output=True, text=True, check=True,
    )
    data = json.loads(result.stdout)
    tracks = data["media"]["track"]

    general = next((t for t in tracks if t["@type"] == "General"), {})
    video = next((t for t in tracks if t["@type"] == "Video"), None)

    mi = MediaInfo(
        path=path,
        filename=os.path.basename(path),
        container=os.path.splitext(path)[1].lstrip(".").upper(),
    )

    if video is None:
        mi.action = "unsupported"
        return mi

    mi.width = int(video.get("Width", 0) or 0)
    mi.height = int(video.get("Height", 0) or 0)
    try:
        mi.duration_sec = float(general.get("Duration", 0))
    except (TypeError, ValueError):
        mi.duration_sec = None

    try:
        size_bytes = float(general.get("FileSize", 0))
        if mi.duration_sec:
            mi.bitrate_mbps = (size_bytes * 8 / mi.duration_sec) / 1_000_000
    except (TypeError, ValueError):
        pass

    # WICHTIG: mediainfo legt je nach Version/Quelle die DV-Details entweder ALLES
    # kommagetrennt in "HDR_Format" (aeltere/andere Schreibweise, z.B. bei manchen
    # MakeMKV-Rips: "Dolby Vision, Version 1.0, Profile 7.6, dvhe.07.06, BL+EL+RPU, ...")
    # ODER in SEPARATEN Feldern mit "<DV-Wert> / <Fallback-Wert>"-Aufbau (beobachtet bei
    # DVDFab-MP4s: HDR_Format="Dolby Vision / SMPTE ST 2086", HDR_Format_Profile=
    # "dvhe.05 / ", HDR_Format_Settings="BL+RPU / ", HDR_Format_Compatibility=" / HDR10").
    # Beide Formen werden hier unterstuetzt, statt nur die zuerst getestete anzunehmen.
    hdr_format = video.get("HDR_Format", "") or ""
    hdr_profile_field = video.get("HDR_Format_Profile", "") or ""
    hdr_settings_field = video.get("HDR_Format_Settings", "") or ""
    compat_str = video.get("HDR_Format_Compatibility", "") or ""
    is_hdr10 = "HDR10" in hdr_format or "HDR10" in compat_str
    mi.is_hdr10 = is_hdr10

    dv_profile = None
    compat_id = None
    if "Dolby Vision" in hdr_format:
        # Form 1: alles kommagetrennt in einem String.
        for part in hdr_format.split(","):
            part = part.strip()
            if part.startswith("Profile "):
                dv_profile = part.replace("Profile ", "").split(".")[0]
            if part in ("BL+EL+RPU", "BL+RPU", "EL+RPU"):
                compat_id = part

        # Form 2: separate Felder, "<DV-Wert> / <Fallback-Wert>" - nur den Teil VOR
        # dem "/" nehmen (das ist der Dolby-Vision-eigene Wert, nicht der Fallback).
        if dv_profile is None:
            dv_part = hdr_profile_field.split("/")[0].strip()  # z.B. "dvhe.05"
            if dv_part.lower().startswith("dvhe."):
                try:
                    dv_profile = str(int(dv_part.split(".")[1]))  # "dvhe.05" -> "5"
                except (IndexError, ValueError):
                    dv_profile = None
        if compat_id is None:
            settings_part = hdr_settings_field.split("/")[0].strip()  # z.B. "BL+RPU"
            if settings_part in ("BL+EL+RPU", "BL+RPU", "EL+RPU"):
                compat_id = settings_part
    mi.dv_profile = dv_profile

    if dv_profile is not None:
        has_el = compat_id in ("BL+EL+RPU", "EL+RPU")
        # Profile 5/9 haben LAUT DV-SPEZIFIKATION nie eine echte nutzbare Base-Layer,
        # auch wenn manche mediainfo-Versionen bei ihnen trotzdem "BL+RPU" im Settings-
        # Feld zeigen (beobachtet, nicht nur angenommen - siehe obiges Beispiel: Profile
        # 5 mit "HDR_Format_Settings":"BL+RPU / "). Deshalb Profilnummer zuerst pruefen,
        # nicht blind dem Compat-String vertrauen - sonst wuerde eine Profile-5-Datei
        # faelschlich nur "relabelt" statt reencodiert, mit falschen Farben im Ergebnis.
        if has_el:
            mi.action = "dual_layer"            # verlustfrei, EL verwerfen
        elif dv_profile in ("5", "9"):
            mi.action = "reencode"              # nie eine echte Base-Layer, immer reencodieren
        elif dv_profile == "8" and not is_hdr10:
            mi.action = "relabel"               # BL vorhanden, aber nicht als 8.1 markiert
        elif dv_profile != "8":
            mi.action = "reencode"              # unbekanntes Profil ohne EL - sicherer Standard

    return mi


# ---------------------------------------------------------------------------
# Fix-Pipelines (Port von DoviConverter.cs)
# ---------------------------------------------------------------------------
def _run(cmd: list[str], log) -> None:
    log(f"$ {' '.join(cmd)}")
    # errors="replace" statt Standard-UTF-8-strict: manche Quelldateien haben
    # Metadaten in gemischter/fehlerhafter Kodierung (z.B. Titel mit Latin-1-
    # Resten) - ein einzelnes ungueltiges Byte im ffmpeg/dovi_tool-Output soll
    # nicht den ganzen Job mit UnicodeDecodeError abschiessen, nur diese eine
    # Log-Zeile zeigt dann ein Ersatzzeichen statt des Original-Bytes.
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                             text=True, encoding="utf-8", errors="replace")
    for line in proc.stdout:
        log(line.rstrip())
    proc.wait()
    if proc.returncode != 0:
        raise RuntimeError(f"Befehl fehlgeschlagen (Exit {proc.returncode}): {' '.join(cmd)}")


def _to_mkv_if_needed(src: str, tmp_dir: str, log) -> str:
    if src.lower().endswith(".mp4"):
        dst = os.path.join(tmp_dir, "src_remux.mkv")
        _run([FFMPEG, "-y", "-i", src, "-c", "copy", dst], log)
        return dst
    return src


def fix_dual_layer(src: str, out_path: str, log) -> None:
    """Profile 7/4/... - verlustfrei, EL verwerfen. Kein Encoder involviert."""
    with _temp_dir("revision_") as tmp:
        mkv_src = _to_mkv_if_needed(src, tmp, log)
        hevc_out = os.path.join(tmp, "video_p81.hevc")

        # ffmpeg (Annex-B-Extraktion) | dovi_tool -m 2 convert --discard - Pipe wie in der
        # Windows-App (ProcessRunner.RunPipedAsync-Aequivalent).
        p1 = subprocess.Popen(
            [FFMPEG, "-i", mkv_src, "-c:v", "copy", "-bsf:v", "hevc_mp4toannexb", "-f", "hevc", "-"],
            stdout=subprocess.PIPE,
        )
        p2 = subprocess.Popen(
            [DOVI_TOOL, "-m", "2", "convert", "--discard", "-", "-o", hevc_out],
            stdin=p1.stdout,
        )
        p1.stdout.close()
        p2.communicate()
        p1.wait()
        if p1.returncode != 0 or p2.returncode != 0:
            raise RuntimeError("Dual-Layer-Fix fehlgeschlagen (ffmpeg/dovi_tool).")

        _run([MKVMERGE, "-o", out_path, hevc_out, "--no-video", mkv_src], log)


def fix_relabel(src: str, out_path: str, log) -> None:
    """Single-Layer MIT Base-Layer, falsch markiert (z.B. 8.2/8.4) - verlustfrei,
    nur RPU-Kennung aendern, kein --discard (keine EL vorhanden)."""
    with _temp_dir("revision_") as tmp:
        mkv_src = _to_mkv_if_needed(src, tmp, log)
        hevc_out = os.path.join(tmp, "video_p81.hevc")

        p1 = subprocess.Popen(
            [FFMPEG, "-i", mkv_src, "-c:v", "copy", "-bsf:v", "hevc_mp4toannexb", "-f", "hevc", "-"],
            stdout=subprocess.PIPE,
        )
        p2 = subprocess.Popen(
            [DOVI_TOOL, "-m", "2", "convert", "-", "-o", hevc_out],
            stdin=p1.stdout,
        )
        p1.stdout.close()
        p2.communicate()
        p1.wait()
        if p1.returncode != 0 or p2.returncode != 0:
            raise RuntimeError("Relabel-Fix fehlgeschlagen (ffmpeg/dovi_tool).")

        _run([MKVMERGE, "-o", out_path, hevc_out, "--no-video", mkv_src], log)


def _cleanup(*paths: str) -> None:
    """Best-effort - loescht Zwischendateien, sobald sie nicht mehr gebraucht
    werden, statt bis zum Jobende alle gleichzeitig liegen zu lassen. Wichtig
    besonders wenn TEMP_ROOT im RAM (tmpfs) liegt - senkt den Spitzenbedarf
    z.B. bei einer 15GB-Rohdatei von ~40-50GB auf ~25-30GB pro Job."""
    for p in paths:
        try:
            os.remove(p)
        except OSError:
            pass


def fix_reencode(src: str, out_path: str, log, profile_key: str = "balanced") -> None:
    """Profile 5/9/... - keine nutzbare Base-Layer, MUSS per VAAPI reencodiert werden."""
    profile = QUALITY_PROFILES[profile_key]
    with _temp_dir("revision_") as tmp:
        mkv_src = _to_mkv_if_needed(src, tmp, log)

        raw_hevc = os.path.join(tmp, "orig.hevc")
        _run([FFMPEG, "-y", "-i", mkv_src, "-c:v", "copy", "-bsf:v", "hevc_mp4toannexb",
              "-f", "hevc", raw_hevc], log)

        rpu_p8 = os.path.join(tmp, "rpu_p8.bin")
        _run([DOVI_TOOL, "-m", "2", "extract-rpu", raw_hevc, "-o", rpu_p8], log)
        _cleanup(raw_hevc)  # nur fuer die RPU-Extraktion gebraucht, danach ueberfluessig

        new_hevc = os.path.join(tmp, "new_base.hevc")
        vaapi_args = build_vaapi_args(profile, profile["quality_fix"])
        _run([FFMPEG, "-y",
              "-hwaccel", "vaapi", "-hwaccel_device", VAAPI_DEVICE, "-hwaccel_output_format", "vaapi",
              "-i", mkv_src, "-map", "0:v:0",
              "-c:v", "hevc_vaapi", *vaapi_args,
              "-profile:v", "main10",
              "-color_primaries", "bt2020", "-color_trc", "smpte2084", "-colorspace", "bt2020nc",
              "-f", "hevc", new_hevc], log)

        injected = os.path.join(tmp, "injected.hevc")
        _run([DOVI_TOOL, "inject-rpu", "-i", new_hevc, "--rpu-in", rpu_p8, "-o", injected], log)
        _cleanup(new_hevc, rpu_p8)  # beide in injected.hevc "aufgegangen", nicht mehr gebraucht

        _run([MKVMERGE, "-o", out_path, injected, "--no-video", mkv_src], log)


def run_fix(mi: MediaInfo, out_path: str, log, profile_key: str = "balanced") -> None:
    if mi.action == "dual_layer":
        fix_dual_layer(mi.path, out_path, log)
    elif mi.action == "relabel":
        fix_relabel(mi.path, out_path, log)
    elif mi.action == "reencode":
        fix_reencode(mi.path, out_path, log, profile_key)
    else:
        raise RuntimeError(f"Kein Fix fuer Aktion '{mi.action}' definiert.")


def can_downsize(mi: MediaInfo, threshold_mbps: float) -> bool:
    """Port von MediaFile.CanDownsize: nur fuer bereits gesunde HDR10/Profile-8-
    Quellen mit hoher Bitrate - Profile 5/7/Relabel-Kandidaten zeigen den Button
    nicht (die brauchen zuerst den Fix, sonst wuerde eine kaputte DV-Struktur nur
    kleiner komprimiert statt repariert)."""
    healthy = mi.is_hdr10 or mi.dv_profile == "8"
    needs_fix = mi.action in ("dual_layer", "reencode", "relabel")
    return healthy and not needs_fix and mi.bitrate_mbps > threshold_mbps


def downsize(mi: MediaInfo, out_path: str, log, profile_key: str = "balanced") -> None:
    """Komprimiert eine bereits gesunde HDR10/Profile-8-Quelle nach - reine
    Bitraten-Reduktion per VAAPI, keine Profilkonvertierung. DV-RPU (falls
    vorhanden) wird unveraendert durchgereicht (dovi_tool inject-rpu), genau wie
    in Downsizer.cs der Windows-App."""
    profile = QUALITY_PROFILES[profile_key]
    with _temp_dir("revision_") as tmp:
        mkv_src = _to_mkv_if_needed(mi.path, tmp, log)
        vaapi_args = build_vaapi_args(profile, profile["quality_downsize"])
        new_hevc = os.path.join(tmp, "new_base.hevc")

        if mi.dv_profile == "8":
            # DV-RPU vorhanden - extrahieren, BL neu encodieren, RPU unveraendert
            # wieder injizieren (Farbmetadaten bleiben exakt erhalten).
            raw_hevc = os.path.join(tmp, "orig.hevc")
            _run([FFMPEG, "-y", "-i", mkv_src, "-c:v", "copy", "-bsf:v", "hevc_mp4toannexb",
                  "-f", "hevc", raw_hevc], log)
            rpu = os.path.join(tmp, "rpu.bin")
            _run([DOVI_TOOL, "-m", "2", "extract-rpu", raw_hevc, "-o", rpu], log)
            _cleanup(raw_hevc)

            _run([FFMPEG, "-y",
                  "-hwaccel", "vaapi", "-hwaccel_device", VAAPI_DEVICE, "-hwaccel_output_format", "vaapi",
                  "-i", mkv_src, "-map", "0:v:0",
                  "-c:v", "hevc_vaapi", *vaapi_args,
                  "-f", "hevc", new_hevc], log)

            injected = os.path.join(tmp, "injected.hevc")
            _run([DOVI_TOOL, "inject-rpu", "-i", new_hevc, "--rpu-in", rpu, "-o", injected], log)
            _cleanup(new_hevc, rpu)
            _run([MKVMERGE, "-o", out_path, injected, "--no-video", mkv_src], log)
        else:
            # Reines HDR10 ohne DV - keine RPU-Behandlung noetig, direkter Reencode.
            _run([FFMPEG, "-y",
                  "-hwaccel", "vaapi", "-hwaccel_device", VAAPI_DEVICE, "-hwaccel_output_format", "vaapi",
                  "-i", mkv_src, "-map", "0:v:0",
                  "-c:v", "hevc_vaapi", *vaapi_args,
                  "-f", "hevc", new_hevc], log)
            _run([MKVMERGE, "-o", out_path, new_hevc, "--no-video", mkv_src], log)
