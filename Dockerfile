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
# erreichbar ist oder Paketnamen aendert: VAAPI ist der Standard-Pfad und haengt
# NICHT an Intels Repo. Nur QSV wuerde dann auf der alten Runtime bleiben.
RUN apt-get update && apt-get install -y --no-install-recommends \
    ffmpeg \
    mkvtoolnix \
    mediainfo \
    intel-media-va-driver-non-free \
    libmfx1 \
    libmfx-gen1.2 \
    libvpl2 \
    va-driver-all \
    vainfo \
    python3 python3-pip \
    gnupg ca-certificates curl \
    && rm -rf /var/lib/apt/lists/*

# Schritt 2 - aktuelle oneVPL/QSV-Laufzeit aus Intels offiziellem Client-Repo.
# Absichtlich fehlertolerant ("|| true" am Ende): Intels beide Doku-Seiten nennen
# UNTERSCHIEDLICHE Paketnamen (PPA-Doku "libmfx-gen1.2", Repo-Doku "libmfx-gen1"),
# deshalb werden beide Varianten nacheinander probiert. Schlaegt der ganze Schritt
# fehl, bleibt das Image mit der Ubuntu-Runtime funktionsfaehig - VAAPI laeuft
# weiter, nur die experimentellen QSV-Presets haetten dann keine neue Runtime.
RUN set -eux; \
    ( curl -fsSL https://repositories.intel.com/gpu/intel-graphics.key \
        | gpg --dearmor -o /usr/share/keyrings/intel-graphics.gpg \
      && echo "deb [arch=amd64 signed-by=/usr/share/keyrings/intel-graphics.gpg] https://repositories.intel.com/gpu/ubuntu noble client" \
        > /etc/apt/sources.list.d/intel-gpu-noble.list \
      && apt-get update \
      && ( apt-get install -y --no-install-recommends libmfx-gen1.2 libvpl2 libvpl-tools \
           || apt-get install -y --no-install-recommends libmfx-gen1 libvpl2 libvpl-tools \
           || apt-get install -y --no-install-recommends libvpl2 ) \
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
