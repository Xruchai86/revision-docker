# ReVision (Docker/Unraid)

Web-Pendant zur Windows-App "ReVision" - Dolby-Vision-Profile nach 8.1 fixen,
über den Browser statt WPF-Fenster, mit Intel QuickSync (QSV) für Unraid-
iGPUs wie den Core Ultra 5. Kein NVENC hier - Unraid-Server haben typischerweise
keine dedizierte NVIDIA-GPU (ließe sich als zweites Backend nachrüsten, falls
doch eine durchgereicht wird - analog zur Windows-App).

## Ehrlich zum aktuellen Funktionsumfang (wichtig, bevor du loslegst)

Was JETZT funktioniert:

- Generischer Profil-Fix (Dual-Layer verlustfrei, Reencode per QSV, Relabel
  verlustfrei) - der eigentliche Kern der App.
- Die vier Qualitätsprofile (Ausgewogen/Maximale Qualität/Kleinere Dateien/
  Schnell) mit den korrigierten QSV-Flags (`extbrc`/`rdo`/`mbbrc`/
  `b_strategy`, korrekt an Lookahead gekoppelt).
- **Downsize** (neu) - für bereits gesunde HDR10/Profile-8-Dateien mit hoher
  Bitrate, inkl. DV-RPU-Erhalt bei Profile-8-Quellen (extrahieren, BL neu
  encodieren, unveränderte RPU wieder injizieren).
- **Einstellungen-Persistenz** (neu) - Zielordner, Qualitätsprofil und
  Downsize-Schwelle landen in `/config/settings.json` und übersteht damit
  Container-Neustarts, solange das `/config`-Volume gemappt ist (siehe
  Unraid-Template/docker-compose.yml).
- Einfache Weboberfläche: Ordner scannen, Dateien auswählen, verarbeiten
  (Fix und Downsize werden pro Zeile automatisch richtig zugeordnet),
  Live-Log pro Job.

**Noch NICHT portiert** (folgt bei Bedarf in weiteren Schritten):
- SDR-Optimierung, Upscale, SDR→HDR-Remap
- MP4-Export, Container-Wahl-Dialog bei DV+Atmos
- Automatische Nachkompression nach dem Fix
- VMAF-Qualitätsvergleich

## Bugfix-Hinweis (Profilerkennung bei bestimmten MP4-Quellen)

Manche mediainfo-Versionen/Quellen (beobachtet bei DVDFab-erzeugten MP4s)
schreiben die Dolby-Vision-Details NICHT als einen kommagetrennten
`HDR_Format`-Text (wie bei den meisten MakeMKV-MKVs), sondern in separate
Felder mit `"<DV-Wert> / <Fallback-Wert>"`-Aufbau (z.B.
`HDR_Format_Profile: "dvhe.05 / "`). Der Scanner erkannte in diesem Fall gar
kein Dolby-Vision-Profil und bot fälschlich nur "Downsize" statt eines Fixes
an. Jetzt werden beide Schreibweisen unterstützt.

**Zusätzlich dabei gefunden, potenziell ernster:** Profile-5-Quellen zeigen
bei manchen mediainfo-Versionen `"BL+RPU"` im Settings-Feld, obwohl Profile 5
laut Spezifikation NIE eine echte nutzbare Base-Layer hat. Die Erkennung
verließ sich zuvor rein auf diesen Compat-String - das hätte eine Profile-5-
Datei fälschlich als "nur verlustfrei relabeln" statt "muss reencodiert
werden" eingestuft, mit falschen Farben im Ergebnis. Jetzt entscheidet die
Profilnummer zuerst (5/9 sind immer Reencode-Fälle), der Compat-String nur
noch für die Dual-Layer-Erkennung.

**Wichtig, falls du auch die Windows-App (ReVision, WPF) nutzt:** Die dortige
Erkennung in `MediaScanner.cs` folgt derselben Grundannahme (ein
kommagetrennter `HDR_Format`-String) und wurde bisher nur an MakeMKV-Rips
getestet, nicht an DVDFab-MP4s wie hier. Ob sie an derselben Stelle hakt,
habe ich nicht geprüft - falls du dort ähnliche Dateien mit "kein Fix
erkannt" siehst, sag Bescheid, dann schauen wir uns das dort genauso an.

## Bugfix-Hinweis (QSV-Session schlägt fehl: "MFX_ERR_NOT_FOUND")

