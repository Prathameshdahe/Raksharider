"""
worker.py
---------
Supabase queue polling worker for the DriveTrust AI pipeline.

Wraps run_pipeline.run() with a polling loop:
  1. Calls claim_next_video() to atomically claim an unprocessed video
  2. Downloads the video from Supabase Storage to a temp file
  3. Runs the existing pipeline (run_pipeline.run)
  4. Uploads evidence frames to Azure Blob Storage
  5. Inserts vehicle_records and violations rows into Supabase
  6. Marks the video as 'processed' or 'failed' with error_reason

Does NOT modify run_pipeline.py — only wraps it.

Usage:
    python worker.py [--interval 0.5] [--poll 10] [--vlm] [--once]

Options:
    --interval FLOAT   Frame sampling interval in seconds (default: 0.5)
    --poll     INT     Seconds between queue checks when idle (default: 10)
    --vlm              Enable VLM tiebreaker (Gemini Vision)
    --once             Process one video then exit (for testing)
"""

from __future__ import annotations

import argparse
import logging
import os
import shutil
import sys
import tempfile
import time
import uuid
from pathlib import Path

import psycopg2
from psycopg2.extras import RealDictCursor
from dotenv import load_dotenv

load_dotenv(dotenv_path=Path(__file__).parent / ".env")

# ── Logging ─────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s — %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger("worker")

# ── Pipeline import ──────────────────────────────────────────────────────────
from run_pipeline import run as pipeline_run

# ── Azure Blob Storage (evidence upload) — graceful if not configured ────────
try:
    from azure_storage import upload_all_evidence
    _azure_ok = True
except ImportError:
    _azure_ok = False
    logger.warning("[azure] azure-storage-blob not installed — evidence will stay local")

# ── Supabase REST (for Storage download + table inserts via HTTP) ────────────
import urllib.request
import json

SUPABASE_URL = os.environ.get("SUPABASE_URL", "https://fbjjoktuzirhpqqpzfbo.supabase.co")
# Worker uses the service role to bypass RLS for inserts
SUPABASE_SERVICE_KEY = os.environ.get(
    "SUPABASE_SERVICE_KEY",
    "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJpc3MiOiJzdXBhYmFzZSIsInJlZiI6ImZiampva3R1emlyaHBxcXB6ZmJvIiwicm9sZSI6InNlcnZpY2Vfcm9sZSIsImlhdCI6MTc4NTQyMDUzNywiZXhwIjoyMTAwOTk2NTM3fQ.x_IhQuYZvB_JWvkab2GyPhzaIXv9f_ETst0vCGHvVUI"
)


# ── DB connection (for claim_next_video + status updates) ────────────────────
def get_db_conn():
    """Return a psycopg2 connection using env vars from .env"""
    return psycopg2.connect(
        host=os.environ["DB_HOST"],
        port=os.environ["DB_PORT"],
        dbname=os.environ["DB_NAME"],
        user=os.environ["DB_USER"],
        password=os.environ["DB_PASSWORD"],
        sslmode="require",
        connect_timeout=10,
    )


def supabase_request(method: str, path: str, body: dict | None = None) -> dict | list:
    """Make a REST API call to Supabase using the service_role key."""
    url = f"{SUPABASE_URL}/rest/v1/{path}"
    data = json.dumps(body).encode() if body else None
    headers = {
        "apikey": SUPABASE_SERVICE_KEY,
        "Authorization": f"Bearer {SUPABASE_SERVICE_KEY}",
        "Content-Type": "application/json",
        "Prefer": "return=representation",
    }
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            return json.loads(resp.read().decode())
    except urllib.error.HTTPError as e:
        err = e.read().decode()
        logger.error("Supabase REST %s %s -> %d %s", method, path, e.code, err)
        raise


# ── Step 1: Claim next unprocessed video ────────────────────────────────────
def claim_next_video() -> dict | None:
    """
    Call claim_next_video() Postgres function.
    Returns the video row dict, or None if the queue is empty.
    """
    conn = get_db_conn()
    try:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute("SELECT * FROM claim_next_video();")
            row = cur.fetchone()
        conn.commit()
        return dict(row) if row and row.get("id") else None
    except Exception as e:
        logger.error("claim_next_video failed: %s", e)
        conn.rollback()
        return None
    finally:
        conn.close()


# ── Step 2: Update video status ───────────────────────────────────────────────
def update_video_status(video_id: str, status: str, error_reason: str = None) -> None:
    """Update the status and optionally error_reason of a video row."""
    patch = {"status": status}
    if error_reason:
        patch["error_reason"] = error_reason[:2000]
    if status == "processed":
        from datetime import datetime, timezone
        patch["processed_at"] = datetime.now(timezone.utc).isoformat()

    try:
        supabase_request("PATCH", f"videos?id=eq.{video_id}", patch)
        logger.info("Video %s -> %s", video_id, status)
    except Exception as e:
        logger.error("Could not update video %s status: %s", video_id, e)


