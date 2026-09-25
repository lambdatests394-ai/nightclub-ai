"""Opt-in Prompt 10 PostgreSQL/RLS certification tests."""

from contextlib import asynccontextmanager
import json
import os
from pathlib import Path
from uuid import UUID, uuid4

import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from backend.tests.prompt10_postgres_safety import validated_prompt10_url


pytestmark = pytest.mark.anyio
ROOT = Path(__file__).parents[2]
A = UUID("10000000-0000-0000-0000-000000000001")
B = UUID("10000000-0000-0000-0000-000000000002")
ORG_A = UUID("20000000-0000-0000-0000-000000000001")
ORG_B = UUID("20000000-0000-0000-0000-000000000002")
def safe_url():
    try:
        return validated_prompt10_url(
            os.environ.get("PROMPT10_RUNTIME_URL", ""), "nightclub_api",
        )
    except ValueError:
        pytest.fail("PROMPT10_RUNTIME_URL: explicit local environment required (value withheld)", pytrace=False)


@pytest.fixture
async def runtime(request):
    if not request.config.getoption("--prompt10-postgres"):
        pytest.skip("Prompt 10 B2-A real PostgreSQL opt-in is not enabled")
    engine = create_async_engine(
        safe_url().set(drivername="postgresql+asyncpg"),
        pool_size=1,
        max_overflow=0,
        hide_parameters=True,
    )
    factory = async_sessionmaker(engine, expire_on_commit=False)
    try:
        yield factory
    finally:
        await engine.dispose()


@asynccontextmanager
async def tenant(runtime, user=A, organization=ORG_A):
    async with runtime() as session:
        async with session.begin():
            await session.execute(
                text("SELECT set_config('app.user_id', :value, true)"),
                {"value": str(user)},
            )
            await session.execute(
                text("SELECT set_config('app.organization_id', :value, true)"),
                {"value": str(organization)},
            )
            yield session


def sqlstate(error):
    return error.value.orig.sqlstate


async def create_connection(runtime, user=A, organization=ORG_A):
    connection_id = uuid4()
    async with tenant(runtime, user, organization) as session:
        await session.execute(text("""INSERT INTO platform_connections
            (id,organization_id,platform,external_account_id,display_name,capabilities,
             credentials_ciphertext,credential_key_version,status,last_verified_at)
            VALUES (:id,:organization,'facebook',:page,'Synthetic Page','{}',
                    decode('010203','hex'),1,'active',now())"""),
            {"id": connection_id, "organization": organization, "page": "page-" + connection_id.hex})
    return connection_id


async def create_content(runtime, connection_id, user=A, organization=ORG_A, status="approved"):
    content_id, version_id = uuid4(), uuid4()
    async with tenant(runtime, user, organization) as session:
        await session.execute(text("""INSERT INTO content_items
            (id,organization_id,platform,connection_id,status,current_version_no,
             approved_version_no,created_by)
            VALUES (:id,:organization,'facebook',:connection,'draft',1,NULL,:actor)"""),
            {"id": content_id, "organization": organization,
             "connection": connection_id, "actor": user})
        await session.execute(text("""INSERT INTO content_versions
            (id,content_item_id,version_no,body,payload,source,created_by)
            VALUES (:id,:content,1,'Synthetic publication fixture','{}','manual',:actor)"""),
            {"id": version_id, "content": content_id, "actor": user})
        if status == "approved":
            await session.execute(text(
                "UPDATE content_items SET status='in_review' WHERE id=:id"), {"id": content_id})
            await session.execute(text("""UPDATE content_items
                SET status='approved',approved_version_no=1 WHERE id=:id"""), {"id": content_id})
    return content_id, version_id


async def create_job(runtime, content_id, version_id, user=A, status="pending"):
    job_id = uuid4()
    async with tenant(runtime, user, ORG_A) as session:
        await session.execute(text("""INSERT INTO publication_jobs
            (id,content_item_id,content_version_id,idempotency_key,scheduled_for,
             status,attempt_count,created_by,next_attempt_at)
            VALUES (:id,:content,:version,:key,now(),'pending',0,:actor,NULL)"""),
            {"id": job_id, "content": content_id, "version": version_id,
             "key": uuid4(), "actor": user})
        if status in {"leased", "publishing", "retryable_failure"}:
            await session.execute(text("""UPDATE publication_jobs SET status='leased',
                lease_token=:lease,lease_expires_at=now()+interval '5 minutes'
                WHERE id=:id"""), {"id": job_id, "lease": uuid4()})
        if status == "publishing":
            await session.execute(text(
                "UPDATE publication_jobs SET status='publishing' WHERE id=:id"), {"id": job_id})
        if status == "retryable_failure":
            await session.execute(text("""UPDATE publication_jobs
                SET status='retryable_failure',lease_token=NULL,lease_expires_at=NULL,
                    next_attempt_at=now()+interval '1 minute' WHERE id=:id"""), {"id": job_id})
    return job_id


