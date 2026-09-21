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
- **Automatische Nachkompression nach dem Fix** (neu) - nach einem
  verlustfreien Dual-Layer-Fix (typisch bei BD-Kopien/Profile 7) oder Relabel
  wird automatisch nachkomprimiert, falls das Ergebnis noch über der
  Downsize-Schwelle liegt. Grund: der Dual-Layer-Fix selbst ist bewusst
  verlustfrei (wirft nur die Enhancement-Layer weg, encodiert nichts neu) -
  das allein macht die Datei oft nur unwesentlich kleiner. Reencode-Fixes
  (Profile 5/9) bekommen das NICHT zusätzlich - die sind durch den Fix selbst
  schon angemessen klein, eine weitere Kompression wäre dort nur eine
  unnötige zweite Encoder-Generation.
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

## Dual-Layer/Profile 7 optional immer neu encodieren (neu)

Beobachtet: eine 1:1-BD-Kopie (Profile 7, Dual-Layer, per DVDFab gerippt) spielte
nach dem normalen (verlustfreien) Fix auf einer nativen LG-webOS-TV-App nicht mit
Dolby Vision/Atmos ab, obwohl die Metadaten der Ausgabedatei nachweislich korrekt
waren (per `mediainfo` verifiziert - Profile 8, RPU vorhanden, Atmos bitgenau
erhalten). Auf einer Android-Box (die DV selbst verarbeitet) lief dieselbe Datei
einwandfrei. Ein per Profile-5-Pfad **komplett neu encodierter** Film lief dagegen
auch auf der LG-App problemlos - deckt sich mit einem bereits dokumentierten
LG-webOS-Bug (native Player-Schwierigkeiten mit bestimmten MKV/DV-Bitstream-
Strukturen, unabhängig von korrekten Metadaten).

Neue Checkbox "Profile 7/Dual-Layer immer neu encodieren" (Standard: aus) - wenn
aktiviert, läuft nach dem verlustfreien Dual-Layer-Fix **immer** derselbe VAAPI-
Reencode-Durchlauf wie bei Downsize (RPU erhalten, Bild komplett neu encodiert),
unabhängig von der Dateigröße/Downsize-Schwelle. Kostet GPU-Zeit und ist nicht
mehr bit-identisch zum Original, kann aber genau dieses Abspielproblem umgehen.
**Nur aktivieren, wenn du tatsächlich Kompatibilitätsprobleme hast** - der
Standard-Fix bleibt der schnellere, verlustfreie Weg.

## VMAF-Kalibrierung – Qualitätswerte messen statt schätzen (neu)

**Ehrlich vorweg:** Die Qualitätswerte in den Presets waren Erfahrungswerte,
keine Messwerte – ursprünglich von NVENC-CQ-Werten der Windows-App abgeleitet
und per Analogie übertragen. Plausibel, aber nie an echtem Material geprüft.

Die Forschung ist sich beim Ziel einig: **VMAF 93–95 gilt als
Transparenzbereich** – Zuschauer nehmen zwischen 93 und 95 keinen Unterschied
mehr wahr, und oberhalb von 95 wird Bandbreite für Qualität ausgegeben, die
niemand unterscheiden kann. Die Bitrate allein sagt nichts darüber aus: Ein
ruhiger Dialog kann bei 4 Mbit/s makellos aussehen, eine Actionszene bei
derselben Bitrate zerfallen.

**Ablauf:** Im Scan-Ergebnis genau eine Datei anhaken, „Qualität kalibrieren…“.
Die App schneidet einen Ausschnitt aus der **Mitte** der Datei (Anfang und Ende
sind oft Schwarzbild oder Abspann und damit untypisch leicht zu komprimieren),
encodiert ihn mit mehreren Qualitätswerten und misst jeden gegen das Original.
Ergebnis ist eine Tabelle: Qualitätswert → VMAF → Bitrate → hochgerechnete
Dateigröße, mit Empfehlung auf dem **niedrigsten** Wert, der noch 93 erreicht.

