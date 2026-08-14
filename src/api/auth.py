import secrets

from fastapi import HTTPException, Security, status
from fastapi.security import APIKeyHeader

_api_key_header = APIKeyHeader(name="X-API-Key", auto_error=False)


def make_auth_dependency(token: str):
    # Pre-encode once; compare_digest needs bytes to stay constant-time for
    # arbitrary client input (a non-ASCII str would raise instead of comparing).
    expected = token.encode("utf-8")

    async def verify_token(api_key: str = Security(_api_key_header)) -> None:
        provided = api_key.encode("utf-8") if api_key else b""
        if not secrets.compare_digest(provided, expected):
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Invalid or missing API key",
                headers={"WWW-Authenticate": "ApiKey"},
            )

    return verify_token
