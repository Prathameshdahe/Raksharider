"""v3 contract tests (API.md): uploads init/withdraw/detail, evidence signing, admin live/quality/escalations, auth.
Same fake-supabase pattern as test_rbac.py, plus rpc() and a stubbed Azure signer."""
import pytest
from fastapi.testclient import TestClient

from app.main import app
from app.services import storage_service
from app.utils.auth import get_current_user

USER = "11111111-1111-1111-1111-111111111111"
OTHER = "22222222-2222-2222-2222-222222222222"
ADMIN = "33333333-3333-3333-3333-333333333333"


class _Resp:
    def __init__(self, data):
        self.data = data
        self.count = len(data)


class _Query:
    def __init__(self, fake, table):
        self.fake, self.table = fake, table

    def __getattr__(self, name):
        def _chain(*a, **k):
            self.fake.calls.append((self.table, name, a, k))
            return self
        return _chain

    def execute(self):
        return _Resp([dict(r) for r in self.fake.tables.get(self.table, [])])


class _RpcError(Exception):
    def __init__(self, code, message):
        super().__init__(message)
        self.code, self.message = code, message


class _Rpc:
    def __init__(self, fake, name, params):
        self.fake, self.name, self.params = fake, name, params

    def execute(self):
        if self.name in self.fake.rpc_errors:
            raise _RpcError(*self.fake.rpc_errors[self.name])
        return _Resp(self.fake.rpc_results.get(self.name, {"ok": True}))


class FakeSupabase:
    def __init__(self):
        self.tables, self.calls, self.rpc_calls, self.rpc_results, self.rpc_errors = {}, [], [], {}, {}

    def table(self, name):
        return _Query(self, name)

    def rpc(self, name, params=None):
        self.rpc_calls.append((name, params))
        return _Rpc(self, name, params)

    def writes(self, table=None):
        return [(t, m, a) for t, m, a, _ in self.calls if m in ("insert", "update", "upsert", "delete") and (table is None or t == table)]


MODULES = ("app.routes.uploads", "app.routes.review", "app.routes.admin", "app.routes.cases", "app.routes.plates",
           "app.routes.evidence", "app.routes.auth", "app.services.video_service", "app.utils.audit")


@pytest.fixture
def client(monkeypatch):
    fake = FakeSupabase()
    for mod in MODULES:
        monkeypatch.setattr(f"{mod}.supabase", fake)
    monkeypatch.setattr(storage_service, "sign_azure_blob",
                        lambda path, minutes=15, container=None: {"url": f"https://sig/{path}?m={minutes}", "expires_at": "later"})

    def as_role(role, user_id=None):
        uid = user_id or {"citizen": USER, "officer": OTHER, "admin": ADMIN}[role]
        app.dependency_overrides[get_current_user] = lambda: {
            "id": uid, "email": f"{role}@x.io", "role": role, "profile": {"role": role, "id": uid}, "claims": {},
        }
        return TestClient(app)

    yield fake, as_role
    app.dependency_overrides.clear()


INIT = {"filename": "a.mp4", "content_type": "video/mp4", "vehicle_type": "two_wheeler", "size_bytes": 1000,
        "note": "rider without a helmet overtook me at the signal", "consent": True}


# ── uploads: init validation ────────────────────────────────────────────────

def test_init_requires_consent_and_note(client):
    _, as_role = client
    c = as_role("citizen")
    assert c.post("/videos/upload/init", json={**INIT, "consent": False}).status_code == 422
    assert c.post("/videos/upload/init", json={**INIT, "note": "short"}).status_code == 422


def test_init_validates_plate_and_violation_and_sets_priority(client, monkeypatch):
    fake, as_role = client
    fake.tables["violation_policy"] = [{"violation_type": "no_helmet", "tier": "middle", "two_wheeler": True, "four_wheeler": False}]
    fake.tables["videos"] = [{"id": "v-new"}]
    monkeypatch.setattr(storage_service.StorageService, "create_signed_upload_url",
                        staticmethod(lambda fn: {"storage_path": "x.mp4", "upload_url": "https://up"}))
    c = as_role("citizen")
    assert c.post("/videos/upload/init", json={**INIT, "claimed_plate": "not a plate"}).status_code == 422
    assert c.post("/videos/upload/init", json={**INIT, "vehicle_type": "four_wheeler", "declared_violation": "no_helmet"}).status_code == 422
    r = c.post("/videos/upload/init", json={**INIT, "declared_violation": "no_helmet", "claimed_plate": "mh 12 ab-1234"})
    assert r.status_code == 201 and r.json()["data"]["priority"] == 2
    inserted = next(a[0] for t, m, a in fake.writes("videos") if m == "insert")
    assert inserted["priority"] == 2 and inserted["claimed_plate"] == "MH12AB1234" and inserted["consent_at"]
    assert inserted["declared_violation"] == "no_helmet"