# ── Step 3: Download video from Supabase Storage ─────────────────────────────
def download_video(video: dict, dest_dir: Path) -> Path | None:
    """
    Download the video file from Supabase Storage to a temp file.
    Tries blob_url first (public URL), then video_url, then storage download API.
    Returns the local path or None on failure.
    """
    ext = ".mp4"
    filename = video.get("filename", "")
    if filename and "." in filename:
        ext = "." + filename.rsplit(".", 1)[-1]

    local_path = dest_dir / f"{video['id']}{ext}"

    # Try blob_url or video_url (public/signed URL)
    for url_key in ("blob_url", "video_url"):
        url = video.get(url_key)
        if url and url.startswith("http"):
            try:
                logger.info("Downloading from %s: %s", url_key, url)
                req = urllib.request.Request(url, headers={"User-Agent": "drivetrust-worker/1.0"})
                with urllib.request.urlopen(req, timeout=120) as resp, open(local_path, "wb") as f:
                    shutil.copyfileobj(resp, f)
                logger.info("Downloaded %s bytes -> %s", local_path.stat().st_size, local_path)
                return local_path
            except Exception as e:
                logger.warning("Download from %s failed: %s", url_key, e)

    # Fallback: Supabase Storage download API
    storage_path = video.get("local_path") or filename
    if storage_path:
        try:
            dl_url = f"{SUPABASE_URL}/storage/v1/object/videos/{storage_path}"
            logger.info("Trying Supabase Storage API: %s", dl_url)
            req = urllib.request.Request(dl_url, headers={
                "apikey": SUPABASE_SERVICE_KEY,
                "Authorization": f"Bearer {SUPABASE_SERVICE_KEY}",
            })
            with urllib.request.urlopen(req, timeout=120) as resp, open(local_path, "wb") as f:
                shutil.copyfileobj(resp, f)
            logger.info("Downloaded via storage API: %s bytes", local_path.stat().st_size)
            return local_path
        except Exception as e:
            logger.error("Storage API download failed: %s", e)

    logger.error("Cannot download video %s — no usable URL found", video["id"])
    return None


# ── Step 4: Insert vehicle_records ───────────────────────────────────────────
def insert_vehicle_records(video_id: str, vehicle_records: list) -> None:
    """Insert pipeline vehicle_records into the Supabase vehicle_records table."""
    if not vehicle_records:
        return

    rows = []
    for vr in vehicle_records:
        rows.append({
            "id": str(uuid.uuid4()),
            "video_id": video_id,
            "track_id": int(vr.get("track_id", 0)),
            "plate_text": vr.get("plate_text") or None,
            "vehicle_type": vr.get("vehicle_class") or "unknown",
            "frames_observed": int(vr.get("frames_observed", 0)),
            "first_seen": float(vr.get("first_seen", 0.0)),
            "last_seen": float(vr.get("last_seen", 0.0)),
            "review_status": "needs_review" if vr.get("has_violation") else "clear",
        })

    try:
        supabase_request("POST", "vehicle_records", rows)
        logger.info("Inserted %d vehicle_record(s) for video %s", len(rows), video_id)
    except Exception as e:
        logger.error("Failed to insert vehicle_records: %s", e)


# ── Step 5: Insert violations ─────────────────────────────────────────────────
def insert_violations(video_id: str, report: dict, vehicle_records: list) -> None:
    """Insert violations into the Supabase violations table."""
    from datetime import datetime, timezone

    report_id_pseudo = abs(hash(video_id)) % (10 ** 15)

    rows = []
    for vr in vehicle_records:
        for viol_name in (vr.get("confirmed_violations") or []):
            verdict = (vr.get("violation_verdicts") or {}).get(viol_name, {})
            conf = verdict.get("confidence", 0.8) if isinstance(verdict, dict) else 0.8
            rows.append({
                "report_id": report_id_pseudo,
                "violation_type": viol_name,
                "confidence": round(float(conf), 4),
                "created_at": datetime.now(timezone.utc).isoformat(),
            })

    clip_violations = report.get("violations_detected", [])
    existing_types = {r["violation_type"] for r in rows}
    for viol_name in clip_violations:
        if viol_name not in existing_types:
            rows.append({
                "report_id": report_id_pseudo,
                "violation_type": viol_name,
                "confidence": round(float(report.get("severity_score", 0.7)), 4),
                "created_at": datetime.now(timezone.utc).isoformat(),
            })

    if not rows:
        logger.info("No violations to insert for video %s", video_id)
        return

    try:
        supabase_request("POST", "violations", rows)
        logger.info("Inserted %d violation(s) for video %s", len(rows), video_id)
    except Exception as e:
        logger.error("Failed to insert violations: %s", e)


