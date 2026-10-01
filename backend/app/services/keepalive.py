"""
Keep the free-tier stack awake and run the DB housekeeping function.

Render free web services spin down after 15 minutes without inbound traffic and
Supabase pauses a free project after a week without API activity. One daemon
thread solves both: every KEEPALIVE_INTERVAL_S it

  1. GETs this service's own public /health?deep=1 (inbound traffic → Render stays up;
     the deep probe touches Supabase → the project stays active), and
  2. calls public.check_idle_alerts() — the 24 h idle alert / 48 h auto-release /
     worker-silent notification routine that nothing else schedules (pg_cron is not
     enabled on the project).

Render exposes RENDER_EXTERNAL_URL automatically, so no configuration is needed
there. Set KEEPALIVE_URL to override, KEEPALIVE_ENABLED=0 to switch it off (local dev).
A second, independent pinger lives in .github/workflows/keepalive.yml so a crashed or
freshly deployed service is woken from the outside as well.
"""
from __future__ import annotations

import logging
import os
import threading
import time
from datetime import datetime, timezone
from typing import Optional

logger = logging.getLogger(__name__)

DEFAULT_INTERVAL_S = 600          # Render idles after 900 s; 600 keeps a safe margin
STATE = {"last_ping_at": None, "last_ping_ok": None, "last_housekeeping_at": None, "last_error": None, "runs": 0}
_thread: Optional[threading.Thread] = None
_stop = threading.Event()


def enabled() -> bool:
    return os.getenv("KEEPALIVE_ENABLED", "1").strip().lower() not in ("0", "false", "no", "off")


def target_url() -> Optional[str]:
    base = (os.getenv("KEEPALIVE_URL") or os.getenv("RENDER_EXTERNAL_URL") or "").strip().rstrip("/")
    return f"{base}/health?deep=1" if base else None


def interval_s() -> int:
    try:
        return max(60, int(os.getenv("KEEPALIVE_INTERVAL_S", DEFAULT_INTERVAL_S)))
    except ValueError:
        return DEFAULT_INTERVAL_S


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _ping(url: str) -> bool:
    import urllib.request
    req = urllib.request.Request(url, headers={"User-Agent": "roadwatch-keepalive/1.0"})
    with urllib.request.urlopen(req, timeout=30) as resp:
        return 200 <= resp.status < 300


def _housekeeping() -> None:
    from app.database.supabase import supabase
    supabase.rpc("check_idle_alerts", {}).execute()


def tick(url: Optional[str]) -> None:
    """One keep-alive round. Never raises: a failure is recorded in STATE and retried next time."""
    STATE["runs"] += 1
    if url:
        try:
            STATE["last_ping_ok"] = _ping(url)
        except Exception as e:  # network blip, cold start, DNS — all retried next interval
            STATE["last_ping_ok"] = False
            STATE["last_error"] = f"ping: {e}"
            logger.warning("[keepalive] self-ping failed: %s", e)
        STATE["last_ping_at"] = _now()
    try:
        _housekeeping()
        STATE["last_housekeeping_at"] = _now()
    except Exception as e:
        STATE["last_error"] = f"check_idle_alerts: {e}"
        logger.warning("[keepalive] check_idle_alerts failed: %s", e)


def _loop() -> None:
    url = target_url()
    period = interval_s()
    logger.info("[keepalive] running every %ss → %s", period, url or "(no public URL; housekeeping only)")
    # first round shortly after boot so a fresh deploy registers a heartbeat immediately
    if not _stop.wait(20):
        tick(url)
    while not _stop.wait(period):
        tick(url)


def start() -> bool:
    """Start the daemon thread once. Returns True when running."""
    global _thread
    if not enabled():
        logger.info("[keepalive] disabled (KEEPALIVE_ENABLED=0)")
        return False
    if _thread and _thread.is_alive():
        return True
    _stop.clear()
    _thread = threading.Thread(target=_loop, name="keepalive", daemon=True)
    _thread.start()
    return True


def stop() -> None:
    _stop.set()


def status() -> dict:
    return {"enabled": enabled(), "running": bool(_thread and _thread.is_alive()), "interval_s": interval_s(),
            "target": target_url(), **STATE}
