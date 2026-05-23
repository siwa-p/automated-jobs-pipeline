"""
Jobs Tracker — Streamlit dashboard.

Run with:
    streamlit run src/app.py
"""

import sys
from pathlib import Path

import pandas as pd
import streamlit as st
from sqlalchemy import text

sys.path.insert(0, str(Path(__file__).parent))
from database import engine, init_db

STATUSES = [
    "new",
    "considering",
    "applied",
    "phone_screen",
    "interview",
    "offer",
    "rejected",
    "ghosted",
]

STATUS_EMOJI = {
    "new": "🆕",
    "considering": "🤔",
    "applied": "📨",
    "phone_screen": "📞",
    "interview": "🤝",
    "offer": "🎉",
    "rejected": "❌",
    "ghosted": "👻",
}


# ── DB helpers ────────────────────────────────────────────────────────────────


@st.cache_resource
def _ensure_db():
    init_db()


_ensure_db()


@st.cache_data(ttl=60)
def _fetch_all_jobs_raw() -> pd.DataFrame:
    """Single cached DB fetch; all filters applied in Python."""
    sql = """
        SELECT id, title, company, location, date_posted, relevance_score,
               job_url, description, flagged, entry_level, experience_req,
               status, notes, ingested_at, applied_at
        FROM jobs
        ORDER BY relevance_score DESC NULLS LAST
    """
    with engine.connect() as conn:
        return pd.read_sql(text(sql), conn)


def fetch_jobs(statuses: list[str] | None = None, min_score: float = 0) -> pd.DataFrame:
    df = _fetch_all_jobs_raw()
    if statuses:
        df = df[df["status"].isin(statuses)]
    if min_score:
        df = df[df["relevance_score"] >= min_score]
    return df


def fetch_job_by_id(job_id: int) -> pd.Series | None:
    df = _fetch_all_jobs_raw()
    row = df[df["id"] == job_id]
    return row.iloc[0] if not row.empty else None


def fetch_stats() -> dict:
    df = _fetch_all_jobs_raw()
    stats = df["status"].value_counts().to_dict()
    stats = {k: int(v) for k, v in stats.items()}
    stats["total"] = len(df)
    return stats


def update_status(job_id: int, new_status: str) -> None:
    applied_clause = ", applied_at = now()" if new_status == "applied" else ""
    with engine.begin() as conn:
        conn.execute(
            text(f"UPDATE jobs SET status = :status{applied_clause} WHERE id = :id"),
            {"status": new_status, "id": job_id},
        )
    _fetch_all_jobs_raw.clear()


def update_notes(job_id: int, notes: str) -> None:
    with engine.begin() as conn:
        conn.execute(
            text("UPDATE jobs SET notes = :notes WHERE id = :id"),
            {"notes": notes, "id": job_id},
        )
    _fetch_all_jobs_raw.clear()


def update_experience_req(job_id: int, value: str | None) -> None:
    with engine.begin() as conn:
        conn.execute(
            text("UPDATE jobs SET experience_req = :value WHERE id = :id"),
            {"value": value or None, "id": job_id},
        )
    _fetch_all_jobs_raw.clear()


def delete_job(job_id: int) -> None:
    with engine.begin() as conn:
        conn.execute(text("DELETE FROM jobs WHERE id = :id"), {"id": job_id})
    _fetch_all_jobs_raw.clear()


def insert_job(title: str, company: str, location: str, job_url: str,
               date_posted: str, notes: str, status: str, description: str,
               entry_level: bool, experience_req: str) -> None:
    score = _compute_score(title, description)
    with engine.begin() as conn:
        conn.execute(text("""
            INSERT INTO jobs (title, company, location, job_url, date_posted,
                              notes, status, description, entry_level,
                              experience_req, relevance_score)
            VALUES (:title, :company, :location, :job_url,
                    :date_posted, :notes, :status, :description, :entry_level,
                    :experience_req, :score)
            ON CONFLICT (job_url) DO NOTHING
        """), {
            "title": title, "company": company, "location": location,
            "job_url": job_url,
            "date_posted": date_posted or None,
            "notes": notes or None,
            "status": status,
            "description": description or None,
            "entry_level": entry_level,
            "experience_req": experience_req or None,
            "score": score,
        })
    _fetch_all_jobs_raw.clear()


