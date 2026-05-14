"""Streamlit dashboard for the quant backtest harness.

Personal-use UI to:
  * Trigger backfill jobs and the backtest run as background subprocesses.
  * Monitor live progress (data coverage, subprocess stdout).
  * Browse prior runs and render markdown analysis reports inline.

Launch with:
    streamlit run apps/backtest_dashboard.py

The dashboard talks directly to Postgres (sync psycopg2) for read-only
status and spawns ``python -m scripts.<...>`` subprocesses for actions.
No new persistence layer — all state lives in the existing tables.
"""
from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import tempfile
import time
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any

import numpy as np
import psycopg2
import psycopg2.extras
import streamlit as st


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

PROJECT_ROOT = Path(__file__).resolve().parent.parent
REPORTS_DIR = PROJECT_ROOT / "reports"
LOGS_DIR = PROJECT_ROOT / ".dashboard_logs"
LOGS_DIR.mkdir(exist_ok=True)
JOBS_REGISTRY = LOGS_DIR / "jobs.json"

DB_DSN = {
    "host": os.environ.get("PGHOST", "localhost"),
    "database": os.environ.get("PGDATABASE", "laabh"),
    "user": os.environ.get("PGUSER", "postgres"),
    "password": os.environ.get("PGPASSWORD", "Ashu@007saxe"),
}


# ---------------------------------------------------------------------------
# DB helpers (sync — Streamlit does not play well with asyncio loops)
# ---------------------------------------------------------------------------

def _conn():
    return psycopg2.connect(**DB_DSN)


@st.cache_data(ttl=5)
def list_portfolios() -> list[dict]:
    with _conn() as c, c.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        cur.execute("SELECT id, name, current_value FROM portfolios ORDER BY name")
        return [dict(r) for r in cur.fetchall()]


@st.cache_data(ttl=3)
def coverage_snapshot() -> dict:
    """One-shot read of the data-coverage indicators the backtest depends on."""
    with _conn() as c, c.cursor() as cur:
        out: dict[str, Any] = {}
        cur.execute("SELECT MIN(date), MAX(date), COUNT(*) FROM price_daily")
        out["price_daily"] = cur.fetchone()
        cur.execute(
            "SELECT MIN(timestamp), MAX(timestamp), COUNT(*), "
            "COUNT(DISTINCT instrument_id) FROM price_intraday"
        )
        out["price_intraday"] = cur.fetchone()
        cur.execute("SELECT COUNT(*) FROM rbi_repo_history")
        out["rbi_repo_rows"] = cur.fetchone()[0]
        cur.execute(
            "SELECT MIN(timestamp::date), MAX(timestamp::date), COUNT(*) FROM vix_ticks"
        )
        out["vix"] = cur.fetchone()
        cur.execute("SELECT COUNT(*) FROM fno_ban_list")
        out["ban_rows"] = cur.fetchone()[0]
        cur.execute("SELECT COUNT(*) FROM instruments WHERE is_fno=true")
        out["fno_universe"] = cur.fetchone()[0]
        cur.execute(
            "SELECT MIN(snapshot_at::date), MAX(snapshot_at::date), COUNT(*) "
            "FROM options_chain"
        )
        out["options_chain"] = cur.fetchone()
        cur.execute(
            "SELECT (timestamp AT TIME ZONE 'Asia/Kolkata')::date d, "
            "COUNT(*), COUNT(DISTINCT instrument_id) "
            "FROM price_intraday "
            "GROUP BY d ORDER BY d DESC LIMIT 14"
        )
        out["price_intraday_by_day"] = cur.fetchall()
        cur.execute("SELECT COUNT(*) FROM backtest_runs")
        out["backtest_runs"] = cur.fetchone()[0]
        cur.execute("SELECT COUNT(*) FROM backtest_trades")
        out["backtest_trades"] = cur.fetchone()[0]
        return out


