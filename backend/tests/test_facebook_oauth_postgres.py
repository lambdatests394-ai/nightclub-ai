"""Explicit opt-in OAuth HTTP certification; all external transports intercepted."""
import asyncio
import base64
from contextlib import asynccontextmanager
from datetime import UTC, datetime
import os
from urllib.parse import parse_qs, urlsplit
from uuid import UUID, uuid4

import httpx
import jwt
import psycopg
import pytest
from cryptography.hazmat.primitives.asymmetric import ec
from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker

from backend.tests.facebook_oauth_postgres_safety import Inspector, local_url, runtime_posture

pytestmark = pytest.mark.anyio
FAULT_ORG = UUID('91000000-0000-0000-0000-000000000001')
FAULT_ACTOR = UUID('92000000-0000-0000-0000-000000000001')
FAULT_PAGE = '990000000000001'


@pytest.mark.parametrize('host,port,database,role', [
    ('remote.example.test',55432,'nightclub_ai_oauth_recert_test','nightclub_api'),
    ('127.0.0.1',5432,'nightclub_ai_oauth_recert_test','nightclub_api'),
    ('127.0.0.1',55432,'postgres','nightclub_api'),
    ('127.0.0.1',55432,'nightclub_ai_oauth_recert_test','postgres'),
])
def test_local_url_rejects_other_instances(host, port, database, role):
    from sqlalchemy.engine import URL
    value = URL.create('postgresql+psycopg', username=role,password='synthetic',
                       host=host,port=port,database=database)
    with pytest.raises(ValueError,match='isolated OAuth'):
        local_url(value.render_as_string(hide_password=False),'nightclub_api')


def test_local_url_accepts_only_explicit_synthetic_instance():
    from sqlalchemy.engine import URL
    value = URL.create('postgresql+psycopg',username='nightclub_api',password='synthetic',
        host='127.0.0.1',port=55432,database='nightclub_ai_oauth_recert_test')
    assert local_url(value.render_as_string(hide_password=False),'nightclub_api') == value


def load_local_script(name):
    from importlib.util import module_from_spec, spec_from_file_location
    from pathlib import Path
    spec = spec_from_file_location(name, Path(__file__).parents[2]/'scripts/local-dev'/name)
    module = module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_separate_current_oauth_source_manifest_and_preserved_automation():
    assert load_local_script('validate_facebook_oauth_recertification.py').validate_sources() > 20


def test_new_gate_cannot_accept_missing_or_failed_postgres_evidence(tmp_path):
    import json
    gate = load_local_script('validate_facebook_oauth_recertification.py')
    path = tmp_path/'synthetic-result.json'
    path.write_text(json.dumps({'postgresql':'PENDING'}),encoding='utf-8')
    with pytest.raises(AssertionError):
        gate.validate_evidence(path)


def test_cleanup_identity_checks_reject_another_run(monkeypatch):
    import json
    harness = load_local_script('validate_facebook_oauth_postgres.py')
    data = {'Id':'a'*64,'Name':'/nightclub-ai-oauth-pg16-cert',
            'Config':{'Labels':{'com.nightclub-ai.purpose':'oauth-recertification',
                                'com.nightclub-ai.run-id':'another-run'}}}
    monkeypatch.setattr(harness,'command',lambda *args,**kwargs: json.dumps([data]))
    with pytest.raises(AssertionError):
        harness.inspect_owned('a'*64,'expected-run','synthetic-image')


def test_fault_injector_targets_connection_audit_without_consumption():
    sql = load_local_script('validate_facebook_oauth_postgres.py').FAULT_INSTALL
    assert 'facebook.connection.created' in sql and 'facebook.connection.updated' in sql
    assert 'facebook.oauth.consumed' not in sql
    assert str(FAULT_ORG) in sql and str(FAULT_ACTOR) in sql and FAULT_PAGE in sql
    assert 'SECURITY INVOKER' in sql and 'SECURITY DEFINER' not in sql


