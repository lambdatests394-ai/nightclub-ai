"""FastAPI composition boundary; authentication precedes persistence access."""
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import Depends, Request

from backend.app.core import database
from backend.app.modules.identity.authentication import TokenVerifier
from backend.app.modules.identity.errors import AuthUnavailable, IdentityUnavailable, SecurityError
from backend.app.modules.identity.policy import CurrentUser
from backend.app.modules.identity.repository import IdentityRepository, SQLAlchemyIdentityRepository
from backend.app.modules.identity.service import IdentityService


async def bearer_token(request: Request) -> str:
    values = request.headers.getlist("authorization")
    if len(values) != 1:
        raise SecurityError()
    parts = values[0].split()
    if len(parts) != 2 or parts[0].lower() != "bearer":
        raise SecurityError()
    return parts[1]


async def get_token_verifier(request: Request, _token: str = Depends(bearer_token)) -> TokenVerifier:
    verifier = getattr(request.app.state, "token_verifier", None)
    if verifier is None:
        raise AuthUnavailable()
    return verifier


async def get_current_user(request: Request, token: str = Depends(bearer_token),
                           verifier: TokenVerifier = Depends(get_token_verifier)) -> CurrentUser:
    user = await verifier.verify(token)
    request.state.verified_user_id = str(user.user_id)
    return user


async def get_identity_repository(_user: CurrentUser = Depends(get_current_user)) -> AsyncIterator[IdentityRepository]:
    if database.SessionFactory is None:
        raise IdentityUnavailable()
    # Propagate route/service exceptions into the existing transaction boundary
    # so rollback and session close complete before leaving the dependency.
    async with asynccontextmanager(database.get_db_session)() as session:
        yield SQLAlchemyIdentityRepository(session)


async def get_identity_service(repository: IdentityRepository = Depends(get_identity_repository)) -> IdentityService:
    return IdentityService(repository)
