"""Opt-in real PostgreSQL tests. Uses an injected test session, never runtime DATABASE_URL."""
import os
from datetime import UTC, datetime
from uuid import uuid4

import httpx
import pytest
from sqlalchemy import select, text
from sqlalchemy.engine import make_url
from sqlalchemy.exc import DBAPIError, IntegrityError
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from backend.app.core.config import Settings
from backend.app.main import create_app
from backend.app.modules.identity.dependencies import get_identity_repository, get_identity_service, get_token_verifier
from backend.app.modules.identity.errors import Forbidden
from backend.app.modules.identity.models import Organization, OrganizationMember, Profile
from backend.app.modules.identity.policy import CurrentUser, MemberRole
from backend.app.modules.identity.repository import SQLAlchemyIdentityRepository
from backend.app.modules.identity.service import IdentityService

pytestmark = pytest.mark.anyio


@pytest.fixture
async def pg_session(request):
    if not request.config.getoption("--local-postgres"):
        pytest.skip("Opt in with --local-postgres and a disposable loopback DATABASE_MIGRATION_URL")
    value = os.environ.get("DATABASE_MIGRATION_URL", "")
    if not value:
        pytest.fail("Local integration URL is required")
    url = make_url(value)
    if (url.host not in {"localhost", "127.0.0.1"} or url.database != "nightclub_ai_prompt4_test"
            or url.username != "alembic_test_user" or url.query):
        pytest.fail("Integration URL must target the approved disposable local database/role")
    # This engine is scoped to the test fixture; production session factory is untouched.
    engine = create_async_engine(url.set(drivername="postgresql+asyncpg"))
    try:
        async with engine.connect() as connection:
            transaction = await connection.begin()
            factory = async_sessionmaker(bind=connection, expire_on_commit=False,
                                         join_transaction_mode="create_savepoint")
            async with factory() as session:
                yield session
            await transaction.rollback()
    finally:
        await engine.dispose()


async def seed(session, role="owner", user_id=None):
    user_id = user_id or uuid4()
    other_user = uuid4()
    for identifier in (user_id, other_user):
        await session.execute(text("INSERT INTO auth.users(id) VALUES (:id)"), {"id": identifier})
    profile = Profile(id=user_id, display_name="Local fixture", email=f"{user_id}@example.test",
                      is_active=True, last_seen_at=datetime.now(UTC))
    second = Profile(id=other_user, display_name="Other fixture", is_active=True)
    own = Organization(id=uuid4(), name="Own organization", slug=str(uuid4()), is_active=True)
    other = Organization(id=uuid4(), name="Other organization", slug=str(uuid4()), is_active=True)
    session.add_all([profile, second, own, other])
    await session.flush()
    session.add_all([
        OrganizationMember(user_id=user_id, organization_id=own.id, role=role),
        OrganizationMember(user_id=other_user, organization_id=other.id, role="owner"),
    ])
    await session.flush()
    await session.commit()
    return user_id, other_user, own.id, other.id


@pytest.mark.parametrize("role", list(MemberRole))
async def test_real_orm_persistence_enum_timezone_and_tenant_scoping(pg_session, role):
    user_id, other_user, own_id, other_id = await seed(pg_session, role.value)
    pg_session.expunge_all()
    repository = SQLAlchemyIdentityRepository(pg_session)
    profile = await repository.get_profile(user_id)
    assert profile.id == user_id and profile.last_seen_at.tzinfo is not None
    member = await repository.get_membership(user_id, own_id)
    assert member.user_id == user_id and member.role == role.value
    assert await repository.get_membership(user_id, other_id) is None
    assert await repository.get_membership(other_user, own_id) is None
    rows = await repository.list_memberships(user_id, None, 51)
    assert [row.organization.id for row in rows] == [own_id]
    service = IdentityService(repository)
    context, org = await service.organization(CurrentUser(user_id), own_id)
    assert context.role == role and org.id == own_id
    with pytest.raises(Forbidden):
        await service.organization(CurrentUser(user_id), other_id)