def _activity_delta() -> dict:
    """Return per-table row-count delta vs the previous refresh.

    Detects external write activity (e.g. a Dhan loader started from the
    terminal, invisible to the dashboard's job table) by sampling row
    counts on each rerun and diffing against the previous sample.
    """
    cov = coverage_snapshot()
    now = time.monotonic()
    counters = {
        "price_intraday": cov["price_intraday"][2],
        "backtest_runs": cov["backtest_runs"],
        "backtest_trades": cov["backtest_trades"],
        "options_chain": cov["options_chain"][2],
    }
    prev = st.session_state.get("_activity_prev")
    deltas: dict[str, tuple[int, float]] = {}
    if prev:
        elapsed = max(0.001, now - prev["t"])
        for k, v in counters.items():
            delta = v - prev["counters"].get(k, v)
            deltas[k] = (delta, elapsed)
    st.session_state["_activity_prev"] = {"t": now, "counters": counters}
    return {"now": counters, "deltas": deltas, "snapshot": cov}


def _phase_label(act: dict) -> tuple[str, str]:
    """Heuristic phase label + colour from row-count state and live deltas."""
    cov = act["snapshot"]
    deltas = act["deltas"]

    pi_growing = any(deltas.get(k, (0, 1))[0] > 0 for k in ("price_intraday",))
    runs_growing = deltas.get("backtest_runs", (0, 1))[0] > 0
    trades_growing = deltas.get("backtest_trades", (0, 1))[0] > 0

    if runs_growing or trades_growing:
        return ("🟢 Phase: backtest running", "running")
    if pi_growing:
        return ("🟡 Phase: backfilling data (loader active)", "loading")
    if cov["backtest_runs"] > 0:
        return ("✅ Phase: report ready (prior runs exist)", "ready")
    pi_inst = cov["price_intraday"][3] or 0
    universe = cov["fno_universe"] or 1
    if pi_inst >= universe * 0.9:
        return ("🔵 Phase: ready to backtest", "ready")
    return ("⚪ Phase: idle / partial data", "idle")


@st.cache_data(ttl=5)
def list_backtest_runs(portfolio_id: str | None = None) -> list[dict]:
    sql = (
        "SELECT id, portfolio_id, backtest_date, starting_nav, final_nav, "
        "pnl_pct, trade_count, winning_trades, completed_at, bandit_seed "
        "FROM backtest_runs "
    )
    params: list[Any] = []
    if portfolio_id:
        sql += "WHERE portfolio_id = %s "
        params.append(portfolio_id)
    sql += "ORDER BY backtest_date DESC, started_at DESC LIMIT 200"
    with _conn() as c, c.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        cur.execute(sql, params)
        return [dict(r) for r in cur.fetchall()]


# ---------------------------------------------------------------------------
# Subprocess management
# ---------------------------------------------------------------------------

def _load_registry() -> dict:
    """Read the on-disk jobs registry. Returns {} if missing/corrupt."""
    if not JOBS_REGISTRY.exists():
        return {}
    try:
        return json.loads(JOBS_REGISTRY.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}


def _save_registry(jobs: dict) -> None:
    """Atomically rewrite the jobs registry on disk."""
    try:
        tmp = JOBS_REGISTRY.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(jobs, indent=2), encoding="utf-8")
        tmp.replace(JOBS_REGISTRY)
    except OSError:
        pass


def _job_state() -> dict:
    """Job table that survives Streamlit session resets / browser refreshes.

    Source of truth is the disk registry; ``st.session_state["jobs"]`` is
    a per-render cache. On the first render of a fresh session we hydrate
    from disk so a refresh doesn't lose track of in-flight subprocesses.
    """
    if "jobs" not in st.session_state:
        st.session_state["jobs"] = _load_registry()
    return st.session_state["jobs"]


def _orphan_logs(known_slugs: set[str], max_age_hours: int = 24) -> list[Path]:
    """Return recent log files in LOGS_DIR not currently tracked as a job.

    Surfaces subprocesses spawned by a now-dead Streamlit session so the
    user can still see their tail. Filters out the registry itself.
    """
    cutoff = time.time() - max_age_hours * 3600
    out: list[Path] = []
    for p in LOGS_DIR.glob("*.log"):
        try:
            if p.stat().st_mtime < cutoff:
                continue
        except OSError:
            continue
        # Slug = filename minus the trailing _<unix-ts>.log
        stem = p.stem
        if "_" in stem:
            slug = stem.rsplit("_", 1)[0]
        else:
            slug = stem
        # If this slug is already represented by a tracked job pointing at
        # this exact log path, skip — we don't want duplicates in the UI.
        registered = any(
            isinstance(j, dict) and j.get("log") == str(p)
            for j in _job_state().values()
        )
        if registered:
            continue
        out.append(p)
    out.sort(key=lambda x: x.stat().st_mtime, reverse=True)
    return out


