"""/status, /health?deep=1 and the keep-alive loop (no network, fake Supabase)."""
import pytest
from fastapi.testclient import TestClient

from app import main as main_mod
from app.routes import status as status_mod
from app.services import keepalive


class _Resp:
    def __init__(self, data, count=None):
        self.data = data
        self.count = count if count is not None else len(data)


class _Query:
    def __init__(self, fake, table):
        self.fake, self.table, self.filters, self.head = fake, table, {}, False

    def select(self, *a, **k):
        self.head = bool(k.get("head"))
        return self

    def eq(self, col, val):
        self.filters[col] = val
        return self

    def limit(self, *_):
        return self

    def execute(self):
        self.fake.calls.append((self.table, dict(self.filters), self.head))
        if self.table in self.fake.errors:
            err = self.fake.errors[self.table]
            if callable(err):
                err = err(self.head)          # per-request shape: HEAD vs GET
            if err is not None:
                raise err if isinstance(err, Exception) else RuntimeError(err)
        rows = [dict(r) for r in self.fake.tables.get(self.table, []) if all(r.get(c) == v for c, v in self.filters.items())]
        return _Resp([] if self.head else rows, count=len(rows))


class _Rpc:
    def __init__(self, fake, name):
        self.fake, self.name = fake, name

    def execute(self):
        self.fake.rpc_calls.append(self.name)
        if self.name in self.fake.rpc_errors:
            raise RuntimeError(self.fake.rpc_errors[self.name])
        return _Resp([])


class FakeSupabase:
    def __init__(self):
        self.tables, self.calls, self.rpc_calls, self.rpc_errors, self.errors = {}, [], [], {}, {}

    def table(self, name):
        return _Query(self, name)

    def rpc(self, name, params=None):
        return _Rpc(self, name)


@pytest.fixture
def fake(monkeypatch):
    fs = FakeSupabase()
    monkeypatch.setattr(status_mod, "supabase", fs)
    monkeypatch.setattr(status_mod, "missing_env", lambda: [])
    import app.database.supabase as dbmod
    monkeypatch.setattr(dbmod, "supabase", fs)
    monkeypatch.setattr(dbmod, "health_client", fs)
    monkeypatch.setattr(dbmod, "missing_env", lambda: [])
    status_mod._cache.update({"at": 0.0, "data": None})
    main_mod._db_probe.update({"at": 0.0, "result": None})
    return fs


@pytest.fixture
def client():
    return TestClient(main_mod.app)


def test_status_aggregates_and_worker_window(fake, client):
    from datetime import datetime, timedelta, timezone
    recent = (datetime.now(timezone.utc) - timedelta(minutes=2)).isoformat()
    fake.tables["system_settings"] = [{"key": "worker_last_seen", "value": recent}, {"key": "worker_paused", "value": False}]
    fake.tables["videos"] = [{"id": "v1", "status": "processed"}, {"id": "v2", "status": "unprocessed"}, {"id": "v3", "status": "processing"}]
    fake.tables["cases"] = [{"id": "c1", "status": "finalized"}, {"id": "c2", "status": "pending_review"}]
    fake.tables["findings"] = [{"id": "f1", "decision": "confirmed"}, {"id": "f2", "decision": "rejected"}, {"id": "f3", "decision": "pending"}]
    r = client.get("/status")
    assert r.status_code == 200
    d = r.json()
    assert d["ok"] is True and d["cached"] is False
    assert d["worker"]["online"] is True
    assert d["intake_paused"] is False
    assert d["queue"] == {"depth": 1, "processing": 1}
    assert d["totals"] == {"clips_submitted": 3, "clips_analysed": 1, "cases_opened": 2, "cases_decided": 1,
                           "findings_confirmed": 1, "findings_rejected": 1}
    # counts are HEAD requests: no row data leaves the database for an anonymous caller
    assert all(head for table, _, head in fake.calls if table in ("videos", "cases", "findings"))
    # second call within the TTL is served from cache
    assert client.get("/status").json()["cached"] is True


