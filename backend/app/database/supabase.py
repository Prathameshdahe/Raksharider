"""
The service-role Supabase client.

Built LAZILY on first use. Creating it at import time means one missing
environment variable takes the whole process down before uvicorn binds a port,
and the platform reports a bare 503 with nothing to go on. With a lazy client
the service always starts, `/health` reports exactly which variable is missing,
and any call that needs the database returns a clear error instead.
"""

import os
from typing import Any, List, Optional

from app.config import SUPABASE_URL, SUPABASE_SERVICE_ROLE_KEY

REQUIRED_ENV = ("SUPABASE_URL", "SUPABASE_SERVICE_ROLE_KEY")
_ENV_VALUES = {"SUPABASE_URL": SUPABASE_URL, "SUPABASE_SERVICE_ROLE_KEY": SUPABASE_SERVICE_ROLE_KEY}


def missing_env() -> List[str]:
    """Names of the required variables that are not set. Empty means configured."""
    return [name for name in REQUIRED_ENV if not _ENV_VALUES.get(name)]


class _LazySupabase:
    """Creates the real client on first attribute access and caches it.

    `timeout` (seconds) bounds every PostgREST call made through this instance; the default
    client keeps the library default so long uploads and RPCs are never cut short.
    """

    __slots__ = ("_client", "_timeout")

    def __init__(self, timeout: Optional[float] = None) -> None:
        self._client = None
        self._timeout = timeout

    def _resolve(self):
        if self._client is None:
            absent = missing_env()
            if absent:
                raise RuntimeError(
                    "Supabase is not configured: missing " + ", ".join(absent) +
                    ". Set it in the deployment environment (Render: Environment tab) or backend/.env."
                )
            from supabase import create_client
            if self._timeout:
                from supabase import ClientOptions
                self._client = create_client(SUPABASE_URL, SUPABASE_SERVICE_ROLE_KEY,
                                             options=ClientOptions(postgrest_client_timeout=self._timeout))
            else:
                self._client = create_client(SUPABASE_URL, SUPABASE_SERVICE_ROLE_KEY)
        return self._client

    def __getattr__(self, name: str) -> Any:
        return getattr(self._resolve(), name)


supabase = _LazySupabase()

# Health probes only: same key, short timeout, so a stalled database answers "unreachable" in
# seconds instead of holding a request thread for the library default.
DB_PROBE_TIMEOUT_S = float(os.environ.get("DB_PROBE_TIMEOUT_S", "6"))
health_client = _LazySupabase(timeout=DB_PROBE_TIMEOUT_S)
