# syntax=docker/dockerfile:1
FROM python:3.12-slim

# System dependencies required for VNC login helper
RUN apt-get update && apt-get install -y --no-install-recommends \
    xvfb x11vnc \
    ca-certificates fonts-liberation \
 && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Install Python dependencies first (better layer caching)
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Install Chromium + all its system dependencies via Playwright's own tooling
RUN playwright install --with-deps chromium

# Copy application code
COPY app.py db.py main.py scraper.py entrypoint.sh ./
COPY templates/ templates/

RUN chmod +x entrypoint.sh

# Data directory — mount a host path here to persist the DB and browser state
RUN mkdir -p /data

ENV DB_PATH=/data/usage.db
ENV BROWSER_STATE_DIR=/data/browser-state
ENV SCAN_INTERVAL=120
ENV PORT=5000

EXPOSE 5000
# VNC port — only used during `docker compose run gh-scraper login`
EXPOSE 5900

ENTRYPOINT ["./entrypoint.sh"]
