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
# Qualitäts-Presets: kombinieren Rate-Control-Modus, Ziel-Bitrate, B-Frames und
# B-Frame-Pyramide (b_depth). Welche rc_modes hier auftauchen, ist NICHT geraten,
# sondern auf der Ziel-Hardware (Arrow Lake-S, iHD-Treiber) einzeln getestet:
#   CQP, CBR, VBR, ICQ, QVBR = unterstuetzt; AVBR = vom Treiber abgelehnt
#   ("Driver does not support AVBR RC mode") -> taucht hier bewusst nicht auf.
#   b_depth 3 ebenfalls getestet und akzeptiert.
#
# Die Modi im Ueberblick (ffmpeg-Doku):
#   CQP  - konstante Qualitaet, KEINE Bitraten-Garantie (szenenabhaengig, kann
#          bei "einfachem" Material weit unter den Erwartungen landen - genau
#          das Problem, das uns urspruenglich zum Wechsel auf VBR brachte)
#   VBR  - Ziel-Bitrate mit Spitzen-Begrenzung, vorhersehbare Dateigroesse
#   ICQ  - "intelligent constant quality": qualitaetsgesteuert, aber adaptiver
#          als CQP. Dateigroesse weiterhin inhaltsabhaengig.
#   QVBR - qualitaetsgesteuert MIT Bitraten-Deckel. Der Kompromiss aus beidem:
#          ruhige Szenen duerfen sparen, komplexe bekommen was sie brauchen,
#          aber die Obergrenze haelt.
#   CBR  - konstante Bitrate, fuer wirklich planbare Dateigroessen
#
# b_depth > 1 aktiviert hierarchische B-Frames (mehrere B-Ebenen, die sich
# gegenseitig referenzieren) - bessere Kompressionseffizienz bei gleicher
# Bitrate, laut ffmpeg-Doku.
# ---------------------------------------------------------------------------
QUALITY_PROFILES = {
    "qvbr_film": dict(
        name="Film – QVBR (empfohlen)", rc_mode="QVBR",
        target_mbps=30, quality=22, bframes=4, b_depth=3,
    ),
    "qvbr_serie": dict(
        name="Serie – QVBR (sparsamer)", rc_mode="QVBR",
        target_mbps=16, quality=24, bframes=4, b_depth=3,
    ),
    "icq_archiv": dict(
        name="Archiv – ICQ (Qualität vor Größe)", rc_mode="ICQ",
        target_mbps=None, quality=20, bframes=4, b_depth=3,
    ),
    "balanced": dict(
        name="Ausgewogen – VBR (bisheriges Verhalten)", rc_mode="VBR",
        target_mbps=20, quality=None, bframes=3, b_depth=1,
    ),
    "cbr_fix": dict(
        name="Feste Größe – CBR", rc_mode="CBR",
        target_mbps=20, quality=None, bframes=3, b_depth=1,
    ),
    "fast": dict(
        name="Schnell – CQP (Entwurf/Test)", rc_mode="CQP",
        target_mbps=None, quality=24, bframes=2, b_depth=1,
    ),
    # --- QSV/oneVPL (experimentell, siehe Kommentar unten) ---
    "qsv_max": dict(
        name="QSV – Maximale Qualität (experimentell)", encoder="qsv",
        preset="veryslow", rc_mode="ICQ", target_mbps=None, quality=20,
        bframes=4, lookahead=40,
    ),
    "qsv_film": dict(
        name="QSV – Film mit Bitraten-Deckel (experimentell)", encoder="qsv",
        preset="slow", rc_mode="QVBR", target_mbps=30, quality=22,
        bframes=4, lookahead=32,
    ),
}

DEFAULT_PROFILE = "qvbr_film"