def test_status_worker_offline_when_stale_or_missing(fake, client):
    fake.tables["system_settings"] = [{"key": "worker_last_seen", "value": "2026-01-01T00:00:00+00:00"}]
    d = client.get("/status").json()
    assert d["worker"]["online"] is False and d["worker"]["silent_for_s"] > 600
    status_mod._cache.update({"at": 0.0, "data": None})
    fake.tables["system_settings"] = []
    d = client.get("/status").json()
    assert d["worker"] == {"online": False, "last_seen": None, "silent_for_s": None}


def test_status_reports_misconfiguration(monkeypatch, client):
    monkeypatch.setattr(status_mod, "missing_env", lambda: ["SUPABASE_URL"])
    status_mod._cache.update({"at": 0.0, "data": None})
    d = client.get("/status").json()
    assert d["ok"] is False and d["database"] == "misconfigured" and d["missing_env"] == ["SUPABASE_URL"]


def test_health_deep_probes_database(fake, client):
    fake.tables["system_settings"] = [{"key": "worker_paused", "value": False}]
    shallow = client.get("/health").json()
    assert shallow["status"] == "healthy" and "database" not in shallow and "keepalive" in shallow
    deep = client.get("/health?deep=1").json()
    assert deep["status"] == "healthy"
    assert deep["database"]["ok"] is True and deep["database"]["cached"] is False
    assert deep["database"]["denied"] == {} and deep["database"]["tables_checked"] == len(main_mod.CRITICAL_TABLES)
    # every critical table is probed with a HEAD request: no row data for a health check
    probed = {t for t, _, head in fake.calls if head}
    assert set(main_mod.CRITICAL_TABLES) <= probed
    assert client.get("/health?deep=1").json()["database"]["cached"] is True


def test_health_deep_names_tables_the_service_role_cannot_read(fake, client):
    fake.errors["system_settings"] = "permission denied for table system_settings"
    fake.errors["audit_log"] = "permission denied for table audit_log"
    deep = client.get("/health?deep=1").json()
    assert deep["status"] == "degraded"
    db = deep["database"]
    assert db["ok"] is False
    assert set(db["denied"]) == {"system_settings", "audit_log"}
    assert db["errors"] == {}
    assert "008_service_role_grants.sql" in db["hint"]
    assert "2 table(s)" in db["error"]


def test_health_deep_separates_transient_errors_from_denials(fake, client):
    fake.errors["escalations"] = "Server disconnected"
    deep = client.get("/health?deep=1").json()
    db = deep["database"]
    assert deep["status"] == "degraded" and db["ok"] is False
    assert db["denied"] == {} and set(db["errors"]) == {"escalations"}
    assert "hint" not in db and "could not be reached" in db["error"]
    # a transient failure is retried once before it is reported
    assert sum(1 for t, _, head in fake.calls if t == "escalations" and head) == 2


def test_keepalive_tick_survives_failures(fake, monkeypatch):
    fake.rpc_errors["check_idle_alerts"] = "permission denied"
    monkeypatch.setattr(keepalive, "_ping", lambda url: (_ for _ in ()).throw(OSError("down")))
    keepalive.tick("https://example.invalid/health?deep=1")
    assert keepalive.STATE["last_ping_ok"] is False
    assert "check_idle_alerts" in fake.rpc_calls
    assert "check_idle_alerts" in (keepalive.STATE["last_error"] or "")


def test_keepalive_tick_runs_housekeeping(fake, monkeypatch):
    monkeypatch.setattr(keepalive, "_ping", lambda url: True)
    keepalive.tick("https://example.invalid/health?deep=1")
    assert keepalive.STATE["last_ping_ok"] is True
    assert keepalive.STATE["last_housekeeping_at"] is not None
    assert fake.rpc_calls[-1] == "check_idle_alerts"


def test_keepalive_target_prefers_explicit_url(monkeypatch):
    monkeypatch.setenv("RENDER_EXTERNAL_URL", "https://raksharider.onrender.com")
    monkeypatch.delenv("KEEPALIVE_URL", raising=False)
    assert keepalive.target_url() == "https://raksharider.onrender.com/health?deep=1"
    monkeypatch.setenv("KEEPALIVE_URL", "https://custom.example/")
    assert keepalive.target_url() == "https://custom.example/health?deep=1"
    monkeypatch.setenv("KEEPALIVE_ENABLED", "0")
    assert keepalive.enabled() is False