**Warum ein zweites ffmpeg im Image:** Das ffmpeg aus den Ubuntu-Quellen ist
ohne `--enable-libvmaf` gebaut – im Container ist nur `vmafmotion` vorhanden,
das lediglich die Bewegungskomponente berechnet, **nicht** den VMAF-Wert. Ein
eigenständiges `vmaf`-Tool gibt es in den Paketquellen ebenfalls nicht (beides
im laufenden Container geprüft, nicht angenommen). Deshalb kommt ein zweites
Binary aus BtbN/FFmpeg-Builds dazu, dessen GPL-Variante laut eigener
Konfigurationszeile `--enable-libvmaf` enthält. Es wird **ausschließlich zum
Messen** aufgerufen; encodiert wird weiter mit dem System-ffmpeg über die
geprüfte QSV/VAAPI-Kette. Schlägt der Download fehl, bleibt nur die
Kalibrierung deaktiviert – der Knopf erscheint dann gar nicht erst.

**Zwei Details, die das Ergebnis sonst wertlos machen würden:** Für 4K-Material
wird automatisch das 4K-Modell verwendet (das Standardmodell ist auf 1080p
trainiert und läge bei 2160p systematisch daneben), und die Referenz bekommt
dieselbe Start-/Dauer-Angabe wie der Encode – sonst vergleicht VMAF
unterschiedliche Frames.

**Übernahme je Kategorie:** In der Ergebnistabelle steht pro Zeile
„Für <Kategorie> übernehmen“. Ab dann verwendet **jede** Datei dieser Kategorie
automatisch den gemessenen Wert – einmal pro Inhaltsart messen genügt.
Vorrangreihenfolge beim Encoden: gemessener Kategorie-Wert → globaler Regler →
Preset-Standard. Anime und Realfilm können damit unterschiedliche Werte haben,
ohne sich gegenseitig zu überschreiben; eine gemischte Auswahl in einem
Durchlauf bekommt pro Datei den jeweils passenden Wert.

Gespeichert wird nicht nur die Zahl, sondern auch **VMAF-Punktzahl, Datum und
Quelldatei** – sonst stehen dort nach ein paar Monaten vier Zahlen ohne
Zusammenhang. Die Einstellungsseite zeigt das an und erlaubt das Verwerfen
einzelner Werte (dann greift wieder der globale Regler).

**Wie oft kalibrieren:** Pro Inhaltsart, nicht pro Datei – innerhalb einer Serie
sind die Folgen technisch nahezu identisch. Neu messen lohnt sich bei
Ausreißern: stark gekörnte alte Filme oder sehr dunkles Material brauchen mehr
Bits als der Durchschnitt.

## RPU-Extraktion per Pipe statt Riesen-Zwischendatei (neu)

Beim Reencode-Fix (Profile 5) und beim Downsize von Profile-8-Quellen wurde die
rohe HEVC-Spur bisher erst **komplett als Datei** in den Temp-Ordner geschrieben
(bei 4K-Material rund 15 GB) und danach von `dovi_tool` wieder eingelesen – ein
vollständiger Schreib- **und** Lesedurchgang über die Platte, nur um an wenige
Megabyte RPU-Daten zu kommen. Der Dual-Layer-Fix nutzte an derselben Stelle
längst eine Pipe; hier war es historisch anders gewachsen und nie angeglichen
worden.

Jetzt läuft `ffmpeg` direkt per Pipe in `dovi_tool extract-rpu`. Das spart pro
Job die Zwischendatei und die zugehörige I/O-Zeit – besonders relevant bei
RAM-basiertem Temp (tmpfs), wo die Datei echten Arbeitsspeicher belegt hat.