async def publication_fixture(runtime, job_status="pending"):
    connection = await create_connection(runtime)
    content, version = await create_content(runtime, connection)
    job = await create_job(runtime, content, version, status=job_status)
    return connection, content, version, job


async def test_prompt10_catalog_counts_history_force_rls_and_grants(runtime):
    from importlib.util import module_from_spec, spec_from_file_location
    spec = spec_from_file_location(
        "prompt10_catalog",
        ROOT / "backend/migrations/versions/20260924_0008_facebook_publication_access.py",
    )
    migration = module_from_spec(spec)
    spec.loader.exec_module(migration)
    async with runtime() as session:
        connection = await session.connection()
        await connection.run_sync(migration.verify_policies)
        await connection.run_sync(migration.verify_grants)
        assert not await session.scalar(text(
            "SELECT has_table_privilege(current_user,'public.alembic_version','SELECT')"
        ))
        assert await session.scalar(text(
            "SELECT count(*) FROM pg_policies WHERE schemaname='public'")) == 43
        rows = (await session.execute(text("""SELECT relrowsecurity,relforcerowsecurity,
            pg_get_userbyid(relowner) FROM pg_class WHERE relnamespace='public'::regnamespace
            AND relkind='r' AND relname=ANY(CAST(:tables AS text[]))"""),
            {"tables": list(migration.TABLES)})).all()
        assert len(rows) == 22
        assert all(rls and force and owner != "nightclub_api" for rls, force, owner in rows)


async def test_oauth_state_actor_and_tenant_isolation_and_one_time_consumption(runtime):
    state_id = uuid4()
    digest = state_id.hex * 2
    async with tenant(runtime) as session:
        await session.execute(text("""INSERT INTO facebook_oauth_states
            (id,organization_id,actor_id,state_digest,nonce_digest,requested_page_id,expires_at)
            VALUES (:id,:organization,:actor,:state,:nonce,'synthetic-page',now()+interval '10 minutes')"""),
            {"id": state_id, "organization": ORG_A, "actor": A,
             "state": digest, "nonce": "b" * 64})
        assert await session.scalar(text(
            "SELECT count(*) FROM facebook_oauth_states WHERE id=:id"), {"id": state_id}) == 1
    async with tenant(runtime, B, ORG_A) as session:
        assert await session.scalar(text(
            "SELECT count(*) FROM facebook_oauth_states WHERE id=:id"), {"id": state_id}) == 0
    async with tenant(runtime, B, ORG_B) as session:
        assert await session.scalar(text(
            "SELECT count(*) FROM facebook_oauth_states WHERE id=:id"), {"id": state_id}) == 0
    async with tenant(runtime) as session:
        await session.execute(text(
            "UPDATE facebook_oauth_states SET consumed_at=now() WHERE id=:id"), {"id": state_id})
    with pytest.raises(DBAPIError) as error:
        async with tenant(runtime) as session:
            await session.execute(text(
                "UPDATE facebook_oauth_states SET consumed_at=now() WHERE id=:id"), {"id": state_id})
    assert sqlstate(error) == "23514"


@pytest.mark.parametrize("assignment", [
    "organization_id='20000000-0000-0000-0000-000000000002'",
    "actor_id='10000000-0000-0000-0000-000000000002'",
    "state_digest='c' || repeat('0',63)",
    "nonce_digest='d' || repeat('0',63)",
    "requested_page_id='different-page'",
    "expires_at=expires_at+interval '1 hour'",
])
async def test_oauth_state_binding_is_immutable(runtime, assignment):
    state_id = uuid4()
    async with tenant(runtime) as session:
        await session.execute(text("""INSERT INTO facebook_oauth_states
            (id,organization_id,actor_id,state_digest,nonce_digest,requested_page_id,expires_at)
            VALUES (:id,:organization,:actor,:state,:nonce,'synthetic-page',now()+interval '10 minutes')"""),
            {"id": state_id, "organization": ORG_A, "actor": A,
             "state": state_id.hex * 2, "nonce": uuid4().hex * 2})
    with pytest.raises(DBAPIError) as error:
        async with tenant(runtime) as session:
            await session.execute(text(
                f"UPDATE facebook_oauth_states SET {assignment} WHERE id=:id"), {"id": state_id})
    assert sqlstate(error) in {"23514", "42501"}


