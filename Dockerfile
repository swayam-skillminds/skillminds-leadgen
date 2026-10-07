# Server mode (APP-BUILD-PROMPT Phase 3.5): one image, two roles.
#   docker compose up            -> the API (port 8000) + a worker (+ optional Postgres)
# The same image runs `leadscraper api` or `leadscraper worker`; see docker-compose.yml
# and docs/deploy.md.
FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    # All state lives on the /data volume: database (SQLite mode), caches, exports.
    LEADSCRAPER_DATA_DIR=/data \
    LEADSCRAPER_MODE=server \
    # A server never sleeps on its own; there is no keep-awake to hold.
    LEADSCRAPER_KEEP_AWAKE=0 \
    # The OS keychain does not exist in a container: secrets come from env / /run/secrets.
    LEADSCRAPER_NO_KEYRING=1 \
    PLAYWRIGHT_BROWSERS_PATH=/ms-playwright

WORKDIR /app

# Dependencies first, so code changes don't reinstall Chromium.
COPY pyproject.toml README.md ./
COPY leadscraper/__init__.py leadscraper/__init__.py
RUN pip install --upgrade pip \
 && pip install -e ".[postgres]" \
 && python -m playwright install --with-deps chromium \
 && rm -rf /var/lib/apt/lists/*

# The application: an editable install, so presets/ resolve next to the package.
COPY leadscraper ./leadscraper
COPY presets ./presets
COPY run.py ./

RUN useradd --create-home --uid 1000 leads \
 && mkdir -p /data \
 && chown -R leads /data
USER leads
VOLUME ["/data"]
EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=10s --start-period=30s --retries=3 \
  CMD python -c "import sys, urllib.request; r = urllib.request.urlopen('http://127.0.0.1:8000/health', timeout=8); sys.exit(0 if r.status == 200 else 1)"

CMD ["python", "-m", "leadscraper", "api", "--host", "0.0.0.0", "--port", "8000"]
