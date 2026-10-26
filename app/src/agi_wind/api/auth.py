import secrets
from fastapi import Header
from ..core.errors import AppError

def bearer_auth(settings,actor):
    def tokens():
        values=[]
        if settings.access_token: values.append(settings.access_token.get_secret_value())
        # The short client key lets a standalone client (server/client split) call the
        # gateway without knowing the long-lived administrative access token.
        if settings.client_key: values.append(settings.client_key.get_secret_value())
        return values

    def authenticate(authorization: str | None=Header(default=None)):
        supplied=authorization[7:] if authorization and authorization.startswith("Bearer ") else ""
        if not any(secrets.compare_digest(supplied,value) for value in tokens()): raise AppError("UNAUTHENTICATED","请提供有效访问凭据",401)
        return actor
    return authenticate