def start_job(slug: str, argv: list[str]) -> bool:
    """Start a python subprocess for the given slug.

    Returns True on a successful spawn (job registered), False otherwise.
    On failure the reason is surfaced via ``st.error`` and a ``last_action``
    banner string in session_state — the user gets feedback no matter
    which tab they're on. Existing alive jobs for the same slug are left
    alone; the user must Stop them first.
    """
    jobs = _job_state()
    existing = jobs.get(slug)
    if existing and _is_alive(existing):
        msg = f"Job '{slug}' already running (PID {existing['pid']}). Stop it first or wait."
        st.warning(msg)
        st.session_state["last_action"] = ("warning", msg, time.time())
        return False

    log_path = LOGS_DIR / f"{slug}_{int(time.time())}.log"
    try:
        log_fp = log_path.open("w", encoding="utf-8")
        log_fp.write(f"# {slug} started {datetime.now().isoformat()}\n")
        log_fp.write(f"# argv: {' '.join(argv)}\n\n")
        log_fp.flush()
    except OSError as exc:
        msg = f"Could not open log file for '{slug}': {exc}"
        st.error(msg)
        st.session_state["last_action"] = ("error", msg, time.time())
        return False

    env = os.environ.copy()
    env["PYTHONUNBUFFERED"] = "1"
    env["PYTHONIOENCODING"] = "utf-8"

    try:
        proc = subprocess.Popen(
            argv,
            cwd=str(PROJECT_ROOT),
            stdout=log_fp,
            stderr=subprocess.STDOUT,
            env=env,
            creationflags=subprocess.CREATE_NEW_PROCESS_GROUP if os.name == "nt" else 0,
        )
    except (OSError, ValueError) as exc:
        msg = f"Popen failed for '{slug}': {exc}"
        st.error(msg)
        st.session_state["last_action"] = ("error", msg, time.time())
        try:
            log_fp.write(f"\nPopen failed: {exc}\n")
            log_fp.close()
        except OSError:
            pass
        return False

    jobs[slug] = {
        "pid": proc.pid,
        "log": str(log_path),
        "started": datetime.now().isoformat(timespec="seconds"),
        "argv": argv,
    }
    st.session_state["jobs"] = jobs
    _save_registry(jobs)
    msg = f"Started '{slug}' (PID {proc.pid}). Watch the Jobs tab for live log."
    st.toast(msg, icon="🟢")
    st.session_state["last_action"] = ("success", msg, time.time())
    return True


def _is_alive(job: dict) -> bool:
    """Cross-platform liveness check by PID without holding the Popen handle."""
    pid = job.get("pid")
    if not pid:
        return False
    try:
        if os.name == "nt":
            import ctypes

            PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
            STILL_ACTIVE = 259
            kernel32 = ctypes.windll.kernel32
            h = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
            if not h:
                return False
            try:
                code = ctypes.c_ulong()
                if kernel32.GetExitCodeProcess(h, ctypes.byref(code)):
                    return code.value == STILL_ACTIVE
                return False
            finally:
                kernel32.CloseHandle(h)
        else:
            os.kill(pid, 0)
            return True
    except OSError:
        return False


def stop_job(slug: str) -> None:
    jobs = _job_state()
    job = jobs.get(slug)
    if not job:
        return
    pid = job.get("pid")
    if not pid:
        return
    try:
        if os.name == "nt":
            os.kill(pid, signal.CTRL_BREAK_EVENT)
        else:
            os.killpg(pid, signal.SIGTERM)
    except (OSError, ProcessLookupError):
        pass


