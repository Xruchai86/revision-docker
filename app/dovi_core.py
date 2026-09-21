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

# Separates ffmpeg NUR zum Messen (VMAF). Das System-ffmpeg aus den
# Ubuntu-Quellen ist ohne --enable-libvmaf gebaut (im Container geprueft: nur
# "vmafmotion" vorhanden, das ist die Bewegungskomponente, NICHT der VMAF-Wert).
# Fehlt das Binary, bleibt die Kalibrierung einfach deaktiviert - Encoden
# laeuft unveraendert ueber FFMPEG weiter.
FFMPEG_VMAF = shutil.which("ffmpeg-vmaf")

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
# Kategorien steuern, welche Profile in der Oberflaeche angeboten werden.
# Hintergrund Anime: grosse einfarbige Flaechen, harte Kanten, kein Filmkorn.
# Das komprimiert deutlich besser als Realfilm - dieselbe wahrgenommene
# Qualitaet wird mit spuerbar weniger Bitrate erreicht. Umgekehrt fallen dort
# Blockartefakte in Farbverlaeufen staerker auf, weshalb die Anime-Presets
# einen NIEDRIGEREN (= besseren) Qualitaetswert mit niedrigerer Bitrate
# kombinieren statt einfach nur die Bitrate zu senken.
CATEGORIES = {
    "film": "Realfilm",
    "serie": "Serie (Realfilm)",
    "animation_film": "Animationsfilm (3D/CGI)",
    "animation_serie": "Animationsserie (3D/CGI)",
    "anime_film": "Anime-Film (2D)",
    "anime_serie": "Anime-Serie (2D)",
}

