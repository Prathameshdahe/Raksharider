"""
worker.py — RoadWatch queue worker (v3)

  claim_next_video()        priority-ordered, leased, self-healing (Postgres function)
  download                  from videos.blob_url
  run_pipeline.run()        → ResultPackage (contract 2.0), evidence + detection video on disk
  upload                    evidence to private blob storage under {video_id}/{run_id}/...
  persist_run_result(jsonb) ONE database call, ONE transaction, owned by the backend team;
                            idempotent on run_id; creates cases; marks the video processed
  failure                   videos.status='failed' + error_reason + error_category, nothing partial

The worker connects as the scoped role drivetrust_ai_worker. It has NO Supabase
service key: a service key bypasses row-level security. Missing env vars fail loudly.
run_id is deterministic per (video, attempt) so a retry of the same attempt is a no-op.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import logging
import os
import shutil
import sys
import tempfile
import threading
import time
import urllib.request
from pathlib import Path
from typing import Dict, List, Optional

import psycopg2
from psycopg2.extras import RealDictCursor
from dotenv import load_dotenv

load_dotenv(dotenv_path=Path(__file__).parent / ".env")
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s — %(message)s", datefmt="%H:%M:%S")
logger = logging.getLogger("worker")

from run_pipeline import run as pipeline_run
from pipeline.contract import validate_package

try:
    from azure_storage import upload_evidence as _azure_upload
    _azure_ok = True
except ImportError:
    _azure_ok = False
    logger.warning("[azure] azure-storage-blob not installed — evidence stays local")

REQUIRED_ENV = ("DB_HOST", "DB_PORT", "DB_NAME", "DB_USER", "DB_PASSWORD")
EVIDENCE_ROOT = Path("pipeline/evidence_output")


# ── Health endpoint (deployment plumbing only; the pipeline itself is untouched) ──────────────
# A hosted worker (Docker, Hugging Face Space, any PaaS) needs something to answer HTTP so the
# host can tell "running" from "crashed", keep-alive pingers can reach it, and an operator can
# see the last job without opening the database. Off by default; set WORKER_HTTP_PORT to enable
# (the Dockerfile sets 7860). Nothing here is ever written to the DB.

HEALTH = {
    "service": "roadwatch-ai-worker", "version": "3.1", "status": "starting",
    "started_at": None, "last_heartbeat_at": None, "last_claim_at": None,
    "jobs_done": 0, "jobs_failed": 0, "current": None, "last_job": None,
}


def _utcnow() -> str:
    from datetime import datetime, timezone
    return datetime.now(timezone.utc).isoformat()


def start_health_server() -> None:
    port = os.environ.get("WORKER_HTTP_PORT", "").strip()
    if not port:
        return
    import http.server
    import socketserver

    class _Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self):  # noqa: N802
            body = json.dumps(HEALTH, default=str).encode()
            self.send_response(200 if HEALTH["status"] != "crashed" else 503)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *_):  # quiet: pingers hit this every few minutes
            return

    class _Server(socketserver.ThreadingMixIn, http.server.HTTPServer):
        daemon_threads = True
        allow_reuse_address = True

    try:
        srv = _Server(("0.0.0.0", int(port)), _Handler)
    except Exception as e:
        logger.warning("[health] could not bind port %s: %s", port, e)
        return
    threading.Thread(target=srv.serve_forever, name="worker-health", daemon=True).start()
    logger.info("[health] GET http://0.0.0.0:%s/health", port)


# ── DB ────────────────────────────────────────────────────────────────────────

def require_env() -> None:
    missing = [k for k in REQUIRED_ENV if not os.environ.get(k)]
    if missing:
        raise SystemExit(f"Missing required env vars: {missing}. The worker never falls back to a service key.")
    if os.environ.get("SUPABASE_SERVICE_KEY") or os.environ.get("SUPABASE_SERVICE_ROLE_KEY"):
        logger.warning("A Supabase service key is present in the environment. The worker does not use it; remove it.")


def get_db_conn():
    return psycopg2.connect(host=os.environ["DB_HOST"], port=os.environ["DB_PORT"], dbname=os.environ["DB_NAME"],
                            user=os.environ["DB_USER"], password=os.environ["DB_PASSWORD"], sslmode="require", connect_timeout=10)


def claim_next_video(conn) -> Optional[dict]:
    with conn.cursor(cursor_factory=RealDictCursor) as cur:
        cur.execute("SELECT * FROM public.claim_next_video();")
        row = cur.fetchone()
    conn.commit()
    return dict(row) if row and row.get("id") else None


class LeaseRenewer:
    """
    Keeps videos.claimed_at fresh while a job runs.

    The queue reaps any video stuck in 'processing' past the lease window, but a
    single clip can legitimately take many minutes on CPU. Without renewal a slow
    job is reaped mid-run and processed twice. A daemon thread renews every
    `period` seconds and stops when the job ends.
    """

    def __init__(self, video_id: str, period: float = 60.0) -> None:
        self.video_id, self.period = video_id, period
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None

    def _loop(self) -> None:
        while not self._stop.wait(self.period):
            try:
                conn = get_db_conn()
                try:
                    with conn.cursor() as cur:
                        cur.execute("UPDATE public.videos SET claimed_at = now() "
                                    "WHERE id = %s AND status = 'processing';", (self.video_id,))
                    conn.commit()
                finally:
                    conn.close()
            except Exception as e:
                logger.debug("lease renewal skipped: %s", e)

    def __enter__(self) -> "LeaseRenewer":
        self._thread = threading.Thread(target=self._loop, daemon=True)
        self._thread.start()
        return self

    def __exit__(self, *exc) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=2.0)


def heartbeat(conn) -> None:
    try:
        with conn.cursor() as cur:
            cur.execute("UPDATE public.system_settings SET value = to_jsonb(now()::text), updated_at = now() WHERE key = 'worker_last_seen';")
        conn.commit()
        HEALTH["last_heartbeat_at"] = _utcnow()
    except Exception as e:
        conn.rollback()
        logger.debug("heartbeat skipped: %s", e)


def mark_failed(video_id: str, reason: str, category: str = "pipeline") -> None:
    """Own connection so it works even when the main transaction is dead."""
    try:
        conn = get_db_conn()
        try:
            with conn.cursor() as cur:
                cur.execute("UPDATE public.videos SET status = 'failed', error_reason = %s, error_category = %s WHERE id = %s;",
                            (reason[:2000], category, video_id))
            conn.commit()
            logger.info("Video %s -> failed [%s] %s", video_id, category, reason[:120])
        finally:
            conn.close()
    except Exception as e:
        logger.error("Could not mark video %s failed: %s", video_id, e)


def persist_package(conn, package: dict) -> dict:
    """ONE call, ONE transaction. The function raises (and nothing persists) on any invariant violation."""
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT public.persist_run_result(%s::jsonb);", (json.dumps(package, default=str),))
            row = cur.fetchone()
        conn.commit()
        return row[0] if row and row[0] is not None else {}
    except Exception:
        conn.rollback()
        raise


# ── Download / upload ─────────────────────────────────────────────────────────

def download_video(video: dict, dest_dir: Path) -> Optional[Path]:
    ext = ".mp4"
    filename = video.get("filename") or ""
    if "." in filename:
        ext = "." + filename.rsplit(".", 1)[-1]
    local_path = dest_dir / f"{video['id']}{ext}"
    for key in ("blob_url", "video_url"):
        url = video.get(key)
        if not (url and str(url).startswith("http")):
            continue
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "roadwatch-worker/3.0"})
            with urllib.request.urlopen(req, timeout=180) as resp, open(local_path, "wb") as f:
                shutil.copyfileobj(resp, f)
            if local_path.stat().st_size > 0:
                return local_path
        except Exception as e:
            logger.warning("Download from %s failed: %s", key, e)
    return None


def upload_artifacts(video_id: str, run_id: str, output_dir: Path, package: dict) -> Dict[str, str]:
    """
    Upload every evidence file and the detection video under immutable, fully
    qualified names. Raises if anything the package promises cannot be stored:
    a case whose evidence is missing is worse than a failed job, because the
    reviewer would be asked to decide on a finding they cannot see.
    """
    rels = [e["path"] for e in package.get("evidence", [])]
    if package.get("detection_video"):
        rels.append(package["detection_video"])
    if not rels:
        return {}
    if not _azure_ok:
        raise RuntimeError("azure-storage-blob is not installed; evidence cannot be stored")
    uploaded: Dict[str, str] = {}
    missing: List[str] = []
    for rel in rels:
        local = output_dir / rel
        if not local.exists():
            missing.append(f"{rel} (not written)")
            continue
        blob_name = f"{video_id}/{run_id}/{rel}"
        if _azure_upload(local, blob_name=blob_name):
            uploaded[rel] = blob_name
        else:
            missing.append(f"{rel} (upload failed)")
    if missing:
        raise RuntimeError(f"{len(missing)} artifact(s) could not be stored: {missing[:5]}")
    return uploaded


def run_id_for(video_id: str, attempt: int) -> str:
    """Deterministic per attempt → retrying the same attempt is a no-op in persist_run_result."""
    return hashlib.sha1(f"{video_id}:{attempt}".encode()).hexdigest()[:12]


# ── One job ───────────────────────────────────────────────────────────────────

def process_video(conn, video: dict, interval: Optional[float], use_vlm: bool) -> bool:
    video_id = str(video["id"])
    attempt = int(video.get("attempts") or 1)
    run_id = run_id_for(video_id, attempt)
    output_dir = EVIDENCE_ROOT / video_id / run_id
    logger.info("=" * 60)
    logger.info("Processing video %s (run %s, attempt %s, priority %s)", video_id, run_id, attempt, video.get("priority"))
    tmpdir = Path(tempfile.mkdtemp(prefix="roadwatch_"))
    try:
      with LeaseRenewer(video_id):
        local_video = download_video(video, tmpdir)
        if not local_video:
            mark_failed(video_id, "Could not download video file (no usable blob_url)", "download")
            return False
        try:
            report = pipeline_run(str(local_video), interval=interval, output_dir=output_dir, use_vlm=use_vlm,
                                  vehicle_type=video.get("vehicle_type") or "two_wheeler",
                                  declared_violation=video.get("declared_violation"),
                                  claimed_plate=video.get("claimed_plate"),
                                  submission_id=video_id, run_id=run_id)
        except AssertionError as e:
            mark_failed(video_id, f"AssertionError: {e}", "contract")
            return False
        except Exception as e:
            mark_failed(video_id, f"{type(e).__name__}: {e}", "pipeline")
            return False
        if report is None:
            mark_failed(video_id, "Pipeline returned no report", "pipeline")
            return False

        package = {k: report[k] for k in ("contract_version", "run_id", "submission_id", "pipeline_version", "model_versions",
                                          "started_at", "finished_at", "duration_seconds", "allegation", "vehicle_tracks",
                                          "plate_observations", "findings", "evidence", "vlm_calls", "summary", "detection_video")}
        validate_package(package)
        try:
            blob_map = upload_artifacts(video_id, run_id, output_dir, package)
        except Exception as e:
            mark_failed(video_id, f"{type(e).__name__}: {e}", "storage")
            return False
        package["blob_prefix"] = f"{video_id}/{run_id}/"
        package["uploaded"] = blob_map
        try:
            persist_package(conn, package)
        except Exception as e:
            mark_failed(video_id, f"{type(e).__name__}: {e}", "persist")
            return False
        logger.info("Video %s -> processed (%d vehicle(s), %d queued finding(s), %d file(s) uploaded)",
                    video_id, len(package["vehicle_tracks"]), package["summary"].get("findings_queued", 0), len(blob_map))
        return True
    except Exception as e:
        mark_failed(video_id, f"{type(e).__name__}: {e}", "pipeline")
        return False
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)


# ── Poll loop ─────────────────────────────────────────────────────────────────

def run_worker(interval: Optional[float], poll_seconds: int, use_vlm: bool, once: bool) -> None:
    require_env()
    HEALTH.update(status="idle", started_at=_utcnow(), role=os.environ["DB_USER"].split(".")[0],
                  vlm=use_vlm, azure=_azure_ok, frames="dense" if interval is None else f"every {interval}s")
    start_health_server()
    logger.info("RoadWatch worker v3 — role %s, poll %ds, VLM %s, Azure %s, frames %s",
                os.environ["DB_USER"], poll_seconds, "on" if use_vlm else "off", "on" if _azure_ok else "off",
                "dense" if interval is None else f"every {interval}s")
    while True:
        conn = None
        try:
            conn = get_db_conn()
            heartbeat(conn)
            video = claim_next_video(conn)
            if video:
                HEALTH.update(status="processing", last_claim_at=_utcnow(),
                              current={"video_id": str(video["id"]), "attempt": video.get("attempts"), "priority": video.get("priority")})
                started = time.time()
                ok = process_video(conn, video, interval, use_vlm)
                HEALTH["jobs_done" if ok else "jobs_failed"] += 1
                HEALTH.update(status="idle", current=None,
                              last_job={"video_id": str(video["id"]), "ok": ok, "seconds": int(time.time() - started), "finished_at": _utcnow()})
                if once:
                    return
                continue
            HEALTH["status"] = "idle"
            if once:
                logger.info("Queue empty — exiting (--once)")
                return
        except KeyboardInterrupt:
            return
        except Exception as e:
            HEALTH.update(status="degraded", last_error=f"{type(e).__name__}: {e}"[:300])
            logger.error("Worker loop error: %s", e)
        finally:
            if conn is not None:
                conn.close()
        time.sleep(poll_seconds)


def main():
    p = argparse.ArgumentParser(description="RoadWatch queue worker")
    p.add_argument("--interval", type=float, default=None, help="legacy sparse sampling; default dense (~15 fps)")
    p.add_argument("--poll", type=int, default=10)
    p.add_argument("--vlm", action="store_true")
    p.add_argument("--once", action="store_true")
    a = p.parse_args()
    run_worker(a.interval, a.poll, a.vlm, a.once)


if __name__ == "__main__":
    main()
