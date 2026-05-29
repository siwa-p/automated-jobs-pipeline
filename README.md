# Jobs Tracker

A FastAPI + HTMX dashboard that ingests job listings from CSV files and tracks application status in Postgres.

## First-time setup

1. Copy the env template and fill in your credentials:
   ```bash
   cp .env.example .env
   ```

2. Start all services (Postgres, pgAdmin, and the web app):
   ```bash
   docker compose up -d
   ```

3. Start the file watcher in a persistent tmux session:
   ```bash
   tmux new -s watcher
   uv run python -m src.watcher
   # Ctrl+B then D to detach
   ```

To reattach to the watcher later:
```bash
tmux attach -t watcher
```

## Accessing the dashboard

From your local machine, open an SSH tunnel:
```bash
ssh -L 8000:localhost:8000 siwa@your-server-ip
```

Then open `http://localhost:8000` in your browser.

## Adding jobs from an email

When you receive an email with a jobs CSV attachment:

1. Save the CSV to your local machine
2. Upload it to the server:
   ```bash
   scp jobs.csv siwa@your-server-ip:~/automated-jobs-pipeline/downloads/
   ```
3. The watcher picks it up within a few seconds and ingests it into Postgres — no further action needed.

For multiple files at once:
```bash
rsync -av *.csv siwa@your-server-ip:~/automated-jobs-pipeline/downloads/
```

## CSV format

Required columns:

| Column | Notes |
|---|---|
| `title` | job title |
| `company` | |
| `location` | |
| `date_posted` | any pandas-parseable date |
| `relevance_score` | 0–100 float |
| `job_url` | unique key — re-dropping the same file updates scores, not duplicate rows |

Optional columns: `description`, `flagged` (bool), `entry_level` (bool), `experience_req` (text), `llm_rating` (int 1–9), `llm_reason` (text explanation from LLM).

## Services

| Service | URL | Credentials |
|---|---|---|
| Jobs dashboard | localhost:8000 | — |
| pgAdmin | localhost:5051 | `PGADMIN_DEFAULT_EMAIL` / `PGADMIN_DEFAULT_PASSWORD` from `.env` |
| Postgres | localhost:5432 | `POSTGRES_USER` / `POSTGRES_PASSWORD` from `.env` |

### Connecting Postgres to pgAdmin

After logging into pgAdmin, register a new server with these parameters:

| Field | Value |
|---|---|
| Host | `postgres` |
| Port | `5432` |
| Database | `jobs_tracker` |
| Username | `POSTGRES_USER` from `.env` |
| Password | `POSTGRES_PASSWORD` from `.env` |

Use `postgres` (the Docker service name) as the host — pgAdmin connects to Postgres over the internal Docker network.
