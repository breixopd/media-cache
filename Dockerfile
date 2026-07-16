# syntax=docker/dockerfile:1.18
FROM python:3.14-alpine3.23@sha256:b165067c5afc37fa5608a3c05609cc3d51aafd808a30fbfd822ee594fef55ad4 AS builder

ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy
WORKDIR /app

RUN python -m pip install --no-cache-dir "uv==0.11.29"
COPY pyproject.toml uv.lock ./
RUN uv sync --locked --no-dev --no-install-project
COPY app.py ./
RUN uv sync --locked --no-dev --no-editable

FROM python:3.14-alpine3.23@sha256:b165067c5afc37fa5608a3c05609cc3d51aafd808a30fbfd822ee594fef55ad4

ARG TARGETARCH
ARG RCLONE_VERSION=v1.74.4
ARG RCLONE_SHA256_AMD64=fe435e0c36228e7c2f116a8701f01127bb1f694005fc11d1f27186c8bca4115d
ARG RCLONE_SHA256_ARM64=97685285c9ad6a0cf17d5844115d2a67245af6444db672187074bd9c358de419

LABEL org.opencontainers.image.source="https://github.com/breixopd/media-cache" \
      org.opencontainers.image.licenses="AGPL-3.0-only" \
      org.opencontainers.image.description="Playback-aware cache promotion and storage tiering service"

ENV PATH="/app/.venv/bin:${PATH}" \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

RUN apk add --no-cache ca-certificates curl tzdata unzip \
    && case "${TARGETARCH}" in \
         amd64) rclone_arch=amd64; rclone_sha="${RCLONE_SHA256_AMD64}" ;; \
         arm64) rclone_arch=arm64; rclone_sha="${RCLONE_SHA256_ARM64}" ;; \
         *) echo "Unsupported architecture: ${TARGETARCH}" >&2; exit 1 ;; \
       esac \
    && curl -fsSL "https://github.com/rclone/rclone/releases/download/${RCLONE_VERSION}/rclone-${RCLONE_VERSION}-linux-${rclone_arch}.zip" -o /tmp/rclone.zip \
    && echo "${rclone_sha}  /tmp/rclone.zip" | sha256sum -c - \
    && unzip /tmp/rclone.zip -d /tmp \
    && install -m 0755 "/tmp/rclone-${RCLONE_VERSION}-linux-${rclone_arch}/rclone" /usr/local/bin/rclone \
    && rm -rf /tmp/rclone.zip "/tmp/rclone-${RCLONE_VERSION}-linux-${rclone_arch}" \
    && apk del unzip

WORKDIR /app
COPY --from=builder /app/.venv /app/.venv
COPY app.py ./

VOLUME ["/state"]
EXPOSE 8686
HEALTHCHECK --interval=30s --timeout=5s --retries=3 \
    CMD curl -fsS http://localhost:8686/health >/dev/null || exit 1
CMD ["gunicorn", "--bind", "0.0.0.0:8686", "--workers", "1", "--threads", "8", "--timeout", "120", "app:app"]