def _compute_score(title: str, description: str) -> float | None:
    """Score a new job by TF-IDF cosine similarity against existing scored jobs."""
    with engine.connect() as conn:
        df = pd.read_sql(
            text("SELECT title, description, relevance_score FROM jobs "
                 "WHERE relevance_score IS NOT NULL"),
            conn,
        )
    if df.empty:
        return None
    from sklearn.feature_extraction.text import TfidfVectorizer
    from sklearn.metrics.pairwise import cosine_similarity

    def _text(row) -> str:
        return f"{row.get('title', '')} {row.get('description', '') or ''}"

    corpus = df.apply(_text, axis=1).tolist()
    new_doc = f"{title} {description or ''}"

    vec = TfidfVectorizer(stop_words="english", max_features=5000)
    vec.fit(corpus)
    existing_vecs = vec.transform(corpus)
    new_vec = vec.transform([new_doc])

    sims = cosine_similarity(new_vec, existing_vecs)[0]
    # weighted average of top-5 most similar jobs' scores
    top_idx = sims.argsort()[-5:][::-1]
    top_sims = sims[top_idx]
    top_scores = df["relevance_score"].iloc[top_idx].values
    if top_sims.sum() == 0:
        return float(df["relevance_score"].mean())
    score = float((top_sims * top_scores).sum() / top_sims.sum())
    return round(score, 1)


# ── UI helpers ────────────────────────────────────────────────────────────────


def status_label(s: str) -> str:
    return f"{STATUS_EMOJI.get(s, '')} {s.replace('_', ' ').title()}"


def render_stats(stats: dict) -> None:
    cols = st.columns(8)
    items = [
        ("Total", stats.get("total", 0)),
        ("New", stats.get("new", 0)),
        ("Shortlisted", stats.get("considering", 0)),
        ("Applied", stats.get("applied", 0)),
        ("Phone Screen", stats.get("phone_screen", 0)),
        ("Interview", stats.get("interview", 0)),
        ("Offer", stats.get("offer", 0)),
        ("Rejected / Ghosted", stats.get("rejected", 0) + stats.get("ghosted", 0)),
    ]
    for col, (label, val) in zip(cols, items):
        col.metric(label, val)


def render_job_card(row: pd.Series, key_prefix: str = "") -> None:
    with st.container(border=True):
        col_info, col_ingested, col_exp, col_entry, col_score, col_status, col_apply, col_detail = (
            st.columns([3, 1, 1, 1, 1, 2, 1, 1])
        )

        with col_info:
            star = " ⭐" if row.get("flagged") else ""
            st.markdown(f"**{row.title}**{star} — {row.company}")
            st.caption(f"{row.location or '—'}  ·  posted {row.date_posted}")

        with col_ingested:
            ingested = row.get("ingested_at")
            ingested_str = pd.to_datetime(ingested).strftime("%b %d") if pd.notna(ingested) else "—"
            st.metric("Ingested", ingested_str)

        with col_exp:
            new_exp = st.text_input(
                "Exp",
                value=str(row["experience_req"]) if pd.notna(row.get("experience_req")) else "",
                key=f"exp_{row.id}",
                placeholder="e.g. 2-4 yrs",
                label_visibility="collapsed",
            )

        with col_entry:
            st.metric("Entry?", "✓" if row.get("entry_level") else "")

        with col_score:
            score = (
                f"{row.relevance_score:.1f}" if pd.notna(row.relevance_score) else "—"
            )
            st.metric("Score", score)

        with col_status:
            new_status = st.selectbox(
                "Status",
                options=STATUSES,
                index=STATUSES.index(row.status),
                format_func=status_label,
                key=f"status_{row.id}",
                label_visibility="collapsed",
            )

        with col_apply:
            st.link_button(
                "Apply ↗", url=row.job_url, type="primary", width="stretch"
            )

        with col_detail:
            if st.button("Detail", key=f"detail_{row.id}", width="stretch"):
                st.session_state["selected_job_id"] = int(row.id)

    if new_exp != (row.get("experience_req") or ""):
        update_experience_req(int(row.id), new_exp)
        st.rerun()

    if new_status != row.status:
        update_status(int(row.id), new_status)
        st.toast(f"Status → {status_label(new_status)}", icon="✅")
        st.rerun()


