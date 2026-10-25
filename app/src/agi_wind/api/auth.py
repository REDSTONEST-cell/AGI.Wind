import secrets
from fastapi import Header
from ..core.errors import AppError

def bearer_auth(settings,actor):
    def authenticate(authorization: str | None=Header(default=None)):
        token=settings.access_token.get_secret_value() if settings.access_token else None
        supplied=authorization[7:] if authorization and authorization.startswith("Bearer ") else ""
        if token is None or not secrets.compare_digest(supplied,token): raise AppError("UNAUTHENTICATED","请提供有效访问凭据",401)
        return actor
    return authenticate
