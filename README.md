# Jobs Tracker

A Streamlit dashboard that ingests job listings from CSV files and tracks application status in Postgres.

## First-time setup

1. Copy the env template and fill in your credentials:
   ```bash
   cp .env.example .env
   ```

2. Start the database services:
   ```bash
   docker compose up -d
   ```

3. Install dependencies:
   ```bash
   pip install uv
   uv sync
   ```

## Running the app

```bash
uv run streamlit run src/app.py      # dashboard at localhost:8501
uv run python -m src.watcher         # watches downloads/ for new CSVs
```

Start the watcher first if you have CSVs ready — it processes any existing files in `downloads/` on startup before switching to live watching.

## Adding jobs

Drop a `.csv` file into `downloads/`. The watcher picks it up within a couple of seconds and ingests it into Postgres. Required columns:

| Column | Notes |
|---|---|
| `title` | job title |
| `company` | |
| `location` | optional |
| `date_posted` | any pandas-parseable date |
| `relevance_score` | 0–100 float |
| `job_url` | unique key — re-dropping the same file updates scores, not duplicate rows |

Optional columns: `description`, `flagged` (bool), `entry_level` (bool), `experience_req` (text).

## Services

| Service | URL | Credentials |
|---|---|---|
| Streamlit dashboard | localhost:8501 | — |
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