**Detail beim Fehler-Handling:** Beendet sich `dovi_tool` früher als `ffmpeg`,
bekommt ffmpeg ein SIGPIPE und endet mit Rückgabewert -13. Das ist **kein**
Fehler, sondern der Normalfall bei Pipes – geprüft wird deshalb ausschließlich
der Rückgabewert von `dovi_tool`. Real durchgespielt: früher Leser → als Erfolg
gewertet, echter Fehlschlag → wird weiterhin erkannt.

## Genauere Kalibrierung: Einbrüche, Banding, mehr Messstellen (neu)

Drei Verbesserungen, jede aus der Recherche abgeleitet:

**1. Die schwächsten Szenen zählen mit.** Der Durchschnitt versteckt kurze,
starke Einbrüche – ein Encode, der fast durchgehend gut aussieht und eine
Sekunde lang zerfällt, kann einen hohen Mittelwert haben und trotzdem einen
sichtbaren Fehler enthalten. Aus den Einzelwerten im JSON-Log werden deshalb
zusätzlich Minimum und das 5. Perzentil (die schlechtesten 5 % der Frames)
berechnet. Eine Stufe gilt nur als erreicht, wenn der Durchschnitt das Ziel
schafft **und** die schwächsten 5 % höchstens **6 Punkte** darunter liegen.
Die 6 sind nicht gewählt, sondern Netflix' „gerade wahrnehmbarer Unterschied“:
ab dort bemerkt mehr als die Hälfte der Zuschauer eine Änderung.

Beispiel, im Test nachgestellt: Qualität 21 schaffte im Durchschnitt 93,4,
fiel in schwierigen Szenen aber auf 84,2 – über einen JND unter dem Ziel.
Früher wäre das „empfohlen“ geworden, jetzt wird stattdessen 19 gewählt.

**2. Banding wird gemessen (CAMBI).** VMAF erfasst Streifen in weichen
Verläufen nur schlecht – genau das Hauptproblem bei HDR und 3D-Animation.
CAMBI ist Netflix' eigene Banding-Metrik aus libvmaf, auf 10 Bit ausgelegt
(0 = kein Banding, höher = mehr). Sie wird angezeigt, fließt aber **bewusst
nicht automatisch** in die Stufenwahl ein: Für einen festen Grenzwert gibt es
keine belastbare Quelle, und ein erfundener Schwellwert wäre schlechter als
keiner. Steigt der Wert zwischen zwei Stufen deutlich, lohnt die bessere.
Fehlt CAMBI in der verwendeten libvmaf, läuft die Messung ohne weiter.

**3. Drei Messstellen statt einer.** Ein einzelner Ausschnitt trifft zufällig
eine ruhige Dialogszene oder eine Actionsequenz und verzerrt das Ergebnis.
Gemessen wird jetzt bei 25, 50 und 75 % der Laufzeit, zusammen so lang wie
vorher der eine Ausschnitt; die Frame-Werte werden gemeinsam ausgewertet.
Bezahlt wird das durch **Subsampling**: Mit nur jedem fünften Frame lag der
Mittelwert in einer Vergleichsmessung bei 93,1308 statt 93,1300 – bei einem
Viertel der Zeit. Verwendet wird vorsichtig jeder dritte Frame, damit für die
Perzentile genug Frames übrig bleiben.

## SDR-Unterstützung, Profil je Datei und Stufen-Kalibrierung (neu)

**SDR wurde bisher komplett übersprungen.** Die Prüfung, ob eine Datei
verkleinert werden darf, verlangte HDR10 oder Dolby-Vision-Profil 8 – reine
SDR-Dateien tauchten im Scan gar nicht erst auf. Das war für Anime und ältere
Serien ein echtes Loch. SDR zählt jetzt ausdrücklich als „gesund“: Dort gibt es
keine DV-Struktur, die kaputt sein könnte. Wichtig dabei: SDR-Material wird
**nicht** mit BT.2020/PQ gekennzeichnet – das passiert nur im DV-Reencode-Pfad,
der für SDR ohnehin nicht greift.

