# Debian trixie is used because its official package repository provides rsgain
# for amd64, armhf, and arm64. The application never substitutes another analyzer.
FROM python:3.13-slim-trixie AS builder

WORKDIR /build
ENV VIRTUAL_ENV=/opt/venv
RUN apt-get update \
    && apt-get install -y --no-install-recommends build-essential cargo rustc pkg-config libffi-dev \
    && rm -rf /var/lib/apt/lists/* \
    && python -m venv "$VIRTUAL_ENV"
ENV PATH="$VIRTUAL_ENV/bin:$PATH"

COPY pyproject.toml README.md ./
COPY app ./app
COPY alembic.ini ./alembic.ini
COPY alembic ./alembic
RUN pip install --no-cache-dir --upgrade pip \
    && pip install --no-cache-dir . \
    && rm -rf "$VIRTUAL_ENV"/bin/pip* \
              "$VIRTUAL_ENV"/lib/python3.13/site-packages/pip \
              "$VIRTUAL_ENV"/lib/python3.13/site-packages/pip-*.dist-info

FROM python:3.13-slim-trixie

ARG OCI_REVISION=unknown
ARG OCI_VERSION=0.1.0
LABEL org.opencontainers.image.title="ReplayGain Watcher" \
      org.opencontainers.image.description="Safe, self-hosted ReplayGain monitoring for mounted music libraries" \
      org.opencontainers.image.source="https://github.com/OWNER/replaygain-watcher" \
      org.opencontainers.image.revision="$OCI_REVISION" \
      org.opencontainers.image.version="$OCI_VERSION" \
      org.opencontainers.image.licenses="MIT"

RUN apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates ffmpeg rsgain tini \
    && rm -rf /var/lib/apt/lists/* \
    && rm -rf /usr/local/bin/pip* /usr/local/lib/python3.13/site-packages/pip \
              /usr/local/lib/python3.13/site-packages/pip-*.dist-info \
    && groupadd --gid 1000 app \
    && useradd --uid 1000 --gid app --create-home --shell /usr/sbin/nologin app \
    && mkdir -p /data /app \
    && chown -R app:app /data /app

WORKDIR /app
COPY --from=builder /opt/venv /opt/venv
COPY --chown=app:app app ./app
COPY --chown=app:app alembic.ini ./alembic.ini
COPY --chown=app:app alembic ./alembic

ENV PATH="/opt/venv/bin:$PATH" \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    DATABASE_URL="sqlite+aiosqlite:////data/replaygain-watcher.db" \
    CONFIG_FILE="/app/config.yml"

VOLUME ["/data"]
EXPOSE 8080
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8080/health/ready')"

USER app
ENTRYPOINT ["/usr/bin/tini", "--"]
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8080"]