def tail_log(path: str, max_lines: int = 200) -> str:
    p = Path(path)
    if not p.exists():
        return ""
    try:
        lines = p.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return ""
    return "\n".join(lines[-max_lines:])


# ---------------------------------------------------------------------------
# Page sections
# ---------------------------------------------------------------------------

def section_overview() -> None:
    st.subheader("Data coverage")
    cov = coverage_snapshot()
    cols = st.columns(4)

    pd_min, pd_max, pd_n = cov["price_daily"]
    cols[0].metric("price_daily rows", f"{pd_n:,}", help=f"{pd_min} → {pd_max}")

    pi_min, pi_max, pi_n, pi_inst = cov["price_intraday"]
    cols[1].metric(
        "price_intraday rows",
        f"{pi_n:,}",
        help=f"instruments: {pi_inst}; range: {pi_min} → {pi_max}",
    )

    oc_min, oc_max, oc_n = cov["options_chain"]
    cols[2].metric(
        "options_chain snapshots", f"{oc_n:,}", help=f"{oc_min} → {oc_max}"
    )

    cols[3].metric("F&O universe", cov["fno_universe"])

    cols2 = st.columns(4)
    cols2[0].metric("RBI repo rows", cov["rbi_repo_rows"])
    vix_min, vix_max, vix_n = cov["vix"]
    cols2[1].metric("VIX rows", vix_n, help=f"{vix_min} → {vix_max}")
    cols2[2].metric("Ban-list rows", cov["ban_rows"])

    if cov["price_intraday_by_day"]:
        st.caption("Recent price_intraday coverage")
        st.dataframe(
            [
                {"date": str(d), "rows": n, "instruments": inst}
                for d, n, inst in cov["price_intraday_by_day"]
            ],
            use_container_width=True,
            hide_index=True,
        )


def section_backfill() -> None:
    st.subheader("Backfill data")
    st.caption(
        "Loaders are independent. Each runs as its own subprocess; logs stream below. "
        "Ban list / VIX / bhavcopy / RBI complete in seconds-to-minutes; Dhan intraday "
        "takes ~5 sec per instrument-day at the default 30 req/min."
    )

    today = date.today()
    default_start = today - timedelta(days=14)
    c1, c2 = st.columns(2)
    start = c1.date_input("Start date", default_start, key="backfill_start")
    end = c2.date_input("End date", today, key="backfill_end")

    aux_cols = st.columns(4)
    if aux_cols[0].button("Backfill ban list", use_container_width=True):
        start_job(
            "backfill_ban_list",
            [
                sys.executable, "-c",
                f"import asyncio; from datetime import date; "
                f"from src.quant.backtest.data_loaders.nse_ban_list_history import backfill; "
                f"print(asyncio.run(backfill(date({start.year},{start.month},{start.day}), "
                f"date({end.year},{end.month},{end.day}))))",
            ],
        )
    if aux_cols[1].button("Backfill VIX", use_container_width=True):
        start_job(
            "backfill_vix",
            [
                sys.executable, "-c",
                f"import asyncio; from datetime import date; "
                f"from src.quant.backtest.data_loaders.nse_vix_history import backfill; "
                f"print(asyncio.run(backfill(date({start.year},{start.month},{start.day}), "
                f"date({end.year},{end.month},{end.day}))))",
            ],
        )
    if aux_cols[2].button("Backfill bhavcopy", use_container_width=True):
        start_job(
            "backfill_bhavcopy",
            [
                sys.executable, "-c",
                f"import asyncio; from datetime import date; "
                f"from src.quant.backtest.data_loaders.nse_bhavcopy import backfill; "
                f"print(asyncio.run(backfill(date({start.year},{start.month},{start.day}), "
                f"date({end.year},{end.month},{end.day}))))",
            ],
        )
    if aux_cols[3].button("Backfill Dhan intraday", use_container_width=True):
        start_job(
            "backfill_dhan",
            [
                sys.executable, "-c",
                f"import asyncio; from datetime import date; "
                f"from src.quant.backtest.data_loaders.dhan_historical import "
                f"backfill, load_universe_from_db; "
                f"async def main(): "
                f"  insts = await load_universe_from_db(only_fno=True); "
                f"  print(await backfill(instruments=insts, "
                f"start_date=date({start.year},{start.month},{start.day}), "
                f"end_date=date({end.year},{end.month},{end.day}))); "
                f"asyncio.run(main())",
            ],
        )

    rbi_csv = st.text_input(
        "RBI repo CSV (optional re-seed)",
        value="data/rbi_repo.csv",
        key="rbi_csv",
    )
    if st.button("Load RBI CSV", use_container_width=False):
        start_job(
            "load_rbi",
            [
                sys.executable, "-c",
                f"import asyncio; from src.quant.backtest.data_loaders.rbi_repo_history "
                f"import load_from_csv; "
                f"print(asyncio.run(load_from_csv({rbi_csv!r}, source='rbi.org.in')))",
            ],
        )