# ── uploads: citizen detail masking ─────────────────────────────────────────

def _processed_video(fake, subject_status="in_review", identity_status="resolved", claimed_plate="MH12AB1234"):
    fake.tables["videos"] = [{"id": "v1", "uploaded_by": USER, "status": "processed", "detection_video_path": "v1/r1/detection.mp4",
                              "claimed_plate": claimed_plate, "blob_url": "https://public/original.mp4",
                              "local_path": "v1.mp4", "storage_path": "v1.mp4", "error_reason": "stack trace"}]
    fake.tables["vehicle_records"] = [{"id": "rec1", "video_id": "v1", "track_id": 1, "plate_text": "MH12AB1234", "vehicle_type": "motorcycle"},
                                      {"id": "rec2", "video_id": "v1", "track_id": 2, "plate_text": "KA01ZZ9999", "vehicle_type": "car"}]
    fake.tables["cases"] = [{"id": "c1", "video_id": "v1", "track_id": 1, "is_subject": True, "status": subject_status,
                             "identity_status": identity_status, "claimed_by": OTHER, "finalized_by": OTHER},
                            {"id": "c2", "video_id": "v1", "track_id": 2, "is_subject": False, "status": "pending_review", "claimed_by": None}]
    fake.tables["findings"] = [{"id": "f1", "case_id": "c1", "violation": "no_helmet", "ai_result": "confirmed", "tier": "A",
                                "decision": "rejected", "rejection_reason": "helmet_worn", "note": "private reviewer note", "decided_by": OTHER}]
    fake.tables["evidence"] = [{"finding_id": "f1", "case_id": "c2", "frame_index": 3, "blob_path": "v1/r1/tracks/2/a.jpg"}]


def test_citizen_detail_hides_other_plates_and_reviewer(client):
    fake, as_role = client
    _processed_video(fake)
    r = as_role("citizen").get("/videos/v1")
    assert r.status_code == 200
    d = r.json()["data"]
    assert d["video"]["user_status"] == "awaiting_review"
    subject, other = d["cases"]
    assert subject["plate"] == "MH12AB1234" and other["plate"] is None       # no partial mask: docs §0
    assert "claimed_by" not in subject and "decided_by" not in subject["findings"][0]
    assert subject["findings"][0]["rejection_reason"] == "helmet_worn" and subject["findings"][0]["note_public"] is None
    assert other["evidence"] == [] and d["detection_video_url"] is None
    o = as_role("officer").get("/videos/v1").json()["data"]
    assert o["cases"][1]["plate"] == "KA01ZZ9999" and o["cases"][0]["findings"][0]["decided_by"] == OTHER
    assert o["detection_video_url"].startswith("https://sig/v1/r1/detection.mp4")


def test_citizen_never_gets_evidence_only_the_detection_video(client):
    """docs §2.6 items 24-25: textual outcome + redacted detection video. No evidence frames, ever."""
    fake, as_role = client
    _processed_video(fake, subject_status="finalized")
    fake.tables["evidence"] = [{"finding_id": "f1", "case_id": "c1", "frame_index": 3, "blob_path": "v1/r1/tracks/1/a.jpg"},
                               {"finding_id": "f1", "case_id": "c2", "frame_index": 4, "blob_path": "v1/r1/tracks/2/a.jpg"}]
    d = as_role("citizen").get("/videos/v1").json()["data"]
    assert [c["evidence"] for c in d["cases"]] == [[], []]
    assert d["detection_video_url"].startswith("https://sig/")
    o = as_role("officer").get("/videos/v1").json()["data"]
    assert [len(c["evidence"]) for c in o["cases"]] == [1, 1] and o["cases"][0]["evidence"][0]["url"].startswith("https://sig/")


def test_citizen_subject_plate_needs_a_matching_claim(client):
    fake, as_role = client
    _processed_video(fake, identity_status="conflict")
    assert as_role("citizen").get("/videos/v1").json()["data"]["cases"][0]["plate"] is None   # identity not resolved
    _processed_video(fake, claimed_plate=None)
    assert as_role("citizen").get("/videos/v1").json()["data"]["cases"][0]["plate"] is None   # nothing typed
    _processed_video(fake, claimed_plate="MH12AB1284")
    assert as_role("citizen").get("/videos/v1").json()["data"]["cases"][0]["plate"] is None   # claim disagrees
    _processed_video(fake, claimed_plate="mh 12 ab-1234")
    assert as_role("citizen").get("/videos/v1").json()["data"]["cases"][0]["plate"] == "MH12AB1234"   # normalised match


