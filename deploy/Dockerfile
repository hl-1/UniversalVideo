FROM node:22-bookworm-slim AS frontend-build

WORKDIR /build/frontend

COPY frontend/package.json frontend/package-lock.json ./
ARG NPM_REGISTRY=https://registry.npmmirror.com
RUN --mount=type=cache,target=/root/.npm \
    npm ci --registry="$NPM_REGISTRY" --replace-registry-host=always \
      --fetch-retries=5 --fetch-timeout=60000

COPY frontend/ ./
RUN npm run build


FROM python:3.12-slim-bookworm AS runtime

ARG INSTALL_INPAINTING=false

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PLAYWRIGHT_BROWSERS_PATH=/ms-playwright \
    VIDEODREAM_NODE_EXE=/usr/local/bin/node

WORKDIR /app

# Keep Debian's suites and signing keys; use the mirror measured on the cloud host.
# Playwright's system dependency installer will use these sources as well.
RUN sed -i \
      -e 's|http://deb.debian.org|https://mirrors.aliyun.com|g' \
      -e 's|https://deb.debian.org|https://mirrors.aliyun.com|g' \
      /etc/apt/sources.list.d/debian.sources \
    && apt-get update \
    && apt-get install -y --no-install-recommends \
       ca-certificates ffmpeg libstdc++6 libatomic1 libgomp1 tini \
    && rm -rf /var/lib/apt/lists/*

# Both stages use Debian Bookworm so the Node executable shares the same libc.
COPY --from=frontend-build /usr/local/bin/node /usr/local/bin/node

COPY backend/requirements*.txt ./backend/
# Declare the Python mirror here to preserve the Debian installation cache.
ARG PIP_INDEX_URL=https://pypi.tuna.tsinghua.edu.cn/simple
RUN --mount=type=cache,target=/root/.cache/pip \
    python -m pip install \
      --index-url "$PIP_INDEX_URL" --timeout 60 --retries 5 \
      -r backend/requirements.txt \
    && if [ "$INSTALL_INPAINTING" = "true" ]; then \
         python -m pip install \
           --index-url "$PIP_INDEX_URL" --timeout 60 --retries 5 \
           -r backend/requirements-inpainting.txt; \
       fi

# Cache Python and system dependencies independently of the browser download.
RUN printf '%s\n' \
      'Acquire::Retries "3";' \
      'Acquire::http::Timeout "60";' \
      'Acquire::https::Timeout "60";' \
      > /etc/apt/apt.conf.d/80videodream-downloads \
    && python -m playwright install-deps chromium \
    && rm -rf /var/lib/apt/lists/*

# Keep browser source changes independent of Python and system dependency layers.
ARG PLAYWRIGHT_DOWNLOAD_HOST=https://npmmirror.com/mirrors/playwright
ARG PLAYWRIGHT_DOWNLOAD_CONNECTION_TIMEOUT=120000
RUN python -m playwright install chromium \
    && chmod -R a+rX /ms-playwright

RUN groupadd --gid 10001 videodream \
    && useradd --uid 10001 --gid 10001 --create-home videodream

COPY --chown=10001:10001 backend/ ./backend/
COPY --chown=10001:10001 scripts/ ./scripts/
COPY --chown=10001:10001 config/flyway.conf ./config/flyway.conf
COPY --chown=10001:10001 deploy/sql/ ./deploy/sql/
COPY --chown=10001:10001 frontend/public/ ./frontend/public/
COPY --from=frontend-build --chown=10001:10001 /build/frontend/dist/ ./frontend/dist/

RUN mkdir -p /app/downloads /app/.run /app/config \
    && chown -R 10001:10001 /app/downloads /app/.run /app/config

USER 10001:10001

EXPOSE 8001

ENTRYPOINT ["/usr/bin/tini", "--"]

# Only the shared Docker networks can reach the app; the edge Nginx sets these headers.
CMD ["python", "-m", "uvicorn", "backend.main:app", "--host", "0.0.0.0", "--port", "8001", "--workers", "1", "--proxy-headers", "--forwarded-allow-ips", "*"]