def section_run() -> None:
    st.subheader("Run backtest")

    portfolios = list_portfolios()
    if not portfolios:
        st.warning("No portfolios in DB. Create one first.")
        return
    pf_labels = {f"{p['name']} ({p['id']})": p["id"] for p in portfolios}
    pf_choice = st.selectbox("Portfolio", list(pf_labels.keys()), key="run_pf")
    portfolio_id = pf_labels[pf_choice]

    today = date.today()
    c1, c2 = st.columns(2)
    start = c1.date_input("Start date", today - timedelta(days=5), key="run_start")
    end = c2.date_input("End date", today - timedelta(days=1), key="run_end")

    c3, c4, c5 = st.columns(3)
    seed = c3.number_input("Seed", value=42, step=1, key="run_seed")
    risk_free = c4.number_input(
        "Risk-free rate (decimal)", value=0.0525, step=0.0025, format="%.4f", key="run_rf"
    )
    smile = c5.selectbox("IV smile", ["linear", "flat"], key="run_smile")

    bcol1, bcol2 = st.columns([1, 4])
    if bcol1.button("Run backtest", type="primary"):
        if start > end:
            st.error("Start date must be ≤ end date.")
        else:
            start_job(
                "backtest_run",
                [
                    sys.executable, "-m", "scripts.backtest_run",
                    "--start-date", start.isoformat(),
                    "--end-date", end.isoformat(),
                    "--portfolio-id", str(portfolio_id),
                    "--seed", str(int(seed)),
                    "--risk-free-rate", f"{risk_free:.4f}",
                    "--smile-method", smile,
                ],
            )

    if bcol2.button("Run + generate report", type="secondary"):
        if start > end:
            st.error("Start date must be ≤ end date.")
        else:
            start_job(
                "backtest_run_and_report",
                [
                    sys.executable, "-m", "scripts.backtest_run_and_report",
                    "--start-date", start.isoformat(),
                    "--end-date", end.isoformat(),
                    "--portfolio-id", str(portfolio_id),
                    "--seed", str(int(seed)),
                    "--risk-free-rate", f"{risk_free:.4f}",
                    "--smile-method", smile,
                ],
            )


