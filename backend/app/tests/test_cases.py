"""Cases / findings routes (API.md §3, §4). The atomic rules (claim lock, 3-claim cap, second-opinion reviewer,
inconclusive → second_opinion → unverifiable, plate history + escalation) live in the SQL RPCs; here we test the
HTTP layer: role/ownership/state checks, validation, append-only writes, and the SQLSTATE → HTTP mapping."""
from app.tests.test_v3 import ADMIN, OTHER, USER, client  # noqa: F401  (fixture)


def _case(fake, status="in_review", claimed_by=OTHER, **kw):
    fake.tables["cases"] = [{"id": "c1", "video_id": "v1", "run_id": "r1", "track_id": 1, "vehicle_record_id": "rec1",
                             "is_subject": True, "lane": "normal", "status": status, "priority": 2, "cycle": 1,
                             "claimed_by": claimed_by, "claimed_at": "2026-09-17T10:00:00+00:00", **kw}]


# ── claim / release ─────────────────────────────────────────────────────────

def test_claim_maps_rpc_conflicts_to_409(client):
    fake, as_role = client
    _case(fake, status="pending_review", claimed_by=None)
    assert as_role("citizen").post("/cases/c1/claim").status_code == 403
    fake.rpc_results["claim_case"] = {"id": "c1", "status": "in_review", "claimed_by": OTHER}
    r = as_role("officer").post("/cases/c1/claim")
    assert r.status_code == 200 and r.json()["data"]["claimed_by"] == OTHER
    assert fake.rpc_calls[-1] == ("claim_case", {"p_case_id": "c1", "p_reviewer": OTHER})
    for msg in ("case already claimed by another reviewer", "you already hold 3 open claims",
                "second opinion must come from a different reviewer"):
        fake.rpc_errors["claim_case"] = ("P0409", msg)
        r = as_role("officer").post("/cases/c1/claim")
        assert r.status_code == 409 and r.json()["detail"] == {"success": False, "error": msg}
    fake.tables["cases"] = []
    assert as_role("officer").post("/cases/nope/claim").status_code == 404


def test_release_by_admin_is_audited(client):
    fake, as_role = client
    _case(fake)
    fake.rpc_results["release_case"] = {"id": "c1", "status": "pending_review"}
    assert as_role("officer").post("/cases/c1/release").status_code == 200 and not fake.writes("audit_log")
    assert as_role("admin").post("/cases/c1/release").status_code == 200
    assert fake.rpc_calls[-1] == ("release_case", {"p_case_id": "c1", "p_actor": ADMIN, "p_is_admin": True})
    assert fake.writes("audit_log")[0][2][0]["action"] == "case.release"
    fake.rpc_errors["release_case"] = ("P0403", "only the claimer or an admin can release this case")
    assert as_role("officer", USER).post("/cases/c1/release").status_code == 403


# ── decisions ───────────────────────────────────────────────────────────────

def test_decision_requires_claim_and_rejection_reason(client):
    fake, as_role = client
    _case(fake)
    url = "/cases/c1/findings/f1/decision"
    assert as_role("officer", USER).post(url, json={"decision": "confirmed"}).status_code == 409   # not the claimer
    c = as_role("officer")
    assert c.post(url, json={"decision": "rejected"}).status_code == 422
    assert c.post(url, json={"decision": "maybe"}).status_code == 422
    fake.rpc_results["decide_finding"] = {"id": "f1", "decision": "rejected", "rejection_reason": "helmet_worn"}
    r = c.post(url, json={"decision": "rejected", "rejection_reason": "helmet_worn", "note": "visor down, helmet on"})
    assert r.status_code == 200
    assert fake.rpc_calls[-1] == ("decide_finding", {"p_case_id": "c1", "p_finding_id": "f1", "p_reviewer": OTHER,
                                                     "p_decision": "rejected", "p_rejection_reason": "helmet_worn",
                                                     "p_note": "visor down, helmet on"})
    fake.rpc_errors["decide_finding"] = ("P0422", "rejection_reason is required and must be a known code")
    assert c.post(url, json={"decision": "rejected", "rejection_reason": "bogus"}).status_code == 422
    _case(fake, status="finalized")
    assert c.post(url, json={"decision": "confirmed"}).status_code == 409


# ── corrections ─────────────────────────────────────────────────────────────