@pytest.fixture
async def environment(request, monkeypatch):
    if not request.config.getoption('--facebook-oauth-postgres'):
        pytest.skip('OAuth PostgreSQL checkpoint requires explicit --facebook-oauth-postgres')
    runtime_url = local_url(os.environ.get('OAUTH_TEST_RUNTIME_URL', ''), 'nightclub_api')
    admin_url = local_url(os.environ.get('OAUTH_TEST_INSPECTOR_URL', ''), 'postgres')
    # Explicit configuration values are synthetic; never load a repository .env.
    from backend.app.core import database
    from backend.app.core.config import Settings
    from backend.app.modules.identity import dependencies
    from backend.app.modules.identity.authentication import SupabaseJWTVerifier
    from backend.app.modules.identity.jwks import HTTPJWKSProvider, JWKSCache
    from backend.app.main import create_app
    engine = create_async_engine(runtime_url.set(drivername='postgresql+asyncpg'),
                                 pool_size=4, max_overflow=0, hide_parameters=True)
    inspector_engine = create_async_engine(admin_url.set(drivername='postgresql+asyncpg'),
                                           hide_parameters=True)
    inspector = Inspector(inspector_engine)
    settings = Settings(_env_file=None, database_url=None, database_migration_url=None,
        facebook_oauth_admin_enabled=True, meta_app_id='123456', meta_graph_api_version='v24.0',
        meta_app_secret='synthetic-app-secret',
        meta_oauth_redirect_uri='https://app.example.test/admin/facebook/return',
        meta_oauth_state_key=base64.urlsafe_b64encode(os.urandom(32)).decode().rstrip('='),
        meta_credential_encryption_key=base64.urlsafe_b64encode(os.urandom(32)).decode().rstrip('='),
        supabase_jwt_issuer='https://identity.example.test/auth/v1',
        supabase_jwt_audience='authenticated',
        supabase_jwks_url='https://identity.example.test/auth/v1/.well-known/jwks.json')
    monkeypatch.setattr(database, 'SessionFactory', async_sessionmaker(engine, expire_on_commit=False))
    monkeypatch.setattr(dependencies, 'get_settings', lambda: settings)
    key = ec.generate_private_key(ec.SECP256R1())
    jwk = jwt.algorithms.ECAlgorithm.to_jwk(key.public_key(), as_dict=True)
    jwk.update(kid='oauth-local', alg='ES256', use='sig')
    control = psycopg.connect(host=admin_url.host, port=admin_url.port,
        dbname=admin_url.database, user=admin_url.username, password=admin_url.password,
        autocommit=True)
    app = create_app(settings)

    class Environment:
        exchanges = 0
        page_queries = 0
        failing = False
        page = '990000000000002'
        org = None
        actor = None
        state_id = None
        revoke_during_exchange = False

        def seed(self, role='owner', *, fixed=False):
            self.org = FAULT_ORG if fixed else uuid4()
            self.actor = FAULT_ACTOR if fixed else uuid4()
            with control.transaction():
                control.execute('INSERT INTO auth.users(id) VALUES (%s)', (self.actor,))
                control.execute('INSERT INTO profiles(id) VALUES (%s)', (self.actor,))
                control.execute("INSERT INTO organizations(id,name,slug) VALUES (%s,'Synthetic',%s)",
                                (self.org, str(self.org)))
                control.execute('INSERT INTO organization_members(organization_id,user_id,role) VALUES (%s,%s,%s)',
                                (self.org, self.actor, role))

        def token(self, actor=None):
            now = int(datetime.now(UTC).timestamp())
            return jwt.encode(dict(iss=settings.supabase_jwt_issuer, aud='authenticated',
                sub=str(actor or self.actor), iat=now-10, exp=now+600, role='authenticated'),
                key, algorithm='ES256', headers={'kid':'oauth-local'})

        async def call(self, endpoint, body, *, actor=None, org=None, start=False):
            headers = {'Authorization':'Bearer '+self.token(actor),
                       'X-Organization-Id':str(org or self.org)}
            if start:
                headers['Idempotency-Key'] = str(uuid4())
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                                         base_url='https://app.example.test', trust_env=False) as client:
                return await client.post('/api/v1/connections/facebook/oauth/'+endpoint,
                                         headers=headers, json=body)

        async def start(self):
            response = await self.call('start', {'requestedPageId':self.page}, start=True)
            assert response.status_code == 201
            state = parse_qs(urlsplit(response.json()['data']['authorizationUrl']).query)['state'][0]
            self.state_id = (await inspector.observe('states', org=self.org))[-1][0]
            return state

        async def complete(self, state, **kwargs):
            return await self.call('complete', {'state':state,'code':'synthetic-code'}, **kwargs)

        async def audits(self):
            return await inspector.observe('audits', org=self.org)

        async def connections(self):
            return await inspector.observe('connections', org=self.org, page=self.page)

        def revoke(self):
            control.execute('DELETE FROM organization_members WHERE organization_id=%s AND user_id=%s',
                            (self.org, self.actor))

    env = Environment()

    async def transport(request):
        if request.url.host == 'identity.example.test':
            assert request.url.path.endswith('/.well-known/jwks.json')
            return httpx.Response(200, json={'keys':[jwk]})
        assert request.url.host == 'graph.facebook.com'
        if request.method == 'POST' and request.url.path.endswith('/oauth/access_token'):
            env.exchanges += 1
            states = await inspector.observe('states', org=env.org)
            assert any(row[0] == env.state_id and row[2] is not None for row in states)
            audits = await env.audits()
            assert any(row[0] == 'facebook.oauth.consumed' and row[3] == env.state_id for row in audits)
            if env.revoke_during_exchange:
                env.revoke()
            if env.failing:
                return httpx.Response(500, json={'error':{'message':'synthetic private provider message'}})
            return httpx.Response(200, json={'access_token':'synthetic-user-token','token_type':'bearer'})
        assert request.method == 'GET' and request.url.path.endswith('/me/accounts')
        env.page_queries += 1
        return httpx.Response(200, json={'data':[{'id':env.page,'name':'Synthetic Page',
            'access_token':'synthetic-page-token','tasks':['CREATE_CONTENT']}]})

    external = httpx.AsyncClient(transport=httpx.MockTransport(transport), trust_env=False)
    app.state.http_client = external
    app.state.token_verifier = SupabaseJWTVerifier(settings,
        JWKSCache(HTTPJWKSProvider(settings.supabase_jwks_url, external)))
    env.inspector, env.control, env.settings = inspector, control, settings
    before = await runtime_posture(engine, inspector)
    try:
        yield env
    finally:
        try:
            assert await runtime_posture(engine, inspector) == before
            assert await inspector.observe('residues') == [(True, True)]
        finally:
            control.close()
            await external.aclose()
            await engine.dispose()
            await inspector_engine.dispose()


