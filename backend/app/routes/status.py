"""
GET /status — public, cached, aggregate-only view of the platform.

Powers the landing-page stats bar and the "system status" strip every signed-in
portal shows. Nothing here is per-user: counts, the worker heartbeat and the intake
switch only. Cached for STATUS_TTL_S so the keep-alive pingers and anonymous
visitors never turn into a query storm on the database.
"""
from __future__ import annotations

import logging
import threading
import time
from datetime import datetime, timezone
from typing import Any, Dict, Optional

from fastapi import APIRouter

from app.database.supabase import missing_env, supabase

logger = logging.getLogger(__name__)
router = APIRouter(tags=["Status"])

STATUS_TTL_S = 60
WORKER_ONLINE_WINDOW_S = 10 * 60      # matches check_idle_alerts' "worker silent" threshold
_cache: Dict[str, Any] = {"at": 0.0, "data": None}
_lock = threading.Lock()


def _count(table: str, **filters) -> Optional[int]:
    """Row count via a HEAD request. None when the query fails so the UI can show '—', not 0."""
    try:
        q = supabase.table(table).select("id", count="exact", head=True)
        for col, val in filters.items():
            q = q.eq(col, val)
        res = q.execute()
        return res.count if res.count is not None else len(res.data or [])
    except Exception as e:
        logger.warning("[status] count %s %s failed: %s", table, filters, e)
        return None


def _setting(key: str):
    try:
        rows = supabase.table("system_settings").select("value").eq("key", key).limit(1).execute().data
        return rows[0]["value"] if rows else None
    except Exception as e:
        logger.warning("[status] setting %s failed: %s", key, e)
        return None


def _parse(ts) -> Optional[datetime]:
    if not ts:
        return None
    try:
        return datetime.fromisoformat(str(ts).strip('"').replace("Z", "+00:00"))
    except Exception:
        return None


def build_status() -> dict:
    now = datetime.now(timezone.utc)
    absent = missing_env()
    if absent:
        return {"ok": False, "generated_at": now.isoformat(), "database": "misconfigured",
                "missing_env": absent, "worker": {"online": False, "last_seen": None}, "intake_paused": None,
                "queue": {"depth": None, "processing": None}, "totals": {}}
    last_seen = _parse(_setting("worker_last_seen"))
    worker_online = bool(last_seen) and (now - last_seen).total_seconds() < WORKER_ONLINE_WINDOW_S
    paused = _setting("worker_paused")
    totals = {
        "clips_submitted": _count("videos"),
        "clips_analysed": _count("videos", status="processed"),
        "cases_opened": _count("cases"),
        "cases_decided": _count("cases", status="finalized"),
        "findings_confirmed": _count("findings", decision="confirmed"),
        "findings_rejected": _count("findings", decision="rejected"),
    }
    return {
        "ok": True,
        "generated_at": now.isoformat(),
        "database": "ok",
        "worker": {"online": worker_online, "last_seen": last_seen.isoformat() if last_seen else None,
                   "silent_for_s": int((now - last_seen).total_seconds()) if last_seen else None},
        "intake_paused": bool(paused) if paused is not None else False,
        "queue": {"depth": _count("videos", status="unprocessed"), "processing": _count("videos", status="processing")},
        "totals": totals,
    }


def cached_status(force: bool = False) -> dict:
    with _lock:
        fresh = _cache["data"] is not None and (time.time() - _cache["at"]) < STATUS_TTL_S
        if fresh and not force:
            return {**_cache["data"], "cached": True}
        try:
            data = build_status()
        except Exception as e:
            logger.exception("[status] build failed")
            data = {"ok": False, "generated_at": datetime.now(timezone.utc).isoformat(), "database": "error",
                    "error": str(e)[:200], "worker": {"online": False, "last_seen": None}, "intake_paused": None,
                    "queue": {"depth": None, "processing": None}, "totals": {}}
        _cache["data"], _cache["at"] = data, time.time()
        return {**data, "cached": False}


@router.get("/status")
def public_status():
    """Aggregate platform status (public; cached 60 s)."""
    return cached_status()
