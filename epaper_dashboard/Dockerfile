ARG BUILD_FROM=ghcr.io/home-assistant/base-debian:trixie
FROM ${BUILD_FROM}

# Chromium draws the page; Python does everything else (Debian packages only, no pip)
RUN apt-get update \
 && apt-get install -y --no-install-recommends \
      chromium fontconfig fonts-dejavu-core tzdata \
      python3 python3-pil python3-numpy python3-jinja2 \
 && rm -rf /var/lib/apt/lists/*

COPY app /app
COPY run.sh /run.sh
RUN chmod a+x /run.sh

CMD ["/run.sh"]

ARG BUILD_ARCH
ARG BUILD_VERSION
LABEL \
  io.hass.version="${BUILD_VERSION}" \
  io.hass.type="app" \
  io.hass.arch="${BUILD_ARCH}"