def build_vaapi_args(profile: dict, bitrate_mbps: float | None = None,
                     quality: int | None = None, async_depth: int = 4) -> list[str]:
    """Baut die hevc_vaapi-Argumentliste passend zum rc_mode des Presets.

    Jeder Modus braucht andere Parameter - falsche Kombinationen laesst der
    Treiber entweder fallen oder lehnt sie ab, deshalb hier sauber getrennt:
      CQP       -> nur -qp
      ICQ       -> nur -global_quality (Bitrate waere wirkungslos)
      VBR/CBR   -> Bitrate (+ maxrate/bufsize bei VBR)
      QVBR      -> Bitrate UND -global_quality (Qualitaetsziel + Deckel)

    bitrate_mbps/quality ueberschreiben die Preset-Werte, wenn gesetzt - so
    wirkt der Bitraten-Regler der Oberflaeche weiterhin, ohne das Preset zu
    verlassen. async_depth erhoeht nur die Parallelitaet (Tempo), nicht die
    Qualitaet."""
    rc = profile["rc_mode"]
    bitrate = bitrate_mbps if bitrate_mbps else profile.get("target_mbps")
    q = quality if quality else profile.get("quality")

    args = ["-rc_mode", rc]

    if rc == "CQP":
        args += ["-qp", str(q or 24)]
    elif rc == "ICQ":
        args += ["-global_quality", str(q or 22)]
    elif rc in ("VBR", "QVBR", "CBR"):
        target_kbps = int((bitrate or 20) * 1000)
        args += ["-b:v", f"{target_kbps}k"]
        if rc == "VBR":
            args += ["-maxrate", f"{int(target_kbps * 1.5)}k",
                     "-bufsize", f"{int(target_kbps * 2)}k"]
        elif rc == "QVBR":
            # QVBR kombiniert Qualitaetsziel mit Bitraten-Deckel: maxrate ist
            # hier die eigentliche Obergrenze, -global_quality das Ziel.
            args += ["-maxrate", f"{int(target_kbps * 1.5)}k",
                     "-bufsize", f"{int(target_kbps * 2)}k",
                     "-global_quality", str(q or 22)]

    args += ["-bf", str(profile["bframes"]), "-b_depth", str(profile.get("b_depth", 1))]
    if async_depth:
        args += ["-async_depth", str(async_depth)]
    return args


def build_qsv_args(profile: dict, bitrate_mbps: float | None = None,
                   quality: int | None = None) -> list[str]:
    """Baut die hevc_qsv-Argumentliste. QSV/oneVPL kann zwei Dinge, die VAAPI
    NICHT bietet und die der eigentliche Grund sind, es ueberhaupt nochmal zu
    versuchen: echte Geschwindigkeits-Presets (veryslow..veryfast, also ein
    echter Tempo/Qualitaets-Tradeoff) und Lookahead. Lookahead laesst den
    Encoder kommende Frames vorausschauen und Bits vorausschauend verteilen -
    das fehlt dem VAAPI-Pfad komplett.

    extbrc ist an den Lookahead gekoppelt: laut ffmpeg-Doku wirkt
    look_ahead_depth ohne extbrc gar nicht (dieselbe Kopplung, die schon in der
    Windows-App und der frueheren QSV-Fassung dieser App drin war)."""
    bitrate = bitrate_mbps if bitrate_mbps else profile.get("target_mbps")
    q = quality if quality else profile.get("quality")
    rc = profile["rc_mode"]

    args = ["-preset", profile.get("preset", "medium")]

    if rc == "ICQ":
        args += ["-global_quality", str(q or 22)]
    elif rc == "QVBR":
        target_kbps = int((bitrate or 20) * 1000)
        args += ["-b:v", f"{target_kbps}k",
                 "-maxrate", f"{int(target_kbps * 1.5)}k",
                 "-global_quality", str(q or 22)]
    elif rc == "CQP":
        args += ["-q", str(q or 24)]
    else:  # VBR/CBR
        target_kbps = int((bitrate or 20) * 1000)
        args += ["-b:v", f"{target_kbps}k", "-maxrate", f"{int(target_kbps * 1.5)}k"]

    args += ["-bf", str(profile["bframes"])]

    la = profile.get("lookahead")
    if la:
        args += ["-look_ahead", "1", "-look_ahead_depth", str(la),
                 "-extbrc", "1"]
    return args


