import logging
from pathlib import Path

import pandas as pd
from sqlalchemy import text

from src.database import engine, init_db

logger = logging.getLogger(__name__)

EXPECTED_COLUMNS = {"title", "company", "location", "date_posted", "relevance_score", "job_url"}


def ingest(csv_path: Path) -> tuple[int, int]:
    """Load a jobs CSV into the database. Returns (inserted, updated)."""
    if csv_path.stat().st_size == 0:
        logger.info("%s: empty file, skipping", csv_path.name)
        return 0, 0
    df = pd.read_csv(csv_path)

    missing = EXPECTED_COLUMNS - set(df.columns)
    if missing:
        raise ValueError(f"{csv_path.name} is missing columns: {missing}")

    df["date_posted"] = pd.to_datetime(df["date_posted"], errors="coerce").dt.date
    df["relevance_score"] = pd.to_numeric(df["relevance_score"], errors="coerce")
    df["company"] = df["company"].fillna("")
    desc = df["description"] if "description" in df.columns else pd.Series(dtype=str)
    df["description"] = desc.where(desc.notna(), None)
    df["flagged"] = df["flagged"].fillna(False).astype(bool) if "flagged" in df.columns else False
    df["entry_level"] = df["entry_level"].fillna(False).astype(bool) if "entry_level" in df.columns else False
    exp = df["experience_req"] if "experience_req" in df.columns else pd.Series(dtype=str)
    df["experience_req"] = exp.where(exp.notna(), None)
    df["llm_rating"] = pd.to_numeric(df["llm_rating"], errors="coerce") if "llm_rating" in df.columns else None
    llm_reason = df["llm_reason"] if "llm_reason" in df.columns else pd.Series(dtype=str)
    df["llm_reason"] = llm_reason.where(llm_reason.notna(), None)
    df["source_file"] = csv_path.name

    rows = df[["title", "company", "location", "date_posted", "relevance_score",
               "job_url", "description", "flagged", "entry_level", "experience_req",
               "llm_rating", "llm_reason", "source_file"]].to_dict("records")

    # convert NaN, NaT, and None → None so psycopg2 writes NULL
    rows = [
        {k: (None if pd.isna(v) else v) for k, v in row.items()}
        for row in rows
    ]

    inserted = 0
    updated = 0

    with engine.begin() as conn:
        for row in rows:
            result = conn.execute(
                text("""
                    INSERT INTO jobs
                        (title, company, location, date_posted, relevance_score,
                         job_url, description, flagged, entry_level, experience_req,
                         llm_rating, llm_reason, source_file)
                    VALUES
                        (:title, :company, :location, :date_posted, :relevance_score,
                         :job_url, :description, :flagged, :entry_level, :experience_req,
                         :llm_rating, :llm_reason, :source_file)
                    ON CONFLICT (job_url) DO UPDATE SET
                        relevance_score = EXCLUDED.relevance_score,
                        flagged         = EXCLUDED.flagged,
                        entry_level     = EXCLUDED.entry_level,
                        experience_req  = EXCLUDED.experience_req,
                        llm_rating      = EXCLUDED.llm_rating,
                        llm_reason      = EXCLUDED.llm_reason
                    RETURNING (xmax = 0) AS is_new
                """),
                row,
            )
            if result.fetchone().is_new:
                inserted += 1
            else:
                updated += 1

    logger.info(
        "%s: inserted %d new jobs, updated %d existing",
        csv_path.name, inserted, updated,
    )
    return inserted, updated


if __name__ == "__main__":
    import sys
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    init_db()
    for path in sys.argv[1:]:
        ingest(Path(path))