def test_citizen_never_gets_storage_paths_or_internal_errors(client):
    fake, as_role = client
    _processed_video(fake)
    hidden = {"blob_url", "local_path", "storage_path", "error_reason"}
    d = as_role("citizen").get("/videos/v1").json()["data"]["video"]
    assert not hidden & set(d)
    assert not hidden & set(as_role("citizen").get("/videos").json()["data"][0])
    assert hidden <= set(as_role("officer").get("/videos/v1").json()["data"]["video"])


def test_list_maps_user_status(client):
    fake, as_role = client
    fake.tables["videos"] = [{"id": "v1", "uploaded_by": USER, "status": "unprocessed"}]
    assert as_role("citizen").get("/videos").json()["data"][0]["user_status"] == "queued"


# ── uploads: withdraw state machine ─────────────────────────────────────────

def test_withdraw_instant_before_processing(client):
    fake, as_role = client
    fake.tables["videos"] = [{"id": "v1", "uploaded_by": USER, "status": "unprocessed"}]
    r = as_role("citizen").post("/videos/v1/withdraw")
    assert r.status_code == 200 and r.json()["data"]["user_status"] == "withdrawn"
    assert ("videos", "update", ({"status": "withdrawn", "withdrawn_at": fake.writes("videos")[0][2][0]["withdrawn_at"]},)) in fake.writes("videos")
    assert as_role("citizen", OTHER).post("/videos/v1/withdraw").status_code == 404


def test_withdraw_becomes_request_while_in_review_and_409_when_decided(client):
    fake, as_role = client
    fake.tables["videos"] = [{"id": "v1", "uploaded_by": USER, "status": "processed"}]
    fake.tables["cases"] = [{"id": "c1", "video_id": "v1", "status": "in_review", "claimed_by": OTHER}]
    assert as_role("citizen").post("/videos/v1/withdraw").status_code == 422              # a reviewer must be told why
    assert as_role("citizen").post("/videos/v1/withdraw", json={"reason": "oops"}).status_code == 422
    r = as_role("citizen").post("/videos/v1/withdraw", json={"reason": "wrong clip uploaded"})
    assert r.status_code == 200 and r.json()["data"]["withdrawal"] == "requested"
    assert fake.writes("withdrawal_requests")[0][1] == "insert" and not fake.writes("videos")
    assert fake.writes("withdrawal_requests")[0][2][0]["reason"] == "wrong clip uploaded"
    fake.tables["withdrawal_requests"] = [{"id": "w1", "status": "pending"}]
    assert as_role("citizen").post("/videos/v1/withdraw").status_code == 409
    fake.tables["cases"] = [{"id": "c1", "video_id": "v1", "status": "finalized"}]
    assert as_role("citizen").post("/videos/v1/withdraw").status_code == 409
    fake.tables["videos"][0]["status"] = "withdrawn"
    assert as_role("citizen").post("/videos/v1/withdraw").status_code == 409


def test_withdraw_processed_without_review_closes_cases(client):
    fake, as_role = client
    fake.tables["videos"] = [{"id": "v1", "uploaded_by": USER, "status": "processed"}]
    fake.tables["cases"] = [{"id": "c1", "video_id": "v1", "status": "pending_review"}]
    assert as_role("citizen").post("/videos/v1/withdraw").status_code == 200
    assert [t for t, _, _ in fake.writes()] == ["videos", "cases"]


def test_requeue_refused_once_a_case_is_decided_or_claimed(client):
    """A new run inserts a second set of cases, so reviewers would see every vehicle twice."""
    fake, as_role = client
    fake.tables["videos"] = [{"id": "v1", "uploaded_by": USER, "status": "processed", "attempts": 1}]
    a = as_role("admin")
    fake.tables["cases"] = [{"id": "c1", "video_id": "v1", "status": "finalized"}]
    assert a.post("/videos/v1/requeue").status_code == 409
    fake.tables["cases"] = [{"id": "c1", "video_id": "v1", "status": "in_review"}]
    assert a.post("/videos/v1/requeue").status_code == 409
    assert not fake.writes("videos")
    fake.tables["cases"] = [{"id": "c1", "video_id": "v1", "status": "pending_review"}]
    assert a.post("/videos/v1/requeue").status_code == 200
    assert fake.writes("videos")[0][2][0]["status"] == "unprocessed"


# ── evidence signing ────────────────────────────────────────────────────────

