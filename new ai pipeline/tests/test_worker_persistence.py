"""
tests/test_worker_persistence.py
────────────────────────────────
The worker hands ONE validated package to ONE database function in ONE
transaction, never marks a job processed itself, and fails the job with a
category on any error. Fake psycopg2 connection — no database.
"""

from __future__ import annotations

import json

import pytest

import worker
from pipeline.contract import CONTRACT_VERSION, VehicleRecord, finding_id
from pipeline.vehicle_state import VehicleObservation, VehicleState


class FakeCursor:
    def __init__(self, conn, fail_on=None):
        self.conn, self.fail_on = conn, fail_on
    def __enter__(self): return self
    def __exit__(self, *a): return False
    def execute(self, sql, params=None):
        if self.fail_on and self.fail_on in sql:
            raise RuntimeError(f"simulated failure on: {self.fail_on}")
        self.conn.executed.append((" ".join(sql.split()), params))
    def fetchone(self):
        return ({"ok": True},)


class FakeConn:
    def __init__(self, fail_on=None):
        self.executed, self.commits, self.rollbacks, self.fail_on = [], 0, 0, fail_on
    def cursor(self, **kw): return FakeCursor(self, self.fail_on)
    def commit(self): self.commits += 1
    def rollback(self): self.rollbacks += 1


def _record(track_id=1) -> dict:
    s = VehicleState(track_id=track_id, vehicle_class="motorcycle", plate_text="MH02FX9484", plate_confidence=0.8,
                     identity_status="resolved", plate_method="ocr")
    for i in range(4):
        s.add_observation(VehicleObservation(i, i * 0.5, "detector", "present", True, 0.9))
        s.add_observation(VehicleObservation(i, i * 0.5, "rules", "no_helmet", True, 0.9))
    s.evidence.append(f"tracks/{track_id}/no_helmet/t0.000s.jpg")
    return VehicleRecord.from_dict(s.to_dict()).to_dict()


def _package(run_id="run1", video_id="vid-1") -> dict:
    rec = _record()
    fid = finding_id(run_id, 1, "no_helmet")
    return {
        "contract_version": CONTRACT_VERSION, "run_id": run_id, "submission_id": video_id, "pipeline_version": "3.0.0",
        "model_versions": {"coco": "yolov8n.pt"}, "started_at": "t0", "finished_at": "t1", "duration_seconds": 5.0,
        "allegation": {"declared_violation": "no_helmet", "claimed_plate": "MH02FX9484", "vehicle_type": "two_wheeler",
                       "subject_track_id": 1, "answer": "supported", "reason": "confirmed"},
        "vehicle_tracks": [rec], "plate_observations": [{"track_id": 1, "text": "MH02FX9484", "engine": "easyocr",
                                                          "confidence": 0.8, "frame_index": 0, "timestamp": 0.0}],
        "findings": [{"finding_id": fid, "track_id": 1, "violation": "no_helmet", "result": "confirmed", "tier": "A",
                      "evidence_frames": 4, "evaluable_frames": 4, "agreement": 1.0, "confidence": 0.9, "reasoning": "x",
                      "evidence": ["tracks/1/no_helmet/t0.000s.jpg"], "vlm_call_id": None}],
        "evidence": [{"finding_id": fid, "track_id": 1, "frame_index": 0, "timestamp": 0.0,
                      "path": "tracks/1/no_helmet/t0.000s.jpg", "sha256": "a" * 64}],
        "vlm_calls": [], "summary": {"total_vehicles_tracked": 1, "findings_queued": 1}, "detection_video": "detection.mp4",
    }


def _report(package):
    return {**package, "vehicles_v2": package["vehicle_tracks"], "meta": {"duration_seconds": 5.0}}


def test_persist_is_one_rpc_call_in_one_transaction():
    conn = FakeConn()
    worker.persist_package(conn, _package())
    assert len(conn.executed) == 1
    sql, params = conn.executed[0]
    assert sql.startswith("SELECT public.persist_run_result(")
    assert json.loads(params[0])["run_id"] == "run1"
    assert conn.commits == 1 and conn.rollbacks == 0


def test_persist_failure_rolls_back_and_raises():
    conn = FakeConn(fail_on="persist_run_result")
    with pytest.raises(RuntimeError):
        worker.persist_package(conn, _package())
    assert conn.rollbacks == 1 and conn.commits == 0


def test_process_video_success_path(monkeypatch, tmp_path):
    marked = {}
    pkg = _package(run_id=worker.run_id_for("vid-1", 2))
    monkeypatch.setattr(worker, "download_video", lambda video, d: tmp_path / "v.mp4")
    monkeypatch.setattr(worker, "pipeline_run", lambda *a, **kw: _report(pkg))
    monkeypatch.setattr(worker, "upload_artifacts", lambda *a, **k: {"tracks/1/no_helmet/t0.000s.jpg": "vid-1/run/x.jpg"})
    monkeypatch.setattr(worker, "mark_failed", lambda vid, reason, category="pipeline": marked.update({vid: (category, reason)}))
    conn = FakeConn()
    assert worker.process_video(conn, {"id": "vid-1", "blob_url": "https://x/v.mp4", "attempts": 2}, None, False) is True
    assert marked == {} and conn.commits == 1
    sent = json.loads(conn.executed[0][1][0])
    assert sent["blob_prefix"] == "vid-1/" + worker.run_id_for("vid-1", 2) + "/"
    assert sent["uploaded"]["tracks/1/no_helmet/t0.000s.jpg"] == "vid-1/run/x.jpg"


