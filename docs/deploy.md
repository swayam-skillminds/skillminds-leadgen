# Deployment

The tool runs in two modes, with the same code:

| | Local mode | Server mode |
|---|---|---|
| Where | A laptop or desktop (macOS, Windows, Linux) | An always-on machine, e.g. a small Linux VPS |
| Start | `leadscraper api` + `leadscraper worker` (or just `python run.py`) | `docker compose up -d` |
| Database | SQLite in the per-user data directory | SQLite on a Docker volume, or Postgres |
| Unattended runs | Only while the machine is awake (keep-awake helps; a closed laptop lid on battery still sleeps) | Yes: this is what server mode is for |
| Secrets | OS keychain (`leadscraper secrets set NAME`), or environment variables | Environment variables, or files mounted under `/run/secrets` |

Every device then uses the server's URL: the API docs at `/api/v1/docs`, and
later the UI. Configs and projects move between machines in **bundles**
(`leadscraper bundle export|import`, `GET/POST /api/v1/bundle/*`).

## Where state lives

Everything local lives under one directory: the `platformdirs` user data directory, or
`LEADSCRAPER_DATA_DIR` when set.

| OS | Default |
|---|---|
| macOS | `~/Library/Application Support/leadscraper` |
| Windows | `%LOCALAPPDATA%\Skillminds\leadscraper` |
| Linux | `~/.local/share/leadscraper` |
| Docker image | `/data` (the `leadscraper-data` volume) |

It holds:
- `leadscraper.sqlite3`, the database, in SQLite mode;
- `cache/` (geocoding, AI responses);
- `projects/<slug>/` (each project's CSV and logs);
- `presets/` (presets imported from bundles);
- `legacy-copies/`.

`LEADSCRAPER_DB_URL` points the application at another database instead, e.g. Postgres.

## Server mode on a small Linux VPS

Anything with 2 vCPUs, 2–4 GB RAM and 20 GB disk is enough. Chromium is the hungry part: give
it 1 GB per concurrent browser unit (`runtime.discovery_workers`, default 3).

1. **Install Docker** (Docker Engine plus the compose plugin) from docker.com's instructions for
   your distribution.
2. **Get the code** onto the server (`git clone` the private repo, or copy the tree without
   `data/`, `.env` or credentials).
3. **Configure** an `.env` next to `docker-compose.yml`. It is gitignored; never commit it.

   ```bash
   # SQLite on the volume is the default. For Postgres:
   POSTGRES_PASSWORD=<a long random password>
   LEADSCRAPER_DB_URL=postgresql+psycopg://leads:<same password>@postgres:5432/leads
   # Optional:
   # AI provider keys (optional; docs/ai.md). Named secrets, never in the database:
   LEADSCRAPER_SECRET_ANTHROPIC_API_KEY=...
   LEADSCRAPER_SECRET_OPENAI_API_KEY=...
   LEADSCRAPER_SECRET_GEMINI_API_KEY=...
   # (or mount files /run/secrets/anthropic-api-key ...; then pick the chain with
   #  PATCH /api/v1/settings {"ai": {"providers": ["anthropic", "openai"]}})
   GOOGLE_SERVICE_ACCOUNT_JSON=/run/secrets/google.json
   LEADSCRAPER_CORS_ORIGINS=https://leads.example.com   # once a UI exists
   LEADSCRAPER_ALLOWED_HOSTS=localhost,127.0.0.1,leads.example.com   # names the API answers to
   ```
4. **Start it**:

   ```bash
   docker compose up -d                       # SQLite
   docker compose --profile postgres up -d    # with Postgres
   docker compose ps                          # api: healthy; worker: running
   curl -s http://127.0.0.1:8000/api/v1/health | python3 -m json.tool
   ```
   The API container creates or migrates the schema on start. The worker starts once the API is healthy.

5. **Put access control in front of it.** The API has **no login yet** (A.2.2), so it binds to
   `127.0.0.1` on the server by default. To use it from other devices:
   - **Recommended:** a private network, such as Tailscale or WireGuard. Set `LEADSCRAPER_BIND` to the
     server's private-network address, and only your devices can reach it.
   - **Or** a reverse proxy on the same host with TLS and authentication. A Caddy example, with
     basic auth and automatic HTTPS:

     ```
     leads.example.com {
         basic_auth {
             staff <bcrypt hash from `caddy hash-password`>
         }
         reverse_proxy 127.0.0.1:8000 {
             flush_interval -1          # don't buffer Server-Sent Events
         }
     }
     ```
   Never publish port 8000 directly on a public interface.

   Two guards stay on behind the proxy too:
   - only the host names in `LEADSCRAPER_ALLOWED_HOSTS` are served (a DNS-rebinding guard; add
     your domain or VPN name);
   - a write (POST, PATCH, DELETE) from a browser page whose origin isn't an allowed host or
     in `LEADSCRAPER_CORS_ORIGINS` is refused with 403.

   Uploads are capped at `LEADSCRAPER_MAX_UPLOAD_MB` (default 200).

6. **Migrate existing data** (optional). Import an original lead store from a copy on the server:
   ```bash
   docker compose exec api python -m leadscraper migrate-legacy --config presets/mumbai-event-vendors.yaml --source /data/leads-copy.sqlite3
   ```
   Or export a bundle on the old machine and import it:
   ```bash
   leadscraper bundle export -o laptop.zip --leads --history
   curl -F file=@laptop.zip http://127.0.0.1:8000/api/v1/bundle/import
   ```

## Operations

| Task | How |
|---|---|
| Logs | `docker compose logs -f api worker` |
| Health | `GET /api/v1/health` (the container healthcheck uses it), or `docker compose exec api python -m leadscraper status` |
| Stop gracefully | `docker compose stop worker`. SIGTERM lets in-flight units finish, flushes, and re-queues the run; `stop_grace_period` is 3 minutes. `leadscraper worker stop` does the same without signals. |
| More throughput | `docker compose up -d --scale worker=2`. Workers claim runs atomically, and each runs one at a time. Mind memory: each run may start up to 3 Chromium instances. |
| Upgrade | `git pull && docker compose up -d --build`. The API migrates the schema on start (Alembic; on SQLite with foreign keys off during the migration, so no child row is lost). |
| Backups, SQLite | `docker compose exec api python -c "import sqlite3; s=sqlite3.connect('/data/leadscraper.sqlite3'); s.backup(sqlite3.connect('/data/backup.sqlite3'))"`, then copy `/data/backup.sqlite3` off the box. A bundle with `--leads --history` is a portable backup too. |
| Backups, Postgres | `docker compose exec postgres pg_dump -U leads leads > leads.sql` |
| Retention | Settings → `retention`, and per project `retention_days`. The worker purges hourly; `leadscraper purge --dry-run` shows what would go. |
| Secrets | Environment variables (`LEADSCRAPER_SECRET_<NAME>` for named secrets such as an SMTP password), or files under `/run/secrets/<name>` (Docker secrets). The keychain is disabled in the image (`LEADSCRAPER_NO_KEYRING=1`). |

## Local mode

```bash
pip install -e ".[dev]" && playwright install chromium
python -m leadscraper api            # 127.0.0.1:8000, docs at /api/v1/docs
python -m leadscraper worker         # executes queued runs; keeps the machine awake while one is active
```

The CLI works without the API (`python run.py …`, `python -m leadscraper …`): both use the same
database. Keep-awake holds only while a run is active and the machine is on. For anything
unattended, use server mode.
