"""
The service-role Supabase client.

Built LAZILY on first use. Creating it at import time means one missing
environment variable takes the whole process down before uvicorn binds a port,
and the platform reports a bare 503 with nothing to go on. With a lazy client
the service always starts, `/health` reports exactly which variable is missing,
and any call that needs the database returns a clear error instead.
"""

from typing import Any, List

from app.config import SUPABASE_URL, SUPABASE_SERVICE_ROLE_KEY

REQUIRED_ENV = ("SUPABASE_URL", "SUPABASE_SERVICE_ROLE_KEY")


def missing_env() -> List[str]:
    """Names of the required variables that are not set. Empty means configured."""
    return [name for name, value in (("SUPABASE_URL", SUPABASE_URL),
                                     ("SUPABASE_SERVICE_ROLE_KEY", SUPABASE_SERVICE_ROLE_KEY))
            if not value]


class _LazySupabase:
    """Creates the real client on first attribute access and caches it."""

    __slots__ = ("_client",)

    def __init__(self) -> None:
        self._client = None

    def _resolve(self):
        if self._client is None:
            absent = missing_env()
            if absent:
                raise RuntimeError(
                    "Supabase is not configured: missing " + ", ".join(absent) +
                    ". Set it in the deployment environment (Render: Environment tab) or backend/.env."
                )
            from supabase import create_client
            self._client = create_client(SUPABASE_URL, SUPABASE_SERVICE_ROLE_KEY)
        return self._client

    def __getattr__(self, name: str) -> Any:
        return getattr(self._resolve(), name)


supabase = _LazySupabase()
