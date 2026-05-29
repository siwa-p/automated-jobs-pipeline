"""FastAPI + HTMX jobs tracker."""

import logging
import sys
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Optional

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger
from fastapi import FastAPI, Form, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import text

sys.path.insert(0, str(Path(__file__).parent))
from database import engine, init_db, prune_old_listings  # noqa: E402

log = logging.getLogger(__name__)

BASE_DIR = Path(__file__).parent.parent
templates = Jinja2Templates(directory=str(BASE_DIR / "templates"))

STATUSES = [
    "new", "considering", "applied", "phone_screen",
    "interview", "offer", "rejected", "ghosted",
]
STATUS_EMOJI = {
    "new": "🆕", "considering": "🤔", "applied": "📨",
    "phone_screen": "📞", "interview": "🤝", "offer": "🎉",
    "rejected": "❌", "ghosted": "👻",
}
TAB_DEFS = [
    ("new",          "🆕 New"),
    ("considering",  "🤔 Shortlist"),
    ("applied",      "📨 Applied"),
    ("phone_screen", "📞 Heard Back"),
    ("interview",    "🤝 Interviewing"),
    ("offer",        "🎉 Offers"),
    ("archived",     "❌ Rejected / Ghosted"),
]


@asynccontextmanager
async def lifespan(_: FastAPI):
    init_db()
    scheduler = BackgroundScheduler()
    scheduler.add_job(
        lambda: log.info("Pruned %d old listings", prune_old_listings(days=30)),
        CronTrigger(hour=3, minute=0),
        id="prune_old_listings",
        replace_existing=True,
    )
    scheduler.start()
    yield
    scheduler.shutdown(wait=False)


app = FastAPI(lifespan=lifespan)


# ── DB helpers ────────────────────────────────────────────────────────────────


def _tab_statuses(tab: str) -> list[str]:
    return ["rejected", "ghosted"] if tab == "archived" else [tab]


SORT_OPTIONS = {
    "score":       "relevance_score DESC NULLS LAST",
    "date_posted": "date_posted DESC NULLS LAST",
    "ingested":    "ingested_at DESC",
    "company":     "company ASC",
}


def get_jobs(
    statuses: list[str] | None = None,
    min_score: float = 0,
    days: int | None = None,
    sort_by: str = "score",
) -> list[dict]:
    clauses = ["1=1"]
    params: dict = {}
    if statuses:
        clauses.append("status::text = ANY(:statuses)")
        params["statuses"] = statuses
    if min_score:
        clauses.append("relevance_score >= :min_score")
        params["min_score"] = min_score
    if days:
        clauses.append("ingested_at >= now() - (:days || ' days')::interval")
        params["days"] = days
    order = SORT_OPTIONS.get(sort_by, SORT_OPTIONS["score"])
    sql = f"""
        SELECT id, title, company, location, date_posted, relevance_score,
               job_url, flagged, entry_level, experience_req,
               llm_rating, status, notes, ingested_at, applied_at
        FROM jobs
        WHERE {" AND ".join(clauses)}
        ORDER BY {order}
    """
    with engine.connect() as conn:
        result = conn.execute(text(sql), params)
        return [dict(r._mapping) for r in result]


def get_job(job_id: int) -> dict | None:
    with engine.connect() as conn:
        result = conn.execute(
            text("""
                SELECT id, title, company, location, date_posted, relevance_score,
                       job_url, description, flagged, entry_level, experience_req,
                       llm_rating, llm_reason, status, notes, ingested_at, applied_at
                FROM jobs WHERE id = :id
            """),
            {"id": job_id},
        )
        row = result.fetchone()
    return dict(row._mapping) if row else None


def get_stats() -> dict:
    with engine.connect() as conn:
        result = conn.execute(
            text("SELECT status, count(*) as cnt FROM jobs GROUP BY status")
        )
        stats = {row.status: int(row.cnt) for row in result}
    stats["total"] = sum(stats.values())
    return stats