def section_llm_monitor() -> None:
    """LLM-feature-generator monitoring panel.

    Plan reference: docs/llm_feature_generator/implementation_plan.md §4.1.

    Reads from llm_decision_log + llm_calibration_models + fno_signals to
    render reliability, drift, three-way Sharpe, cost, drawdown, and the
    v9-vs-synthetic-v10 agreement matrix. Synchronous psycopg2 (matches
    the rest of this dashboard); the production scheduler uses the async
    helpers in ``src.fno.llm_monitoring``.
    """
    st.subheader("LLM feature generator — health panel")

    # --- Active calibration models ---
    st.markdown("**Active calibration models**")
    with _conn() as c, c.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        cur.execute("""
            SELECT feature_name, instrument_tier, method, n_observations,
                   cv_ece, cv_residual_var, fitted_at
            FROM llm_calibration_models
            WHERE is_active = TRUE
            ORDER BY feature_name, instrument_tier
        """)
        active_rows = [dict(r) for r in cur.fetchall()]
    if active_rows:
        st.dataframe(active_rows, hide_index=True, use_container_width=True)
    else:
        st.info("No active calibration models yet. Wait for the weekly Sunday-22:00 fit job.")

    # --- Feature drift (weekly means over last 28 days) ---
    st.markdown("**Feature drift — weekly mean of each LLM dimension**")
    with _conn() as c, c.cursor() as cur:
        cur.execute("""
            SELECT
                DATE_TRUNC('week', run_date)::DATE,
                AVG(directional_conviction),
                AVG(thesis_durability),
                AVG(catalyst_specificity),
                AVG(risk_flag),
                COUNT(*)
            FROM llm_decision_log
            WHERE prompt_version = 'v10_continuous'
              AND run_date >= CURRENT_DATE - INTERVAL '28 days'
            GROUP BY 1
            ORDER BY 1
        """)
        drift_rows = cur.fetchall()
    if drift_rows:
        st.dataframe(
            [
                {
                    "week": r[0].isoformat(),
                    "dc": r[1], "td": r[2], "cs": r[3], "rf": r[4],
                    "n": r[5],
                }
                for r in drift_rows
            ],
            hide_index=True, use_container_width=True,
        )
    else:
        st.info("No v10 rows yet — flip LAABH_LLM_MODE=shadow to start logging.")

    # --- Three-way Sharpe (v9 / v10 / deterministic) ---
    st.markdown("**Three-way Sharpe (30-day rolling)**")
    with _conn() as c, c.cursor() as cur:
        cur.execute("""
            SELECT COALESCE(l.prompt_version, 'v9') AS pv,
                   s.final_pnl::FLOAT AS pnl
            FROM fno_signals s
            JOIN fno_candidates c ON c.id = s.candidate_id
            LEFT JOIN llm_decision_log l
              ON l.run_date = c.run_date
             AND l.instrument_id = c.instrument_id
             AND l.phase = 'fno_thesis'
             AND l.prompt_version != 'v9'
            WHERE s.status = 'closed'
              AND s.closed_at >= NOW() - INTERVAL '30 days'
              AND s.final_pnl IS NOT NULL
        """)
        pnl_rows = cur.fetchall()
    pv_pnls: dict[str, list[float]] = {}
    for pv, pnl in pnl_rows:
        pv_pnls.setdefault(pv or "v9", []).append(float(pnl))

    import math as _math
    def _sharpe_local(p: list[float]) -> float | None:
        if len(p) < 5:
            return None
        arr = np.array(p, dtype=float)
        std = float(arr.std())
        if std == 0:
            return None
        return float(arr.mean() / std * _math.sqrt(252))

    sharpe_rows = []
    for pv in ("v9", "v10_continuous"):
        s = _sharpe_local(pv_pnls.get(pv, []))
        sharpe_rows.append({"pipeline": pv, "n_trades": len(pv_pnls.get(pv, [])), "sharpe": s})
    st.dataframe(sharpe_rows, hide_index=True, use_container_width=True)

    # --- Reliability diagrams (PNG per fit) ---
    st.markdown("**Reliability diagrams**")
    static_dir = PROJECT_ROOT / "apps" / "static" / "calibration"
    if static_dir.exists():
        pngs = sorted(static_dir.glob("*.png"), reverse=True)[:6]
        if pngs:
            cols = st.columns(min(len(pngs), 3))
            for i, p in enumerate(pngs):
                with cols[i % len(cols)]:
                    st.image(str(p), caption=p.stem, use_container_width=True)
        else:
            st.caption("No reliability PNGs yet — each calibration fit drops one here.")
    else:
        st.caption(f"PNG output directory not yet created: {static_dir}")