@pytest.mark.parametrize("disabled", ["profile", "organization", "membership"])
async def test_real_inactivation_and_membership_removal_take_effect(pg_session, disabled):
    user_id, _, own_id, _ = await seed(pg_session)
    repository = SQLAlchemyIdentityRepository(pg_session)
    assert await repository.get_membership(user_id, own_id)
    if disabled == "profile":
        (await pg_session.get(Profile, user_id)).is_active = False
    elif disabled == "organization":
        (await pg_session.get(Organization, own_id)).is_active = False
    else:
        await pg_session.delete(await pg_session.get(OrganizationMember, (own_id, user_id)))
    await pg_session.commit()
    pg_session.expunge_all()
    assert await repository.get_membership(user_id, own_id) is None
    assert await repository.list_memberships(user_id, None, 51) == []


async def test_real_foreign_key_and_enum_reject_invalid_persistence(pg_session):
    with pytest.raises(IntegrityError):
        async with pg_session.begin_nested():
            pg_session.add(Profile(id=uuid4(), is_active=True))
            await pg_session.flush()
    user_id, _, own_id, _ = await seed(pg_session)
    with pytest.raises(DBAPIError):
        async with pg_session.begin_nested():
            await pg_session.execute(text("UPDATE organization_members SET role = 'admin' WHERE user_id = :id"),
                                     {"id": user_id})
    role = await pg_session.scalar(select(OrganizationMember.role).where(OrganizationMember.organization_id == own_id))
    assert role == "owner"


async def test_real_pagination_never_exposes_other_users_organizations(pg_session):
    user_id, _, own_id, foreign_id = await seed(pg_session)
    additional = Organization(id=uuid4(), name="Another own organization", slug=str(uuid4()), is_active=True)
    pg_session.add(additional)
    await pg_session.flush()
    pg_session.add(OrganizationMember(user_id=user_id, organization_id=additional.id, role="viewer"))
    await pg_session.commit()
    service = IdentityService(SQLAlchemyIdentityRepository(pg_session))
    first, cursor = await service.organizations(CurrentUser(user_id), None, 1)
    second, last_cursor = await service.organizations(CurrentUser(user_id), cursor, 1)
    assert {first[0].organization.id, second[0].organization.id} == {own_id, additional.id}
    assert last_cursor is None
    # A forged position does not widen user filtering.
    from backend.app.modules.identity.service import encode_cursor
    after, _ = await service.organizations(CurrentUser(user_id), encode_cursor(foreign_id), 100)
    assert all(row.organization.id != foreign_id for row in after)


async def test_http_real_repository_denies_tenant_spoof_in_all_input_locations(pg_session, security_material):
    user_id, other_user, own_id, foreign_id = await seed(pg_session, user_id=security_material.user_id)
    app = create_app(Settings(_env_file=None))
    app.dependency_overrides[get_token_verifier] = lambda: security_material.verifier
    app.dependency_overrides[get_identity_repository] = lambda: SQLAlchemyIdentityRepository(pg_session)
    # Historical 0002 owner/savepoint persistence test, not runtime RLS proof.
    # Prompt 5 tests exercise the real context-installing dependency separately.
    app.dependency_overrides[get_identity_service] = lambda: IdentityService(SQLAlchemyIdentityRepository(pg_session))
    headers = {"Authorization": "Bearer " + security_material.token()}
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        assert (await client.get("/api/v1/me", headers=headers)).json()["data"]["id"] == str(user_id)
        rows = (await client.get("/api/v1/me/organizations", headers=headers)).json()["data"]
        assert [row["id"] for row in rows] == [str(own_id)]
        assert (await client.get(f"/api/v1/organizations/{own_id}", headers=headers)).status_code == 200
        for foreign in (foreign_id, uuid4()):
            denied = await client.request("GET", f"/api/v1/organizations/{foreign}",
                params={"user_id": str(other_user), "organization_id": str(own_id)},
                headers={**headers, "X-User-Id": str(other_user)},
                json={"user_id": str(other_user), "organization_id": str(own_id)})
            assert denied.status_code == 403
            assert denied.json()["code"] == "ACCESS_DENIED"
        denied = await client.get(f"/api/v1/organizations/{own_id}",
                                  headers={**headers, "X-Organization-Id": str(foreign_id)})
        assert denied.status_code == 403