**Profil je Datei plus Sammelauswahl.** Weil SDR und HDR bei vielen Sammlungen
im selben Ordner liegen, hat jede Zeile im Scan-Ergebnis ein eigenes
Profil-Dropdown, gefiltert nach Kategorie und passend zum Material vorbelegt
(SDR-Datei → SDR-Profil). Oben im Fenster setzt „Profil für alle angehakten“
eine Auswahl auf einmal; Zeilen, die das Profil nicht anbieten, bleiben
unverändert und werden gezählt gemeldet.

**Stufen-Kalibrierung – und ein dabei gefundener Konstruktionsfehler:**
Bei QVBR ist der Bitraten-Deckel bindend – wird ein Frame durch Obergrenze oder
Puffer beschränkt, liegt die erreichte Qualität unter der angeforderten. Bei
einem Anime-Preset mit 10 Mbit/s Deckel und Qualitätswert 20 hätte also der
**Deckel** begrenzt, nicht der Qualitätswert: Die Kalibrierung hätte gemessen,
was der Deckel erlaubt, und ein besserer Qualitätswert hätte nichts geändert.
Dazu passend: QVBR braucht zwingend eine `maxrate`-Angabe, sonst fällt es
faktisch auf ICQ zurück – und ICQ allein ignoriert `maxrate`.

Deshalb wird jetzt **in ICQ ohne Deckel gemessen**, damit der Qualitätswert
allein wirkt. Aus derselben Messreihe ergeben sich alle drei Stufen – jeweils
der sparsamste Wert, der das Ziel noch erreicht – und der dazu passende Deckel
(1,5× der gemessenen Bitrate, also Sicherheitsnetz statt Bremse):

| Stufe | Ziel-VMAF | Beispielmessung |
|---|---|---|
| sparsam | 90 | Qualität 23 → 10,2 Mbit/s, Deckel 15,3 |
| empfohlen | 93 | Qualität 21 → 13,7 Mbit/s, Deckel 20,5 |
| max | 95 | Qualität 19 → 18,3 Mbit/s, Deckel 27,5 |

Erreicht kein gemessener Wert ein Ziel, bleibt die Stufe leer statt einen Wert
zu erfinden – die Oberfläche sagt dann, dass mit niedrigeren Werten erneut
gemessen werden sollte. Welche Stufe beim Encoden greift, hängt am gewählten
Profil; fehlt für ein Profil die passende Stufe, gilt „empfohlen“.

## Sechs Kategorien: 3D-Animation als eigener Fall (neu)

Realfilm und 2D-Anime greifen zu kurz – **3D-/CGI-Animation ist ein dritter
Fall** mit eigenen Anforderungen:

- **Realfilm**: Filmkorn, Rauschen, echte Texturen → braucht viele Bits.
- **2D-Anime**: große einfarbige Flächen, harte Kanten → sehr genügsam.
- **3D-Animation**: kein Korn (darf also sparsamer sein als Realfilm), aber
  viele weiche Verläufe, Tiefenunschärfe und subtile Schattierungen – genau dort
  entsteht **Banding**, also sichtbare Streifen in Verläufen. Deshalb deutlich
  besserer Qualitätswert als Realfilm, aber nicht so niedrige Bitrate wie 2D-Anime.

| Kategorie | Bitrate | Qualität | Preset |
|---|---|---|---|
| Realfilm | 30 Mbit/s | 22 | slow |
| Serie (Realfilm) | 16 Mbit/s | 23 | medium |
| Animationsfilm (3D/CGI) | 22 Mbit/s | 20 | slow |
| Animationsserie (3D/CGI) | 13 Mbit/s | 21 | medium |
| Anime-Film (2D) | 18 Mbit/s | 19 | slow |
| Anime-Serie (2D) | 10 Mbit/s | 20 | medium |

