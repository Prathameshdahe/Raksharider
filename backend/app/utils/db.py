"""Call a Postgres RPC and map the SQLSTATEs raised by our functions to HTTP errors."""
import re
from typing import Any, Optional

from fastapi import status

from app.utils.auth import api_error

SQLSTATE_TO_HTTP = {
    "P0403": status.HTTP_403_FORBIDDEN,
    "P0404": status.HTTP_404_NOT_FOUND,
    "P0409": status.HTTP_409_CONFLICT,
    "P0422": status.HTTP_422_UNPROCESSABLE_ENTITY,
}

# Indian registration: MH12AB1234 / DL1CAA1234 / 22BH1234AB (Bharat series). Normalised: upper, no spaces/hyphens.
PLATE_RE = re.compile(r"^(?:[A-Z]{2}\d{1,2}[A-Z]{0,3}\d{4}|\d{2}BH\d{4}[A-Z]{1,2})$")


def normalise_plate(plate: Optional[str]) -> Optional[str]:
    if plate is None:
        return None
    p = re.sub(r"[\s\-]", "", plate).upper()
    return p or None


def rpc(client, name: str, params: dict) -> Any:
    """client.rpc(...).execute().data; raises the API error envelope on our custom SQLSTATEs."""
    try:
        return client.rpc(name, params).execute().data
    except Exception as e:  # postgrest.exceptions.APIError carries .code (SQLSTATE) and .message
        code = getattr(e, "code", None)
        if code in SQLSTATE_TO_HTTP:
            raise api_error(SQLSTATE_TO_HTTP[code], getattr(e, "message", None) or str(e))
        raise api_error(status.HTTP_500_INTERNAL_SERVER_ERROR, f"{name} failed: {getattr(e, 'message', None) or e}")
