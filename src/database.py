import os
from dotenv import load_dotenv
from sqlalchemy import bindparam, create_engine, text

load_dotenv()

DATABASE_URL = os.environ["DATABASE_URL"]

engine = create_engine(DATABASE_URL, pool_pre_ping=True)


def init_db() -> None:
    with engine.begin() as conn:
        conn.execute(text("""
            DO $$ BEGIN
                CREATE TYPE application_status AS ENUM (
                    'new', 'applied', 'phone_screen', 'interview',
                    'offer', 'rejected', 'ghosted'
                );
            EXCEPTION WHEN duplicate_object THEN NULL;
            END $$;
        """))
        conn.execute(text("""
            CREATE TABLE IF NOT EXISTS jobs (
                id              SERIAL PRIMARY KEY,
                title           TEXT NOT NULL,
                company         TEXT NOT NULL,
                location        TEXT,
                date_posted     DATE,
                relevance_score FLOAT,
                job_url         TEXT UNIQUE NOT NULL,
                description     TEXT,
                flagged         BOOLEAN NOT NULL DEFAULT FALSE,
                entry_level     BOOLEAN NOT NULL DEFAULT FALSE,
                experience_req  TEXT,
                status          application_status NOT NULL DEFAULT 'new',
                notes           TEXT,
                source_file     TEXT,
                ingested_at     TIMESTAMPTZ DEFAULT now(),
                applied_at      TIMESTAMPTZ,
                updated_at      TIMESTAMPTZ DEFAULT now()
            );
        """))
        # auto-update updated_at on row change
        conn.execute(text("""
            CREATE OR REPLACE FUNCTION set_updated_at()
            RETURNS TRIGGER AS $$
            BEGIN
                NEW.updated_at = now();
                RETURN NEW;
            END;
            $$ LANGUAGE plpgsql;
        """))
        conn.execute(text("""
            ALTER TABLE jobs ADD COLUMN IF NOT EXISTS flagged BOOLEAN NOT NULL DEFAULT FALSE;
        """))
        conn.execute(text("""
            ALTER TABLE jobs ADD COLUMN IF NOT EXISTS entry_level BOOLEAN NOT NULL DEFAULT FALSE;
        """))
        conn.execute(text("""
            ALTER TABLE jobs ADD COLUMN IF NOT EXISTS experience_req TEXT;
        """))
        conn.execute(text("""
            DO $$ BEGIN
                CREATE TRIGGER jobs_updated_at
                BEFORE UPDATE ON jobs
                FOR EACH ROW EXECUTE FUNCTION set_updated_at();
            EXCEPTION WHEN duplicate_object THEN NULL;
            END $$;
        """))
    # ALTER TYPE ADD VALUE cannot run inside a transaction block (PG < 12)
    with engine.connect().execution_options(isolation_level="AUTOCOMMIT") as conn:
        conn.execute(text(
            "ALTER TYPE application_status ADD VALUE IF NOT EXISTS 'considering';"
        ))


ACTIVE_STATUSES = ("applied", "phone_screen", "interview", "offer")


def prune_old_listings(days: int = 15) -> int:
    """Delete listings whose date_posted is older than `days` days, skipping active applications."""
    with engine.begin() as conn:
        result = conn.execute(
            text("""
                DELETE FROM jobs
                WHERE date_posted < CURRENT_DATE - (:days || ' days')::interval
                  AND status NOT IN :protected
            """),
            {"days": days, "protected": ACTIVE_STATUSES},
        )
        return result.rowcount


def bulk_update_status(job_ids: list[int], new_status: str) -> None:
    if not job_ids:
        return
    with engine.begin() as conn:
        conn.execute(
            text("UPDATE jobs SET status = :status WHERE id = ANY(:ids)"),
            {"status": new_status, "ids": job_ids},
        )


if __name__ == "__main__":
    init_db()
    print("Database initialised.")