def build_encode_cmd(profile: dict, src: str, out_hevc: str,
                     bitrate_mbps: float | None = None,
                     extra_out_args: list[str] | None = None) -> list[str]:
    """Baut den kompletten ffmpeg-Encode-Befehl - inkl. der Encoder-Weiche
    zwischen VAAPI (Standard, bewaehrt) und QSV (experimentell).

    Der Unterschied steckt nicht nur im Codec-Namen: VAAPI und QSV brauchen
    unterschiedliche Hardware-Initialisierung VOR der Eingabedatei, deshalb
    wird der Befehl hier an einer Stelle gebaut statt an drei Stellen
    dupliziert."""
    if profile.get("encoder") == "qsv":
        pre = ["-hwaccel", "qsv", "-qsv_device", VAAPI_DEVICE,
               "-hwaccel_output_format", "qsv"]
        codec, enc_args = "hevc_qsv", build_qsv_args(profile, bitrate_mbps)
    else:
        pre = ["-hwaccel", "vaapi", "-hwaccel_device", VAAPI_DEVICE,
               "-hwaccel_output_format", "vaapi"]
        codec, enc_args = "hevc_vaapi", build_vaapi_args(profile, bitrate_mbps)

    return [FFMPEG, "-y", *pre, "-i", src, "-map", "0:v:0",
            "-c:v", codec, *enc_args, *(extra_out_args or []),
            "-f", "hevc", out_hevc]


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