def section_jobs() -> None:
    st.subheader("Background jobs")
    jobs = _job_state()

    if jobs:
        for slug, job in list(jobs.items()):
            alive = _is_alive(job)
            status = "🟢 running" if alive else "✅ exited"
            with st.expander(
                f"{status} — {slug} (started {job['started']})", expanded=alive
            ):
                st.code(" ".join(job["argv"]), language="bash")
                log = tail_log(job["log"], max_lines=200)
                st.code(log or "(log empty)", language="text")
                cols = st.columns([1, 1, 4])
                if alive and cols[0].button("Stop", key=f"stop_{slug}"):
                    stop_job(slug)
                    st.toast(f"Sent stop signal to {slug}.")
                if cols[1].button("Forget", key=f"forget_{slug}"):
                    jobs.pop(slug, None)
                    st.session_state["jobs"] = jobs
                    _save_registry(jobs)
                    st.rerun()
    else:
        st.caption("No tracked jobs. Recent logs in `.dashboard_logs/` are listed below.")

    # Always surface orphan logs (subprocesses spawned by prior sessions or
    # CLI). Sorted newest first, capped to last 24h.
    orphans = _orphan_logs(set(jobs.keys()))
    if orphans:
        st.markdown("### Recent log files (orphaned / from prior sessions)")
        for p in orphans[:10]:
            mtime = datetime.fromtimestamp(p.stat().st_mtime).isoformat(timespec="seconds")
            with st.expander(f"📄 {p.name} (modified {mtime})", expanded=False):
                log = tail_log(str(p), max_lines=200)
                st.code(log or "(log empty)", language="text")


def section_runs_and_reports() -> None:
    st.subheader("Backtest runs & reports")

    portfolios = list_portfolios()
    pf_labels = {"All portfolios": None}
    pf_labels.update({p["name"]: p["id"] for p in portfolios})
    pf_choice = st.selectbox("Filter by portfolio", list(pf_labels.keys()), key="report_pf")
    portfolio_id = pf_labels[pf_choice]

    runs = list_backtest_runs(str(portfolio_id) if portfolio_id else None)
    if not runs:
        st.info("No backtest_runs rows yet. Use the Run tab to create one.")
        return

    table = []
    for r in runs:
        snav = float(r["starting_nav"]) if r["starting_nav"] is not None else 0.0
        fnav = float(r["final_nav"]) if r["final_nav"] is not None else None
        pnl = float(r["pnl_pct"]) if r["pnl_pct"] is not None else None
        table.append(
            {
                "date": str(r["backtest_date"]),
                "start_nav": f"{snav:,.2f}",
                "final_nav": f"{fnav:,.2f}" if fnav is not None else "—",
                "pnl_pct": f"{pnl * 100:+.4f}%" if pnl is not None else "—",
                "trades": r["trade_count"] or 0,
                "wins": r["winning_trades"] or 0,
                "seed": r["bandit_seed"],
                "completed": str(r["completed_at"])[:19] if r["completed_at"] else "",
            }
        )
    st.dataframe(table, use_container_width=True, hide_index=True)

    st.markdown("---")
    st.markdown("### Generate / view report")

    if portfolio_id is None:
        st.caption("Pick a single portfolio above to generate a range report.")
        return

    dates_present = sorted({r["backtest_date"] for r in runs})
    c1, c2 = st.columns(2)
    rep_start = c1.date_input(
        "Range start", dates_present[0], key="rep_start"
    )
    rep_end = c2.date_input(
        "Range end", dates_present[-1], key="rep_end"
    )

    cols = st.columns(2)
    if cols[0].button("Generate report"):
        start_job(
            "report",
            [
                sys.executable, "-m", "scripts.backtest_report",
                "--start-date", rep_start.isoformat(),
                "--end-date", rep_end.isoformat(),
                "--portfolio-id", str(portfolio_id),
            ],
        )

    # List existing markdown reports
    reports = sorted(REPORTS_DIR.glob("backtest_*.md"), reverse=True)
    if reports:
        names = [r.name for r in reports]
        choice = st.selectbox("View existing report", ["—"] + names, key="rep_choice")
        if choice and choice != "—":
            md = (REPORTS_DIR / choice).read_text(encoding="utf-8")
            st.markdown(md)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def _last_action_banner(ttl_sec: int = 30) -> None:
    """Render a persistent status line for the most recent ``start_job`` call.

    Streamlit's ``st.toast`` is consumed in one rerun; the auto-refresh
    swallows it before the user notices. Mirroring the same message into
    a session-state cell with a TTL means the user sees the result of
    their click for at least ``ttl_sec`` seconds, on whichever tab they're on.
    """
    last = st.session_state.get("last_action")
    if not last:
        return
    kind, msg, ts = last
    if time.time() - ts > ttl_sec:
        st.session_state.pop("last_action", None)
        return
    if kind == "success":
        st.success(msg, icon="🟢")
    elif kind == "warning":
        st.warning(msg, icon="🟡")
    elif kind == "error":
        st.error(msg, icon="🔴")
    else:
        st.info(msg)


