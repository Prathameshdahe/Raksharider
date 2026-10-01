import logging
import os
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
DB_PROBE_TTL_S = 60
CRITICAL_TABLES = ("system_settings", "audit_log", "videos", "cases", "findings", "vehicle_records",
                   "violation_policy", "rejection_reasons", "profiles", "evidence", "escalations", "withdrawal_requests")
GRANTS_HINT = "Run backend/database/008_service_role_grants.sql in the Supabase SQL editor"


def probe_database(force: bool = False) -> dict:
    now = time.time()
    if _db_probe["result"] is not None and (now - _db_probe["at"]) < DB_PROBE_TTL_S and not force:
        return {**_db_probe["result"], "cached": True}
    from app.database.supabase import missing_env, supabase
    if missing_env():
        result = {"ok": False, "error": "not configured"}
    else:
        started = time.time()
        denied = {}
        for table in CRITICAL_TABLES:
            try:
                supabase.table(table).select("*", count="exact", head=True).limit(1).execute()
            except Exception as e:
                denied[table] = str(e)[:160]
        result = {"ok": not denied, "latency_ms": int((time.time() - started) * 1000),
                  "tables_checked": len(CRITICAL_TABLES), "denied": denied}
        if denied:
            result["error"] = f"{len(denied)} table(s) refused the service role: " + ", ".join(denied)
            result["hint"] = GRANTS_HINT
    _db_probe["at"], _db_probe["result"] = now, result
    return {**result, "cached": False}


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