# Stufen ("tier"): Die VMAF-Kalibrierung misst je Kategorie EINMAL und legt
# danach fuer jede Stufe den passenden Qualitaetswert ab - "sparsam" zielt auf
# VMAF 90, "empfohlen" auf 93, "max" auf 95. So muessen die drei Stufen nicht
# geraten werden, sondern ergeben sich aus derselben Messung.
#
# SDR-Varianten: SDR kommt bei gleicher wahrgenommener Qualitaet mit weniger
# Bits aus als HDR (kleinerer Dynamikumfang, weniger Farbtiefe). Die Zielwerte
# liegen deshalb rund ein Viertel niedriger. WICHTIG ist aber vor allem, dass
# SDR-Material NICHT mit BT.2020/PQ gekennzeichnet wird - das passiert nur im
# DV-Reencode-Pfad, der fuer SDR gar nicht erst greift.
QUALITY_PROFILES = {
    "qsv_film": dict(
        name="Realfilm – empfohlen", categories=["film"], tier="empfohlen",
        encoder="qsv", preset="slow", rc_mode="QVBR",
        target_mbps=30, quality=22, bframes=4, lookahead=32,
    ),
    "qsv_film_max": dict(
        name="Realfilm – maximale Qualität", categories=["film"], tier="max",
        encoder="qsv", preset="veryslow", rc_mode="QVBR",
        target_mbps=40, quality=19, bframes=4, lookahead=40,
    ),
    "qsv_film_save": dict(
        name="Realfilm – sparsam", categories=["film"], tier="sparsam",
        encoder="qsv", preset="medium", rc_mode="QVBR",
        target_mbps=21, quality=24, bframes=4, lookahead=24,
    ),
    "qsv_film_sdr": dict(
        name="Realfilm · SDR – empfohlen", categories=["film"], tier="empfohlen", sdr=True,
        encoder="qsv", preset="slow", rc_mode="QVBR",
        target_mbps=22, quality=22, bframes=4, lookahead=32,
    ),
    "qsv_film_sdr_max": dict(
        name="Realfilm · SDR – maximale Qualität", categories=["film"], tier="max", sdr=True,
        encoder="qsv", preset="veryslow", rc_mode="QVBR",
        target_mbps=30, quality=19, bframes=4, lookahead=40,
    ),
    "qsv_film_sdr_save": dict(
        name="Realfilm · SDR – sparsam", categories=["film"], tier="sparsam", sdr=True,
        encoder="qsv", preset="medium", rc_mode="QVBR",
        target_mbps=16, quality=24, bframes=4, lookahead=24,
    ),
    "qsv_serie": dict(
        name="Serie – empfohlen", categories=["serie"], tier="empfohlen",
        encoder="qsv", preset="medium", rc_mode="QVBR",
        target_mbps=16, quality=23, bframes=4, lookahead=32,
    ),
    "qsv_serie_max": dict(
        name="Serie – maximale Qualität", categories=["serie"], tier="max",
        encoder="qsv", preset="veryslow", rc_mode="QVBR",
        target_mbps=22, quality=20, bframes=4, lookahead=40,
    ),
    "qsv_serie_save": dict(
        name="Serie – sparsam", categories=["serie"], tier="sparsam",
        encoder="qsv", preset="medium", rc_mode="QVBR",
        target_mbps=11, quality=25, bframes=4, lookahead=24,
    ),
    "qsv_serie_sdr": dict(
        name="Serie · SDR – empfohlen", categories=["serie"], tier="empfohlen", sdr=True,
        encoder="qsv", preset="medium", rc_mode="QVBR",
        target_mbps=12, quality=23, bframes=4, lookahead=32,
    ),
    "qsv_serie_sdr_max": dict(
        name="Serie · SDR – maximale Qualität", categories=["serie"], tier="max", sdr=True,
        encoder="qsv", preset="veryslow", rc_mode="QVBR",
        target_mbps=16, quality=20, bframes=4, lookahead=40,
    ),
    "qsv_serie_sdr_save": dict(
        name="Serie · SDR – sparsam", categories=["serie"], tier="sparsam", sdr=True,
        encoder="qsv", preset="medium", rc_mode="QVBR",
        target_mbps=8, quality=25, bframes=4, lookahead=24,
    ),
    "qsv_animation_film": dict(
        name="Animationsfilm – empfohlen", categories=["animation_film"], tier="empfohlen",
        encoder="qsv", preset="slow", rc_mode="QVBR",
        target_mbps=22, quality=20, bframes=4, lookahead=32,
    ),
    "qsv_animation_film_max": dict(
        name="Animationsfilm – maximale Qualität", categories=["animation_film"], tier="max",
        encoder="qsv", preset="veryslow", rc_mode="QVBR",
        target_mbps=30, quality=17, bframes=4, lookahead=40,
    ),
    "qsv_animation_film_save": dict(
        name="Animationsfilm – sparsam", categories=["animation_film"], tier="sparsam",
        encoder="qsv", preset="medium", rc_mode="QVBR",
        target_mbps=15, quality=22, bframes=4, lookahead=24,
    ),
    "qsv_animation_film_sdr": dict(
        name="Animationsfilm · SDR – empfohlen", categories=["animation_film"], tier="empfohlen", sdr=True,
        encoder="qsv", preset="slow", rc_mode="QVBR",
        target_mbps=16, quality=20, bframes=4, lookahead=32,
    ),
    "qsv_animation_film_sdr_max": dict(
        name="Animationsfilm · SDR – maximale Qualität", categories=["animation_film"], tier="max", sdr=True,
        encoder="qsv", preset="veryslow", rc_mode="QVBR",
        target_mbps=22, quality=17, bframes=4, lookahead=40,
    ),
    "qsv_animation_film_sdr_save": dict(
        name="Animationsfilm · SDR – sparsam", categories=["animation_film"], tier="sparsam", sdr=True,
        encoder="qsv", preset="medium", rc_mode="QVBR",
        target_mbps=12, quality=22, bframes=4, lookahead=24,
    ),
    "qsv_animation_serie": dict(
        name="Animationsserie – empfohlen", categories=["animation_serie"], tier="empfohlen",
        encoder="qsv", preset="medium", rc_mode="QVBR",
        target_mbps=13, quality=21, bframes=4, lookahead=32,
    ),
    "qsv_animation_serie_max": dict(
        name="Animationsserie – maximale Qualität", categories=["animation_serie"], tier="max",
        encoder="qsv", preset="veryslow", rc_mode="QVBR",
        target_mbps=18, quality=18, bframes=4, lookahead=40,
    ),
    "qsv_animation_serie_save": dict(
        name="Animationsserie – sparsam", categories=["animation_serie"], tier="sparsam",
        encoder="qsv", preset="medium", rc_mode="QVBR",
        target_mbps=9, quality=23, bframes=4, lookahead=24,
    ),
    "qsv_animation_serie_sdr": dict(
        name="Animationsserie · SDR – empfohlen", categories=["animation_serie"], tier="empfohlen", sdr=True,
        encoder="qsv", preset="medium", rc_mode="QVBR",
        target_mbps=10, quality=21, bframes=4, lookahead=32,
    ),
    "qsv_animation_serie_sdr_max": dict(
        name="Animationsserie · SDR – maximale Qualität", categories=["animation_serie"], tier="max", sdr=True,
        encoder="qsv", preset="veryslow", rc_mode="QVBR",
        target_mbps=13, quality=18, bframes=4, lookahead=40,
    ),
    "qsv_animation_serie_sdr_save": dict(
        name="Animationsserie · SDR – sparsam", categories=["animation_serie"], tier="sparsam", sdr=True,
        encoder="qsv", preset="medium", rc_mode="QVBR",
        target_mbps=7, quality=23, bframes=4, lookahead=24,
    ),
    "qsv_anime_film": dict(
        name="Anime-Film – empfohlen", categories=["anime_film"], tier="empfohlen",
        encoder="qsv", preset="slow", rc_mode="QVBR",
        target_mbps=18, quality=19, bframes=4, lookahead=32,
    ),
    "qsv_anime_film_max": dict(
        name="Anime-Film – maximale Qualität", categories=["anime_film"], tier="max",
        encoder="qsv", preset="veryslow", rc_mode="QVBR",
        target_mbps=24, quality=16, bframes=4, lookahead=40,
    ),
    "qsv_anime_film_save": dict(
        name="Anime-Film – sparsam", categories=["anime_film"], tier="sparsam",
        encoder="qsv", preset="medium", rc_mode="QVBR",
        target_mbps=13, quality=21, bframes=4, lookahead=24,
    ),
    "qsv_anime_film_sdr": dict(
        name="Anime-Film · SDR – empfohlen", categories=["anime_film"], tier="empfohlen", sdr=True,
        encoder="qsv", preset="slow", rc_mode="QVBR",
        target_mbps=14, quality=19, bframes=4, lookahead=32,
    ),
    "qsv_anime_film_sdr_max": dict(
        name="Anime-Film · SDR – maximale Qualität", categories=["anime_film"], tier="max", sdr=True,
        encoder="qsv", preset="veryslow", rc_mode="QVBR",
        target_mbps=18, quality=16, bframes=4, lookahead=40,
    ),
    "qsv_anime_film_sdr_save": dict(
        name="Anime-Film · SDR – sparsam", categories=["anime_film"], tier="sparsam", sdr=True,
        encoder="qsv", preset="medium", rc_mode="QVBR",
        target_mbps=9, quality=21, bframes=4, lookahead=24,
    ),
    "qsv_anime_serie": dict(
        name="Anime-Serie – empfohlen", categories=["anime_serie"], tier="empfohlen",
        encoder="qsv", preset="medium", rc_mode="QVBR",
        target_mbps=10, quality=20, bframes=4, lookahead=32,
    ),
    "qsv_anime_serie_max": dict(
        name="Anime-Serie – maximale Qualität", categories=["anime_serie"], tier="max",
        encoder="qsv", preset="veryslow", rc_mode="QVBR",
        target_mbps=14, quality=17, bframes=4, lookahead=40,
    ),
    "qsv_anime_serie_save": dict(
        name="Anime-Serie – sparsam", categories=["anime_serie"], tier="sparsam",
        encoder="qsv", preset="medium", rc_mode="QVBR",
        target_mbps=7, quality=22, bframes=4, lookahead=24,
    ),
    "qsv_anime_serie_sdr": dict(
        name="Anime-Serie · SDR – empfohlen", categories=["anime_serie"], tier="empfohlen", sdr=True,
        encoder="qsv", preset="medium", rc_mode="QVBR",
        target_mbps=8, quality=20, bframes=4, lookahead=32,
    ),
    "qsv_anime_serie_sdr_max": dict(
        name="Anime-Serie · SDR – maximale Qualität", categories=["anime_serie"], tier="max", sdr=True,
        encoder="qsv", preset="veryslow", rc_mode="QVBR",
        target_mbps=10, quality=17, bframes=4, lookahead=40,
    ),
    "qsv_anime_serie_sdr_save": dict(
        name="Anime-Serie · SDR – sparsam", categories=["anime_serie"], tier="sparsam", sdr=True,
        encoder="qsv", preset="medium", rc_mode="QVBR",
        target_mbps=5, quality=22, bframes=4, lookahead=24,
    ),
    # In jeder Kategorie verfuegbar: Entwurf und der VAAPI-Rueckfallweg.
    "qsv_fast": dict(
        name="Schnell – QSV (Entwurf/Test)", categories=list(CATEGORIES), tier="entwurf",
        encoder="qsv", preset="veryfast", rc_mode="VBR",
        target_mbps=12, quality=None, bframes=2, lookahead=None,
    ),
    "qvbr_film": dict(
        name="VAAPI-Fallback – QVBR", categories=list(CATEGORIES), tier="empfohlen",
        rc_mode="QVBR", target_mbps=30, quality=22, bframes=4, b_depth=3,
    ),
    "icq_archiv": dict(
        name="VAAPI-Fallback – ICQ (ohne Deckel)", categories=list(CATEGORIES), tier="max",
        rc_mode="ICQ", target_mbps=None, quality=20, bframes=4, b_depth=3,
    ),
}

