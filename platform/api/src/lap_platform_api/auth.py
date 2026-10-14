import hashlib
import secrets
from typing import Annotated, NoReturn

from fastapi import Security
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from lap_platform_api.errors import PlatformError

BEARER_SCHEME = HTTPBearer(auto_error=False)


def hash_bearer_token(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


class BearerTokenAuthenticator:
    def __init__(self, expected_hash: str) -> None:
        self._expected_hash = expected_hash

    def authenticate(self, authorization: str | None) -> None:
        if authorization is None:
            self._reject()
        scheme, separator, token = authorization.partition(" ")
        if separator != " " or scheme.casefold() != "bearer" or not token:
            self._reject()
        if not secrets.compare_digest(hash_bearer_token(token), self._expected_hash):
            self._reject()

    @staticmethod
    def _reject() -> NoReturn:
        raise PlatformError(
            401,
            "AUTHENTICATION_FAILED",
            "a valid bearer token is required",
            headers={"WWW-Authenticate": "Bearer"},
        )


class BearerAuthenticationDependency:
    def __init__(self, authenticator: BearerTokenAuthenticator) -> None:
        self._authenticator = authenticator

    def __call__(
        self,
        credentials: Annotated[
            HTTPAuthorizationCredentials | None,
            Security(BEARER_SCHEME),
        ],
    ) -> None:
        authorization = None
        if credentials is not None:
            authorization = f"{credentials.scheme} {credentials.credentials}"
        self._authenticator.authenticate(authorization)