@pytest.mark.parametrize('role', ['owner','manager','editor','reviewer','operator','viewer'])
async def test_real_http_role_authorization(environment, role):
    env = environment
    env.seed(role)
    if role in {'owner','manager'}:
        state = await env.start()
        response = await env.complete(state)
        assert response.status_code == 200
        assert env.exchanges == env.page_queries == 1
    else:
        response = await env.complete('synthetic-invalid-state')
        assert response.status_code == 403
        assert env.exchanges == env.page_queries == 0


@pytest.mark.parametrize('kind', ['actor','organization','foreign-membership'])
async def test_real_http_isolation(environment, kind):
    env = environment
    env.seed()
    state = await env.start()
    actor, org = env.actor, env.org
    env.seed()
    other_actor, other_org = env.actor, env.org
    env.actor, env.org = actor, org
    if kind == 'actor':
        env.control.execute("INSERT INTO organization_members(organization_id,user_id,role) VALUES (%s,%s,'owner')",
                            (org, other_actor))
        response = await env.complete(state, actor=other_actor)
        assert response.status_code == 400
    elif kind == 'organization':
        env.control.execute("INSERT INTO organization_members(organization_id,user_id,role) VALUES (%s,%s,'owner')",
                            (other_org, actor))
        response = await env.complete(state, org=other_org)
        assert response.status_code == 400
    else:
        response = await env.complete(state, org=other_org)
        assert response.status_code == 403
    assert env.exchanges == 0 and await env.connections() == []
    assert all(row[2] is None for row in await env.inspector.observe('states', org=org))