# Ziel-VMAF je Stufe. Grundlage: 93-95 gilt als Transparenzbereich (darueber
# zahlt man Bits fuer Unterschiede, die niemand mehr sieht), 90 ist die
# bewusst sparsame Stufe.
TIER_VMAF_TARGETS = {"sparsam": 90.0, "empfohlen": 93.0, "max": 95.0}

DEFAULT_PROFILE = "qsv_film"


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
        # Diese vier Optionen haengen zusammen und werden bewusst NUR gemeinsam
        # mit Lookahead gesetzt - dieselbe Kopplung, die schon in der Windows-App
        # recherchiert wurde: look_ahead_depth wirkt laut ffmpeg-Doku ohne extbrc
        # gar nicht. adaptive_i/adaptive_b lassen den Encoder I- und B-Frames
        # szenenabhaengig platzieren statt starr, b_strategy erlaubt ihm, die
        # B-Frame-Anzahl selbst zu waehlen. Alles Dinge, die erst mit
        # Vorausschau sinnvoll sind.
        args += ["-look_ahead", "1", "-look_ahead_depth", str(la),
                 "-extbrc", "1",
                 "-adaptive_i", "1", "-adaptive_b", "1", "-b_strategy", "1"]
    return args


def build_encode_cmd(profile: dict, src: str, out_hevc: str,
                     bitrate_mbps: float | None = None,
                     extra_out_args: list[str] | None = None,
                     quality: int | None = None) -> list[str]:
    """Baut den kompletten ffmpeg-Encode-Befehl - inkl. der Encoder-Weiche
    zwischen QSV (bevorzugt) und VAAPI (Fallback).

    Der Unterschied steckt nicht nur im Codec-Namen: VAAPI und QSV brauchen
    unterschiedliche Hardware-Initialisierung VOR der Eingabedatei, deshalb
    wird der Befehl hier an einer Stelle gebaut statt an drei Stellen
    dupliziert.

    quality ueberschreibt den Qualitaetswert des Presets (global_quality/qp).
    NIEDRIGER = mehr Bits, mehr Details. Bei QVBR/ICQ ist das der eigentliche
    Steuerwert; die Ziel-Bitrate wirkt dort nur als Obergrenze."""
    if profile.get("encoder") == "qsv":
        pre = ["-hwaccel", "qsv", "-qsv_device", VAAPI_DEVICE,
               "-hwaccel_output_format", "qsv"]
        codec, enc_args = "hevc_qsv", build_qsv_args(profile, bitrate_mbps, quality)
    else:
        pre = ["-hwaccel", "vaapi", "-hwaccel_device", VAAPI_DEVICE,
               "-hwaccel_output_format", "vaapi"]
        codec, enc_args = "hevc_vaapi", build_vaapi_args(profile, bitrate_mbps, quality)

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
    is_sdr: bool = False
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
    # SDR = weder eine HDR-Kennung noch Dolby Vision. Wichtig fuer alles
    # Weitere: SDR-Material darf NICHT mit BT.2020/PQ gekennzeichnet werden,
    # das wuerde die Farben zerstoeren.
    mi.is_sdr = not hdr_format.strip() and not compat_str.strip()

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