SORT_OPTIONS = {
    "Score": ("relevance_score", False),
    "Ingested (newest)": ("ingested_at", False),
    "Ingested (oldest)": ("ingested_at", True),
    "Entry Level first": ("entry_level", False),
    "Experience (low→high)": ("experience_req", True),
    "Experience (high→low)": ("experience_req", False),
    "Date Posted (newest)": ("date_posted", False),
    "Date Posted (oldest)": ("date_posted", True),
}

INGEST_WINDOWS = {
    "All time": None,
    "Last 2 days": 2,
    "Last 7 days": 7,
    "Last 14 days": 14,
    "Last 30 days": 30,
}


def _exp_sort_key(val) -> float:
    """Extract lower-bound number from experience strings. NaN/None → 0."""
    if pd.isna(val) or val is None:
        return 0
    import re

    m = re.search(r"\d+", str(val))
    return float(m.group()) if m else 0


def filter_by_ingest_window(df: pd.DataFrame, days: int | None) -> pd.DataFrame:
    if days is None or df.empty:
        return df
    cutoff = pd.Timestamp.now(tz="UTC") - pd.Timedelta(days=days)
    return df[pd.to_datetime(df["ingested_at"], utc=True) >= cutoff]


def render_jobs_list(df: pd.DataFrame, key_prefix: str = "") -> None:
    if df.empty:
        st.info("No jobs here yet.")
        return

    sort_label = st.selectbox(
        "Sort by",
        options=list(SORT_OPTIONS.keys()),
        index=0,
        key=f"sort_{key_prefix}",
        label_visibility="collapsed",
    )
    sort_col, sort_asc = SORT_OPTIONS[sort_label]

    if sort_col == "experience_req":
        df = df.assign(_exp_key=df["experience_req"].apply(_exp_sort_key))
        df = df.sort_values("_exp_key", ascending=sort_asc).drop(columns="_exp_key")
    else:
        df = df.sort_values(sort_col, ascending=sort_asc, na_position="last")

    st.caption(f"{len(df)} job{'s' if len(df) != 1 else ''} · sorted by {sort_label}")

    for _, row in df.iterrows():
        render_job_card(row, key_prefix=key_prefix)


def render_detail_panel() -> None:
    selected_id = st.session_state.get("selected_job_id")
    if selected_id is None:
        st.info("Click **Detail** on any job to view it here.")
        return

    row = fetch_job_by_id(selected_id)
    if row is None:
        st.warning("Job not found.")
        return

    st.subheader(row.title)
    st.markdown(f"**{row.company}** · {row.location or '—'}")
    score_str = f"{row.relevance_score:.1f}" if pd.notna(row.relevance_score) else "—"
    st.markdown(
        f"<span style='font-size:1rem'>Posted: {row.date_posted}  ·  Score: {score_str}  ·  Ingested: {row.ingested_at}</span>",
        unsafe_allow_html=True,
    )
    if pd.notna(row.applied_at):
        st.caption(f"Applied: {row.applied_at}")

    st.caption(f"Status: {status_label(row.status)}")
    btn_col1, btn_col2 = st.columns([1, 1], gap="small")
    with btn_col1:
        st.link_button("Apply Now ↗", url=row.job_url, type="primary")
    with btn_col2:
        if st.button("Discard posting", type="secondary", key="discard_btn"):
            st.session_state["confirm_discard"] = True

    if st.session_state.get("confirm_discard"):
        st.warning("Delete this posting permanently?")
        yes_col, no_col = st.columns(2)
        with yes_col:
            if st.button("Yes, delete", type="primary", key="confirm_yes"):
                delete_job(selected_id)
                st.session_state["selected_job_id"] = None
                st.session_state.pop("confirm_discard", None)
                st.rerun()
        with no_col:
            if st.button("Cancel", key="confirm_no"):
                st.session_state.pop("confirm_discard", None)
                st.rerun()

    st.divider()
    note_key = f"notes_{selected_id}"
    if note_key not in st.session_state:
        st.session_state[note_key] = str(row.notes) if pd.notna(row.notes) else ""
    notes = st.text_area(
        "Notes",
        key=note_key,
        height=150,
        placeholder="Interview notes, contacts, follow-up dates…",
    )
    if st.button("Save Notes", key=f"save_notes_{selected_id}"):
        update_notes(selected_id, notes)
        st.success("Saved.")

    st.divider()
    with st.expander("Job Description", expanded=False):
        if row.description:
            st.markdown(row.description)
        else:
            st.caption("No description available.")


