# DocTheCode in one image: web app + API + a built-in PostgreSQL that is created on first start.
#
#   docker build -t docthecode .
#   docker run -d -p 8080:8080 -v docthecode-data:/data docthecode
#
# Then open the address in a browser; everything else is set up there. See README → Self-host DocTheCode.

# ---- 1. Build the React app
FROM node:22-alpine AS web
WORKDIR /web
COPY frontend/package.json ./
RUN npm install --no-audit --no-fund
COPY frontend/ ./
RUN npm run build

# ---- 2. Runtime: Python + PostgreSQL 17 + git
FROM python:3.13-slim-trixie
# Don't let the Debian package create its own default cluster; DocTheCode creates one in /data on first start.
RUN mkdir -p /etc/postgresql-common \
    && echo "create_main_cluster = false" > /etc/postgresql-common/createcluster.conf \
    && apt-get update \
    && apt-get install -y --no-install-recommends postgresql-17 git \
    && rm -rf /var/lib/apt/lists/*
WORKDIR /srv/backend
COPY backend/requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt
COPY backend/app ./app
COPY --from=web /web/dist /srv/frontend/dist
COPY docker/entrypoint.sh /usr/local/bin/docthecode-entrypoint
RUN chmod +x /usr/local/bin/docthecode-entrypoint

# Everything DocTheCode stores lives in /data: the database, doc repos and the auto-generated secret key.
# Give the container a persistent volume there. (Hosts that wipe the container's disk on restart:
# mount a bucket or network folder at /backup instead, see README.)
ENV DOCTHECODE_DATA_DIR=/data \
    PYTHONUNBUFFERED=1
VOLUME ["/data"]
EXPOSE 8080
HEALTHCHECK --interval=30s --timeout=5s --start-period=60s \
    CMD python -c "import os,urllib.request; urllib.request.urlopen(f'http://127.0.0.1:{os.environ.get(\"PORT\", \"8080\")}/api/health', timeout=4)"

ENTRYPOINT ["docthecode-entrypoint"]
# Listens on $PORT if the host sets one, otherwise 8080. One worker: background jobs run in-process.
CMD ["sh", "-c", "exec uvicorn app.main:app --host 0.0.0.0 --port ${PORT:-8080} --proxy-headers --forwarded-allow-ips='*' --workers 1 --timeout-graceful-shutdown 8"]