def _extract_rpu_piped(src: str, rpu_out: str, log, mode: str = "2") -> None:
    """Extrahiert die DV-RPU direkt aus der Quelle, per Pipe statt ueber eine
    riesige Zwischendatei.

    Vorher wurde die rohe HEVC-Spur erst komplett als Datei geschrieben (bei
    4K-Material ~15 GB) und danach von dovi_tool wieder eingelesen - ein
    kompletter Schreib- UND Lesedurchgang ueber die Platte, nur um an ein paar
    Megabyte RPU zu kommen. Der Dual-Layer-Fix nutzte an derselben Stelle laengst
    eine Pipe; hier war es historisch anders und wurde nie angeglichen.

    Spart pro Job den Platz und die I/O-Zeit - besonders relevant bei
    RAM-basiertem Temp (tmpfs), wo die Zwischendatei echten Arbeitsspeicher
    belegt hat."""
    log(f"$ {FFMPEG} -i {src} ... | {DOVI_TOOL} -m {mode} extract-rpu - -o {rpu_out}")
    p1 = subprocess.Popen(
        [FFMPEG, "-v", "error", "-i", src, "-map", "0:v:0", "-c:v", "copy",
         "-bsf:v", "hevc_mp4toannexb", "-f", "hevc", "-"],
        stdout=subprocess.PIPE,
    )
    p2 = subprocess.Popen(
        [DOVI_TOOL, "-m", mode, "extract-rpu", "-", "-o", rpu_out],
        stdin=p1.stdout,
    )
    p1.stdout.close()
    p2.communicate()
    p1.wait()
    if p2.returncode != 0:
        raise RuntimeError("RPU-Extraktion fehlgeschlagen (dovi_tool).")
    # p1 darf mit SIGPIPE enden, wenn dovi_tool frueher fertig ist - das ist
    # kein Fehler, deshalb wird nur p2 streng geprueft.


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
                  target_bitrate_mbps: float | None = None,
                  quality: int | None = None) -> None:
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
        rpu_p8 = os.path.join(tmp, "rpu_p8.bin")
        _extract_rpu_piped(src, rpu_p8, log)

        new_hevc = os.path.join(tmp, "new_base.hevc")
        _run(build_encode_cmd(profile, src, new_hevc, bitrate, quality=quality, extra_out_args=[
            "-profile:v", "main10",
            "-color_primaries", "bt2020", "-color_trc", "smpte2084", "-colorspace", "bt2020nc",
        ]), log)

        injected = os.path.join(tmp, "injected.hevc")
        _run([DOVI_TOOL, "inject-rpu", "-i", new_hevc, "--rpu-in", rpu_p8, "-o", injected], log)
        _cleanup(new_hevc, rpu_p8)  # beide in injected.hevc "aufgegangen", nicht mehr gebraucht

        _run([MKVMERGE, "-o", out_path, injected, "--no-video", src], log)