**Wichtig – Grenze der automatischen Zuordnung:** Die Ordnerregeln können
Realfilm und Animationsfilm nur unterscheiden, wenn sie in **getrennten Ordnern**
liegen. Steht ein CGI-Film im selben Ordner wie Realfilme, kann keine Pfadregel
das trennen – dann die Kategorie vor dem Verarbeiten manuell im Dropdown wählen.

**Dabei behobener Fehler:** Die Kategorie-Auswahl steuerte bisher nur die
Profilliste, während Qualitätswert und Zielordner serverseitig ausschließlich
über die Ordnerregeln bestimmt wurden – eine manuelle Umstellung wirkte also nur
halb. Jetzt wird die gewählte Kategorie mitgesendet und schlägt die Ordnerregel
für beides.

## Kategorien, eigene Einstellungsseite und Live-Log (neu)

**Eigene Einstellungsseite** unter `/einstellungen` (Link oben rechts). Dorthin
sind alle Werte gewandert, die man einmal setzt und danach nicht mehr anfasst –
Zielordner, Downsize-Schwelle, Qualitätswert und die Dual-Layer-Option. Die
Hauptseite zeigt nur noch, was sich pro Aufgabe ändert: Quellordner, Kategorie,
Profil, Ziel-Bitrate.

**Ordner-Zuordnung:** Regeln der Form „Pfad enthält X → Kategorie Y“. Beim
Scannen wird der Quellpfad dagegen geprüft, die passende Kategorie automatisch
gewählt und die Profilliste darauf gefiltert. Bei fester Ordnerstruktur muss
damit pro Aufgabe nichts mehr manuell umgestellt werden.

**Kategorien und Anime-Presets:** Realfilm, Serie, Anime-Film, Anime-Serie.
Anime hat große einfarbige Flächen, harte Kanten und kein Filmkorn – das
komprimiert deutlich besser, gleichzeitig fallen Blockartefakte in
Farbverläufen stärker auf. Die Anime-Presets kombinieren deshalb eine
**niedrigere Bitrate mit einem besseren Qualitätswert** statt einfach nur die
Bitrate zu senken (Anime-Film 18 Mbit/s bei Qualität 19 gegenüber Realfilm
30 Mbit/s bei 22).

**Qualitätswert einstellbar (wichtig):** Bei QVBR und ICQ ist *dieser* Wert der
eigentliche Steuerhebel – die Ziel-Bitrate wirkt dort nur als **Obergrenze**.
Deshalb können Ergebnisse deutlich darunter landen: eine GoT-Folge mit
eingestellten 30 Mbit/s kam bei Qualität 22 tatsächlich bei rund 12–13 Mbit/s
heraus (66 % kleiner als das Original, ohne sichtbaren Verlust). Wer die
Bitrate ausreizen will, senkt den Qualitätswert. Richtwerte: 17–19 sehr
hochwertig, 20–23 guter Kompromiss, ab 24 sichtbar sparsamer. `0` bedeutet
„Wert des Profils verwenden“.

**Live-Log:** Der Log-Dialog lädt jetzt alle zwei Sekunden nach, statt den Stand
beim Öffnen einzufrieren. Er springt nur dann ans Ende, wenn man ohnehin unten
war – beim Zurückscrollen bleibt die Position erhalten.

## Qualitäts-Presets mit Rate-Control-Modi (neu)

Statt nur einer Ziel-Bitrate gibt es jetzt sechs Presets, die Rate-Control-Modus,
Bitrate, B-Frames und B-Frame-Pyramide kombinieren. Welche Modi hier auftauchen,
wurde **auf der Ziel-Hardware einzeln getestet**, nicht aus der ffmpeg-Optionsliste
abgeschrieben (die zeigt auch Modi, die der Treiber ablehnt):

| Modus | Ergebnis auf Arrow Lake-S / iHD |
|---|---|
| CQP, CBR, VBR, ICQ, QVBR | unterstützt |
| AVBR | vom Treiber abgelehnt (`Driver does not support AVBR RC mode`) |
| `b_depth 3` | unterstützt |

