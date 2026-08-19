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
    "quality_profile": "balanced",
    "downsize_threshold_mbps": 35.0,
    "target_bitrate_mbps": 30.0,  # 0/leer = Profil-Standardwert verwenden
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
