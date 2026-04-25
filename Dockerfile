# Stage: build wheels / install deps
FROM python:3.14-alpine AS builder

WORKDIR /app

# Build-time toolchain + dev headers for any C extensions that lack a
# musllinux wheel. None of the current pure-Python deps require this, but
# keeping the toolchain here avoids surprises when adding new dependencies.
RUN apk add --no-cache \
    build-base \
    libffi-dev \
    openssl-dev \
    zstd-dev

COPY app/requirements.txt /app/requirements.txt
RUN pip install --upgrade pip \
    && pip install --prefix=/install -r requirements.txt

# ----------------------------------------------------
# Stage: test image (contains app + deps + pytest)
FROM python:3.14-alpine AS test

WORKDIR /app

# Runtime toolchain for tests:
#   - bash: rename_volume / integration tests rely on `bash -c "set -euo pipefail"`
#   - tar:  GNU tar (BusyBox tar lacks --format=pax which DVM uses for backups)
#   - par2cmdline: PAR2 recovery / verification (Alpine community repo)
#   - gnupg + zstd + pigz: compressors + crypto used end-to-end.
#     Backup formats are intentionally limited to gz / zstd; pigz is
#     auto-detected as a multi-threaded drop-in for gzip in the encrypt
#     pipeline. zstd is the recommended default (level 19, all cores).
RUN apk add --no-cache \
    bash \
    tar \
    gnupg \
    par2cmdline \
    zstd \
    pigz

COPY --from=builder /install /usr/local
COPY app /app

# pytest only in the test image — keeps runtime stage minimal
RUN pip install --no-cache-dir pytest==8.4.2 pytest-cov==6.0.0

ENV PYTHONPATH=/app

CMD ["pytest", "/app/tests", "-q", "--disable-warnings"]
# ----------------------------------------------------
# Stage: production runtime image
FROM python:3.14-alpine AS runtime

WORKDIR /app

# Default timezone for logs; override at runtime with `docker run -e TZ=...`.
ENV TZ=Europe/Madrid

# Same set of system packages as the test stage minus pytest. tzdata is needed
# for the TZ env var to take effect; `cp` from /usr/share/zoneinfo handles
# the localtime symlink on Alpine.
RUN apk add --no-cache \
    bash \
    curl \
    tar \
    gnupg \
    par2cmdline \
    zstd \
    pigz \
    tzdata \
    && cp /usr/share/zoneinfo/${TZ} /etc/localtime \
    && echo ${TZ} > /etc/timezone

COPY --from=builder /install /usr/local
COPY app /app

ENV PYTHONPATH=/app

# Healthcheck for container orchestration (only for runtime)
HEALTHCHECK --interval=30s --timeout=5s --start-period=5s --retries=3 \
  CMD python /app/health_check.py || exit 1

# Production entrypoint
CMD ["python", "main.py"]