@pytest.mark.parametrize('timing', ['before','after-consumption'])
async def test_real_http_revocation_revalidates(environment, timing):
    env = environment
    env.seed()
    state = await env.start()
    if timing == 'before':
        env.revoke()
    else:
        env.revoke_during_exchange = True
    response = await env.complete(state)
    assert response.status_code == 403
    assert env.exchanges == (timing == 'after-consumption')
    assert await env.connections() == []
    states = await env.inspector.observe('states', org=env.org)
    assert (states[0][2] is not None) == (timing == 'after-consumption')


@pytest.mark.parametrize('concurrent', [False, True])
async def test_real_http_exactly_once_replay_distinct_sessions(environment, concurrent):
    env = environment
    env.seed()
    state = await env.start()
    if concurrent:
        responses = await asyncio.gather(env.complete(state), env.complete(state))
    else:
        responses = [await env.complete(state), await env.complete(state)]
    assert sorted(response.status_code for response in responses) == [200,409]
    assert env.exchanges == 1 and env.page_queries == 1
    assert len(await env.connections()) == 1
    actions = [row[0] for row in await env.audits()]
    assert actions.count('facebook.oauth.consumed') == 1
    assert actions.count('facebook.connection.created') == 1


async def test_real_http_creation_reconnection_encryption_and_audit(environment):
    from backend.app.modules.integrations.credentials import CredentialCipher, CredentialBinding
    env = environment
    env.seed()
    assert (await env.complete(await env.start())).status_code == 200
    first = (await env.connections())[0]
    assert (await env.complete(await env.start())).status_code == 200
    second = (await env.connections())[0]
    assert first[0] == second[0] and first[1] != second[1]
    cipher = CredentialCipher(env.settings.meta_credential_encryption_key, 1)
    binding = CredentialBinding(env.org,'facebook',env.page,1)
    assert cipher.decrypt(bytes(second[1]),binding).access_token.get_secret_value() == 'synthetic-page-token'
    assert b'synthetic-page-token' not in bytes(second[1])
    actions = [row[0] for row in await env.audits()]
    assert actions.count('facebook.connection.created') == actions.count('facebook.connection.updated') == 1
    assert 'synthetic-page-token' not in repr(await env.audits())


async def test_real_http_provider_failure_consumed_no_connection(environment):
    env = environment
    env.seed()
    state = await env.start()
    env.failing = True
    response = await env.complete(state)
    assert response.status_code == 502
    assert 'synthetic private provider message' not in response.text
    assert env.exchanges == 1 and env.page_queries == 0
    assert await env.connections() == []
    assert (await env.complete(state)).status_code == 409
    assert env.exchanges == 1
    assert [row[0] for row in await env.audits()] == ['facebook.oauth.started','facebook.oauth.consumed']


async def test_real_http_persistence_failure_atomic_rollback(environment):
    env = environment
    env.seed(fixed=True)
    env.page = FAULT_PAGE
    state = await env.start()
    # Exact reviewed SQL is retained in the harness, never a migration.
    from importlib.util import module_from_spec, spec_from_file_location
    from pathlib import Path
    spec = spec_from_file_location('oauth_harness', Path(__file__).parents[2] /
                                   'scripts/local-dev/validate_facebook_oauth_postgres.py')
    harness = module_from_spec(spec)
    spec.loader.exec_module(harness)
    try:
        env.control.execute(harness.FAULT_INSTALL)
        response = await env.complete(state)
        assert response.status_code == 503
        assert response.json()['code'] == 'IDENTITY_UNAVAILABLE'
        assert 'synthetic oauth persistence failure' not in response.text
        assert await env.connections() == []
        assert [row[0] for row in await env.audits()] == ['facebook.oauth.started','facebook.oauth.consumed']
        assert env.exchanges == env.page_queries == 1
        assert (await env.complete(state)).status_code == 409
    finally:
        env.control.execute(harness.FAULT_REMOVE)
    assert await env.inspector.observe('residues') == [(True,True)]