# ── App shell ─────────────────────────────────────────────────────────────────

st.set_page_config(
    page_title="Jobs Tracker",
    page_icon="💼",
    layout="wide",
    initial_sidebar_state="expanded",
)

st.markdown(
    """
<style>
/* narrower sidebar */
[data-testid="stSidebar"] { min-width: 180px !important; max-width: 180px !important; }

/* global font scale-down */
html, body, [class*="css"] { font-size: 14px !important; }

/* titles */
h1 { font-size: 1.3rem !important; }
h2 { font-size: 1.1rem !important; }
h3 { font-size: 1rem !important; }

/* metric labels and values */
[data-testid="stMetricLabel"] { font-size: 0.8rem !important; }
[data-testid="stMetricValue"] { font-size: 1rem !important; }

/* tab labels */
[data-testid="stTabs"] button { font-size: 0.8rem !important; padding: 4px 8px !important; }

/* captions */
[data-testid="stCaptionContainer"] { font-size: 0.8rem !important; }

/* buttons */
[data-testid="stButton"] button,
[data-testid="stLinkButton"] a { font-size: 0.82rem !important; padding: 2px 8px !important; }

/* selectbox */
[data-testid="stSelectbox"] label,
[data-testid="stSelectbox"] div { font-size: 0.82rem !important; }

</style>
""",
    unsafe_allow_html=True,
)

st.title("💼 Jobs Tracker")

# stats bar
try:
    stats = fetch_stats()
    render_stats(stats)
except Exception as e:
    st.error(f"Could not connect to database: {e}")
    st.stop()

st.divider()

# sidebar
with st.sidebar:
    st.header("Filters")
    min_score = st.slider("Min relevance score", 0, 100, 0)
    ingest_window_label = st.selectbox("Ingested", options=list(INGEST_WINDOWS.keys()), index=0)
    ingest_days = INGEST_WINDOWS[ingest_window_label]
    st.divider()
    if st.button("🔄 Refresh", width="stretch"):
        st.rerun()

page_pipeline, page_analytics, page_add = st.tabs(["📋 Pipeline", "📊 Analytics", "➕ Add Job"])

# ── Pipeline tab ──────────────────────────────────────────────────────────────
with page_pipeline:
    col_list, col_detail = st.columns([3, 2])

    with col_list:
        with st.container(height=750, border=False):
            (
                tab_new,
                tab_shortlist,
                tab_applied,
                tab_heard,
                tab_interview,
                tab_offers,
                tab_archived,
            ) = st.tabs(
                [
                    "🆕 New",
                    "🤔 Shortlist",
                    "📨 Applied",
                    "📞 Heard Back",
                    "🤝 Interviewing",
                    "🎉 Offers",
                    "❌ Rejected / Ghosted",
                ]
            )

            with tab_new:
                render_jobs_list(filter_by_ingest_window(fetch_jobs(["new"], min_score), ingest_days), key_prefix="new")

            with tab_shortlist:
                render_jobs_list(
                    filter_by_ingest_window(fetch_jobs(["considering"], min_score), ingest_days), key_prefix="shortlist"
                )

            with tab_applied:
                render_jobs_list(filter_by_ingest_window(fetch_jobs(["applied"], min_score), ingest_days), key_prefix="applied")

            with tab_heard:
                render_jobs_list(
                    filter_by_ingest_window(fetch_jobs(["phone_screen"], min_score), ingest_days), key_prefix="heard"
                )

            with tab_interview:
                render_jobs_list(
                    filter_by_ingest_window(fetch_jobs(["interview"], min_score), ingest_days), key_prefix="interview"
                )

            with tab_offers:
                render_jobs_list(filter_by_ingest_window(fetch_jobs(["offer"], min_score), ingest_days), key_prefix="offers")

            with tab_archived:
                render_jobs_list(
                    filter_by_ingest_window(fetch_jobs(["rejected", "ghosted"], min_score), ingest_days), key_prefix="archived"
                )

    with col_detail:
        with st.container(height=750, border=False):
            st.subheader("Job Detail")
            render_detail_panel()