Die Presets:

- **Film – QVBR (neuer Standard)**: Qualitätsziel MIT Bitraten-Deckel. Ruhige
  Szenen dürfen sparen, komplexe bekommen was sie brauchen, die Obergrenze hält.
  Das löst den ursprünglichen Konflikt zwischen CQP (unvorhersehbare Größe) und
  reinem VBR (Bitrate unabhängig vom Bildinhalt).
- **Serie – QVBR (sparsamer)**: gleiches Prinzip, niedrigere Zielwerte.
- **Archiv – ICQ**: qualitätsgesteuert ohne Bitraten-Deckel, Größe bleibt
  inhaltsabhängig.
- **Ausgewogen – VBR**: exakt das bisherige Verhalten, unverändert verfügbar.
- **Feste Größe – CBR**: konstante Bitrate für planbare Dateigrößen.
- **Schnell – CQP**: Entwurf/Test.

Zusätzlich neu in allen Presets: **`b_depth`** (hierarchische B-Frames, laut
ffmpeg-Doku bessere Kompressionseffizienz bei gleicher Bitrate) und
**`async_depth`** (mehr Parallelität, reines Tempo).

Der Bitraten-Regler überschreibt weiterhin den Preset-Standardwert, ohne das
Preset zu verlassen. Alte gespeicherte Profilnamen (`max`, `smaller`) fallen
automatisch auf den neuen Standard zurück statt abzustürzen.

## QSV/oneVPL – zweiter Versuch mit aktueller Runtime (experimentell)

**Warum es beim ersten Mal scheiterte (neue Erkenntnis):** Ubuntu 24.04 liefert
`libmfx-gen1.2` in **Version 23.2.3** – Stand 2023, via packages.ubuntu.com
verifiziert. Arrow Lake kam erst **Oktober 2024** auf den Markt. Diese
oneVPL-Runtime kann die Geräte-IDs dieser Chip-Generation schlicht nicht kennen.
Das erklärt sauber, warum VAAPI (eigener, aktueller iHD-Treiber) lief, QSV aber
nie ansprang – und warum die damaligen Fixes nichts brachten: sie zielten alle
auf die ffmpeg-Kommandozeile, während das Problem in der Laufzeitbibliothek saß.

**Was sich geändert hat:** Die Medien-Laufzeit kommt jetzt aus Intels offiziellem
Client-GPU-Repository statt aus den Ubuntu-Quellen (Repo-Zeile und Paketliste
aus Intels eigener Installationsdoku übernommen). Damit sollten aktuelle
Geräte-IDs inklusive Arrow Lake vorhanden sein.

**Warum QSV überhaupt interessant ist** – es kann zwei Dinge, die dem
VAAPI-Pfad komplett fehlen:

- **Echte Geschwindigkeits-Presets** (`veryslow` … `veryfast`), also ein echter
  Tempo/Qualitäts-Tradeoff. Genau das, was wir bei VAAPI bewusst weggelassen
  haben, weil sich für `-compression_level` keine verlässliche Wertespanne fand.
- **Lookahead** – der Encoder schaut kommende Frames voraus und verteilt Bits
  vorausschauend. `extbrc` ist daran gekoppelt, weil `look_ahead_depth` laut
  ffmpeg-Doku ohne `extbrc` wirkungslos bleibt.

Zwei neue Presets, beide ausdrücklich als **experimentell** gekennzeichnet:
`QSV – Maximale Qualität` (veryslow + ICQ + Lookahead 40) und
`QSV – Film mit Bitraten-Deckel` (slow + QVBR + Lookahead 32).

**Ehrlich zum Status:** Ob QSV auf der Hardware jetzt wirklich läuft, ist
**nicht verifiziert** – das kann nur ein echter Testlauf zeigen. Die
VAAPI-Presets bleiben deshalb Standard und unverändert; die QSV-Presets sind ein
Angebot zum Ausprobieren, kein Ersatz. Schlägt QSV fehl, betrifft das nur den
jeweiligen Job, nicht die App.

