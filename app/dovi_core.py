"""
Kernlogik von ReVision, portiert nach Python fuer die Docker/Unraid-Version.
Gleiche Prinzipien wie in der Windows-App (DoviConverter.cs/MediaScanner.cs):

- Dual-Layer-Quellen (Profile 7, 4, ...) -> verlustfrei, EL wird verworfen.
- Single-Layer ohne Base-Layer (Profile 5, 9, ...) -> muss reencodiert werden.
- Single-Layer MIT Base-Layer, falsch markiert (z.B. 8.2/8.4) -> verlustfrei
  relabeln, nur die RPU-Kennung wird auf 8.1 umgestellt.

Encoder-Backend hier: QSV (hevc_qsv via VAAPI), passend zur Intel-iGPU auf
Unraid-Boxen (z.B. Core Ultra 5) - kein NVENC, da Unraid-Server typischerweise
keine dedizierte NVIDIA-GPU haben (waere aber als zweites Backend nachruestbar,
analog zur Windows-App).
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


# ---------------------------------------------------------------------------
# Qualitätsprofile - dieselben vier wie in ReVision (Ausgewogen/Maximale
# Qualität/Kleinere Dateien/Schnell), mit den QSV-Werten aus der Windows-App
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


def build_qsv_args(profile: dict, quality: int) -> list[str]:
    """Baut die hevc_qsv-Argumentliste - identische Logik/Begruendung wie
    QsvOptions.cs in der Windows-App: extbrc/rdo/mbbrc/b_strategy nur bei
    aktivem Lookahead, weil die Lookahead-Tiefe laut ffmpeg-Doku ohne extbrc
    gar nicht erst wirkt."""
    args = [
        "-preset", profile["preset"],
        "-global_quality", str(quality),
        "-bf", str(profile["bframes"]),
        "-adaptive_i", "1",
        "-adaptive_b", "1",
        "-look_ahead", "1" if profile["lookahead"] else "0",
    ]
    if profile["lookahead"]:
        args += [
            "-extbrc", "1",
            "-rdo", "1",
            "-mbbrc", "1",
            "-b_strategy", "1",
            "-look_ahead_depth", str(profile["lookahead_depth"]),
        ]
    return args


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

    hdr_format = video.get("HDR_Format", "") or ""
    compat_str = video.get("HDR_Format_Compatibility", "") or ""
    is_hdr10 = "HDR10" in hdr_format or "HDR10" in compat_str
    mi.is_hdr10 = is_hdr10

    dv_profile = None
    compat_id = None
    if "Dolby Vision" in hdr_format:
        # z.B. "Dolby Vision, Version 1.0, Profile 7.6, dvhe.07.06, BL+EL+RPU, ..."
        for part in hdr_format.split(","):
            part = part.strip()
            if part.startswith("Profile "):
                dv_profile = part.replace("Profile ", "").split(".")[0]
            if part in ("BL+EL+RPU", "BL+RPU", "EL+RPU"):
                compat_id = part
    mi.dv_profile = dv_profile

    if dv_profile is not None:
        has_el = compat_id in ("BL+EL+RPU", "EL+RPU")
        has_bl = compat_id in ("BL+EL+RPU", "BL+RPU")
        if has_el and has_bl:
            mi.action = "dual_layer"       # verlustfrei, EL verwerfen
        elif not has_bl:
            mi.action = "reencode"          # keine Base-Layer -> muss reencodiert werden
        elif dv_profile != "8" or not is_hdr10:
            mi.action = "relabel"           # BL vorhanden, aber nicht als 8.1 markiert

    return mi


# ---------------------------------------------------------------------------
# Fix-Pipelines (Port von DoviConverter.cs)
# ---------------------------------------------------------------------------
def _run(cmd: list[str], log) -> None:
    log(f"$ {' '.join(cmd)}")
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
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
    with tempfile.TemporaryDirectory() as tmp:
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
    with tempfile.TemporaryDirectory() as tmp:
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


def fix_reencode(src: str, out_path: str, log, profile_key: str = "balanced") -> None:
    """Profile 5/9/... - keine nutzbare Base-Layer, MUSS per QSV reencodiert werden."""
    profile = QUALITY_PROFILES[profile_key]
    with tempfile.TemporaryDirectory() as tmp:
        mkv_src = _to_mkv_if_needed(src, tmp, log)

        raw_hevc = os.path.join(tmp, "orig.hevc")
        _run([FFMPEG, "-y", "-i", mkv_src, "-c:v", "copy", "-bsf:v", "hevc_mp4toannexb",
              "-f", "hevc", raw_hevc], log)

        rpu_p8 = os.path.join(tmp, "rpu_p8.bin")
        _run([DOVI_TOOL, "-m", "2", "extract-rpu", raw_hevc, "-o", rpu_p8], log)

        new_hevc = os.path.join(tmp, "new_base.hevc")
        qsv_args = build_qsv_args(profile, profile["quality_fix"])
        _run([FFMPEG, "-y", "-hwaccel", "qsv", "-i", mkv_src, "-map", "0:v:0",
              "-c:v", "hevc_qsv", *qsv_args,
              "-pix_fmt", "p010le", "-profile:v", "main10",
              "-color_primaries", "bt2020", "-color_trc", "smpte2084", "-colorspace", "bt2020nc",
              "-f", "hevc", new_hevc], log)

        injected = os.path.join(tmp, "injected.hevc")
        _run([DOVI_TOOL, "inject-rpu", "-i", new_hevc, "--rpu-in", rpu_p8, "-o", injected], log)

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
    Bitraten-Reduktion per QSV, keine Profilkonvertierung. DV-RPU (falls
    vorhanden) wird unveraendert durchgereicht (dovi_tool inject-rpu), genau wie
    in Downsizer.cs der Windows-App."""
    profile = QUALITY_PROFILES[profile_key]
    with tempfile.TemporaryDirectory() as tmp:
        mkv_src = _to_mkv_if_needed(mi.path, tmp, log)
        qsv_args = build_qsv_args(profile, profile["quality_downsize"])
        new_hevc = os.path.join(tmp, "new_base.hevc")

        if mi.dv_profile == "8":
            # DV-RPU vorhanden - extrahieren, BL neu encodieren, RPU unveraendert
            # wieder injizieren (Farbmetadaten bleiben exakt erhalten).
            raw_hevc = os.path.join(tmp, "orig.hevc")
            _run([FFMPEG, "-y", "-i", mkv_src, "-c:v", "copy", "-bsf:v", "hevc_mp4toannexb",
                  "-f", "hevc", raw_hevc], log)
            rpu = os.path.join(tmp, "rpu.bin")
            _run([DOVI_TOOL, "-m", "2", "extract-rpu", raw_hevc, "-o", rpu], log)

            _run([FFMPEG, "-y", "-i", mkv_src, "-map", "0:v:0",
                  "-c:v", "hevc_qsv", *qsv_args,
                  "-pix_fmt", "p010le", "-f", "hevc", new_hevc], log)

            injected = os.path.join(tmp, "injected.hevc")
            _run([DOVI_TOOL, "inject-rpu", "-i", new_hevc, "--rpu-in", rpu, "-o", injected], log)
            _run([MKVMERGE, "-o", out_path, injected, "--no-video", mkv_src], log)
        else:
            # Reines HDR10 ohne DV - keine RPU-Behandlung noetig, direkter Reencode.
            _run([FFMPEG, "-y", "-i", mkv_src, "-map", "0:v:0",
                  "-c:v", "hevc_qsv", *qsv_args,
                  "-pix_fmt", "p010le", "-f", "hevc", new_hevc], log)
            _run([MKVMERGE, "-o", out_path, new_hevc, "--no-video", mkv_src], log)
