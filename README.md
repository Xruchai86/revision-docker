# ReVision (Docker/Unraid)

Web-Pendant zur Windows-App "ReVision" - Dolby-Vision-Profile nach 8.1 fixen,
über den Browser statt WPF-Fenster, mit Intel-VAAPI-Hardware-Beschleunigung
für Unraid-iGPUs (Core Ultra/Arrow Lake/Xe-Grafik). Kein NVENC hier - Unraid-
Server haben typischerweise keine dedizierte NVIDIA-GPU (ließe sich als
zweites Backend nachrüsten, falls doch eine durchgereicht wird - analog zur
Windows-App).

## Ehrlich zum aktuellen Funktionsumfang (wichtig, bevor du loslegst)

Was JETZT funktioniert:

- Generischer Profil-Fix (Dual-Layer verlustfrei, Reencode per VAAPI, Relabel
  verlustfrei) - der eigentliche Kern der App.
- Die vier Qualitätsprofile (Ausgewogen/Maximale Qualität/Kleinere Dateien/
  Schnell), Qualität über CQP/`-qp` gesteuert (siehe Encoder-Backend-Abschnitt
  weiter unten - Umstieg von QSV auf VAAPI, nachdem QSV auf der tatsächlichen
  Hardware zuverlässig scheiterte).
- **Downsize** (neu) - für bereits gesunde HDR10/Profile-8-Dateien mit hoher
  Bitrate, inkl. DV-RPU-Erhalt bei Profile-8-Quellen (extrahieren, BL neu
  encodieren, unveränderte RPU wieder injizieren).
- **Einstellungen-Persistenz** (neu) - Zielordner, Qualitätsprofil und
  Downsize-Schwelle landen in `/config/settings.json` und übersteht damit
  Container-Neustarts, solange das `/config`-Volume gemappt ist (siehe
  Unraid-Template/docker-compose.yml).
- Einfache Weboberfläche: **Ordner-Browser-Popup** (kompletten Medien-Root
  einbinden, innerhalb der App navigieren statt Pfade zu tippen), Scan-
  Ergebnisse in einem eigenen Auswahl-Popup (Fix und Downsize werden pro
  Zeile automatisch richtig zugeordnet), Live-Log pro Job.

**Noch NICHT portiert** (folgt bei Bedarf in weiteren Schritten):
- SDR-Optimierung, Upscale, SDR→HDR-Remap
- MP4-Export, Container-Wahl-Dialog bei DV+Atmos
- Automatische Nachkompression nach dem Fix
- VMAF-Qualitätsvergleich

## Ordner-Browser statt Pfade tippen (neu)

Der Quellordner wird jetzt als **kompletter Medien-Root** eingebunden (z.B.
`/mnt/user/Media`, nicht mehr ein einzelner Serien-Unterordner) - "Durchsuchen…"
öffnet ein Popup, das innerhalb dieses Roots navigierbar ist (Ordner anklicken
zum Reinwechseln, Breadcrumb oben zum Zurückspringen). "Diesen Ordner wählen &
scannen" startet direkt den Scan für den gerade angezeigten Unterordner - kein
manuelles Pfad-Tippen mehr nötig. Ein neuer `/api/browse`-Endpunkt liefert die
Unterordner-Liste, mit Pfad-Traversal-Schutz (kann nicht aus dem gemounteten
Root heraus navigieren, selbst mit `../../`-Tricks in der URL).

Die Scan-Ergebnisse erscheinen jetzt ebenfalls in einem eigenen Popup statt
fest auf der Hauptseite - Auswahl treffen, "Ausgewählte verarbeiten", Popup
schließt sich automatisch.

**Kein großes Einstellungen-Fenster** (wie bei der Windows-App) - für den
aktuellen Funktionsumfang (Qualitätsprofil + Downsize-Schwelle) reichen die
zwei Regler oben auf der Hauptseite völlig aus. Sobald SDR-Optimierung/Upscale
dazukommen, macht ein eigener Bereich dafür Sinn - bis dahin bewusst schlank
gehalten, um nicht unnötig einen Klick zwischen Nutzer und Arbeit zu stellen.

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

## Encoder-Backend: VAAPI statt QSV/oneVPL (finale Lösung nach mehreren Fehlschlägen)

**Ehrliche Historie, weil sie zeigt, wie wir tatsächlich zur Lösung kamen:**
Drei aufeinanderfolgende QSV/oneVPL-Fixes (fehlende Laufzeit-Pakete, explizite
Geräteangabe, zweistufige Geräte-Initialisierung) scheiterten alle am selben
Fehler (`Error setting child device handle: -17`). Grund am Ende gefunden:
Die tatsächliche Hardware ist **Arrow Lake-S** (Desktop-Core-Ultra-200S-Serie),
nicht Meteor Lake wie ursprünglich angenommen - alle bisherigen Fixes bezogen
sich auf die falsche Chip-Generation. Per `lspci -k` verifiziert (`Intel
Corporation Arrow Lake-S [Intel Graphics]`).

Ein direkter VAAPI-Testencode (komplett ohne QSV/oneVPL-Beteiligung) lief auf
genau diesem System sofort fehlerfrei durch:
```bash
docker exec ReVision ffmpeg -hide_banner -f lavfi -i color=c=black:s=1280x720:d=1:r=25 \
  -vaapi_device /dev/dri/renderD128 -vf "format=nv12,hwupload" -c:v hevc_vaapi -f null -
```
Das beweist: GPU und Treiber sind einwandfrei, das Problem saß ausschließlich
in der oneVPL/QSV-Softwareschicht. Die ganze App läuft deshalb jetzt auf
**direktem `hevc_vaapi`** statt `hevc_qsv` - ein einfacherer, auf dieser
Hardware nachweislich funktionierender ffmpeg-Codepfad ohne die fehleranfällige
oneVPL-Geräte-Verkettung.