async def test_oauth_cross_actor_insert_and_delete_are_denied(runtime):
    with pytest.raises(DBAPIError) as error:
        async with tenant(runtime) as session:
            await session.execute(text("""INSERT INTO facebook_oauth_states
                (id,organization_id,actor_id,state_digest,nonce_digest,requested_page_id,expires_at)
                VALUES (:id,:organization,:actor,:state,:nonce,'synthetic-page',now()+interval '10 minutes')"""),
                {"id": uuid4(), "organization": ORG_A, "actor": B,
                 "state": uuid4().hex * 2, "nonce": uuid4().hex * 2})
    assert sqlstate(error) == "42501"
    with pytest.raises(DBAPIError) as error:
        async with tenant(runtime) as session:
            await session.execute(text("DELETE FROM facebook_oauth_states"))
    assert sqlstate(error) == "42501"


async def test_facebook_connection_shape_identity_and_credential_isolation(runtime):
    connection_id = await create_connection(runtime)
    async with tenant(runtime) as session:
        row = (await session.execute(text("""SELECT id,organization_id,platform,
            external_account_id,display_name,capabilities,credentials_ciphertext,
            credential_key_version,token_expires_at,status,last_verified_at,
            last_error_code,last_error_at,created_at,updated_at
            FROM platform_connections WHERE id=:id"""),
            {"id": connection_id})).one()
        assert row.id == connection_id and bytes(row.credentials_ciphertext) == b"\x01\x02\x03"
        assert row.created_at.tzinfo is not None and row.updated_at.tzinfo is not None
    # Amendment 1: the single runtime role intentionally has all connection
    # SELECT columns. FORCE RLS, AES-GCM and explicit application projections
    # remain the security boundary.
    async with tenant(runtime) as session:
        row = (await session.execute(text(
            "SELECT * FROM platform_connections WHERE id=:id"), {"id": connection_id})).one()
        assert row.id == connection_id
    async with tenant(runtime, B, ORG_B) as session:
        assert (await session.execute(text(
            "SELECT * FROM platform_connections WHERE id=:id"), {"id": connection_id})).one_or_none() is None
    for assignment in (
        "external_account_id='changed'", "organization_id='20000000-0000-0000-0000-000000000002'",
        "platform='whatsapp'", "status='active',last_verified_at=NULL",
    ):
        with pytest.raises(DBAPIError) as error:
            async with tenant(runtime) as session:
                await session.execute(text(
                    f"UPDATE platform_connections SET {assignment} WHERE id=:id"), {"id": connection_id})
        assert sqlstate(error) in {"23514", "42501"}


async def test_non_facebook_and_cross_tenant_connection_insert_and_delete_denied(runtime):
    for organization, platform in ((ORG_B, "facebook"), (ORG_A, "whatsapp")):
        with pytest.raises(DBAPIError) as error:
            async with tenant(runtime) as session:
                await session.execute(text("""INSERT INTO platform_connections
                    (id,organization_id,platform,external_account_id,display_name,capabilities,
                     credentials_ciphertext,credential_key_version,status)
                    VALUES (:id,:organization,:platform,'synthetic','Synthetic','{}',
                            decode('01','hex'),1,'pending')"""),
                    {"id": uuid4(), "organization": organization, "platform": platform})
        assert sqlstate(error) == "42501"
    with pytest.raises(DBAPIError) as error:
        async with tenant(runtime) as session:
            await session.execute(text("DELETE FROM platform_connections"))
    assert sqlstate(error) == "42501"