def test_process_video_marks_failed_with_category_when_persist_fails(monkeypatch, tmp_path):
    marked = {}
    pkg = _package(run_id=worker.run_id_for("vid-9", 1))
    monkeypatch.setattr(worker, "download_video", lambda video, d: tmp_path / "v.mp4")
    monkeypatch.setattr(worker, "pipeline_run", lambda *a, **kw: _report(pkg))
    monkeypatch.setattr(worker, "upload_artifacts", lambda *a, **k: {})
    monkeypatch.setattr(worker, "mark_failed", lambda vid, reason, category="pipeline": marked.update({vid: (category, reason)}))
    conn = FakeConn(fail_on="persist_run_result")
    assert worker.process_video(conn, {"id": "vid-9", "blob_url": "https://x/v.mp4", "attempts": 1}, None, False) is False
    assert marked["vid-9"][0] == "persist" and "simulated failure" in marked["vid-9"][1]
    assert conn.commits == 0


def test_process_video_rejects_corrupt_package(monkeypatch, tmp_path):
    """A finding pointing at an unknown track never reaches the database."""
    marked = {}
    pkg = _package(run_id=worker.run_id_for("vid-2", 1))
    pkg["findings"][0]["track_id"] = 99
    monkeypatch.setattr(worker, "download_video", lambda video, d: tmp_path / "v.mp4")
    monkeypatch.setattr(worker, "pipeline_run", lambda *a, **kw: _report(pkg))
    monkeypatch.setattr(worker, "mark_failed", lambda vid, reason, category="pipeline": marked.update({vid: (category, reason)}))
    conn = FakeConn()
    assert worker.process_video(conn, {"id": "vid-2", "blob_url": "https://x/v.mp4", "attempts": 1}, None, False) is False
    assert "AssertionError" in marked["vid-2"][1] and conn.executed == []


def test_run_id_is_deterministic_per_attempt():
    assert worker.run_id_for("v", 1) == worker.run_id_for("v", 1)
    assert worker.run_id_for("v", 1) != worker.run_id_for("v", 2)


def test_artifact_blob_names_are_fully_qualified(monkeypatch, tmp_path):
    (tmp_path / "tracks" / "1" / "no_helmet").mkdir(parents=True)
    (tmp_path / "tracks" / "1" / "no_helmet" / "t0.000s.jpg").write_bytes(b"x")
    (tmp_path / "detection.mp4").write_bytes(b"x")
    names = []
    monkeypatch.setattr(worker, "_azure_ok", True)
    monkeypatch.setattr(worker, "_azure_upload", lambda local, blob_name: names.append(blob_name) or f"https://blob/{blob_name}")
    out = worker.upload_artifacts("vid-1", "run-1", tmp_path, _package())
    assert names == ["vid-1/run-1/tracks/1/no_helmet/t0.000s.jpg", "vid-1/run-1/detection.mp4"]
    assert out["detection.mp4"] == "vid-1/run-1/detection.mp4"


def test_require_env_fails_loudly(monkeypatch):
    for k in worker.REQUIRED_ENV:
        monkeypatch.delenv(k, raising=False)
    with pytest.raises(SystemExit):
        worker.require_env()


def test_upload_failure_is_a_storage_failure_not_a_silent_drop(monkeypatch, tmp_path):
    """A case whose evidence never reached storage must fail the job, not be persisted."""
    monkeypatch.setattr(worker, "_azure_ok", True)
    monkeypatch.setattr(worker, "_azure_upload", lambda local, blob_name: None)   # upload refused
    (tmp_path / "tracks" / "1" / "no_helmet").mkdir(parents=True)
    (tmp_path / "tracks" / "1" / "no_helmet" / "t0.000s.jpg").write_bytes(b"x")
    (tmp_path / "detection.mp4").write_bytes(b"x")
    with pytest.raises(RuntimeError, match="could not be stored"):
        worker.upload_artifacts("vid-1", "run-1", tmp_path, _package())


def test_missing_evidence_file_raises(monkeypatch, tmp_path):
    monkeypatch.setattr(worker, "_azure_ok", True)
    monkeypatch.setattr(worker, "_azure_upload", lambda local, blob_name: "https://blob/x")
    with pytest.raises(RuntimeError, match="not written"):
        worker.upload_artifacts("vid-1", "run-1", tmp_path, _package())


def test_no_azure_with_evidence_raises(monkeypatch, tmp_path):
    monkeypatch.setattr(worker, "_azure_ok", False)
    with pytest.raises(RuntimeError, match="azure-storage-blob"):
        worker.upload_artifacts("vid-1", "run-1", tmp_path, _package())


def test_process_video_marks_storage_category(monkeypatch, tmp_path):
    marked = {}
    pkg = _package(run_id=worker.run_id_for("vid-7", 1))
    monkeypatch.setattr(worker, "download_video", lambda video, d: tmp_path / "v.mp4")
    monkeypatch.setattr(worker, "pipeline_run", lambda *a, **kw: _report(pkg))
    monkeypatch.setattr(worker, "upload_artifacts", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("blob down")))
    monkeypatch.setattr(worker, "mark_failed", lambda vid, reason, category="pipeline": marked.update({vid: (category, reason)}))
    conn = FakeConn()
    assert worker.process_video(conn, {"id": "vid-7", "blob_url": "https://x/v.mp4", "attempts": 1}, None, False) is False
    assert marked["vid-7"][0] == "storage" and conn.executed == []