def run_fix(mi: MediaInfo, out_path: str, log, profile_key: str = "balanced",
            target_bitrate_mbps: float | None = None,
            quality: int | None = None) -> None:
    if mi.action == "dual_layer":
        fix_dual_layer(mi.path, out_path, log)
    elif mi.action == "relabel":
        fix_relabel(mi.path, out_path, log)
    elif mi.action == "reencode":
        fix_reencode(mi.path, out_path, log, profile_key, target_bitrate_mbps, quality)
    else:
        raise RuntimeError(f"Kein Fix fuer Aktion '{mi.action}' definiert.")


def can_downsize(mi: MediaInfo, threshold_mbps: float) -> bool:
    """Port von MediaFile.CanDownsize: nur fuer bereits gesunde HDR10/Profile-8-
    Quellen mit hoher Bitrate - Profile 5/7/Relabel-Kandidaten zeigen den Button
    nicht (die brauchen zuerst den Fix, sonst wuerde eine kaputte DV-Struktur nur
    kleiner komprimiert statt repariert)."""
    # SDR zaehlt ausdruecklich als "gesund": Es gibt dort keine DV-Struktur, die
    # kaputt sein koennte - solche Dateien haben schlicht kein HDR und sind
    # trotzdem legitime Downsize-Kandidaten. Frueher fielen sie komplett aus dem
    # Scan, weil hier nur HDR10/Profile 8 als gesund galt.
    healthy = mi.is_hdr10 or mi.dv_profile == "8" or mi.is_sdr
    needs_fix = mi.action in ("dual_layer", "reencode", "relabel")
    return healthy and not needs_fix and mi.bitrate_mbps > threshold_mbps


def downsize(mi: MediaInfo, out_path: str, log, profile_key: str = "balanced",
             target_bitrate_mbps: float | None = None,
             quality: int | None = None) -> None:
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
            rpu = os.path.join(tmp, "rpu.bin")
            _extract_rpu_piped(src, rpu, log)

            _run(build_encode_cmd(profile, src, new_hevc, bitrate, quality=quality), log)

            injected = os.path.join(tmp, "injected.hevc")
            _run([DOVI_TOOL, "inject-rpu", "-i", new_hevc, "--rpu-in", rpu, "-o", injected], log)
            _cleanup(new_hevc, rpu)
            _run([MKVMERGE, "-o", out_path, injected, "--no-video", src], log)
        else:
            # Reines HDR10 ohne DV - keine RPU-Behandlung noetig, direkter Reencode.
            _run(build_encode_cmd(profile, src, new_hevc, bitrate, quality=quality), log)
            _run([MKVMERGE, "-o", out_path, new_hevc, "--no-video", src], log)


def maybe_chain_downsize(mi: MediaInfo, out_path: str, log, profile_key: str, threshold_mbps: float,
                          target_bitrate_mbps: float | None = None, force: bool = False,
                          quality: int | None = None) -> None:
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
        downsize(probed, downsized_path, log, profile_key, target_bitrate_mbps, quality)
        os.replace(downsized_path, out_path)
        log("Reencode-Durchlauf abgeschlossen.")
    except Exception as ex:  # noqa: BLE001
        log(f"Reencode-Durchlauf fehlgeschlagen (verlustfreies Fix-Ergebnis bleibt erhalten): {ex}")
        try:
            os.remove(downsized_path)
        except OSError:
            pass