def test_corrections_are_append_only_and_plate_needs_reason(client):
    fake, as_role = client
    _case(fake)
    c = as_role("officer")
    plate = {"field": "plate", "ai_value": "MH12AB1234", "corrected_value": "MH12AB1284"}
    assert c.post("/cases/c1/corrections", json=plate).status_code == 422                       # reason mandatory
    assert c.post("/cases/c1/corrections", json={**plate, "corrected_value": "??", "reason": "misread 3 as 8"}).status_code == 422
    r = c.post("/cases/c1/corrections", json={**plate, "corrected_value": "mh 12 ab 1284", "reason": "misread 3 as 8"})
    assert r.status_code == 200
    assert fake.writes() == [("case_corrections", "insert", ({"case_id": "c1", "finding_id": None, "field": "plate",
                                                              "ai_value": "MH12AB1234", "corrected_value": "MH12AB1284",
                                                              "reason": "misread 3 as 8", "reviewer_id": OTHER},))]
    # no update/delete ever touches AI columns or earlier corrections
    assert not [w for w in fake.writes() if w[1] in ("update", "delete")]
    assert c.post("/cases/c1/corrections", json={"field": "violation_label", "corrected_value": "phone_usage"}).status_code == 422
    fake.tables["findings"] = []
    assert c.post("/cases/c1/corrections", json={"field": "violation_label", "finding_id": "f9", "corrected_value": "phone_usage"}).status_code == 404
    assert c.post("/cases/c1/corrections", json={"field": "vehicle_type", "corrected_value": "truck"}).status_code == 422
    assert as_role("officer", USER).post("/cases/c1/corrections", json={"field": "vehicle_type", "corrected_value": "four_wheeler"}).status_code == 409


def test_evidence_correction_accepts_a_list_of_frames(client):
    fake, as_role = client
    _case(fake)
    fake.tables["findings"] = [{"id": "f1", "case_id": "c1"}]
    c = as_role("officer")
    body = {"field": "evidence", "finding_id": "f1", "ai_value": ["a.jpg", "b.jpg"], "corrected_value": ["a.jpg"],
            "reason": "frame b shows the wrong vehicle"}
    assert c.post("/cases/c1/corrections", json=body).status_code == 200
    row = fake.writes("case_corrections")[0][2][0]
    assert row["ai_value"] == '["a.jpg", "b.jpg"]' and row["corrected_value"] == '["a.jpg"]'   # text columns
    assert c.post("/cases/c1/corrections", json={**body, "field": "plate"}).status_code == 422   # a list is not a plate


# ── finalize ────────────────────────────────────────────────────────────────

def test_finalize_blocked_while_a_finding_is_pending(client):
    fake, as_role = client
    _case(fake)
    fake.tables["findings"] = [{"id": "f1", "case_id": "c1", "decision": "pending"}]
    c = as_role("officer")
    assert c.post("/cases/c1/finalize").status_code == 422 and not fake.rpc_calls
    fake.tables["findings"] = []
    fake.rpc_results["finalize_case"] = {"id": "c1", "status": "second_opinion"}
    r = c.post("/cases/c1/finalize")
    assert r.status_code == 200 and r.json()["data"]["status"] == "second_opinion"
    assert fake.rpc_calls == [("finalize_case", {"p_case_id": "c1", "p_reviewer": OTHER})]
    assert as_role("officer", USER).post("/cases/c1/finalize").status_code == 409


# ── list / detail / lookups ─────────────────────────────────────────────────

def test_list_and_detail_shapes(client):
    fake, as_role = client
    _case(fake, status="pending_review", claimed_by=None)
    fake.tables["vehicle_records"] = [{"id": "rec1", "plate_text": "MH12AB1234", "plate_confidence": 0.8, "vehicle_type": "motorcycle"}]
    fake.tables["videos"] = [{"id": "v1", "claimed_plate": "MH12AB1234", "allegation_answer": "supported", "declared_violation": "no_helmet",
                              "note": "no helmet", "summary": {"pipeline_version": "3.0", "model_versions": {"helmet": "v2"},
                                                               "vlm_calls": [{"track_id": 1, "model": "gemini"}, {"track_id": 2}]}}]
    fake.tables["case_corrections"] = [{"case_id": "c1", "field": "plate", "corrected_value": "MH12AB1284", "created_at": "2"}]
    fake.tables["findings"] = [{"id": "f1", "case_id": "c1", "violation": "no_helmet", "decision": "pending"}]
    fake.tables["evidence"] = [{"finding_id": "f1", "case_id": "c1", "blob_path": "v1/r1/tracks/1/a.jpg", "frame_index": 1}]
    assert as_role("citizen").get("/cases").status_code == 403
    row = as_role("officer").get("/cases?lane=normal").json()["data"][0]
    assert row["plate"] == {"ai": "MH12AB1234", "confidence": 0.8, "claimed": "MH12AB1234", "corrected": "MH12AB1284"}
    assert row["findings_count"] == 1 and row["allegation_answer"] == "supported" and row["vehicle_type"] == "motorcycle"
    assert as_role("officer").get("/cases?lane=sideways").status_code == 422
    d = as_role("officer").get("/cases/c1").json()["data"]
    assert d["uploader_claim"]["note"] == "no helmet" and d["ai"]["model_versions"] == {"helmet": "v2"}
    assert d["ai"]["vlm_calls"] == [{"track_id": 1, "model": "gemini"}]
    assert d["evidence"]["f1"][0]["url"].startswith("https://sig/v1/r1/tracks/1/a.jpg")
    # detail carries the list enrichment: the frontend reads the same plate object and vehicle_type everywhere
    assert d["plate"] == row["plate"] and d["vehicle_type"] == "motorcycle" and d["allegation_answer"] == "supported"
    assert d["plate_of_record"] == "MH12AB1284" and d["plate_history"] is None        # reviewer: history only after finalize
    fake.tables["plate_history"] = [{"plate": "MH12AB1284", "layer": "confirmed"}, {"plate": "MH12AB1284", "layer": "observed"}]
    history = as_role("admin").get("/cases/c1").json()["data"]["plate_history"]
    assert history == {"confirmed": [fake.tables["plate_history"][0]], "observed": [fake.tables["plate_history"][1]]}
    assert as_role("officer").get("/rejection-reasons").status_code == 200
    mine = as_role("officer").get("/cases/mine").json()["data"]
    assert mine[0]["idle_hours"] > 0


