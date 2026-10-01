import logging
import os
import threading
import time
from contextlib import asynccontextmanager

from fastapi import FastAPI, Query
from fastapi.middleware.cors import CORSMiddleware

from app.routes.uploads import router as videos_router
from app.routes.auth import router as auth_router
from app.routes.admin import router as admin_router
from app.routes.cases import router as cases_router
from app.routes.plates import router as plates_router
from app.routes.evidence import router as evidence_router
from app.routes.status import router as status_router
from app.services import keepalive

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s — %(message)s")
logger = logging.getLogger("app")

APP_VERSION = "3.1.0"
BOOTED_AT = time.time()


@asynccontextmanager
async def lifespan(_: FastAPI):
    # Keep Render + Supabase awake and run DB housekeeping; see app/services/keepalive.py.
    keepalive.start()
    yield
    keepalive.stop()


app = FastAPI(
    title="RoadWatch.AI Backend",
    version=APP_VERSION,
    lifespan=lifespan,
)

# Allow the web client (local dev servers and the deployed Vercel URL) to call the API.
# CORS_EXTRA_ORIGINS (comma-separated) adds a custom domain without a code change.
_extra_origins = [o.strip() for o in os.getenv("CORS_EXTRA_ORIGINS", "").split(",") if o.strip()]
app.add_middleware(
    CORSMiddleware,
    allow_origins=[
        # Local development: static server, Vite, CRA
        "http://localhost:5051",
        "http://127.0.0.1:5051",
        "http://localhost:5173",
        "http://127.0.0.1:5173",
        "http://localhost:3000",
        # Vercel production
        "https://raksharider.vercel.app",
        *_extra_origins,
    ],
    # allow_origins does EXACT string matching, so "https://raksharider-*.vercel.app"
    # never matched anything. Preview deployments need a regex.
    allow_origin_regex=r"https://raksharider(-[a-z0-9-]+)?\.vercel\.app",
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Register routers
app.include_router(auth_router)
app.include_router(videos_router)
app.include_router(admin_router)
app.include_router(cases_router)
app.include_router(plates_router)
app.include_router(evidence_router)
app.include_router(status_router)


@app.get("/")
def root():
    return {"message": "RoadWatch.AI backend running", "version": APP_VERSION, "docs": "/docs", "health": "/health", "status": "/status"}


# Cached Supabase probe, so Render's frequent health checks do not hammer the DB. It touches
# every table the backend reads with the service role (HEAD requests, no rows) and names the
# ones that refuse — a missing GRANT then shows up here instead of as a 500 on some route.
_db_probe = {"at": 0.0, "result": None}
_db_probe_lock = threading.Lock()
DB_PROBE_TTL_S = 60
DB_PROBE_BUDGET_S = float(os.environ.get("DB_PROBE_BUDGET_S", "8"))   # wall-clock cap for one probe
CRITICAL_TABLES = ("system_settings", "audit_log", "videos", "cases", "findings", "vehicle_records",
                   "violation_policy", "rejection_reasons", "profiles", "evidence", "escalations", "withdrawal_requests")
GRANTS_HINT = "Run backend/database/008_service_role_grants.sql in the Supabase SQL editor"
KEY_HINT = "Supabase rejected the service key: check SUPABASE_SERVICE_ROLE_KEY in the Render environment"


def _is_permission_error(e: Exception) -> bool:
    """A missing GRANT is SQLSTATE 42501 / 'permission denied'. A bare HTTP 403 is not one (a
    gateway or WAF block looks the same at the status level) and must not point at 008."""
    code = str(getattr(e, "code", "") or "")
    text = str(e).lower()
    return code == "42501" or "permission denied" in text or "'code': '42501'" in text


def _is_key_error(e: Exception) -> bool:
    """Kong answers 401 'Invalid API key' when the service key is wrong or was rotated."""
    code = str(getattr(e, "code", "") or "")
    text = str(e).lower()
    return code == "401" or "invalid api key" in text or "'code': 401" in text


def probe_database(force: bool = False) -> dict:
    def fresh():
        r = _db_probe["result"]
        return r if (r is not None and (time.time() - _db_probe["at"]) < DB_PROBE_TTL_S and not force) else None
    cached = fresh()
    if cached is not None:
        return {**cached, "cached": True}
    with _db_probe_lock:                 # Render, GitHub and the keep-alive thread share one probe
        cached = fresh()
        if cached is not None:
            return {**cached, "cached": True}
        result = _run_probe()
        _db_probe["at"], _db_probe["result"] = time.time(), result   # stamped AFTER the probe, so the TTL is real
    return {**result, "cached": False}


def _run_probe() -> dict:
    # health_client: same service key, but a short HTTP timeout, so a stalled database cannot
    # pin a request thread for minutes (the keep-alive pingers give up after 30-90 s anyway).
    from app.database.supabase import health_client, missing_env
    if missing_env():
        return {"ok": False, "error": "not configured"}
    started = time.time()
    denied, errors, skipped, checked, key_rejected = {}, {}, [], 0, False
    for i, table in enumerate(CRITICAL_TABLES):
        if time.time() - started > DB_PROBE_BUDGET_S:
            skipped.append(table)
            continue
        checked += 1
        last = None
        for _attempt in (1, 2):   # one retry: a cold instance drops the odd connection on its first requests
            try:
                health_client.table(table).select("*", head=True).limit(1).execute()   # HEAD: no rows, no COUNT(*)
                last = None
                break
            except Exception as e:
                last = e
                if _is_permission_error(e) or _is_key_error(e):
                    break
        if last is None:
            continue
        if _is_permission_error(last):
            denied[table] = str(last)[:160]   # a GRANT problem is per table: keep probing the rest
            continue
        errors[table] = str(last)[:160]
        key_rejected = key_rejected or _is_key_error(last)
        # a connection failure, timeout or gateway block hits every table alike: stop paying for it
        skipped.extend(CRITICAL_TABLES[i + 1:])
        break
    result = {"ok": not denied and not errors, "latency_ms": int((time.time() - started) * 1000),
              "tables_checked": checked, "denied": denied, "errors": errors}
    if skipped:
        result["skipped"] = skipped
    if denied:
        result["error"] = f"{len(denied)} table(s) refused the service role: " + ", ".join(denied)
        result["hint"] = GRANTS_HINT
    elif errors:
        result["error"] = f"{len(errors)} table(s) could not be reached: " + ", ".join(errors)
        if key_rejected:
            result["hint"] = KEY_HINT
    return result


@app.get("/health")
def health(deep: int = Query(default=0, ge=0, le=1)):
    """
    Liveness + configuration. `?deep=1` also probes Supabase (cached 60 s) — the keep-alive
    pingers use it so every ping counts as database activity and the free project never pauses.
    A deploy that boots but cannot reach Supabase says so here instead of failing later on a
    request the user cannot interpret.
    """
    from app.database.supabase import missing_env

    absent = missing_env()
    body = {
        "status": "healthy" if not absent else "misconfigured",
        "message": "Backend is running" if not absent else "Missing environment variables: " + ", ".join(absent),
        "version": APP_VERSION,
        "uptime_s": int(time.time() - BOOTED_AT),
        "missing_env": absent,
        "azure_evidence_configured": bool(os.getenv("AZURE_STORAGE_CONNECTION_STRING")),
        "keepalive": keepalive.status(),
    }
    if deep:
        db = probe_database()
        body["database"] = db
        if not db.get("ok") and not absent:
            body["status"] = "degraded"
    return body