**Was sich dadurch geändert hat:**
- CQP-Modus mit `-qp` (0-52, niedriger=besser) statt `-global_quality`/ICQ -
  Letzteres zeigte in Community-Tests inkonsistente Skalierung je nach
  Treiber-Version, `-qp` ist direkt aus `ffmpeg -h encoder=hevc_vaapi`
  eindeutig dokumentiert.
- Kein direktes Äquivalent zu QSVs `extbrc`/`rdo`/`mbbrc`/`look_ahead` mehr -
  das waren MediaSDK/oneVPL-spezifische Erweiterungen, VAAPIs Rate-Control ist
  bewusst einfacher gehalten. Qualität wird jetzt rein über `-qp` gesteuert.
- Umgebungsvariable heißt jetzt `VAAPI_DEVICE` statt `QSV_DEVICE` (alte
  Variable wird als Fallback noch gelesen, falls sie irgendwo gesetzt ist).

**Falls du selbst auf einem anderen System (z.B. echtem Meteor Lake) bist**
und dort lieber QSV/oneVPL testen willst: die alte QSV-Logik ist im Git-
Verlauf nachvollziehbar, aber angesichts der hier gemachten Erfahrung würde
ich direkt mit dem VAAPI-Testbefehl oben anfangen, bevor Zeit in QSV
investiert wird.

**Unabhängiger Bugfix, im selben Test aufgefallen:** ein `UnicodeDecodeError`
bei manchen Dateien mit ungewöhnlich kodierten Metadaten ließ den kompletten
Job abstürzen, statt nur die betroffene Log-Zeile zu markieren - jetzt mit
`errors="replace"` toleriert (weiterhin im Code, unabhängig vom VAAPI-Umstieg).

**Nachtrag (Hardware-Decode ergänzt):** Im Unraid-Dashboard fiel auf, dass die
CPU trotz aktiver GPU (Video Load im GPU-Panel > 0%) spürbar mitarbeitete
(z.B. 45% Last). Grund: nur der **Encode** lief auf der GPU, das **Decode**
der Quelldatei lief per Software auf der CPU, mit anschließendem Hochladen
der Frames zur GPU (`format=p010,hwupload`-Filter). Jetzt läuft die komplette
Kette (Decode UND Encode) auf der GPU (`-hwaccel vaapi -hwaccel_device ...
-hwaccel_output_format vaapi` vor der Eingabedatei, kein Software-Zwischenschritt
mehr) - Muster direkt aus mehreren übereinstimmenden, unabhängigen Quellen
verifiziert (u.a. offizielle ffmpeg-VAAPI-Dokumentation), nicht geraten. Sollte
die CPU-Last spürbar senken und die Geschwindigkeit weiter erhöhen.

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

## Temp-Ordner im RAM (tmpfs) - optional, deutlich schneller

Statt eines Platten-Pfads kann `/media/temp` auch direkt in den Arbeitsspeicher
gelegt werden (tmpfs) - spart bei den vielen GB an Zwischendateien pro Job
komplett das Festplatten-I/O. **Nur sinnvoll, wenn genug freier RAM da ist:**

Peak-Speicherbedarf pro Job wurde optimiert (Zwischendateien werden jetzt so
früh wie möglich gelöscht statt bis Jobende alle gleichzeitig zu liegen), liegt
aber bei 4K-Dateien wie GoT trotzdem grob bei **25-30 GB pro laufendem Job**
(Jobs laufen sequentiell, nie mehrere gleichzeitig - der Bedarf addiert sich
also nicht). Plane entsprechend Puffer für Unraid selbst und andere Container
ein, sonst droht ein OOM-Absturz des ganzen Servers, nicht nur des Containers.

**Einrichtung in Unraid** (Container bearbeiten → unten **"Add another Path,
Port, Variable"** → Typ auf **"Device"** oder direkt über die erweiterten
Container-Einstellungen die **"Extra Parameters"** nutzen):

```
--tmpfs /media/temp:size=32g,mode=1777
```

Das ersetzt die bisherige Path-Zuordnung für `/media/temp` (dann NICHT
zusätzlich als normaler Path eintragen, nur den tmpfs-Parameter). Größe
(`size=32g`) an deinen tatsächlich verfügbaren RAM anpassen - lieber knapp
unter dem, was du sicher übrig hast, als zu knapp kalkuliert.

**Wichtig:** Der Inhalt ist beim Container-Neustart automatisch weg (RAM ist
per Definition nicht dauerhaft) - für Zwischendateien ist das aber ohnehin
gewünscht, die sollen nach jedem Job sowieso gelöscht werden.

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
Weboberfläche an ("Docker / VAAPI · Temp: ...") - damit lässt sich sofort
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
4. **Wichtig für VAAPI:** Die Zeile mit `/dev/dri` muss stehen bleiben, sonst
   schlägt jeder Reencode-Fix (Profile 5/9) fehl - verlustfreie Fixes
   (Profile 7/4/Relabel) brauchen keine GPU und funktionieren auch ohne.
5. Container starten, `http://<Unraid-IP>:8080` im Browser öffnen.

## Lokal testen (bevor es auf Unraid landet)

Falls du einen Rechner mit Docker zur Hand hast, bevor du auf Unraid gehst:

```bash
docker compose up --build
```

Testdateien in `./test-media` legen, unter `http://localhost:8080` öffnen.