def test_plate_history_visibility(client):
    fake, as_role = client
    fake.tables["plate_history"] = [{"plate": "MH12AB1234", "layer": "confirmed", "case_id": "c1"},
                                    {"plate": "MH12AB1234", "layer": "observed", "case_id": "c2"}]
    fake.tables["plate_status"] = [{"plate": "MH12AB1234", "status": "watch"}]
    assert as_role("officer").get("/plates/bad/history").status_code == 422
    assert as_role("officer").get("/plates/MH12AB1234/history").status_code == 403       # no finalized case of theirs
    fake.tables["cases"] = [{"id": "c1", "finalized_by": OTHER}]
    d = as_role("officer").get("/plates/mh12 ab 1234/history").json()["data"]
    assert d["status"] == "watch" and len(d["confirmed"]) == 1 and len(d["observed"]) == 1
    fake.tables["cases"] = []
    assert as_role("admin").get("/plates/MH12AB1234/history").status_code == 200


# ── admin case ops + withdrawal answer ──────────────────────────────────────

def test_admin_reopen_and_reassign(client):
    fake, as_role = client
    _case(fake, status="in_review")
    a = as_role("admin")
    assert a.post("/admin/cases/c1/reopen", json={"reason": "new footage from the uploader"}).status_code == 409
    _case(fake, status="finalized", claimed_by=None)
    assert a.post("/admin/cases/c1/reopen", json={"reason": "new footage from the uploader"}).status_code == 200
    cases_upd = next(x[0] for t, m, x in fake.writes("cases") if m == "update")
    assert cases_upd["status"] == "reopened" and cases_upd["cycle"] == 2 and cases_upd["reopened_by"] == ADMIN
    assert next(x[0] for t, m, x in fake.writes("findings"))["decision"] == "pending"
    assert fake.writes("audit_log")[0][2][0]["action"] == "case.reopen"
    fake.tables["profiles"] = [{"id": USER, "role": "citizen"}]
    assert a.post("/admin/cases/c1/reassign", json={"reviewer_id": USER}).status_code == 409   # finalized
    _case(fake, status="pending_review", claimed_by=None)
    assert a.post("/admin/cases/c1/reassign", json={"reviewer_id": USER}).status_code == 422   # not a reviewer
    fake.tables["profiles"] = [{"id": USER, "role": "officer"}]
    assert a.post("/admin/cases/c1/reassign", json={"reviewer_id": USER}).status_code == 200
    assert as_role("officer").post("/admin/cases/c1/reassign", json={"reviewer_id": USER}).status_code == 403


def test_withdrawal_answer(client):
    fake, as_role = client
    _case(fake)
    assert as_role("officer").post("/cases/c1/withdrawal", json={"accept": True}).status_code == 404   # nothing pending
    fake.tables["withdrawal_requests"] = [{"id": "w1", "video_id": "v1", "status": "pending"}]
    assert as_role("officer", USER).post("/cases/c1/withdrawal", json={"accept": True}).status_code == 409
    assert as_role("officer").post("/cases/c1/withdrawal", json={"accept": False, "reason": "review nearly done"}).status_code == 200
    assert [t for t, _, _ in fake.writes()] == ["withdrawal_requests"]
    fake.calls.clear()
    assert as_role("admin").post("/cases/c1/withdrawal", json={"accept": True}).status_code == 200
    assert [t for t, _, _ in fake.writes()] == ["videos", "cases", "withdrawal_requests"]
