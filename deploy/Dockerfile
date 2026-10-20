FROM node:22-bookworm-slim AS frontend-build

WORKDIR /build/frontend

COPY frontend/package.json frontend/package-lock.json ./
RUN npm ci

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

RUN apt-get update \
    && apt-get install -y --no-install-recommends \
       ca-certificates ffmpeg libstdc++6 libatomic1 libgomp1 tini \
    && rm -rf /var/lib/apt/lists/*

# Both stages use Debian Bookworm so the Node executable shares the same libc.
COPY --from=frontend-build /usr/local/bin/node /usr/local/bin/node

COPY backend/requirements*.txt ./backend/
RUN python -m pip install --no-cache-dir -r backend/requirements.txt \
    && if [ "$INSTALL_INPAINTING" = "true" ]; then \
         python -m pip install --no-cache-dir -r backend/requirements-inpainting.txt; \
       fi \
    && python -m playwright install --with-deps chromium \
    && chmod -R a+rX /ms-playwright \
    && rm -rf /var/lib/apt/lists/*

RUN groupadd --gid 10001 videodream \
    && useradd --uid 10001 --gid 10001 --create-home videodream

COPY --chown=10001:10001 backend/ ./backend/
COPY --chown=10001:10001 scripts/ ./scripts/
COPY --chown=10001:10001 frontend/public/ ./frontend/public/
COPY --from=frontend-build --chown=10001:10001 /build/frontend/dist/ ./frontend/dist/

RUN mkdir -p /app/downloads /app/.run /app/config \
    && chown -R 10001:10001 /app/downloads /app/.run /app/config

USER 10001:10001

EXPOSE 8001

ENTRYPOINT ["/usr/bin/tini", "--"]

# Only the shared Docker networks can reach the app; the edge Nginx sets these headers.
CMD ["python", "-m", "uvicorn", "backend.main:app", "--host", "0.0.0.0", "--port", "8001", "--workers", "1", "--proxy-headers", "--forwarded-allow-ips", "*"]
