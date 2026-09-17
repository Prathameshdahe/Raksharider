"""RBAC / contract tests: stub get_current_user and the supabase client per route module."""
import pytest
from fastapi.testclient import TestClient

from app.main import app
from app.utils.auth import get_current_user

USER = "11111111-1111-1111-1111-111111111111"
OTHER = "22222222-2222-2222-2222-222222222222"


class _Resp:
    def __init__(self, data):
        self.data = data
        self.count = len(data)


class _Query:
    """Chainable stand-in for a postgrest query; execute() returns the table's canned rows."""

    def __init__(self, fake, table):
        self.fake, self.table = fake, table

    def __getattr__(self, name):
        def _chain(*a, **k):
            self.fake.calls.append((self.table, name, a, k))
            return self
        return _chain

    def execute(self):
        rows = self.fake.tables.get(self.table, [])
        return _Resp([dict(r) for r in rows])


class FakeSupabase:
    def __init__(self, tables=None):
        self.tables = tables or {}
        self.calls = []

    def table(self, name):
        return _Query(self, name)


ROUTE_MODULES = ("app.routes.uploads", "app.routes.review", "app.routes.admin",
                 "app.services.video_service", "app.utils.audit")


@pytest.fixture
def client(monkeypatch):
    fake = FakeSupabase()
    for mod in ROUTE_MODULES:
        monkeypatch.setattr(f"{mod}.supabase", fake)

    def as_role(role, user_id=USER):
        app.dependency_overrides[get_current_user] = lambda: {
            "id": user_id, "email": f"{role}@x.io", "role": role, "profile": {"role": role}, "claims": {},
        }
        return TestClient(app)

    yield fake, as_role
    app.dependency_overrides.clear()


def test_citizen_forbidden_on_reviewer_and_admin_routes(client):
    _, as_role = client
    c = as_role("citizen")
    assert c.get("/review/queue").status_code == 403
    assert c.get("/admin/users").status_code == 403


def test_officer_can_read_review_queue(client):
    fake, as_role = client
    fake.tables["vehicle_records"] = [{"id": "r1", "video_id": "v1", "review_status": "needs_review"}]
    fake.tables["videos"] = [{"id": "v1", "uploaded_by": OTHER}]
    r = as_role("officer").get("/review/queue")
    assert r.status_code == 200
    assert r.json()["data"][0]["video"]["id"] == "v1"


def test_officer_forbidden_on_admin(client):
    _, as_role = client
    assert as_role("officer").get("/admin/users").status_code == 403


def test_admin_cannot_change_own_role(client):
    _, as_role = client
    r = as_role("admin").patch(f"/admin/users/{USER}/role", json={"role": "citizen", "reason": "demote me"})
    assert r.status_code == 400


def test_decision_requires_reason(client):
    fake, as_role = client
    fake.tables["vehicle_records"] = [{"id": "r1", "review_status": "needs_review"}]
    c = as_role("officer")
    assert c.post("/review/records/r1/decision", json={"decision": "confirmed"}).status_code == 422
    assert c.post("/review/records/r1/decision", json={"decision": "confirmed", "reason": "ok"}).status_code == 422


def test_decision_on_decided_record_conflicts_unless_admin_override(client):
    fake, as_role = client
    fake.tables["vehicle_records"] = [{"id": "r1", "review_status": "confirmed", "plate_text": "MH12AB1234"}]
    body = {"decision": "rejected", "reason": "wrong plate read"}
    assert as_role("officer").post("/review/records/r1/decision", json=body).status_code == 409
    assert as_role("officer").post("/review/records/r1/decision", json={**body, "override": True}).status_code == 409
    r = as_role("admin").post("/review/records/r1/decision", json={**body, "override": True})
    assert r.status_code == 200
    assert ("audit_log", "insert") in [(t, m) for t, m, *_ in fake.calls]


def test_confirm_writes_ledger_then_record(client):
    fake, as_role = client
    fake.tables["vehicle_records"] = [{"id": "r1", "review_status": "needs_review", "plate_text": "MH12AB1234",
                                       "review_violations": ["no_helmet"]}]
    fake.tables["violations"] = [{"id": "x1", "vehicle_record_id": "r1", "violation_type": "no_helmet", "status": "needs_review"}]
    fake.tables["violation_policy"] = [{"violation_type": "no_helmet", "trust_delta": -10}]
    r = as_role("officer").post("/review/records/r1/decision", json={"decision": "confirmed", "reason": "helmet clearly missing"})
    assert r.status_code == 200
    writes = [(t, m) for t, m, *_ in fake.calls if m in ("insert", "update")]
    assert writes == [("violations", "update"), ("score_ledger", "insert"), ("vehicle_records", "update")]
    ledger = next(a[0] for t, m, a, _ in fake.calls if t == "score_ledger" and m == "insert")
    assert ledger == [{"plate": "MH12AB1234", "vehicle_record_id": "r1", "violation_type": "no_helmet",
                       "delta": -10, "reason": "helmet clearly missing", "actor_id": USER}]


def test_citizen_sees_no_plate_unless_they_claimed_it(client):
    """docs §0: a partial mask still leaks the state and RTO district, so a plate the uploader may not
    read is null. They read the subject's plate only when they typed it and the AI resolved the same one."""
    fake, as_role = client
    fake.tables["videos"] = [{"id": "v1", "uploaded_by": USER, "status": "processed"}]
    fake.tables["vehicle_records"] = [{"id": "r1", "video_id": "v1", "track_id": 1, "plate_text": "MH12AB1234", "corrected_plate": "MH12AB1299"}]
    r = as_role("citizen").get("/videos/v1")
    assert r.status_code == 200
    rec = r.json()["data"]["vehicle_records"][0]
    assert rec["plate_text"] is None and rec["corrected_plate"] is None
    # officer sees the full plate
    assert as_role("officer").get("/videos/v1").json()["data"]["vehicle_records"][0]["plate_text"] == "MH12AB1234"


def test_citizen_cannot_see_others_video(client):
    fake, as_role = client
    fake.tables["videos"] = [{"id": "v1", "uploaded_by": OTHER, "status": "processed"}]
    assert as_role("citizen").get("/videos/v1").status_code == 404


def test_upload_init_quota_and_size(client):
    fake, as_role = client
    body = {"filename": "a.mp4", "content_type": "video/mp4", "vehicle_type": "two_wheeler", "size_bytes": 1000,
            "note": "rider without helmet at the signal", "consent": True}
    c = as_role("citizen")
    assert c.post("/videos/upload/init", json={**body, "size_bytes": 201 * 1024 * 1024}).status_code == 413
    assert c.post("/videos/upload/init", json={**body, "vehicle_type": "truck"}).status_code == 422
    fake.tables["videos"] = [{"id": str(i)} for i in range(10)]
    fake.tables["system_settings"] = [{"value": 10}]
    assert c.post("/videos/upload/init", json=body).status_code == 429


def test_requeue_admin_only(client):
    fake, as_role = client
    fake.tables["videos"] = [{"id": "v1", "status": "failed", "attempts": 3}]
    assert as_role("officer").post("/videos/v1/requeue").status_code == 403
    assert as_role("admin").post("/videos/v1/requeue").status_code == 200
    fake.tables["videos"] = [{"id": "v1", "status": "processing"}]
    assert as_role("admin").post("/videos/v1/requeue").status_code == 409