## Ziel-Bitrate-Regler statt fixer CQP-Werte (neu)

CQP (Constant Quantization) ist szenen-adaptiv und garantiert **keine**
Mindest-Bitrate - bei "einfachem" Bildmaterial (wenig Bewegung/Detail) fällt
die Bitrate von sich aus, unabhängig vom QP-Wert. Beobachtet: "Maximale
Qualität" (CQP 18) landete bei einem Realfilm nur bei ~9 Mbit/s im Schnitt -
für eine "maximale Qualität"-Einstellung unerwartet niedrig, obwohl der
Encoder technisch nichts falsch gemacht hat.

Encoding läuft jetzt auf **VBR mit expliziter Ziel-Bitrate** statt CQP -
direkte, vorhersehbare Kontrolle über die Zieldateigröße. Neuer Regler oben
("Ziel-Bitrate Video") überschreibt den Profil-Standardwert:

| Profil | Standard-Ziel-Bitrate |
|---|---|
| Schnell (Test) | 10 Mbit/s |
| Kleinere Dateien | 14 Mbit/s |
| Ausgewogen | 20 Mbit/s |
| Maximale Qualität | 30 Mbit/s |

Regler-Bereich: 5-60 Mbit/s, manuell nachjustierbar (z.B. 25-35 Mbit/s für
Filme, wie ursprünglich gewünscht). `-maxrate` (1,5x Ziel) und `-bufsize`
(2x Ziel) begrenzen kurzzeitige Spitzen, ohne die durchschnittliche Bitrate
künstlich zu deckeln - Standard-VBR-Faktoren, keine Besonderheit.

**Nachtrag (Profile aufgeräumt):** Die Profile enthielten noch Karteileichen
aus der alten CQP-Ära (`preset`, `quality_fix/downsize/sdr`,
`lookahead/lookahead_depth`) - Felder, die seit dem VBR-Umstieg von
`build_vaapi_args()` gar nicht mehr gelesen wurden, aber den falschen
Eindruck erweckt hätten, sie würden noch etwas bewirken. Entfernt - jedes
Profil hat jetzt nur noch `name`, `bframes` und `target_mbps`, exakt das, was
tatsächlich wirkt. Bewusst KEIN Ersatz für QSVs "Preset"-Konzept (Encoder-
Geschwindigkeit/Qualitäts-Tradeoff) ergänzt, da sich dafür keine verlässlich
dokumentierte Wertespanne für den konkreten Intel-iHD-Treiber finden ließ -
lieber ehrlich weglassen als raten.

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

Peak-Speicherbedarf pro Job wurde **zweimal** optimiert: Zwischendateien werden
so früh wie möglich gelöscht statt bis Jobende alle gleichzeitig zu liegen, UND
die komplette Remux-Zwischenkopie der Quelldatei (früher ~15 GB pro Job, nur um
am Ende die Audiospur rauszuziehen) entfällt jetzt komplett - Video-Extraktion,
Encode und finales Audio-Muxen lesen alle direkt aus der Originaldatei. Neuer
grober Richtwert bei 4K-Dateien wie GoT: **rund 15 GB pro laufendem Job**
(dominiert von der rohen, unkomprimierten HEVC-Zwischendatei während der RPU-
Extraktion) statt vorher ~25-30 GB. Jobs laufen weiterhin sequentiell, nie
mehrere gleichzeitig - der Bedarf addiert sich also nicht. Plane trotzdem
Puffer für Unraid selbst und andere Container ein, sonst droht ein OOM-Absturz
des ganzen Servers, nicht nur des Containers - bei z.B. 32 GB Gesamt-RAM und
~10 GB bereits belegt (22 GB frei) reicht der neue Bedarf von ~15 GB jetzt
mit spürbarerem Puffer als vorher, aber immer noch nicht üppig.

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
