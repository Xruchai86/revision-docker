FROM ubuntu:24.04

ENV DEBIAN_FRONTEND=noninteractive

# ffmpeg (Ubuntu 24.04 bringt QSV/VAAPI-Unterstuetzung bereits mit),
# intel-media-va-driver-non-free (iHD-Treiber fuer Core-Ultra/Xe-Grafik),
# mkvtoolnix (mkvmerge) und mediainfo fuer die Profilerkennung.
RUN apt-get update && apt-get install -y --no-install-recommends \
    ffmpeg \
    mkvtoolnix \
    mediainfo \
    intel-media-va-driver-non-free \
    vainfo \
    python3 python3-pip \
    curl ca-certificates \
    && rm -rf /var/lib/apt/lists/*

# dovi_tool - offizielles Release-Binary, fest auf eine geprüfte Version gepinnt
# (nicht "latest", da dessen URL-Muster den Dateinamen inkl. Versionsnummer braucht -
# ein "latest" bei sich aenderndem Dateinamen wuerde spaeter einfach 404en). Zum
# Aktualisieren: neue Version unter https://github.com/quietvoid/dovi_tool/releases
# nachsehen und hier ersetzen.
ARG DOVI_TOOL_VERSION=2.3.3
RUN curl -kL "https://github.com/quietvoid/dovi_tool/releases/download/${DOVI_TOOL_VERSION}/dovi_tool-${DOVI_TOOL_VERSION}-x86_64-unknown-linux-musl.tar.gz" \
    -o /tmp/dovi_tool.tar.gz \
    && tar -xzf /tmp/dovi_tool.tar.gz -C /usr/local/bin dovi_tool \
    && rm /tmp/dovi_tool.tar.gz \
    && chmod +x /usr/local/bin/dovi_tool

WORKDIR /app
COPY app/requirements.txt .
RUN pip3 install --break-system-packages --no-cache-dir -r requirements.txt

COPY app/ .

EXPOSE 8080
CMD ["python3", "app.py"]