def get_analytics_data() -> dict:
    with engine.connect() as conn:
        metrics_row = conn.execute(text("""
            SELECT
                COUNT(*) FILTER (WHERE status IN ('applied','phone_screen','interview','offer','rejected','ghosted'))
                    AS total_applied,
                COUNT(*) FILTER (WHERE status IN ('phone_screen','interview','offer'))
                    AS got_response,
                COUNT(*) FILTER (WHERE status = 'offer')
                    AS offers,
                ROUND(AVG(CASE
                    WHEN applied_at IS NOT NULL
                         AND status IN ('phone_screen','interview','offer','rejected','ghosted')
                    THEN EXTRACT(EPOCH FROM (updated_at - applied_at)) / 86400.0
                END)::numeric, 1) AS avg_days_to_response,
                ROUND(AVG(relevance_score)::numeric, 1) AS avg_score
            FROM jobs
        """)).fetchone()
        metrics = dict(metrics_row._mapping) if metrics_row else {}
        total_applied = int(metrics.get("total_applied") or 0)
        got_response = int(metrics.get("got_response") or 0)
        metrics["total_applied"] = total_applied
        metrics["got_response"] = got_response
        metrics["offers"] = int(metrics.get("offers") or 0)
        metrics["response_rate_pct"] = (
            round(got_response / total_applied * 100, 1) if total_applied else 0
        )

        velocity_rows = conn.execute(text("""
            SELECT
                TO_CHAR(DATE_TRUNC('week', applied_at), 'Mon DD') AS week_label,
                DATE_TRUNC('week', applied_at)                    AS week_start,
                COUNT(*)                                           AS cnt
            FROM jobs
            WHERE applied_at IS NOT NULL
              AND applied_at >= NOW() - INTERVAL '56 days'
            GROUP BY DATE_TRUNC('week', applied_at)
            ORDER BY week_start
        """)).fetchall()
        velocity = [dict(r._mapping) for r in velocity_rows]

        followup_rows = conn.execute(text("""
            SELECT
                id, title, company, applied_at,
                EXTRACT(DAY FROM (NOW() - applied_at))::int AS days_elapsed,
                notes
            FROM jobs
            WHERE status = 'applied' AND applied_at IS NOT NULL
            ORDER BY applied_at ASC
        """)).fetchall()
        followup = [dict(r._mapping) for r in followup_rows]

    velocity_max = max((r["cnt"] for r in velocity), default=1)
    return {
        "metrics": metrics,
        "velocity": velocity,
        "velocity_max": int(velocity_max),
        "followup": followup,
        "pipeline": get_stats(),
    }


def _tmpl(name: str, req: Request, ctx: dict, headers: dict | None = None) -> HTMLResponse:
    ctx.update({"statuses": STATUSES, "status_emoji": STATUS_EMOJI})
    return templates.TemplateResponse(req, name, ctx, headers=headers)


# ── Routes ────────────────────────────────────────────────────────────────────


@app.get("/", response_class=HTMLResponse)
async def index(request: Request):
    jobs = get_jobs(["new"])
    return _tmpl("index.html", request, {
        "stats": get_stats(),
        "jobs": jobs,
        "active_tab": "new",
        "tab_defs": TAB_DEFS,
    })


@app.get("/jobs", response_class=HTMLResponse)
async def jobs_list(
    request: Request,
    status: str = "new",
    min_score: float = 0,
    days: Optional[str] = None,
    sort_by: str = "score",
):
    jobs = get_jobs(
        _tab_statuses(status),
        min_score=min_score,
        days=int(days) if days else None,
        sort_by=sort_by,
    )
    return _tmpl("partials/job_list.html", request, {
        "jobs": jobs,
        "current_tab": status,
    })


@app.get("/jobs/{job_id}", response_class=HTMLResponse)
async def job_detail(request: Request, job_id: int):
    job = get_job(job_id)
    if not job:
        return HTMLResponse("<p class='text-gray-400 text-sm'>Job not found.</p>")
    return _tmpl("partials/detail.html", request, {"job": job})