# ── Main processing function ──────────────────────────────────────────────────
def process_video(video: dict, interval: float, use_vlm: bool) -> bool:
    """
    Full processing cycle for one video.
    Returns True on success, False on failure.
    """
    video_id = video["id"]
    logger.info("=" * 60)
    logger.info("Processing video: %s (%s)", video_id, video.get("filename"))
    logger.info("=" * 60)

    tmpdir = Path(tempfile.mkdtemp(prefix="drivetrust_"))
    output_dir = Path("pipeline/evidence_output") / video_id

    try:
        # Download
        local_video = download_video(video, tmpdir)
        if not local_video:
            update_video_status(video_id, "failed", "Could not download video file")
            return False

        # Run pipeline
        logger.info("Running pipeline on %s ...", local_video)
        report = pipeline_run(
            source=str(local_video),
            interval=interval,
            output_dir=output_dir,
            use_tracker=True,
            use_vlm=use_vlm,
            vehicle_type=video.get("vehicle_type") or "two_wheeler",
        )

        if report is None:
            update_video_status(video_id, "failed", "Pipeline returned no report")
            return False

        # ── Upload evidence frames to Azure Blob Storage ──────────────────────
        evidence_urls = []
        if _azure_ok and output_dir.exists():
            logger.info("Uploading evidence frames to Azure Blob Storage...")
            uploaded = upload_all_evidence(str(output_dir))
            evidence_urls = uploaded or []
            if evidence_urls:
                logger.info("Azure upload: %d frame(s) uploaded", len(evidence_urls))
                for url in evidence_urls:
                    logger.info("  → %s", url)
            else:
                logger.warning(
                    "Azure returned no URLs — check AZURE_STORAGE_CONNECTION_STRING in .env"
                )
        elif not _azure_ok:
            logger.info(
                "[azure] Skipped — azure-storage-blob not installed. Evidence at: %s", output_dir
            )

        # ── Write to Supabase ─────────────────────────────────────────────────
        pipeline_vehicle_records = report.get("vehicle_records") or []

        insert_vehicle_records(video_id, pipeline_vehicle_records)
        insert_violations(video_id, report, pipeline_vehicle_records)

        # Store Azure evidence URLs on vehicle_records rows
        if evidence_urls:
            try:
                supabase_request(
                    "PATCH",
                    f"vehicle_records?video_id=eq.{video_id}",
                    {"evidence_urls": evidence_urls},
                )
                logger.info("Stored %d Azure URL(s) on vehicle_records", len(evidence_urls))
            except Exception as e:
                logger.warning("Could not update evidence_urls column: %s", e)

        # Mark complete
        update_video_status(video_id, "processed")
        logger.info("Video %s processed successfully.", video_id)
        return True

    except Exception as e:
        error_msg = f"{type(e).__name__}: {e}"
        logger.exception("Pipeline failed for video %s: %s", video_id, error_msg)
        update_video_status(video_id, "failed", error_msg)
        return False

    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)


# ── Poll loop ─────────────────────────────────────────────────────────────────
def run_worker(interval: float, poll_seconds: int, use_vlm: bool, once: bool) -> None:
    logger.info("=" * 60)
    logger.info("DriveTrust AI Worker — Queue Polling Mode")
    logger.info("  Supabase:    %s", SUPABASE_URL)
    logger.info("  Poll every:  %ds when idle", poll_seconds)
    logger.info("  VLM:         %s", "enabled" if use_vlm else "disabled")
    logger.info("  Once mode:   %s", "yes" if once else "no")
    logger.info("  Azure:       %s", "enabled" if _azure_ok else "disabled (install azure-storage-blob)")
    logger.info("=" * 60)

    try:
        conn = get_db_conn()
        conn.close()
        logger.info("DB connection: OK")
    except Exception as e:
        logger.error("Cannot connect to Supabase DB: %s", e)
        sys.exit(1)

    logger.info("Worker polling for videos... (Ctrl+C to stop)")

    idle_logged = False
    while True:
        try:
            video = claim_next_video()

            if video is None:
                if not idle_logged:
                    logger.info("Queue empty — waiting for new uploads...")
                    idle_logged = True
                time.sleep(poll_seconds)
                continue

            idle_logged = False
            process_video(video, interval=interval, use_vlm=use_vlm)

            if once:
                logger.info("--once flag set: exiting after one video.")
                break

        except KeyboardInterrupt:
            logger.info("Worker stopped by user.")
            break
        except Exception as e:
            logger.error("Unexpected error in poll loop: %s", e)
            time.sleep(poll_seconds)


# ── Entry point ───────────────────────────────────────────────────────────────
def main():
    parser = argparse.ArgumentParser(
        description="DriveTrust AI Worker — Supabase queue polling mode"
    )
    parser.add_argument("--interval", type=float, default=0.5,
                        help="Frame sampling interval in seconds (default: 0.5)")
    parser.add_argument("--poll", type=int, default=10,
                        help="Seconds between queue checks when idle (default: 10)")
    parser.add_argument("--vlm", action="store_true",
                        help="Enable VLM tiebreaker (Gemini Vision)")
    parser.add_argument("--once", action="store_true",
                        help="Process one video then exit (for testing)")
    args = parser.parse_args()

    run_worker(
        interval=args.interval,
        poll_seconds=args.poll,
        use_vlm=args.vlm,
        once=args.once,
    )


if __name__ == "__main__":
    main()
