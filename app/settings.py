"""
Einstellungen-Persistenz - Python-Pendant zu AppSettings.cs in der Windows-App.
Liegt unter /config/settings.json (Docker-Volume, siehe docker-compose.yml /
unraid-template.xml) - ohne dieses Volume-Mapping geht der Inhalt bei jedem
Container-Neustart verloren, GENAU das Problem, das wir bei der Windows-App
mit %AppData%\\ReVision\\settings.json geloest hatten.

Quellordner wird bewusst NICHT gespeichert (siehe MainWindow-Pendant in der
Windows-App) - wechselt typischerweise pro Aufgabe, waehrend Zielordner,
Qualitaetsprofil und Downsize-Schwelle meist gleich bleiben.
"""
import json
import os

SETTINGS_PATH = os.environ.get("SETTINGS_PATH", "/config/settings.json")

DEFAULTS = {
    "output_folder": "",
    "quality_profile": "qsv_film",
    "downsize_threshold_mbps": 35.0,
    "target_bitrate_mbps": 30.0,  # 0/leer = Profil-Standardwert verwenden
    "force_reencode_dual_layer": False,  # Profile 7/Dual-Layer immer neu encodieren statt nur EL zu verwerfen

    # Qualitaetswert (global_quality/-qp). NIEDRIGER = mehr Bits, mehr Details.
    # 0 = Profil-Standard verwenden. Bei QVBR/ICQ ist DAS der eigentliche
    # Steuerwert - die Ziel-Bitrate wirkt dort nur als Obergrenze, weshalb
    # Ergebnisse deutlich unter dem Bitraten-Regler landen koennen.
    "quality_override": 0,

    # Ausgabeordner je Kategorie: {"film": "/media/output/Filme", ...}.
    # Leer oder fehlend = der allgemeine output_folder wird verwendet. Damit
    # landen Serien, Filme und Anime automatisch in getrennten Zielordnern,
    # ohne dass pro Aufgabe etwas umgestellt werden muss.
    "output_folders": {},

    # Ordner-Zuordnung: Liste von {"pfad": "...", "kategorie": "..."}.
    # Beim Scannen wird der Quellpfad gegen diese Fragmente geprueft (erste
    # Uebereinstimmung gewinnt) und die passende Kategorie vorausgewaehlt, die
    # wiederum die Profilliste filtert. Damit muss bei strikter Ordnerstruktur
    # nichts mehr pro Aufgabe manuell umgestellt werden.
    "category_rules": [],
}


def load() -> dict:
    try:
        with open(SETTINGS_PATH, "r", encoding="utf-8") as f:
            data = json.load(f)
        merged = dict(DEFAULTS)
        merged.update({k: v for k, v in data.items() if k in DEFAULTS})
        return merged
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return dict(DEFAULTS)


def save(settings: dict) -> None:
    try:
        os.makedirs(os.path.dirname(SETTINGS_PATH), exist_ok=True)
        to_write = {k: settings.get(k, DEFAULTS[k]) for k in DEFAULTS}
        with open(SETTINGS_PATH, "w", encoding="utf-8") as f:
            json.dump(to_write, f, indent=2)
    except OSError:
        pass  # best effort, wie in der Windows-App - darf nie die App zum Absturz bringen