# ── Analytics tab ─────────────────────────────────────────────────────────────
with page_analytics:
    import altair as alt

    df_all = fetch_jobs()
    df_all = df_all[df_all["status"] != "new"]
    if df_all.empty:
        st.info("No data yet.")
    else:
        # ── traction charts ───────────────────────────────────────────────────
        df_all["ingested_week"] = pd.to_datetime(df_all["ingested_at"]).dt.tz_localize(None).dt.to_period("W").dt.start_time
        response_statuses = {"phone_screen", "interview", "offer"}

        weekly_applied = (
            df_all[df_all["status"].isin(["applied", *response_statuses, "rejected", "ghosted"])]
            .groupby("ingested_week").size().reset_index(name="count")
            .assign(type="Applied")
        )
        weekly_response = (
            df_all[df_all["status"].isin(response_statuses)]
            .groupby("ingested_week").size().reset_index(name="count")
            .assign(type="Got response")
        )
        weekly_df = pd.concat([weekly_applied, weekly_response])

        st.subheader("Weekly activity — applications vs. responses")
        if weekly_df.empty:
            st.caption("No data yet.")
        else:
            st.altair_chart(
                alt.Chart(weekly_df).mark_bar().encode(
                    x=alt.X("ingested_week:T", title="Week"),
                    y=alt.Y("count:Q", title="Jobs"),
                    color=alt.Color("type:N", scale=alt.Scale(
                        domain=["Applied", "Got response"],
                        range=["#4C72B0", "#55A868"],
                    ), legend=alt.Legend(title="")),
                    xOffset="type:N",
                    tooltip=[alt.Tooltip("ingested_week:T", title="Week"), "type", "count"],
                ).properties(height=240),
                width="stretch",
            )

        st.divider()

        # ── cumulative applied vs responses ───────────────────────────────────
        st.subheader("Cumulative traction")
        cum_applied = (
            df_all[df_all["status"].isin(["applied", *response_statuses, "rejected", "ghosted"])]
            .groupby("ingested_week").size().reset_index(name="count")
            .sort_values("ingested_week")
            .assign(type="Applied")
        )
        cum_response = (
            df_all[df_all["status"].isin(response_statuses)]
            .groupby("ingested_week").size().reset_index(name="count")
            .sort_values("ingested_week")
            .assign(type="Got response")
        )
        for df_ in [cum_applied, cum_response]:
            df_["cumulative"] = df_["count"].cumsum()
        cum_df = pd.concat([cum_applied, cum_response])

        if cum_df.empty:
            st.caption("No data yet.")
        else:
            st.altair_chart(
                alt.Chart(cum_df).mark_line(point=True).encode(
                    x=alt.X("ingested_week:T", title="Week"),
                    y=alt.Y("cumulative:Q", title="Total jobs"),
                    color=alt.Color("type:N", scale=alt.Scale(
                        domain=["Applied", "Got response"],
                        range=["#4C72B0", "#55A868"],
                    ), legend=alt.Legend(title="")),
                    tooltip=[alt.Tooltip("ingested_week:T", title="Week"), "type", "cumulative"],
                ).properties(height=220),
                width="stretch",
            )

        st.divider()

        # ── row 1: funnel + applications over time ────────────────────────────
        r1a, r1b = st.columns([1, 2])

        with r1a:
            st.subheader("Pipeline funnel")
            funnel_order = ["considering", "applied",
                            "phone_screen", "interview", "offer",
                            "rejected", "ghosted"]
            funnel_df = (
                df_all.groupby("status")
                .size()
                .reset_index(name="count")
            )
            funnel_df["order"] = funnel_df["status"].map(
                {s: i for i, s in enumerate(funnel_order)}
            ).fillna(99)
            funnel_df = funnel_df[funnel_df["status"].isin(funnel_order)]
            funnel_df["label"] = funnel_df["status"].map(STATUS_EMOJI) + " " + funnel_df["status"]
            funnel_df = funnel_df.sort_values("order")
            st.altair_chart(
                alt.Chart(funnel_df).mark_bar().encode(
                    x=alt.X("count:Q", title="Jobs"),
                    y=alt.Y("label:N", sort=None, title=""),
                    color=alt.value("#4C72B0"),
                    tooltip=["label", "count"],
                ).properties(height=260),
                width="stretch",
            )

        with r1b:
            st.subheader("Ingested over time")
            df_all["ingested_date"] = pd.to_datetime(df_all["ingested_at"]).dt.date
            time_df = (
                df_all.groupby("ingested_date")
                .size()
                .reset_index(name="count")
            )
            time_df["ingested_date"] = pd.to_datetime(time_df["ingested_date"])
            st.altair_chart(
                alt.Chart(time_df).mark_area(line=True, point=True, opacity=0.4).encode(
                    x=alt.X("ingested_date:T", title="Date"),
                    y=alt.Y("count:Q", title="Jobs added"),
                    tooltip=[alt.Tooltip("ingested_date:T", title="Date"), "count"],
                ).properties(height=260),
                width="stretch",
            )

        st.divider()

        # ── row 2: top companies + score distribution ─────────────────────────
        r2a, r2b = st.columns(2)

        with r2a:
            st.subheader("Top companies")
            co_df = (
                df_all.groupby("company")
                .size()
                .reset_index(name="count")
                .sort_values("count", ascending=False)
                .head(15)
            )
            st.altair_chart(
                alt.Chart(co_df).mark_bar().encode(
                    x=alt.X("count:Q", title="Listings"),
                    y=alt.Y("company:N", sort="-x", title=""),
                    tooltip=["company", "count"],
                ).properties(height=350),
                width="stretch",
            )

        with r2b:
            st.subheader("Relevance score distribution")
            score_df = df_all.dropna(subset=["relevance_score"])
            if score_df.empty:
                st.caption("No scores yet.")
            else:
                st.altair_chart(
                    alt.Chart(score_df).mark_bar().encode(
                        x=alt.X("relevance_score:Q", bin=alt.Bin(maxbins=20), title="Score"),
                        y=alt.Y("count():Q", title="Jobs"),
                        tooltip=["count()"],
                    ).properties(height=350),
                    width="stretch",
                )

        st.divider()

        # ── row 3: location callback rate + response rate ────────────────────
        r3a, r3b = st.columns(2)

        with r3a:
            st.subheader("Location — callback rate")
            loc_base = df_all[
                df_all["location"].notna() & (df_all["location"] != "") &
                df_all["status"].isin(["applied","phone_screen","interview","offer","rejected","ghosted"])
            ].copy()
            loc_summary = (
                loc_base.groupby("location")
                .agg(
                    applied=("status", "count"),
                    callbacks=("status", lambda s: s.isin(["phone_screen","interview","offer"]).sum()),
                )
                .reset_index()
            )
            loc_summary = loc_summary[loc_summary["applied"] >= 2]  # need at least 2 to be meaningful
            loc_summary["rate"] = (loc_summary["callbacks"].astype(float) / loc_summary["applied"].astype(float) * 100).round(1)
            loc_summary = loc_summary.sort_values("rate", ascending=False).head(15)

            if loc_summary.empty:
                st.caption("Not enough data yet — need at least 2 applications per location.")
            else:
                st.altair_chart(
                    alt.Chart(loc_summary).mark_bar().encode(
                        x=alt.X("rate:Q", title="Callback rate (%)"),
                        y=alt.Y("location:N", sort="-x", title=""),
                        color=alt.Color("rate:Q", scale=alt.Scale(scheme="greens"), legend=None),
                        tooltip=[
                            "location",
                            alt.Tooltip("applied:Q", title="Applied"),
                            alt.Tooltip("callbacks:Q", title="Callbacks"),
                            alt.Tooltip("rate:Q", title="Rate %"),
                        ],
                    ).properties(height=350),
                    width="stretch",
                )

        with r3b:
            st.subheader("Response rate")
            applied_total = len(df_all[df_all["status"].isin(
                ["applied", "phone_screen", "interview", "offer", "rejected", "ghosted"]
            )])
            progressed = len(df_all[df_all["status"].isin(
                ["phone_screen", "interview", "offer"]
            )])
            rate = (progressed / applied_total * 100) if applied_total else 0
            st.metric("Applied", applied_total)
            st.metric("Got a response (screen / interview / offer)",
                      progressed, f"{rate:.1f}%")

            exp_df = (
                df_all[df_all["experience_req"].notna()]
                .groupby("experience_req")
                .size()
                .reset_index(name="count")
                .sort_values("count", ascending=False)
            )
            if not exp_df.empty:
                st.subheader("Experience required")
                st.altair_chart(
                    alt.Chart(exp_df).mark_bar().encode(
                        x=alt.X("count:Q", title="Jobs"),
                        y=alt.Y("experience_req:N", sort="-x", title=""),
                        tooltip=["experience_req", "count"],
                    ).properties(height=200),
                    width="stretch",
                )

        # ── callback intelligence ─────────────────────────────────────────────
        st.divider()
        st.subheader("What kinds of jobs get callbacks?")

        df_cb  = df_all[df_all["status"].isin(["phone_screen", "interview", "offer"])]
        df_no  = df_all[df_all["status"].isin(["applied", "rejected", "ghosted"])]

        if df_cb.empty:
            st.info("No callbacks yet — check back once you start hearing back.")
        else:
            from sklearn.feature_extraction.text import CountVectorizer

            STOP = {
                "the","and","for","with","you","are","this","that","have","will",
                "our","your","we","in","of","to","a","an","is","it","be","as",
                "at","by","or","on","not","can","their","they","but","from","all",
                "more","about","into","other","has","its","who","also","any","each",
                "been","such","than","within","across","what","how","when","where",
                "these","those","would","which","there","them","both","well","own",
                "new","use","used","using","including","ability","experience",
                "work","working","role","team","position","job","candidate",
                "required","requirements","preferred","plus","strong","excellent",
            }

            @st.cache_data(ttl=120)
            def top_terms(texts_tuple, n=20):
                corpus = [str(t) for t in texts_tuple if t and str(t).strip()]
                if not corpus:
                    return pd.DataFrame(columns=["term", "count"])
                vec = CountVectorizer(
                    ngram_range=(1, 2), stop_words=list(STOP),
                    min_df=1, token_pattern=r"[a-zA-Z][a-zA-Z0-9\+\#\.]{1,}"
                )
                X = vec.fit_transform(corpus)
                counts = X.sum(axis=0).A1
                terms = vec.get_feature_names_out()
                return (
                    pd.DataFrame({"term": terms, "count": counts})
                    .sort_values("count", ascending=False)
                    .head(n)
                    .reset_index(drop=True)
                )

            def term_chart(df, color, height=300):
                return (
                    alt.Chart(df).mark_bar(color=color).encode(
                        x=alt.X("count:Q", title="Frequency"),
                        y=alt.Y("term:N", sort="-x", title=""),
                        tooltip=["term", "count"],
                    ).properties(height=height)
                )

            # ── title keywords ────────────────────────────────────────────────
            cb4a, cb4b = st.columns(2)
            with cb4a:
                st.markdown("**Title keywords — callbacks**")
                t_cb = top_terms(tuple(df_cb["title"]))
                if not t_cb.empty:
                    st.altair_chart(term_chart(t_cb, "#55A868"), width="stretch")
            with cb4b:
                st.markdown("**Title keywords — no callback**")
                t_no = top_terms(tuple(df_no["title"]))
                if not t_no.empty:
                    st.altair_chart(term_chart(t_no, "#C44E52"), width="stretch")

            st.divider()

            # ── description keywords ──────────────────────────────────────────
            cb5a, cb5b = st.columns(2)
            with cb5a:
                st.markdown("**Description keywords — callbacks**")
                d_cb = top_terms(tuple(df_cb["description"].dropna()), n=25)
                if not d_cb.empty:
                    st.altair_chart(term_chart(d_cb, "#55A868", height=380), width="stretch")
            with cb5b:
                st.markdown("**Description keywords — no callback**")
                d_no = top_terms(tuple(df_no["description"].dropna()), n=25)
                if not d_no.empty:
                    st.altair_chart(term_chart(d_no, "#C44E52", height=380), width="stretch")

            st.divider()

            # ── experience req + entry level ──────────────────────────────────
            cb6a, cb6b, cb6c = st.columns(3)

            with cb6a:
                st.markdown("**Experience required — callbacks**")
                ex_cb = (
                    df_cb[df_cb["experience_req"].notna()]
                    .groupby("experience_req").size().reset_index(name="count")
                    .sort_values("count", ascending=False)
                )
                if not ex_cb.empty:
                    st.altair_chart(term_chart(ex_cb.rename(columns={"experience_req": "term"}),
                                               "#55A868", height=220), width="stretch")
                else:
                    st.caption("—")

            with cb6b:
                st.markdown("**Experience required — no callback**")
                ex_no = (
                    df_no[df_no["experience_req"].notna()]
                    .groupby("experience_req").size().reset_index(name="count")
                    .sort_values("count", ascending=False)
                )
                if not ex_no.empty:
                    st.altair_chart(term_chart(ex_no.rename(columns={"experience_req": "term"}),
                                               "#C44E52", height=220), width="stretch")
                else:
                    st.caption("—")

            with cb6c:
                st.markdown("**Entry-level breakdown**")
                el_df = pd.DataFrame({
                    "group": ["Callbacks", "Callbacks", "No callback", "No callback"],
                    "entry":  ["Entry", "Not entry", "Entry", "Not entry"],
                    "count":  [
                        int(df_cb["entry_level"].sum()),
                        int((~df_cb["entry_level"].astype(bool)).sum()),
                        int(df_no["entry_level"].sum()),
                        int((~df_no["entry_level"].astype(bool)).sum()),
                    ],
                })
                st.altair_chart(
                    alt.Chart(el_df).mark_bar().encode(
                        x=alt.X("group:N", title=""),
                        y=alt.Y("count:Q", title="Jobs"),
                        color=alt.Color("entry:N", legend=alt.Legend(title="")),
                        tooltip=["group", "entry", "count"],
                    ).properties(height=220),
                    width="stretch",
                )