def test_evidence_sign_authorisation(client, monkeypatch):
    fake, as_role = client
    fake.tables["videos"] = [{"id": "v1", "uploaded_by": OTHER, "detection_video_path": "v1/r1/detection.mp4"}]
    c = as_role("citizen")
    assert c.get("/evidence/sign", params={"path": "v1/r1/tracks/1/a.jpg"}).status_code == 403
    assert c.get("/evidence/sign", params={"path": "../v1/x"}).status_code == 422
    fake.tables["videos"][0]["uploaded_by"] = USER
    fake.tables["cases"] = [{"id": "c1", "video_id": "v1", "is_subject": True, "status": "in_review"},
                            {"id": "c2", "video_id": "v1", "is_subject": False, "status": "pending_review"}]
    fake.tables["evidence"] = [{"case_id": "c1"}]
    # a citizen may sign the detection video and nothing else — never an evidence frame, not even their own
    assert c.get("/evidence/sign", params={"path": "v1/r1/tracks/1/a.jpg"}).status_code == 403
    assert c.get("/evidence/sign", params={"path": "v1/r1/tracks/2/a.jpg"}).status_code == 403
    assert c.get("/evidence/sign", params={"path": "v1/r1/detection.mp4"}).status_code == 403     # detection video before finalize
    fake.tables["cases"][0]["status"] = "finalized"
    r = c.get("/evidence/sign", params={"path": "v1/r1/detection.mp4"})
    assert r.status_code == 200 and r.json()["data"]["url"].startswith("https://sig/v1/r1/detection.mp4?m=15")
    assert c.get("/evidence/sign", params={"path": "v1/r1/tracks/1/a.jpg"}).status_code == 403    # still no frames
    assert as_role("officer").get("/evidence/sign", params={"path": "v1/r1/tracks/2/a.jpg"}).status_code == 200

    def boom(path, minutes=15, container=None):
        raise storage_service.AzureNotConfigured("AZURE_STORAGE_CONNECTION_STRING is not set")
    monkeypatch.setattr(storage_service, "sign_azure_blob", boom)
    assert as_role("admin").get("/evidence/sign", params={"path": "v1/r1/tracks/2/a.jpg"}).status_code == 503


def test_sign_azure_blob_503_without_env(monkeypatch):
    monkeypatch.delenv("AZURE_STORAGE_CONNECTION_STRING", raising=False)
    with pytest.raises(storage_service.AzureNotConfigured):
        storage_service.sign_azure_blob("v1/r1/a.jpg")


# ── admin ───────────────────────────────────────────────────────────────────

def test_admin_live_shape(client):
    fake, as_role = client
    fake.tables["videos"] = [{"id": "v1", "priority": 3, "status": "unprocessed"}, {"id": "v2", "priority": 0, "claimed_at": "2026-09-17T00:00:00+00:00", "attempts": 1}]
    fake.tables["cases"] = [{"id": "c1", "created_at": "2026-09-16T00:00:00+00:00", "status": "pending_review", "claimed_by": OTHER}]
    fake.tables["profiles"] = [{"id": OTHER, "full_name": "Officer O"}]
    assert as_role("officer").get("/admin/live").status_code == 403
    d = as_role("admin").get("/admin/live").json()["data"]
    assert set(d) == {"users_online", "uploads_today", "queue", "processing", "worker_last_seen", "failed_today", "review"}
    assert d["queue"]["depth"] == 2 and d["queue"]["by_priority"] == {"3": 1, "2": 0, "1": 0, "0": 1}
    assert d["processing"][1]["elapsed_s"] > 0 and d["review"]["oldest_waiting_s"] > 0
    assert d["review"]["reviewers_active"] == [{"id": OTHER, "name": "Officer O", "open_claims": 1}]


def test_admin_quality_shape(client):
    fake, as_role = client
    fake.tables["finding_decisions"] = [{"finding_id": "f1", "decision": "rejected", "rejection_reason": "plate_misread"},
                                        {"finding_id": "f2", "decision": "confirmed", "rejection_reason": None}]
    fake.tables["findings"] = [{"id": "f1", "violation": "no_helmet"}, {"id": "f2", "violation": "no_helmet"}]
    fake.tables["cases"] = [{"id": "c1", "claimed_at": "2026-09-17T10:00:00+00:00", "finalized_at": "2026-09-17T10:05:00+00:00"}]
    fake.tables["case_corrections"] = [{"id": 1}]
    fake.tables["videos"] = [{"id": "v1", "allegation_answer": "not_supported"}, {"id": "v2", "allegation_answer": "supported"}]
    d = as_role("admin").get("/admin/quality?days=7").json()["data"]
    assert d["rejections_by_reason"] == {"plate_misread": 1}
    assert d["agreement_by_violation"] == {"no_helmet": {"confirmed": 1, "rejected": 1, "inconclusive": 0}}
    assert d["plate_misread_rate"] == 1.0 and d["not_supported_rate"] == 0.5 and d["avg_review_seconds"] == 300


