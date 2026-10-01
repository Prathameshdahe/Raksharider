"""get_current_user: role from profiles only; a missing profiles row is created as citizen."""
import pytest
from fastapi.security import HTTPAuthorizationCredentials

from app.utils import auth as auth_mod

USER = "7511a206-b407-4443-af68-2c261ba9c5f9"


class _Resp:
    def __init__(self, data):
        self.data = data


class _Query:
    def __init__(self, fake, table):
        self.fake, self.table, self.op, self.payload = fake, table, "select", None

    def select(self, *a, **k):
        self.op = "select"
        return self

    def upsert(self, row, **k):
        self.op, self.payload, self.kwargs = "upsert", row, k
        return self

    def update(self, row, **k):
        self.op, self.payload = "update", row
        return self

    def eq(self, *a):
        return self

    def limit(self, *a):
        return self

    def execute(self):
        self.fake.calls.append((self.table, self.op, self.payload))
        if self.op == "update":
            rows = self.fake.tables.get(self.table, [])
            for r in rows:
                r.update(self.payload)
            return _Resp([dict(r) for r in rows])
        if self.op == "upsert":
            if self.fake.upsert_error:
                raise RuntimeError(self.fake.upsert_error)
            existing = [r for r in self.fake.tables.get(self.table, []) if r["id"] == self.payload["id"]]
            if existing:          # ignore_duplicates: nothing written, nothing returned
                return _Resp([])
            self.fake.tables.setdefault(self.table, []).append(dict(self.payload))
            return _Resp([dict(self.payload)])
        return _Resp([dict(r) for r in self.fake.tables.get(self.table, [])])


class _Auth:
    def __init__(self, claims):
        self.claims = claims

    def get_claims(self, token):
        return {"claims": self.claims}


class FakeSupabase:
    def __init__(self, claims):
        self.tables, self.calls, self.upsert_error = {}, [], None
        self.auth = _Auth(claims)

    def table(self, name):
        return _Query(self, name)


CLAIMS = {"sub": USER, "email": "Rider@Example.com", "user_metadata": {"full_name": "Asha Verma", "picture": "https://x/y.png", "role": "admin"}}


def _creds():
    return HTTPAuthorizationCredentials(scheme="Bearer", credentials="jwt")


def test_missing_profile_is_created_as_citizen(monkeypatch):
    fake = FakeSupabase(CLAIMS)
    monkeypatch.setattr(auth_mod, "supabase", fake)
    user = auth_mod.get_current_user(_creds())
    assert user["id"] == USER and user["role"] == "citizen"
    written = [c for c in fake.calls if c[1] == "upsert"]
    assert len(written) == 1
    row = written[0][2]
    assert row == {"id": USER, "email": "rider@example.com", "full_name": "Asha Verma", "avatar_url": "https://x/y.png", "role": "citizen"}
    # the JWT's user_metadata.role = admin is ignored: never a source of role
    assert fake.tables["profiles"][0]["role"] == "citizen"
    assert user["profile"]["email"] == "rider@example.com"


def test_existing_profile_is_never_touched(monkeypatch):
    fake = FakeSupabase(CLAIMS)
    fake.tables["profiles"] = [{"id": USER, "email": "rider@example.com", "full_name": "Asha", "role": "officer"}]
    monkeypatch.setattr(auth_mod, "supabase", fake)
    user = auth_mod.get_current_user(_creds())
    assert user["role"] == "officer"
    assert not [c for c in fake.calls if c[1] == "upsert"]


def test_profile_creation_failure_still_signs_in_as_citizen(monkeypatch):
    fake = FakeSupabase(CLAIMS)
    fake.upsert_error = "permission denied for table profiles"
    monkeypatch.setattr(auth_mod, "supabase", fake)
    user = auth_mod.get_current_user(_creds())
    assert user["role"] == "citizen" and user["profile"] is None


def test_concurrent_trigger_wins_without_overwrite(monkeypatch):
    """Lookup finds nothing, the trigger inserts before our upsert: ignore_duplicates returns [] and we re-read."""
    fake = FakeSupabase(CLAIMS)
    monkeypatch.setattr(auth_mod, "supabase", fake)
    original_execute = _Query.execute

    def racing_execute(self):
        if self.op == "upsert" and not self.fake.tables.get("profiles"):
            self.fake.tables["profiles"] = [{"id": USER, "email": "rider@example.com", "full_name": "Trigger", "role": "citizen", "requested_role": "officer"}]
        return original_execute(self)

    monkeypatch.setattr(_Query, "execute", racing_execute)
    user = auth_mod.get_current_user(_creds())
    assert user["profile"]["full_name"] == "Trigger" and user["profile"]["requested_role"] == "officer"


def test_profile_update_is_404_when_no_row_matched(monkeypatch):
    """PostgREST answers 200 [] for an UPDATE that matched nothing; echoing the request back hid a missing row."""
    from fastapi import HTTPException
    from app.routes import auth as routes_auth
    from app.schemas.auth import ProfileUpdateRequest
    fake = FakeSupabase(CLAIMS)
    monkeypatch.setattr(routes_auth, "supabase", fake)
    with pytest.raises(HTTPException) as ex:
        routes_auth.update_profile(ProfileUpdateRequest(full_name="Asha"), current_user={"id": USER})
    assert ex.value.status_code == 404
    fake.tables["profiles"] = [{"id": USER, "email": "rider@example.com", "full_name": "Old", "role": "citizen"}]
    out = routes_auth.update_profile(ProfileUpdateRequest(full_name="Asha"), current_user={"id": USER})
    assert out["data"]["full_name"] == "Asha" and out["data"]["role"] == "citizen"