@app.patch("/jobs/{job_id}/status", response_class=HTMLResponse)
async def update_status(
    request: Request,
    job_id: int,
    status: str = Form(...),
    current_tab: str = Form("new"),
):
    applied_clause = ", applied_at = now()" if status == "applied" else ""
    with engine.begin() as conn:
        conn.execute(
            text(f"UPDATE jobs SET status = :status{applied_clause} WHERE id = :id"),
            {"status": status, "id": job_id},
        )
    job = get_job(job_id)
    if not job:
        return HTMLResponse("")
    # if job moved out of the current tab, remove the card from the list
    if job["status"] not in _tab_statuses(current_tab):
        return HTMLResponse("")
    return _tmpl("partials/job_card.html", request, {"job": job, "current_tab": current_tab})


@app.patch("/jobs/{job_id}/experience", response_class=HTMLResponse)
async def update_experience(
    request: Request,
    job_id: int,
    experience_req: str = Form(""),
    current_tab: str = Form("new"),
):
    with engine.begin() as conn:
        conn.execute(
            text("UPDATE jobs SET experience_req = :v WHERE id = :id"),
            {"v": experience_req or None, "id": job_id},
        )
    job = get_job(job_id)
    if not job:
        return HTMLResponse("")
    return _tmpl("partials/job_card.html", request, {"job": job, "current_tab": current_tab})


@app.patch("/jobs/{job_id}/notes", response_class=HTMLResponse)
async def update_notes(job_id: int, notes: str = Form("")):
    with engine.begin() as conn:
        conn.execute(
            text("UPDATE jobs SET notes = :notes WHERE id = :id"),
            {"notes": notes or None, "id": job_id},
        )
    return HTMLResponse('<span class="text-green-600 text-xs">Saved</span>')


@app.delete("/jobs/{job_id}", response_class=HTMLResponse)
async def delete_job(job_id: int):
    with engine.begin() as conn:
        conn.execute(text("DELETE FROM jobs WHERE id = :id"), {"id": job_id})
    # empty outerHTML swap removes the card; HX-Trigger refreshes the stats bar
    return HTMLResponse("", headers={"HX-Trigger": "statsChanged"})


@app.get("/add-job-form", response_class=HTMLResponse)
async def add_job_form(request: Request):
    return _tmpl("partials/add_job_form.html", request, {})


@app.post("/jobs", response_class=HTMLResponse)
async def add_job(
    request: Request,
    title: str = Form(...),
    company: str = Form(...),
    job_url: str = Form(...),
    location: str = Form(""),
    date_posted: str = Form(""),
    status: str = Form("applied"),
    experience_req: str = Form(""),
    entry_level: str = Form("false"),
    description: str = Form(""),
    notes: str = Form(""),
):
    with engine.begin() as conn:
        conn.execute(
            text("""
                INSERT INTO jobs (title, company, location, job_url, date_posted,
                                  notes, status, description, entry_level, experience_req)
                VALUES (:title, :company, :location, :job_url, :date_posted,
                        :notes, :status, :description, :entry_level, :experience_req)
                ON CONFLICT (job_url) DO NOTHING
            """),
            {
                "title": title,
                "company": company,
                "location": location or None,
                "job_url": job_url,
                "date_posted": date_posted or None,
                "notes": notes or None,
                "status": status,
                "description": description or None,
                "entry_level": entry_level == "true",
                "experience_req": experience_req or None,
            },
        )
    return _tmpl("partials/stats.html", request, {"stats": get_stats()},
                 headers={"HX-Trigger": "refreshList"})


@app.get("/stats", response_class=HTMLResponse)
async def stats_fragment(request: Request):
    return _tmpl("partials/stats.html", request, {"stats": get_stats()})


@app.get("/analytics", response_class=HTMLResponse)
async def analytics(request: Request):
    return _tmpl("analytics.html", request, get_analytics_data())