def test_escalation_approve_builds_package(client):
    fake, as_role = client
    fake.tables["escalations"] = [{"id": "e1", "plate": "MH12AB1234", "status": "pending_approval"}]
    fake.tables["plate_history"] = [{"plate": "MH12AB1234", "layer": "confirmed", "case_id": "c1", "finding_id": "f1"}]
    fake.tables["cases"] = [{"id": "c1", "video_id": "v1", "track_id": 1, "finalized_by": OTHER, "finalized_at": "t"}]
    fake.tables["findings"] = [{"id": "f1", "case_id": "c1", "violation": "phone_usage", "decision": "confirmed", "tier": "A"}]
    fake.tables["finding_decisions"] = [{"finding_id": "f1", "case_id": "c1", "decision": "confirmed", "cycle": 1}]
    fake.tables["evidence"] = [{"finding_id": "f1", "case_id": "c1", "blob_path": "v1/r1/tracks/1/a.jpg", "sha256": "x"}]
    fake.tables["videos"] = [{"id": "v1", "detection_video_path": "v1/r1/detection.mp4"}]
    fake.tables["profiles"] = [{"id": OTHER, "badge_number": "B123"}]
    r = as_role("admin").post("/admin/escalations/e1/approve")
    assert r.status_code == 200
    update = next(a[0] for t, m, a in fake.writes("escalations") if m == "update")
    assert update["status"] == "approved" and update["approved_by"] == ADMIN
    case = update["package"]["cases"][0]
    assert case["reviewer_badge"] == "B123" and case["findings"][0]["violation"] == "phone_usage"
    assert case["findings"][0]["evidence"][0]["url"].endswith("?m=10080")      # 7 days
    assert case["detection_video"]["url"].startswith("https://sig/v1/r1/detection.mp4")
    assert ("audit_log", "insert") in [(t, m) for t, m, _ in fake.writes()]
    fake.tables["escalations"][0]["status"] = "approved"
    assert as_role("admin").post("/admin/escalations/e1/approve").status_code == 409


def test_role_change_to_officer_needs_verified_via(client):
    fake, as_role = client
    fake.tables["profiles"] = [{"id": USER, "role": "citizen", "requested_role": "officer"}]
    body = {"role": "officer", "reason": "badge checked"}
    assert as_role("admin").patch(f"/admin/users/{USER}/role", json=body).status_code == 422
    r = as_role("admin").patch(f"/admin/users/{USER}/role", json={**body, "verified_via": "Pune RTO desk, 17 Sep"})
    assert r.status_code == 200
    assert next(a[0] for t, m, a in fake.writes("profiles"))["verified_via"] == "Pune RTO desk, 17 Sep"


def test_settings_put_audits(client):
    fake, as_role = client
    r = as_role("admin").put("/admin/settings", json={"escalation_threshold_any": 5})
    assert r.status_code == 200 and r.json()["data"] == {"escalation_threshold_any": 5}
    assert [t for t, _, _ in fake.writes()] == ["system_settings", "audit_log"]
    assert as_role("admin").put("/admin/settings", json={}).status_code == 422


def test_worker_lease_minutes_is_readable_and_editable(client):
    """claim_next_video() reaps a lease older than this; a setting the worker obeys but no admin can see is a trap."""
    fake, as_role = client
    fake.tables["system_settings"] = [{"key": "worker_lease_minutes", "value": 120, "updated_at": "t"}]
    assert as_role("admin").get("/admin/settings").json()["data"]["worker_lease_minutes"] == 120
    assert as_role("admin").put("/admin/settings", json={"worker_lease_minutes": 5}).status_code == 422   # reaps mid-run
    r = as_role("admin").put("/admin/settings", json={"worker_lease_minutes": 240})
    assert r.status_code == 200 and r.json()["data"] == {"worker_lease_minutes": 240}


# ── auth ────────────────────────────────────────────────────────────────────

def test_me_returns_portal_and_nav_and_presence(client):
    fake, as_role = client
    me = as_role("officer").get("/auth/me").json()
    assert me["portal"] == "reviewer" and "queue" in me["nav"]
    assert as_role("citizen").get("/auth/me").json()["portal"] == "user"
    assert as_role("citizen").post("/auth/presence").status_code == 200
    assert fake.writes("profiles")[0][2][0].keys() == {"last_seen_at"}
