# Edit OS API + in-process workers, with the review screen built in.
#   docker build -t edit-os .
#   docker run -p 8000:8000 -e EDITOS_API_KEYS=... -v editos-data:/data edit-os
# The same image runs a standalone worker: `python -m server.worker JOB_ID` (or an SQS consumer later).

FROM node:22.12-bookworm-slim AS web
WORKDIR /web
COPY web/package.json web/package-lock.json ./
RUN npm ci
COPY web/ ./
RUN npm run build

FROM python:3.12-slim-bookworm
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 \
    EDITOS_DATA_DIR=/data EDITOS_CACHE=/data/cache EDIT_OS_DATA=/data/cache \
    EDITOS_WEB_DIR=/app/web/dist
WORKDIR /app
COPY pyproject.toml README.md ./
COPY edit_os/ edit_os/
RUN pip install --no-cache-dir '.[server]'
COPY server/ server/
COPY --from=web /web/dist web/dist
RUN useradd --system --uid 10001 editos && mkdir -p /data && chown editos /data
USER editos
VOLUME /data
EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=5s CMD python -c "import urllib.request;urllib.request.urlopen('http://127.0.0.1:8000/api/health')"
CMD ["uvicorn", "--factory", "server.app:create_app", "--host", "0.0.0.0", "--port", "8000", "--proxy-headers"]