# ---------------------------------------------------------------------------
# VMAF-Kalibrierung
#
# Hintergrund: Die Qualitaetswerte in den Presets sind Erfahrungswerte, keine
# Messwerte. Welcher Wert fuer DIESES Material auf DIESER Hardware der richtige
# ist, laesst sich nicht herleiten - nur messen. Die Forschung ist sich beim
# Ziel einig: VMAF 93-95 gilt als Transparenzbereich (darueber zahlt man Bits
# fuer Qualitaet, die niemand mehr unterscheiden kann).
#
# Ablauf: kurzer Ausschnitt aus der Mitte der Datei, mit mehreren
# Qualitaetswerten encodieren, jeweils gegen das Original messen. Das Ergebnis
# ist eine Tabelle "Qualitaetswert -> VMAF + hochgerechnete Dateigroesse".
# ---------------------------------------------------------------------------

def vmaf_available() -> bool:
    return FFMPEG_VMAF is not None


def _vmaf_model_for(height: int) -> str:
    """VMAF bringt eigene Modelle fuer unterschiedliche Sichtbedingungen mit.
    Fuer 4K-Material ist das 4K-Modell das passende - das Standardmodell ist auf
    1080p trainiert und wuerde bei 2160p systematisch danebenliegen."""
    return "vmaf_4k_v0.6.1" if height >= 1600 else "vmaf_v0.6.1"


def _percentile(values: list[float], pct: float) -> float:
    """Einfaches Perzentil ohne numpy-Abhaengigkeit (lineare Interpolation)."""
    if not values:
        return 0.0
    v = sorted(values)
    k = (len(v) - 1) * pct / 100.0
    lo, hi = int(k), min(int(k) + 1, len(v) - 1)
    return v[lo] + (v[hi] - v[lo]) * (k - lo)


def measure_vmaf(reference: str, distorted: str, log, height: int = 2160,
                 ref_start: float = 0.0, ref_duration: float | None = None,
                 subsample: int = 3) -> dict:
    """Misst einen encodierten Ausschnitt gegen das Original.

    Liefert nicht nur den Mittelwert: Der Durchschnitt versteckt kurze, starke
    Einbrueche - ein Encode, der fast durchgehend gut aussieht und eine Sekunde
    lang zerfaellt, kann einen hohen Mittelwert haben und trotzdem einen
    sichtbaren Fehler enthalten. Deshalb zusaetzlich harmonischer Mittelwert,
    Minimum und das 5. Perzentil (die schlechtesten 5 % der Frames), berechnet
    aus den Einzelwerten im JSON-Log.

    Dazu CAMBI, Netflix' Banding-Metrik aus libvmaf (auf 10 Bit ausgelegt):
    VMAF erfasst Banding - Streifen in weichen Verlaeufen - nur schlecht, und
    genau das ist bei HDR und 3D-Animation das Hauptproblem. 0 = kein Banding,
    hoeher = mehr. Faellt CAMBI aus (aeltere libvmaf), laeuft die Messung ohne
    weiter.

    subsample: nur jeden n-ten Frame bewerten. Gemessen lag der Mittelwert mit
    jedem 5. Frame praktisch gleich (93,1308 statt 93,1300) bei einem Viertel
    der Zeit. 3 ist ein vorsichtiger Wert, der fuer die Perzentile genug Frames
    uebrig laesst."""
    if not FFMPEG_VMAF:
        raise RuntimeError("Kein VMAF-faehiges ffmpeg vorhanden.")

    model = _vmaf_model_for(height)
    ref_args = ["-ss", str(ref_start)]
    if ref_duration:
        ref_args += ["-t", str(ref_duration)]

    def _run_vmaf(with_cambi: bool) -> dict | None:
        with tempfile.TemporaryDirectory() as td:
            log_path = os.path.join(td, "vmaf.json")
            feats = ":feature=name=cambi" if with_cambi else ""
            cmd = [FFMPEG_VMAF, "-v", "error",
                   "-i", distorted,
                   *ref_args, "-i", reference,
                   "-lavfi",
                   f"[0:v]setpts=PTS-STARTPTS[dist];"
                   f"[1:v]setpts=PTS-STARTPTS[ref];"
                   f"[dist][ref]libvmaf=model=version={model}{feats}:"
                   f"n_subsample={subsample}:"
                   f"log_fmt=json:log_path={log_path}:n_threads=4",
                   "-f", "null", "-"]
            log("$ " + " ".join(cmd))
            proc = subprocess.run(cmd, capture_output=True, text=True, errors="replace")
            if proc.returncode != 0:
                if with_cambi:
                    log("CAMBI nicht verfuegbar - messe ohne Banding-Metrik weiter.")
                    return None
                raise RuntimeError(f"VMAF-Messung fehlgeschlagen: {proc.stderr.strip()[:400]}")
            # ffmpeg kann mit Rueckgabewert 0 enden, OHNE dass libvmaf ein
            # Ergebnis schreibt - etwa wenn einer der Eingaenge keine Frames
            # liefert. Dann klar melden statt "No such file or directory".
            if not os.path.exists(log_path) or os.path.getsize(log_path) == 0:
                raise RuntimeError(
                    "VMAF lieferte kein Ergebnis - vermutlich bekam einer der "
                    "Eingaenge keine Frames (Zeitbereich ausserhalb der Datei?). "
                    f"ffmpeg meldete: {proc.stderr.strip()[:300] or 'nichts'}")
            with open(log_path, "r", encoding="utf-8") as f:
                return json.load(f)

    data = _run_vmaf(with_cambi=True) or _run_vmaf(with_cambi=False)

    frames = data.get("frames") or []
    per_frame = [float(fr["metrics"]["vmaf"]) for fr in frames
                 if "metrics" in fr and "vmaf" in fr["metrics"]]
    cambi_vals = [float(fr["metrics"]["cambi"]) for fr in frames
                  if "metrics" in fr and "cambi" in fr["metrics"]]
    pooled = (data.get("pooled_metrics") or {}).get("vmaf") or {}

    return {
        "mean": float(pooled.get("mean", sum(per_frame) / max(len(per_frame), 1))),
        "harmonic_mean": float(pooled.get("harmonic_mean", 0.0)),
        "min": float(pooled.get("min", min(per_frame) if per_frame else 0.0)),
        "p5": _percentile(per_frame, 5),
        "cambi": (sum(cambi_vals) / len(cambi_vals)) if cambi_vals else None,
        "cambi_max": max(cambi_vals) if cambi_vals else None,
        "frames": per_frame,
    }


