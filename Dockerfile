# syntax=docker/dockerfile:1
# Two targets: backend (API + independent workers) and frontend (static nginx).
FROM node:22-alpine AS frontend-build
WORKDIR /build/frontend
COPY frontend/package.json frontend/package-lock.json ./
RUN npm ci
COPY frontend/ ./
RUN npm run build

FROM python:3.11-slim AS backend
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    MK_ARTIFACT_ROOT=/data/artifacts \
    MK_ENABLE_LIVE=0 \
    MK_DEMO=0
WORKDIR /app
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt \
    && groupadd --gid 10001 mk \
    && useradd --uid 10001 --gid 10001 --no-create-home mk \
    && mkdir -p /data \
    && chown 10001:10001 /data
COPY --chown=10001:10001 backend/ ./backend/
COPY --chown=10001:10001 docs/ ./docs/
USER 10001:10001
EXPOSE 8765
HEALTHCHECK --interval=15s --timeout=5s --start-period=20s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8765/healthz', timeout=3)" || exit 1
CMD ["python", "-m", "uvicorn", "backend.app:app", "--host", "0.0.0.0", "--port", "8765", "--proxy-headers", "--forwarded-allow-ips", "*"]

FROM nginx:stable-alpine AS frontend
COPY deploy/nginx.conf /etc/nginx/nginx.conf
COPY --from=frontend-build /build/frontend/dist /usr/share/nginx/html
# Run nginx on an unprivileged port without the image's root entrypoint scripts.
USER nginx
EXPOSE 8080
ENTRYPOINT ["nginx"]
CMD ["-g", "daemon off;"]
