"""
Watches downloads/ for new CSV files and ingests them into Postgres.

Usage:
    python -m src.watcher
"""
import logging
import threading
import time
from pathlib import Path

from watchdog.events import FileCreatedEvent, FileModifiedEvent, FileSystemEventHandler
from watchdog.observers import Observer

from src.database import engine, init_db
from src.ingestor import ingest
from sqlalchemy import text

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger(__name__)

WATCH_DIR = Path(__file__).parent.parent / "downloads"
_DEBOUNCE_SECONDS = 2.0
_GC_INTERVAL_SECONDS = 24 * 60 * 60  # run daily
_STALE_DAYS = 14


def gc_stale_jobs() -> None:
    """Delete 'new' jobs that have been sitting unreviewed for more than _STALE_DAYS days."""
    with engine.begin() as conn:
        result = conn.execute(text("""
            DELETE FROM jobs
            WHERE status = 'new'
              AND ingested_at < now() - (:days || ' days')::interval
            RETURNING id
        """), {"days": _STALE_DAYS})
        n = result.rowcount
    if n:
        logger.info("GC: deleted %d stale 'new' job(s) older than %d days", n, _STALE_DAYS)
    else:
        logger.info("GC: nothing to clean up")


def _gc_loop() -> None:
    while True:
        try:
            gc_stale_jobs()
        except Exception:
            logger.exception("GC run failed")
        time.sleep(_GC_INTERVAL_SECONDS)


class CsvHandler(FileSystemEventHandler):
    def __init__(self):
        self._timers: dict[str, threading.Timer] = {}

    def _handle(self, event) -> None:
        path = Path(event.src_path)
        if event.is_directory or path.suffix.lower() != ".csv":
            return
        key = str(path)
        existing = self._timers.pop(key, None)
        if existing:
            existing.cancel()
        timer = threading.Timer(_DEBOUNCE_SECONDS, self._ingest, args=(path,))
        self._timers[key] = timer
        timer.start()

    def _ingest(self, path: Path) -> None:
        self._timers.pop(str(path), None)
        logger.info("Ingesting %s", path.name)
        try:
            inserted, updated = ingest(path)
            logger.info("Done — %d inserted, %d updated", inserted, updated)
            path.unlink()
            logger.info("Deleted %s", path.name)
        except Exception:
            logger.exception("Failed to ingest %s", path.name)

    def on_created(self, event: FileCreatedEvent) -> None:
        self._handle(event)

    def on_modified(self, event: FileModifiedEvent) -> None:
        self._handle(event)


def process_existing(watch_dir: Path) -> None:
    csvs = sorted(watch_dir.glob("*.csv"))
    if not csvs:
        logger.info("No existing CSVs found in %s", watch_dir)
        return
    logger.info("Processing %d existing CSV(s) on startup…", len(csvs))
    for csv in csvs:
        try:
            inserted, updated = ingest(csv)
            logger.info("%s — %d inserted, %d updated", csv.name, inserted, updated)
            csv.unlink()
            logger.info("Deleted %s", csv.name)
        except Exception:
            logger.exception("Failed to ingest %s", csv.name)


def main() -> None:
    WATCH_DIR.mkdir(exist_ok=True)
    init_db()
    process_existing(WATCH_DIR)

    # run GC once on startup, then spin up daily background thread
    gc_stale_jobs()
    gc_thread = threading.Thread(target=_gc_loop, daemon=True)
    gc_thread.start()

    observer = Observer()
    observer.schedule(CsvHandler(), path=str(WATCH_DIR), recursive=False)
    observer.start()
    logger.info("Watching %s for new CSV files. Ctrl-C to stop.", WATCH_DIR)

    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        pass
    finally:
        observer.stop()
        observer.join()


if __name__ == "__main__":
    main()