# ── Add Job tab ───────────────────────────────────────────────────────────────
with page_add:
    st.subheader("Add a job manually")
    with st.form("add_job_form", clear_on_submit=True):
        row1_a, row1_b, row1_c = st.columns([2, 2, 1])
        f_title    = row1_a.text_input("Job title *")
        f_company  = row1_b.text_input("Company *")
        f_location = row1_c.text_input("Location")

        row2_a, row2_b, row2_c = st.columns([3, 1, 1])
        f_url      = row2_a.text_input("Job URL *")
        f_date     = row2_b.date_input("Date posted", value=None)
        f_status   = row2_c.selectbox("Status", options=STATUSES,
                                      index=STATUSES.index("applied"),
                                      format_func=status_label)

        row3_a, row3_b = st.columns([2, 1])
        f_exp        = row3_a.text_input("Experience required (e.g. 2-4 years)")
        f_entry      = row3_b.checkbox("Entry-level role")

        f_desc  = st.text_area("Description", height=120)
        f_notes = st.text_area("Notes", height=80)
        submitted = st.form_submit_button("Add Job", type="primary")

    if submitted:
        if not f_title or not f_company or not f_url:
            st.warning("Title, company, and URL are required.")
        else:
            try:
                with st.spinner("Calculating relevance score…"):
                    insert_job(
                        title=f_title, company=f_company, location=f_location,
                        job_url=f_url,
                        date_posted=str(f_date) if f_date else "",
                        notes=f_notes, status=f_status, description=f_desc,
                        entry_level=f_entry, experience_req=f_exp,
                    )
                score = _compute_score(f_title, f_desc)
                score_str = f"{score:.0f}/100" if score is not None else "n/a"
                st.success(f"Added: {f_title} @ {f_company} — score {score_str}")
                st.rerun()
            except Exception as e:
                st.error(str(e))
