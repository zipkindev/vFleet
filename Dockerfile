# syntax=docker/dockerfile:1.7

FROM node:26-bookworm-slim@sha256:662933cf47f013bc8e4beb31a6116448427a82057ba7c42c97e4c5ba766504c2 AS frontend
WORKDIR /build/frontend
COPY frontend/package.json frontend/package-lock.json ./
RUN npm ci
COPY frontend/ ./
RUN npm run build

FROM python:3.12-slim-bookworm@sha256:392307d22300de8b5986851a12d9176dfc0fc073e65bf6523ebd7dcbeb23564e AS runtime

# Apply Debian security fixes newer than the pinned base image.
RUN apt-get update \
    && apt-get install --yes --no-install-recommends --only-upgrade libpcre2-8-0 perl-base \
    && rm -rf /var/lib/apt/lists/*

ARG VERSION=dev
ARG VCS_REF=unknown
LABEL org.opencontainers.image.title="vFleet" \
      org.opencontainers.image.description="Local-first VMware vCenter and ESXi operator console" \
      org.opencontainers.image.url="https://github.com/zipkindev/vFleet" \
      org.opencontainers.image.source="https://github.com/zipkindev/vFleet" \
      org.opencontainers.image.documentation="https://github.com/zipkindev/vFleet#readme" \
      org.opencontainers.image.version="${VERSION}" \
      org.opencontainers.image.revision="${VCS_REF}" \
      org.opencontainers.image.licenses="AGPL-3.0-or-later"

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    APP_MODE=auto \
    DATA_DIR=/var/lib/vfleet \
    VFLEET_MASTER_KEY_FILE=/var/lib/vfleet-key/credential.key \
    VFLEET_RUNTIME=container

WORKDIR /opt/vfleet
COPY backend/requirements.txt /tmp/requirements.txt
RUN python -m pip install --no-cache-dir -r /tmp/requirements.txt \
    && python -m pip uninstall --yes pip \
    && groupadd --gid 10001 vfleet \
    && useradd --uid 10001 --gid vfleet --create-home --home-dir /home/vfleet --shell /usr/sbin/nologin vfleet \
    && install -d -o vfleet -g vfleet -m 0700 /var/lib/vfleet /var/lib/vfleet-key

COPY --chown=vfleet:vfleet backend/app/ /opt/vfleet/backend/app/
COPY --chown=vfleet:vfleet --from=frontend /build/frontend/dist/ /opt/vfleet/frontend/dist/
COPY --chown=vfleet:vfleet CHANGELOG.md VERSION LICENSE /opt/vfleet/
COPY --chown=root:root docker/entrypoint.sh /usr/local/bin/vfleet-container
RUN chmod 0755 /usr/local/bin/vfleet-container

USER 10001:10001
EXPOSE 8080
VOLUME ["/var/lib/vfleet", "/var/lib/vfleet-key"]
HEALTHCHECK --interval=15s --timeout=5s --start-period=20s --retries=5 \
  CMD ["python", "-c", "import json,urllib.request; r=json.load(urllib.request.urlopen('http://127.0.0.1:8080/api/health', timeout=3)); assert r.get('ok') is True"]

ENTRYPOINT ["/usr/local/bin/vfleet-container"]