Reencode-Fixes scheiterten mit `Error creating a MFX session: -9` /
`No device available for decoder: device type qsv` - obwohl `/dev/dri`
korrekt durchgereicht war. Grund: Ubuntu 24.04s ffmpeg nutzt den modernen
oneVPL-Pfad (`--enable-libvpl --disable-libmfx`, siehe eigene ffmpeg-Log-
Ausgabe), der eigene, bisher fehlende Laufzeit-Pakete braucht - allen voran
`libmfx-gen1.2` (das oneVPL-Backend speziell für neuere "Gen"-GPUs wie Meteor
Lake/Core Ultra), dazu `libmfx1` und `libvpl2`. Alle drei Paketnamen wurden
direkt gegen die echte Ubuntu-24.04-Paketquelle (nicht nur die Web-Ansicht)
geprüft, bevor sie ins Dockerfile kamen.

**Nachtrag:** Selbst mit allen drei Paketen installiert (per `dpkg -l` im
Container bestätigt) und funktionierendem VAAPI-Treiber (bestätigt per
`vainfo` - der iHD-Treiber findet die GPU und listet HEVC-Encoding als
unterstützt) blieb der Fehler bestehen. Ursache: ffmpegs automatische QSV-
Geräteerkennung funktioniert bei manchen Meteor-Lake/Alder-Lake-Systemen
trotz allem nicht zuverlässig (dokumentiertes Community-Problem, u.a. im
Gentoo-Forum). Fix: Gerätepfad jetzt explizit angegeben (`-qsv_device
/dev/dri/renderD128`) statt der automatischen Erkennung zu vertrauen -
konfigurierbar über die Umgebungsvariable `QSV_DEVICE`, falls dein System
einen anderen Render-Node nutzt (mehrere GPUs im System o.ä.). Prüfen, welche
Render-Nodes existieren:

```bash
docker exec ReVision ls -la /dev/dri
```

Steht dort statt `renderD128` etwas anderes (z.B. `renderD129`), die
Umgebungsvariable `QSV_DEVICE` im Unraid-Template auf den passenden Pfad
setzen.

## Teil 1: Vom Handy auf GitHub hochladen

Am einfachsten geht das **ohne Git-Kommandozeile**, direkt über die
GitHub-Weboberfläche im Handy-Browser:

1. Auf [github.com](https://github.com) einloggen, oben rechts **"+" → "New repository"**.
2. Name vergeben (z.B. `revision-docker`), auf "Create repository" tippen.
3. Auf der neuen, leeren Repo-Seite: **"uploading an existing file"** antippen
   (Link erscheint mittig auf der Seite).
4. Alle Dateien aus diesem Ordner hochladen - am einfachsten: das ganze
   `revision-docker`-Verzeichnis vorher auf deinem Handy als ZIP entpacken
   (z.B. mit einer Datei-App), dann alle Dateien/Unterordner einzeln in das
   Upload-Feld ziehen bzw. über "choose your files" auswählen. GitHub behält
   dabei die Ordnerstruktur bei, wenn du ganze Ordner aus der Dateien-App
   auswählst (funktioniert je nach Handy-Browser/App unterschiedlich gut -
   bei Problemen: [github.com/apps/github-mobile](https://github.com/apps/github-mobile)
   installieren, die offizielle GitHub-App erlaubt teils komfortableres
   Hochladen ganzer Ordner als der reine Browser-Weg).
5. Unten einen Commit-Kommentar eingeben (z.B. "Erste Version"), **"Commit
   changes"**.

## Eigener Temp-Ordner (Fix: "No space left on device")

Zwischendateien (MP4→MKV-Remux, RPU-Extraktion, Reencode-Zwischenschritte)
liefen bisher im Container-eigenen `/tmp` - das liegt technisch auf dem
Cache/appdata-Laufwerk und ist bei 4K-Dateien (mehrere GB pro Zwischenschritt)
schnell voll. Neue Volume-Zuordnung `/media/temp` (Unraid-Template: "Temp-
Ordner", Standard `/mnt/user/Convert_Temp` - **auf dem Array**, nicht Cache)
plus `ENV TMPDIR=/media/temp` im Dockerfile - Python's `tempfile`-Modul liest
das automatisch, kein Code-Fix nötig, nur die Volume-Zuordnung.

**Sofort-Fix ohne neuen Image-Build**, falls du nicht auf einen neuen
GitHub-Actions-Lauf warten willst: Container in Unraid bearbeiten → "Add
another Path, Port, Variable" → einmal Path (`/media/temp` → z.B.
`/mnt/user/Convert_Temp`) und einmal Variable (`TMPDIR` = `/media/temp`)
hinzufügen, Apply. Wirkt sofort, auch mit dem alten Image.

**Nachtrag:** Falls die Variable trotz korrektem Eintrag nicht zu greifen
scheint (im Log weiterhin `/tmp/tmp...` statt `/media/temp/tmp...`) - der
Code liest `TMPDIR` jetzt **explizit selbst aus** (`TEMP_ROOT` in
`dovi_core.py`) statt sich rein auf Pythons eigene, implizite Herleitung zu
verlassen, und zeigt den tatsächlich verwendeten Pfad direkt oben in der
Weboberfläche an ("Docker / QSV · Temp: ...") - damit lässt sich sofort
prüfen, ob die Variable überhaupt ankommt, ohne SSH. Zusätzlich bringt ein
Aufräumfehler beim Löschen (z.B. Restdateien nach einem vorherigen
"Festplatte voll"-Abbruch) jetzt nicht mehr den ganzen Job zum Scheitern.

## Teil 2: Docker-Image bauen lassen (GitHub Actions - läuft in der Cloud, nicht auf deinem Handy)

Damit Unraid das Image beziehen kann, muss es irgendwo als fertiges
Docker-Image liegen - nicht nur als Quellcode auf GitHub. Hier: **GitHub
Container Registry (ghcr.io)** - kein separater Account nötig, läuft direkt
über dein GitHub-Konto. GitHub Actions baut das Image automatisch, jedes Mal
wenn du Code hochlädst.

1. Die Datei `.github/workflows/build.yml` liegt schon in diesem Ordner und
   ist bereits fertig konfiguriert - **kein Bearbeiten nötig**, sie leitet
   Repo-Name/Benutzername automatisch von deinem GitHub-Repo ab. Wichtig nur:
   dein Repo muss **exakt `revision-docker` heißen**, sonst landet das Image
   unter einem anderen Pfad als im Unraid-Template hinterlegt.
2. Nach dem Hochladen: im Reiter **"Actions"** des Repos nachsehen, ob der
   Build grün durchläuft (dauert einige Minuten).
3. **Wichtigster Schritt, wird leicht übersehen:** GitHub-Pakete sind
   standardmäßig **privat**, auch wenn das Repo selbst öffentlich ist - Unraid
   kann ohne Anmeldung dann nicht pullen ("denied", obwohl das Image
   existiert). Auf GitHub: dein Profil → **Packages** → `revision-docker`
   anklicken → **Package settings** (Zahnrad, unten auf der Seite) →
   **Change visibility** → **Public**. Ohne diesen Schritt bleibt der Pull
   auf Unraid dauerhaft verweigert, unabhängig davon wie oft der Build läuft.

## Teil 3: Auf Unraid einrichten

**Alternative per SSH** (schneller als der GUI-Weg, falls dir das lieber ist):

```bash
ssh root@<UNRAID-IP>

mkdir -p /boot/config/plugins/dockerMan/templates-user
curl -o /boot/config/plugins/dockerMan/templates-user/revision.xml \
  https://raw.githubusercontent.com/<dein-github-name>/<dein-repo>/main/unraid-template.xml
```

Danach im Unraid-Webinterface **Docker-Tab → Add Container** öffnen - oben im
Feld "Template" erscheint jetzt "revision" zur Auswahl, alle Felder werden
automatisch vorausgefüllt. Voraussetzung: Teil 2 (Docker-Hub-Image bauen
lassen) muss vorher fertig sein.

**Oder per GUI:**

1. In `unraid-template.xml` steht bereits `ghcr.io/xruchai86/revision-docker:latest` -
   nur bei abweichendem GitHub-Namen/Repo-Namen anpassen.
2. Unraid-Weboberfläche → **Docker-Tab → "Add Container"** → unten **"Template
   repositories"** einen Link zu deinem GitHub-Repo eintragen, ODER einfacher:
   **"Add Container"** → oben rechts auf **XML bearbeiten** umschalten → Inhalt
   von `unraid-template.xml` einfügen.
3. Pfade anpassen: Quell-/Zielordner auf deine tatsächlichen Unraid-Share-Pfade
   (z.B. `/mnt/user/Filme`) zeigen lassen.
4. **Wichtig für QSV:** Die Zeile mit `/dev/dri` muss stehen bleiben, sonst
   schlägt jeder Reencode-Fix (Profile 5/9) fehl - verlustfreie Fixes
   (Profile 7/4/Relabel) brauchen keine GPU und funktionieren auch ohne.
5. Container starten, `http://<Unraid-IP>:8080` im Browser öffnen.

## Lokal testen (bevor es auf Unraid landet)

Falls du einen Rechner mit Docker zur Hand hast, bevor du auf Unraid gehst:

```bash
docker compose up --build
```

Testdateien in `./test-media` legen, unter `http://localhost:8080` öffnen.