@pytest.mark.parametrize("start,target,assignment,passes", [
    ("pending", "leased", "lease_token=:lease,lease_expires_at=now()+interval '5 minutes'", True),
    ("pending", "cancelled", "lease_token=NULL,lease_expires_at=NULL", True),
    ("pending", "succeeded", "published_external_id='post'", False),
    ("retryable_failure", "leased", "lease_token=:lease,lease_expires_at=now()+interval '5 minutes',next_attempt_at=NULL", True),
    ("retryable_failure", "cancelled", "next_attempt_at=NULL", True),
    ("leased", "publishing", "lease_token=lease_token", True),
    ("leased", "succeeded", "lease_token=NULL,lease_expires_at=NULL,published_external_id='post'", False),
    ("publishing", "succeeded", "lease_token=NULL,lease_expires_at=NULL,published_external_id='post'", True),
    ("publishing", "retryable_failure", "lease_token=NULL,lease_expires_at=NULL,next_attempt_at=now()+interval '1 minute'", True),
    ("publishing", "permanent_failure", "lease_token=NULL,lease_expires_at=NULL", True),
])
async def test_publication_job_transition_matrix(runtime, start, target, assignment, passes):
    _, _, _, job = await publication_fixture(runtime, start)
    parameters = {"id": job, "lease": uuid4()}
    statement = text(f"UPDATE publication_jobs SET status=:target,{assignment} WHERE id=:id")
    if passes:
        async with tenant(runtime) as session:
            await session.execute(statement, {**parameters, "target": target})
    else:
        with pytest.raises(DBAPIError) as error:
            async with tenant(runtime) as session:
                await session.execute(statement, {**parameters, "target": target})
        assert sqlstate(error) == "23514"


async def test_active_job_uniqueness_includes_retryable_and_terminal_releases_slot(runtime):
    _, content, version, job = await publication_fixture(runtime, "retryable_failure")
    with pytest.raises(DBAPIError) as error:
        await create_job(runtime, content, version)
    assert sqlstate(error) == "23505"
    async with tenant(runtime) as session:
        await session.execute(text("""UPDATE publication_jobs SET status='cancelled',next_attempt_at=NULL
            WHERE id=:id"""), {"id": job})
    assert await create_job(runtime, content, version)


async def test_publication_job_identity_and_delete_are_denied(runtime):
    _, _, _, job = await publication_fixture(runtime)
    with pytest.raises(DBAPIError) as error:
        async with tenant(runtime) as session:
            await session.execute(text(
                "UPDATE publication_jobs SET scheduled_for=scheduled_for+interval '1 minute' WHERE id=:id"),
                {"id": job})
    assert sqlstate(error) in {"23514", "42501"}
    with pytest.raises(DBAPIError) as error:
        async with tenant(runtime) as session:
            await session.execute(text("DELETE FROM publication_jobs WHERE id=:id"), {"id": job})
    assert sqlstate(error) == "42501"


async def create_attempt(runtime, job):
    attempt_id = uuid4()
    async with tenant(runtime) as session:
        await session.execute(text("""INSERT INTO publication_attempts
            (id,publication_job_id,attempt_no,started_at,request_fingerprint,outcome)
            VALUES (:id,:job,1,now(),:fingerprint,'in_progress')"""),
            {"id": attempt_id, "job": job, "fingerprint": "f" * 64})
    return attempt_id


@pytest.mark.parametrize("outcome", ["succeeded", "retryable_failure", "permanent_failure"])
async def test_publication_attempt_one_way_terminalization(runtime, outcome):
    _, _, _, job = await publication_fixture(runtime)
    attempt = await create_attempt(runtime, job)
    async with tenant(runtime) as session:
        await session.execute(text("""UPDATE publication_attempts
            SET outcome=:outcome,finished_at=now(),provider_response=CAST(:response AS jsonb)
            WHERE id=:id"""),
            {"outcome": outcome, "id": attempt,
             "response": json.dumps({"status_category": "synthetic", "http_status": 200})})
    with pytest.raises(DBAPIError) as error:
        async with tenant(runtime) as session:
            await session.execute(text("""UPDATE publication_attempts
                SET outcome='in_progress',finished_at=NULL WHERE id=:id"""), {"id": attempt})
    assert sqlstate(error) == "23514"


@pytest.mark.parametrize("response", [
    "'1'::jsonb",
    "'[]'::jsonb",
    "'{\"access_token\":\"synthetic\"}'::jsonb",
    "jsonb_build_object('status_category',repeat('x',5000))",
])
async def test_provider_response_rejects_non_object_unapproved_or_oversized(runtime, response):
    _, _, _, job = await publication_fixture(runtime)
    attempt = await create_attempt(runtime, job)
    with pytest.raises(DBAPIError) as error:
        async with tenant(runtime) as session:
            await session.execute(text(f"""UPDATE publication_attempts
                SET outcome='succeeded',finished_at=now(),provider_response={response}
                WHERE id=:id"""), {"id": attempt})
    assert sqlstate(error) == "23514"