class _ApiError(Exception):
    """Shape of postgrest.APIError: a `.code` plus the dict repr as the message."""

    def __init__(self, code, message):
        super().__init__(str({"message": message, "code": code}))
        self.code = str(code)


def test_health_deep_stops_after_the_first_connection_failure(fake, client):
    """A refused connection or timeout hits every table alike: probe it twice, then skip the rest."""
    fake.errors["system_settings"] = "Server disconnected"
    db = client.get("/health?deep=1").json()["database"]
    assert db["ok"] is False and set(db["errors"]) == {"system_settings"} and db["denied"] == {}
    assert db["tables_checked"] == 1
    assert db["skipped"] == list(main_mod.CRITICAL_TABLES[1:])
    assert [t for t, _, head in fake.calls if head] == ["system_settings", "system_settings"]
    assert "hint" not in db


def test_health_deep_keeps_probing_after_a_denied_table(fake, client):
    fake.errors["system_settings"] = "permission denied for table system_settings"
    db = client.get("/health?deep=1").json()["database"]
    assert set(db["denied"]) == {"system_settings"} and db["errors"] == {}
    assert db["tables_checked"] == len(main_mod.CRITICAL_TABLES) and "skipped" not in db


def test_health_deep_bare_403_is_not_a_missing_grant(fake, client):
    """A gateway or WAF 403 must not tell the operator to re-run 008."""
    fake.errors["system_settings"] = _ApiError(403, "Forbidden")
    db = client.get("/health?deep=1").json()["database"]
    assert db["denied"] == {} and set(db["errors"]) == {"system_settings"}
    assert "008_service_role_grants" not in db.get("hint", "")


def test_health_deep_sqlstate_42501_is_a_missing_grant(fake, client):
    fake.errors["audit_log"] = _ApiError("42501", "permission denied for table audit_log")
    db = client.get("/health?deep=1").json()["database"]
    assert set(db["denied"]) == {"audit_log"} and "008_service_role_grants" in db["hint"]


def test_health_deep_names_a_rejected_service_key(fake, client):
    fake.errors["system_settings"] = _ApiError(401, "Invalid API key")
    db = client.get("/health?deep=1").json()["database"]
    assert db["denied"] == {} and "system_settings" in db["errors"]
    assert "SUPABASE_SERVICE_ROLE_KEY" in db["hint"]
    assert sum(1 for t, _, head in fake.calls if head) == 1   # no retry: the key will not change between attempts


def test_health_deep_probe_is_shared_under_a_lock(fake, client, monkeypatch):
    """Two concurrent deep health calls run one probe, not two."""
    import threading
    import time as _time
    original, runs, results = main_mod._run_probe, [], []

    def slow_probe():
        runs.append(1)
        _time.sleep(0.3)
        return original()

    monkeypatch.setattr(main_mod, "_run_probe", slow_probe)
    threads = [threading.Thread(target=lambda: results.append(client.get("/health?deep=1").json()["database"])) for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(10)
    assert len(runs) == 1 and len(results) == 2
    assert sorted(r["cached"] for r in results) == [False, True]


def test_health_deep_reads_the_real_error_behind_a_head_403(fake, client):
    """Live PostgREST: a HEAD on a table the role cannot read is a bare 403 with no body
    ('JSON could not be generated'); the GET carries SQLSTATE 42501. Must still count as denied."""
    fake.errors["system_settings"] = lambda head: (_ApiError(403, "JSON could not be generated") if head
                                                   else _ApiError("42501", "permission denied for table system_settings"))
    db = client.get("/health?deep=1").json()["database"]
    assert set(db["denied"]) == {"system_settings"} and db["errors"] == {}
    assert "008_service_role_grants" in db["hint"]
    assert db["tables_checked"] == len(main_mod.CRITICAL_TABLES) and "skipped" not in db
    gets = [t for t, _, head in fake.calls if t == "system_settings" and not head]
    assert len(gets) == 1   # exactly one confirming GET