def calibrate_quality(src: str, profile_key: str, quality_values: list[int], log,
                      sample_seconds: int = 120) -> list[dict]:
    """Encodiert Ausschnitte mit mehreren Qualitaetswerten und misst jeden.

    Statt EINES Ausschnitts aus der Mitte werden jetzt drei Stellen gemessen
    (25 %, 50 %, 75 % der Laufzeit), zusammen so lang wie frueher der eine.
    Ein einzelner Ausschnitt trifft zufaellig eine ruhige Dialogszene oder eine
    Actionsequenz und verzerrt damit das Ergebnis in eine Richtung. Die
    Frame-Werte aller drei Stellen werden gemeinsam ausgewertet.

    Anfang und Ende bleiben weiter aussen vor - dort sind oft Schwarzbild,
    Logos oder Abspann, die untypisch leicht zu komprimieren sind."""
    if not FFMPEG_VMAF:
        raise RuntimeError("Kein VMAF-faehiges ffmpeg vorhanden - Kalibrierung nicht moeglich.")

    profile = QUALITY_PROFILES.get(profile_key) or QUALITY_PROFILES[DEFAULT_PROFILE]
    mi = probe(src)
    duration = mi.duration_sec or 0.0

    # Drei Messstellen, zusammen sample_seconds lang
    seg_len = max(10, sample_seconds // 3)
    if duration > seg_len * 4:
        starts = [max(0.0, duration * f - seg_len / 2) for f in (0.25, 0.50, 0.75)]
    else:
        starts = [0.0]              # sehr kurze Datei: eine Stelle genuegt
        seg_len = int(min(sample_seconds, duration)) or sample_seconds

    # Ohne Bitraten-Deckel messen (ICQ): Bei QVBR begrenzt der Deckel das
    # Ergebnis, sobald der Qualitaetswert mehr Bits verlangt als erlaubt - dann
    # misst man den Deckel statt den Qualitaetswert.
    measure_profile = dict(profile)
    measure_profile["rc_mode"] = "ICQ"
    measure_profile["target_mbps"] = None

    results = []
    with _temp_dir("revision_cal_") as tmp:
        for q in quality_values:
            log(f"--- Qualitätswert {q} ({len(starts)} Messstellen à {seg_len}s) ---")
            all_frames, cambis, cambi_peaks, total_bytes = [], [], [], 0

            for idx, start in enumerate(starts):
                out = os.path.join(tmp, f"sample_q{q}_{idx}.hevc")
                cmd = build_encode_cmd(measure_profile, src, out, quality=q)
                i = cmd.index("-i")
                cmd = cmd[:i] + ["-ss", str(start), "-t", str(seg_len)] + cmd[i:]
                _run(cmd, log)
                total_bytes += os.path.getsize(out)

                # Schluesselwort-Argumente mit Absicht: Positional waren hier
                # Referenz und Encode vertauscht - der Zeitsprung landete dann
                # auf dem 40-Sekunden-Ausschnitt statt auf dem Original, und
                # libvmaf bekam keine Frames.
                m = measure_vmaf(reference=src, distorted=out, log=log,
                                 height=mi.height, ref_start=start,
                                 ref_duration=seg_len)
                all_frames.extend(m["frames"])
                if m["cambi"] is not None:
                    cambis.append(m["cambi"])
                    cambi_peaks.append(m["cambi_max"])
                _cleanup(out)

            measured_s = seg_len * len(starts)
            bitrate = (total_bytes * 8 / 1_000_000) / measured_s
            full_gb = (bitrate * duration / 8) / 1024 if duration else 0.0
            mean = sum(all_frames) / max(len(all_frames), 1)
            row = {
                "quality": q,
                "vmaf": round(mean, 2),
                "vmaf_p5": round(_percentile(all_frames, 5), 2),
                "vmaf_min": round(min(all_frames), 2) if all_frames else 0.0,
                "cambi": round(sum(cambis) / len(cambis), 2) if cambis else None,
                "cambi_max": round(max(cambi_peaks), 2) if cambi_peaks else None,
                "bitrate_mbps": round(bitrate, 1),
                "estimated_gb": round(full_gb, 2),
            }
            results.append(row)
            log(f"Qualität {q}: VMAF Ø {row['vmaf']}, schlechteste 5 % {row['vmaf_p5']}, "
                f"Minimum {row['vmaf_min']}"
                + (f", CAMBI Ø {row['cambi']} (Spitze {row['cambi_max']})" if cambis else "")
                + f" · {bitrate:.1f} Mbit/s, hochgerechnet {full_gb:.2f} GB")
    return results


# Wie weit die schlechtesten 5 % der Frames hoechstens unter dem Ziel liegen
# duerfen. 6 VMAF-Punkte sind Netflix' "gerade wahrnehmbarer Unterschied"
# (JND) - ab da bemerkt mehr als die Haelfte der Zuschauer eine Aenderung.
# Liegen die schwaechsten Szenen weniger als einen JND unter dem Ziel, faellt
# der Einbruch den meisten nicht auf; darueber hinaus schon.
P5_MAX_DROP = 6.0


def tiers_from_calibration(results: list[dict]) -> dict:
    """Ordnet den Messreihen die drei Stufen zu.

    Pro Stufe wird der SPARSAMSTE Wert gesucht, der BEIDES erfuellt:
      1. Durchschnitt >= Ziel (die normale Anforderung)
      2. schlechteste 5 % der Frames >= Ziel - 1 JND

    Punkt 2 ist neu und der eigentliche Gewinn: Der Durchschnitt allein
    versteckt kurze, starke Einbrueche. Ein Wert, der im Mittel 93 schafft,
    aber in schwierigen Szenen auf 80 faellt, waere frueher als "empfohlen"
    durchgegangen - obwohl genau diese Szene sichtbar zerfaellt.

    Wird ein Ziel von keinem gemessenen Wert erreicht, bleibt die Stufe leer
    statt einen Wert zu erfinden. Der Bitraten-Deckel wird aus der gemessenen
    Bitrate abgeleitet (Faktor 1,5) und ist damit Sicherheitsnetz, keine Bremse.

    CAMBI (Banding) fliesst bewusst NICHT automatisch in die Auswahl ein: Fuer
    einen festen Schwellwert gibt es keine belastbare Quelle, und ein
    erfundener Grenzwert waere schlechter als keiner. Der Wert wird angezeigt,
    damit man steigendes Banding zwischen den Stufen selbst sieht."""
    tiers = {}
    for tier, target in TIER_VMAF_TARGETS.items():
        passing = [
            r for r in results
            if r["vmaf"] >= target
            and r.get("vmaf_p5", r["vmaf"]) >= target - P5_MAX_DROP
        ]
        if not passing:
            continue
        best = max(passing, key=lambda r: r["quality"])   # sparsamster Treffer
        tiers[tier] = {
            "quality": best["quality"],
            "vmaf": best["vmaf"],
            "vmaf_p5": best.get("vmaf_p5"),
            "cambi": best.get("cambi"),
            "target_mbps": round(best["bitrate_mbps"] * 1.5, 1),
            "measured_mbps": best["bitrate_mbps"],
            "estimated_gb": best["estimated_gb"],
        }
    return tiers