def _activity_banner() -> dict:
    """Render the top-of-page activity strip and return the activity dict."""
    act = _activity_delta()
    phase, _ = _phase_label(act)

    cols = st.columns([3, 2, 2, 2])
    cols[0].markdown(f"### {phase}")

    def _delta_text(key: str, label: str) -> str:
        d = act["deltas"].get(key)
        cur = act["now"][key]
        if d is None:
            return f"{label}: {cur:,}"
        delta, elapsed = d
        if delta > 0:
            rate = delta / elapsed
            return f"{label}: {cur:,}  🟢 +{delta:,} ({rate:,.0f}/s)"
        return f"{label}: {cur:,}"

    cols[1].caption(_delta_text("price_intraday", "price_intraday"))
    cols[2].caption(_delta_text("backtest_runs", "backtest_runs"))
    cols[3].caption(_delta_text("backtest_trades", "backtest_trades"))
    return act


def main() -> None:
    st.set_page_config(
        page_title="Laabh — Backtest Dashboard",
        page_icon="🟢",
        layout="wide",
    )
    st.title("Laabh — Backtest dashboard")
    st.caption(
        f"Project: `{PROJECT_ROOT}` · "
        f"DB: `{DB_DSN['user']}@{DB_DSN['host']}/{DB_DSN['database']}`"
    )

    act = _activity_banner()
    _last_action_banner()
    st.divider()

    auto = st.sidebar.toggle(
        "Auto-refresh", value=True,
        help="Reruns the page on a fixed interval. Always on by default so external "
             "loaders (started from the terminal) show up in the activity banner.",
    )
    refresh_every = st.sidebar.slider(
        "Refresh interval (s)", min_value=2, max_value=30, value=5
    )
    only_when_busy = st.sidebar.toggle(
        "Only refresh when busy", value=False,
        help="If on, auto-refresh pauses once everything is idle (saves DB queries).",
    )
    if st.sidebar.button("Refresh now"):
        st.cache_data.clear()
        st.rerun()

    st.sidebar.markdown("---")
    st.sidebar.markdown(
        "**How to read this dashboard**\n\n"
        "1. **Overview** — current data coverage. Row counts grow when a loader is active.\n"
        "2. **Run** — pick a portfolio + date range, hit *Run backtest*.\n"
        "3. **Backfill** — manual buttons for each loader.\n"
        "4. **Runs & reports** — past `backtest_runs` rows + markdown reports.\n"
        "5. **Jobs** — subprocesses spawned *by this dashboard* (loaders started "
        "from the terminal won't appear here, but their writes show in the banner above)."
    )

    tabs = st.tabs(
        ["Overview", "Run", "Backfill", "Runs & reports", "Jobs", "LLM monitor"]
    )
    with tabs[0]:
        section_overview()
    with tabs[1]:
        section_run()
    with tabs[2]:
        section_backfill()
    with tabs[3]:
        section_runs_and_reports()
    with tabs[4]:
        section_jobs()
    with tabs[5]:
        section_llm_monitor()

    # Auto-refresh decision:
    #   * Always-on when ``auto`` is set and ``only_when_busy`` is not.
    #   * If ``only_when_busy``, only when a job (dashboard-spawned) is alive
    #     OR the activity banner detected positive deltas this rerun.
    if auto:
        any_delta = any(d[0] > 0 for d in act["deltas"].values())
        any_job_alive = any(_is_alive(j) for j in _job_state().values())
        if (not only_when_busy) or any_delta or any_job_alive:
            time.sleep(refresh_every)
            st.cache_data.clear()
            st.rerun()


if __name__ == "__main__":
    main()