def fix_dual_layer(src: str, out_path: str, log) -> None:
    """Profile 7/4/... - verlustfrei, EL verwerfen. Kein Encoder involviert.
    Liest direkt aus der Originaldatei (src), keine Zwischenkopie mehr noetig -
    mkvmerge kann Audiospuren auch direkt aus MP4 lesen, nicht nur aus MKV."""
    with _temp_dir("revision_") as tmp:
        hevc_out = os.path.join(tmp, "video_p81.hevc")

        # ffmpeg (Annex-B-Extraktion) | dovi_tool -m 2 convert --discard - Pipe wie in der
        # Windows-App (ProcessRunner.RunPipedAsync-Aequivalent).
        p1 = subprocess.Popen(
            [FFMPEG, "-i", src, "-map", "0:v:0", "-c:v", "copy", "-bsf:v", "hevc_mp4toannexb", "-f", "hevc", "-"],
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

        _run([MKVMERGE, "-o", out_path, hevc_out, "--no-video", src], log)


def fix_relabel(src: str, out_path: str, log) -> None:
    """Single-Layer MIT Base-Layer, falsch markiert (z.B. 8.2/8.4) - verlustfrei,
    nur RPU-Kennung aendern, kein --discard (keine EL vorhanden). Liest direkt
    aus der Originaldatei, keine Zwischenkopie mehr noetig."""
    with _temp_dir("revision_") as tmp:
        hevc_out = os.path.join(tmp, "video_p81.hevc")

        p1 = subprocess.Popen(
            [FFMPEG, "-i", src, "-map", "0:v:0", "-c:v", "copy", "-bsf:v", "hevc_mp4toannexb", "-f", "hevc", "-"],
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

        _run([MKVMERGE, "-o", out_path, hevc_out, "--no-video", src], log)


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


def fix_reencode(src: str, out_path: str, log, profile_key: str = "balanced",
                  target_bitrate_mbps: float | None = None) -> None:
    """Profile 5/9/... - keine nutzbare Base-Layer, MUSS per VAAPI reencodiert werden.
    Liest Video-Extraktion, Encode UND das finale Audio-Muxen alle direkt aus der
    Originaldatei (src) - KEINE komplette Zwischenkopie mehr (frueher ~15GB pro
    Job nur um am Ende die Audiospur rauszuziehen). Senkt den Speicherbedarf im
    Temp-Ordner deutlich, wichtig besonders bei RAM-basiertem Temp (tmpfs).
    target_bitrate_mbps ueberschreibt den Profil-Standardwert, wenn gesetzt -
    z.B. vom Bitrate-Regler in der Weboberflaeche."""
    profile = QUALITY_PROFILES.get(profile_key) or QUALITY_PROFILES[DEFAULT_PROFILE]
    bitrate = target_bitrate_mbps
    with _temp_dir("revision_") as tmp:
        raw_hevc = os.path.join(tmp, "orig.hevc")
        _run([FFMPEG, "-y", "-i", src, "-map", "0:v:0", "-c:v", "copy", "-bsf:v", "hevc_mp4toannexb",
              "-f", "hevc", raw_hevc], log)

        rpu_p8 = os.path.join(tmp, "rpu_p8.bin")
        _run([DOVI_TOOL, "-m", "2", "extract-rpu", raw_hevc, "-o", rpu_p8], log)
        _cleanup(raw_hevc)  # nur fuer die RPU-Extraktion gebraucht, danach ueberfluessig

        new_hevc = os.path.join(tmp, "new_base.hevc")
        _run(build_encode_cmd(profile, src, new_hevc, bitrate, extra_out_args=[
            "-profile:v", "main10",
            "-color_primaries", "bt2020", "-color_trc", "smpte2084", "-colorspace", "bt2020nc",
        ]), log)

        injected = os.path.join(tmp, "injected.hevc")
        _run([DOVI_TOOL, "inject-rpu", "-i", new_hevc, "--rpu-in", rpu_p8, "-o", injected], log)
        _cleanup(new_hevc, rpu_p8)  # beide in injected.hevc "aufgegangen", nicht mehr gebraucht

        _run([MKVMERGE, "-o", out_path, injected, "--no-video", src], log)


def run_fix(mi: MediaInfo, out_path: str, log, profile_key: str = "balanced",
            target_bitrate_mbps: float | None = None) -> None:
    if mi.action == "dual_layer":
        fix_dual_layer(mi.path, out_path, log)
    elif mi.action == "relabel":
        fix_relabel(mi.path, out_path, log)
    elif mi.action == "reencode":
        fix_reencode(mi.path, out_path, log, profile_key, target_bitrate_mbps)
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


def downsize(mi: MediaInfo, out_path: str, log, profile_key: str = "balanced",
             target_bitrate_mbps: float | None = None) -> None:
    """Komprimiert eine bereits gesunde HDR10/Profile-8-Quelle nach - reine
    Bitraten-Reduktion per VAAPI, keine Profilkonvertierung. DV-RPU (falls
    vorhanden) wird unveraendert durchgereicht (dovi_tool inject-rpu), genau wie
    in Downsizer.cs der Windows-App. Liest direkt aus der Originaldatei (mi.path),
    keine Zwischenkopie mehr noetig. target_bitrate_mbps ueberschreibt den
    Profil-Standardwert, wenn gesetzt."""
    profile = QUALITY_PROFILES.get(profile_key) or QUALITY_PROFILES[DEFAULT_PROFILE]
    bitrate = target_bitrate_mbps
    with _temp_dir("revision_") as tmp:
        src = mi.path
        new_hevc = os.path.join(tmp, "new_base.hevc")

        # Diagnose: welcher Zweig wird genommen - DV-Erhalt oder reiner HDR10-
        # Reencode ohne RPU? Bei mi.dv_profile != "8" geht die DV-RPU verloren,
        # das soll hier sichtbar sein statt still zu passieren.
        if mi.dv_profile == "8":
            log(f"Downsize: DV-Profil 8 erkannt (dv_profile={mi.dv_profile!r}) - RPU wird erhalten.")
        else:
            log(f"Downsize: KEIN DV-Profil 8 erkannt (dv_profile={mi.dv_profile!r}) - "
                "reiner HDR10-Reencode ohne RPU-Erhalt. Falls die Quelle eigentlich Dolby "
                "Vision hatte, ist das ein Erkennungsproblem, kein gewolltes Verhalten.")

        if mi.dv_profile == "8":
            # DV-RPU vorhanden - extrahieren, BL neu encodieren, RPU unveraendert
            # wieder injizieren (Farbmetadaten bleiben exakt erhalten).
            raw_hevc = os.path.join(tmp, "orig.hevc")
            _run([FFMPEG, "-y", "-i", src, "-map", "0:v:0", "-c:v", "copy", "-bsf:v", "hevc_mp4toannexb",
                  "-f", "hevc", raw_hevc], log)
            rpu = os.path.join(tmp, "rpu.bin")
            _run([DOVI_TOOL, "-m", "2", "extract-rpu", raw_hevc, "-o", rpu], log)
            _cleanup(raw_hevc)

            _run(build_encode_cmd(profile, src, new_hevc, bitrate), log)

            injected = os.path.join(tmp, "injected.hevc")
            _run([DOVI_TOOL, "inject-rpu", "-i", new_hevc, "--rpu-in", rpu, "-o", injected], log)
            _cleanup(new_hevc, rpu)
            _run([MKVMERGE, "-o", out_path, injected, "--no-video", src], log)
        else:
            # Reines HDR10 ohne DV - keine RPU-Behandlung noetig, direkter Reencode.
            _run(build_encode_cmd(profile, src, new_hevc, bitrate), log)
            _run([MKVMERGE, "-o", out_path, new_hevc, "--no-video", src], log)


def maybe_chain_downsize(mi: MediaInfo, out_path: str, log, profile_key: str, threshold_mbps: float,
                          target_bitrate_mbps: float | None = None, force: bool = False) -> None:
    """Nach einem VERLUSTFREIEN Fix (Dual-Layer/Relabel) automatisch nachkomprimieren,
    falls das Ergebnis immer noch ueber der Downsize-Schwelle liegt - analog zu
    MaybeChainDownsizeAsync in der Windows-App. Nur fuer die verlustfreien Aktionen:
    Profile 5/9 (Reencode-Fix) sind durch den Fix selbst schon angemessen klein,
    eine zusaetzliche Kompression waere dort eine unnoetige zweite Encoder-
    Generation (Qualitaetsverlust) ohne echten Nutzen. Ersetzt out_path direkt
    durch das kleinere Ergebnis, best-effort - ein Fehler hier laesst den
    urspruenglichen (verlustfreien) Fix unangetastet bestehen.

    force=True ueberspringt die Bitraten-Schwelle und reencodiert IMMER - fuer
    Faelle, wo nicht die Dateigroesse das Ziel ist, sondern ein frisch encodierter
    (statt des Original-Rip-)Bitstream gewuenscht ist. Hintergrund: beobachtet,
    dass manche Player (native LG-webOS-App) den unveraenderten Original-Bitstream
    nach einem reinen Dual-Layer-Discard nicht abspielen, waehrend ein komplett
    neu encodierter Stream (wie er bei Profile 5 zwangsläufig entsteht) dort
    problemlos lief - dieselbe Reencode-Pipeline jetzt auch fuer Profile 7 optional
    nutzbar, unabhaengig von der Dateigroesse."""
    if mi.action not in ("dual_layer", "relabel"):
        return

    try:
        probed = probe(out_path)
    except Exception as ex:  # noqa: BLE001
        log(f"Nachprüfung der Ergebnisgröße fehlgeschlagen: {ex}")
        return

    # Diagnose-Logging: zeigt genau, was probe() an der frisch gefixten Datei
    # erkannt hat, BEVOR downsize() sich darauf verlaesst - falls DV/Atmos nach
    # der Nachkompression fehlen sollten, zeigt das hier sofort, ob die Ursache
    # schon in der Erkennung liegt (dv_profile falsch/leer erkannt) oder erst
    # spaeter im downsize()-Schritt selbst.
    log(f"Nachkompressions-Vorprüfung: dv_profile={probed.dv_profile!r}, "
        f"is_hdr10={probed.is_hdr10}, bitrate={probed.bitrate_mbps:.1f} Mbit/s")

    if not force and probed.bitrate_mbps <= threshold_mbps:
        return

    reason = "manuell erzwungen (Reencode statt Original-Bitstream)" if force else \
        f"Ergebnis liegt bei {probed.bitrate_mbps:.1f} Mbit/s (Schwelle {threshold_mbps:.1f})"
    log(f"{reason} - verlustfreier Fix, zusätzlicher Reencode-Durchlauf.")

    downsized_path = out_path + ".downsized.mkv"
    try:
        downsize(probed, downsized_path, log, profile_key, target_bitrate_mbps)
        os.replace(downsized_path, out_path)
        log("Reencode-Durchlauf abgeschlossen.")
    except Exception as ex:  # noqa: BLE001
        log(f"Reencode-Durchlauf fehlgeschlagen (verlustfreies Fix-Ergebnis bleibt erhalten): {ex}")
        try:
            os.remove(downsized_path)
        except OSError:
            pass
