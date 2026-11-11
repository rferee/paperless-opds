import secrets

from fastapi import Depends, HTTPException, Request
from fastapi.security import HTTPBasic, HTTPBasicCredentials

from app.config import settings

security = HTTPBasic(auto_error=False)


def _credentials_valid(credentials: HTTPBasicCredentials) -> bool:
    username_ok = secrets.compare_digest(credentials.username, settings.opds_username or "")
    password_ok = secrets.compare_digest(credentials.password, settings.opds_password or "")
    return username_ok and password_ok


async def request_is_authorized(request: Request) -> bool:
    """Return True if the request would pass verify_auth (used before serving cached responses)."""
    if settings.opds_username is None or settings.opds_password is None:
        return True
    try:
        # HTTPBasic raises on a malformed header even with auto_error=False
        credentials = await security(request)
    except HTTPException:
        return False
    return credentials is not None and _credentials_valid(credentials)


async def verify_auth(
    request: Request,
    credentials: HTTPBasicCredentials | None = Depends(security),
) -> None:
    if settings.opds_username is None or settings.opds_password is None:
        return

    if credentials is None:
        raise HTTPException(
            status_code=401,
            detail="Authentication required",
            headers={"WWW-Authenticate": "Basic"},
        )

    if not _credentials_valid(credentials):
        raise HTTPException(
            status_code=401,
            detail="Invalid credentials",
            headers={"WWW-Authenticate": "Basic"},
        )
