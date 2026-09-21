FROM ubuntu:24.04

ENV DEBIAN_FRONTEND=noninteractive

# ffmpeg (Ubuntu 24.04 bringt QSV/VAAPI-Unterstuetzung bereits mit - Build nutzt
# den modernen oneVPL-Pfad, "--enable-libvpl --disable-libmfx", kein Legacy-MediaSDK),
# intel-media-va-driver-non-free (iHD-VAAPI-Treiber), mkvtoolnix (mkvmerge) und
# mediainfo fuer die Profilerkennung.
#
# WICHTIG - oneVPL/QSV-Laufzeit kommt NICHT aus den Ubuntu-Quellen:
# Ubuntu 24.04 liefert libmfx-gen1.2 in Version 23.2.3 (Stand 2023, via
# packages.ubuntu.com verifiziert). Arrow Lake kam erst im Oktober 2024 -
# diese Runtime kennt die Geraete-IDs dieser Generation schlicht nicht. Das ist
# die wahrscheinlichste Ursache dafuer, dass QSV auf der Zielhardware trotz
# funktionierendem VAAPI nie ansprang ("MFX_ERR_NOT_FOUND", spaeter
# "Error setting child device handle: -17"). Deshalb wird die Medien-Laufzeit
# aus Intels offiziellem Client-GPU-Repository installiert (Paketliste und
# Repo-Zeile aus Intels eigener Installationsdoku uebernommen, nicht geraten).
# Schritt 1 - Basis aus den Ubuntu-Quellen. Bewusst als EIGENER Schritt, damit
# der Build auch dann durchlaeuft, wenn Intels Repository (Schritt 2) nicht
# erreichbar ist: VAAPI ist der Standard-Pfad und haengt NICHT an Intels Repo.
#
# Bewusst NICHT mehr dabei: libmfx1 (Legacy-MediaSDK 22.5.4) und libmfx-gen1.2
# (Ubuntu-Stand 23.2.3). Beide sind aelter als Arrow Lake. "vpl-inspect" zeigte
# mit ihnen als einzige Implementierung "mfxhw64" (die Legacy-MediaSDK), die die
# GPU zwar als DeviceID 7d67 sah, aber MFX_MEDIA_UNKNOWN meldete und bei Encoder-
# wie Decoder-Faehigkeiten "Version: 0.0" - also gar keine. Genau daher der
# Abbruch mit "Error setting child device handle: -17". Die brauchbare Runtime
# kommt ausschliesslich aus Schritt 2.
RUN apt-get update && apt-get install -y --no-install-recommends \
    ffmpeg \
    mkvtoolnix \
    mediainfo \
    intel-media-va-driver-non-free \
    libvpl2 \
    va-driver-all \
    vainfo \
    python3 python3-pip \
    gnupg ca-certificates curl \
    xz-utils \
    && rm -rf /var/lib/apt/lists/*

# Schritt 2 - aktuelle oneVPL/QSV-Laufzeit aus Intels offiziellem Client-Repo.
# Paketname NICHT geraten, sondern per "apt-cache policy" im laufenden Container
# ermittelt: Ubuntus "libmfx-gen1.2" bietet nur 23.2.3, waehrend Intels Repo das
# Paket "libmfx-gen1" in 24.3.4-1018~24.04 fuehrt (Alternativname "libmfxgen1"
# mit 24.2.4 als Rueckfallebene). Wichtig: frueher wurde hier "libmfx-gen1.2"
# mitinstalliert - das galt als Erfolg, weil es aus Schritt 1 schon da war, und
# die eigentliche neue Runtime wurde nie geholt.
#
# Absichtlich fehlertolerant: schlaegt der Schritt fehl, bleibt das Image ohne
# QSV-Runtime, aber voll funktionsfaehig - VAAPI ist davon unabhaengig.
RUN set -eux; \
    ( curl -fsSL https://repositories.intel.com/gpu/intel-graphics.key \
        | gpg --dearmor -o /usr/share/keyrings/intel-graphics.gpg \
      && echo "deb [arch=amd64 signed-by=/usr/share/keyrings/intel-graphics.gpg] https://repositories.intel.com/gpu/ubuntu noble client" \
        > /etc/apt/sources.list.d/intel-gpu-noble.list \
      && apt-get update \
      && ( apt-get install -y --no-install-recommends libmfx-gen1 libvpl2 libvpl-tools \
           || apt-get install -y --no-install-recommends libmfxgen1 libvpl2 libvpl-tools ) \
      && echo "Intel-Repo: oneVPL-Laufzeit aktualisiert" ) \
    || echo "WARNUNG: Intel-Repo nicht nutzbar - bleibe bei Ubuntu-Runtime (VAAPI unbeeintraechtigt)"; \
    rm -rf /var/lib/apt/lists/*

# dovi_tool - offizielles Release-Binary, fest auf eine geprüfte Version gepinnt.
# Version bewusst direkt in der URL (keine ARG-Variable) - robuster, keine Frage
# von Variablen-Expansion. -f sorgt dafür, dass curl bei einem HTTP-Fehler laut
# fehlschlägt statt eine Fehlerseite still als "tar.gz" zu speichern. Extraktion
# OHNE expliziten Mitgliedsnamen - das Release-Archiv enthaelt die Datei als
# "./dovi_tool" (mit Pfad-Praefix), ein exaktes "dovi_tool" ohne Praefix findet
# tar darin nicht (getestet, nicht angenommen). Zum Aktualisieren: neue Version
# unter https://github.com/quietvoid/dovi_tool/releases nachsehen und ersetzen.
RUN curl -fkL "https://github.com/quietvoid/dovi_tool/releases/download/2.3.3/dovi_tool-2.3.3-x86_64-unknown-linux-musl.tar.gz" \
    -o /tmp/dovi_tool.tar.gz \
    && tar -xzf /tmp/dovi_tool.tar.gz -C /usr/local/bin \
    && rm /tmp/dovi_tool.tar.gz \
    && chmod +x /usr/local/bin/dovi_tool

# Zweites ffmpeg AUSSCHLIESSLICH zum Messen (VMAF). Grund: das ffmpeg aus den
# Ubuntu-Quellen ist ohne "--enable-libvmaf" gebaut - im Container ist nur der
# Filter "vmafmotion" vorhanden, der lediglich die Bewegungskomponente
# berechnet, NICHT den VMAF-Wert. Ein eigenstaendiges vmaf-Tool gibt es in den
# Paketquellen ebenfalls nicht (beides im laufenden Container geprueft).
#
# xz-utils wird in Schritt 1 installiert: das Archiv ist .tar.xz, und ohne das
# xz-Programm scheitert "tar -xJ" - das fiel im ersten Anlauf still durch.
#
# Der Build von BtbN/FFmpeg-Builds enthaelt laut dessen eigener
# Konfigurationszeile "--enable-libvmaf". Die "latest"-URL ist laut deren
# README bewusst stabil ("provides consistent URLs always pointing to the
# latest build"), also keine geratene Datums-URL.
#
# Es ersetzt das System-ffmpeg NICHT: Encoden laeuft weiter ueber /usr/bin/ffmpeg
# mit der geprueften QSV/VAAPI-Kette. Dieses Binary wird nur fuer die
# Qualitaetsmessung aufgerufen - ein kaputter Download kann den Encode-Pfad
# also nicht beschaedigen, deshalb auch hier fehlertolerant.
RUN set -eux; \
    ( curl -fL "https://github.com/BtbN/FFmpeg-Builds/releases/download/latest/ffmpeg-master-latest-linux64-gpl.tar.xz" \
        -o /tmp/ffmpeg-vmaf.tar.xz \
      && mkdir -p /tmp/ffvmaf \
      && tar -xJf /tmp/ffmpeg-vmaf.tar.xz -C /tmp/ffvmaf --strip-components=1 \
      && cp /tmp/ffvmaf/bin/ffmpeg /usr/local/bin/ffmpeg-vmaf \
      && chmod +x /usr/local/bin/ffmpeg-vmaf \
      && rm -rf /tmp/ffmpeg-vmaf.tar.xz /tmp/ffvmaf \
      && /usr/local/bin/ffmpeg-vmaf -hide_banner -filters 2>/dev/null | grep -q " libvmaf" \
      && echo "VMAF: Mess-ffmpeg installiert, libvmaf-Filter vorhanden" ) \
    || { echo "WARNUNG: VMAF-ffmpeg nicht verfuegbar - Kalibrierung bleibt deaktiviert, Encoden unbeeintraechtigt"; \
         echo "Diagnose: xz=$(command -v xz || echo FEHLT), Archiv=$(ls -la /tmp/ffmpeg-vmaf.tar.xz 2>/dev/null || echo FEHLT)"; }

WORKDIR /app
COPY app/requirements.txt .
RUN pip3 install --break-system-packages --no-cache-dir -r requirements.txt

COPY app/ .

# Zwischendateien (MP4->MKV-Remux, RPU-Extraktion, Reencode-Zwischenschritte)
# landen standardmaessig NICHT mehr im Container-eigenen /tmp - das liegt auf
# dem Cache/appdata-Laufwerk und ist bei 4K-Dateien schnell voll ("No space
# left on device"). /media/temp ist fuer eine Volume-Zuordnung auf das Array
# gedacht (siehe unraid-template.xml/docker-compose.yml) - Python's tempfile-
# Modul liest TMPDIR automatisch, kein Code muss dafuer wissen, wo das liegt.
ENV TMPDIR=/media/temp
RUN mkdir -p /media/temp

EXPOSE 8080
CMD ["python3", "app.py"]
