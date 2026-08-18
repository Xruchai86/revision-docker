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