async def test_attempt_identity_terminal_without_finish_and_delete_are_denied(runtime):
    _, _, _, job = await publication_fixture(runtime)
    with pytest.raises(DBAPIError) as error:
        async with tenant(runtime) as session:
            await session.execute(text("""INSERT INTO publication_attempts
                (id,publication_job_id,attempt_no,started_at,request_fingerprint,outcome)
                VALUES (:id,:job,1,now(),:fingerprint,'succeeded')"""),
                {"id": uuid4(), "job": job, "fingerprint": "e" * 64})
    assert sqlstate(error) in {"23514", "42501"}
    attempt = await create_attempt(runtime, job)
    with pytest.raises(DBAPIError) as error:
        async with tenant(runtime) as session:
            await session.execute(text(
                "UPDATE publication_attempts SET attempt_no=2 WHERE id=:id"), {"id": attempt})
    assert sqlstate(error) in {"23514", "42501"}
    with pytest.raises(DBAPIError) as error:
        async with tenant(runtime) as session:
            await session.execute(text("DELETE FROM publication_attempts WHERE id=:id"), {"id": attempt})
    assert sqlstate(error) == "42501"


@pytest.mark.parametrize("start,target,values,passes", [
    ("approved", "scheduled", {"scheduled": True}, True),
    ("scheduled", "approved", {"cancel": True}, True),
    ("scheduled", "publishing", {}, True),
    ("publishing", "published", {"published": True}, True),
    ("publishing", "failed", {"failed": True}, True),
    ("draft", "scheduled", {"scheduled": True}, False),
    ("approved", "publishing", {"scheduled": True}, False),
    ("scheduled", "published", {"published": True}, False),
])
async def test_content_publication_transition_matrix(runtime, start, target, values, passes):
    connection = await create_connection(runtime)
    content, _ = await create_content(runtime, connection, status="approved")
    async with tenant(runtime) as session:
        if start == "draft":
            await session.execute(text(
                "UPDATE content_items SET status='draft',approved_version_no=NULL WHERE id=:id"),
                {"id": content})
        elif start in {"scheduled", "publishing"}:
            await session.execute(text("""UPDATE content_items
                SET status='scheduled',scheduled_for=now()+interval '1 hour' WHERE id=:id"""),
                {"id": content})
            if start == "publishing":
                await session.execute(text(
                    "UPDATE content_items SET status='publishing' WHERE id=:id"), {"id": content})
    assignments = ["status=:target"]
    if values.get("scheduled"):
        assignments.append("scheduled_for=now()+interval '1 hour'")
    if values.get("cancel"):
        assignments.append("scheduled_for=NULL")
    if values.get("published"):
        assignments.extend(("published_at=now()", "external_post_id='synthetic-post'"))
    if values.get("failed"):
        assignments.append("last_error_code='SYNTHETIC_FAILURE'")
    statement = text("UPDATE content_items SET " + ",".join(assignments) + " WHERE id=:id")
    if passes:
        async with tenant(runtime) as session:
            await session.execute(statement, {"target": target, "id": content})
    else:
        with pytest.raises(DBAPIError) as error:
            async with tenant(runtime) as session:
                await session.execute(statement, {"target": target, "id": content})
        assert sqlstate(error) in {"23514", "42501"}


async def test_publication_transition_cannot_change_version_pointers(runtime):
    connection = await create_connection(runtime)
    content, _ = await create_content(runtime, connection)
    with pytest.raises(DBAPIError) as error:
        async with tenant(runtime) as session:
            await session.execute(text("""UPDATE content_items
                SET status='scheduled',scheduled_for=now()+interval '1 hour',
                    current_version_no=2,approved_version_no=2 WHERE id=:id"""), {"id": content})
    assert sqlstate(error) == "23514"


async def test_historical_content_transitions_remain_available(runtime):
    connection = await create_connection(runtime)
    content, _ = await create_content(runtime, connection, status="draft")
    async with tenant(runtime) as session:
        await session.execute(text(
            "UPDATE content_items SET status='in_review' WHERE id=:id"), {"id": content})
        await session.execute(text("""UPDATE content_items
            SET status='changes_requested' WHERE id=:id"""), {"id": content})
        await session.execute(text("""UPDATE content_items
            SET status='draft',current_version_no=2 WHERE id=:id"""), {"id": content})
        assert await session.scalar(text(
            "SELECT status FROM content_items WHERE id=:id"), {"id": content}) == "draft"
