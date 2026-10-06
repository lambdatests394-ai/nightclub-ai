"""Isolated PostgreSQL 16 OAuth checkpoint; never use native/remote databases."""
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import argparse
import hashlib
import json
import os
import re
import secrets
import shutil
import socket
import subprocess
import time
from uuid import uuid4

import psycopg
from sqlalchemy import create_engine
from sqlalchemy.engine import URL

from backend.tests.facebook_oauth_postgres_safety import DATABASE, NAME, PURPOSE, IMAGE, DIGEST, HEAD, QUERIES, migration


class PreflightBlocked(RuntimeError):
    """Only fixed, non-secret diagnostic codes may leave the preflight."""

    def __init__(self, code):
        self.code = code
        super().__init__(code)


STAGES = ('PREFLIGHT', 'IMAGE_VERIFY', 'CONTAINER_CREATE', 'CONTAINER_START',
          'POSTGRES_READINESS', 'DATABASE_BOOTSTRAP', 'ROLE_PROVISIONING',
          'AUTH_STUB', 'ALEMBIC_UPGRADE', 'POSTGRES_TESTS', 'TRIGGER_INSTALL',
          'TRIGGER_REMOVE', 'CATALOG_COMPARE', 'CLEANUP')
OBS = None


class Observation:
    """Only sanitized, allow-listed diagnostics leave the process."""

    def __init__(self, directory):
        self.directory = directory
        self.stage = 'PREFLIGHT'
        self.secrets = []
        self.events = []
        self.failure = None
        self.states = dict.fromkeys(STAGES, 'NOT_REACHED')

    def sanitize(self, value):
        if isinstance(value, dict):
            return {key: self.sanitize(item) for key, item in value.items()}
        if isinstance(value, (list, tuple)):
            return [self.sanitize(item) for item in value]
        if not isinstance(value, (str, bytes)):
            return value
        text = value.decode('utf-8', 'replace') if isinstance(value, bytes) else value
        for secret in sorted(self.secrets, key=len, reverse=True):
            if secret:
                text = text.replace(secret, '[REDACTED]')
        text = re.sub(r'(?i)\b(?:postgres(?:ql)?(?:\+[\w]+)?|https?)://[^\s\]\)"\']+', '[URL REDACTED]', text)
        text = re.sub(r'-----BEGIN [^-]*PRIVATE KEY-----.*?-----END [^-]*PRIVATE KEY-----', '[PRIVATE KEY REDACTED]', text, flags=re.S)
        text = re.sub(r'\beyJ[\w-]+\.[\w-]+\.[\w-]+', '[JWT REDACTED]', text)
        text = re.sub(r'(?i)\b(?:Bearer|Basic)\s+[^\s,\]"\']+', '[AUTH REDACTED]', text)
        text = re.sub(r'(?i)([\w.-]*(?:password|secret|token|api[_-]?key|encryption[_-]?key|authorization|oauth[_-]?code|\bcode|\bstate)[\w.-]*["\']?\s*[:=]\s*)(?:"[^"\n]*"|\'[^\'\n]*\'|[^\s,;\]}]+)', r'\1[REDACTED]', text)
        text = re.sub(r'\b(?:sk-[A-Za-z0-9_-]{10,}|gh[pousr]_[A-Za-z0-9_]+)\b', '[KEY REDACTED]', text)
        return text

    def arguments(self, args):
        safe, redact_next = [], False
        for arg in map(str, args):
            safe.append('[REDACTED]' if redact_next else self.sanitize(arg))
            redact_next = bool(re.fullmatch(r'(?i)--?(?:password|token|secret|api-key|code|state|authorization)', arg))
        return safe

    def emit(self, kind, **fields):
        self.events.append(self.sanitize(dict(kind=kind, stage=self.stage, **fields)))
        self.save()

    def save(self):
        document = self.sanitize({'stages': self.states, 'failure': self.failure, 'events': self.events})
        (self.directory/'observability.json').write_text(json.dumps(document, indent=2)+'\n', encoding='utf-8')

    def enter(self, stage):
        self.stage = stage
        self.states[stage] = 'REACHED'
        self.emit('stage')
        print('STAGE:', stage, flush=True)

    def fail(self, error):
        original = error
        while original.__cause__ is not None:
            original = original.__cause__
        details = {'FAILED_STAGE': self.stage, 'exceptionClass': type(original).__name__,
                   'message': self.sanitize(str(original)),
                   'exitCode': getattr(original, 'returncode', None)}
        if self.failure is None:
            self.failure = details
        self.emit('exception', **details)
        print(json.dumps(self.sanitize(details)), flush=True)


def stage(name):
    if OBS is not None:
        OBS.enter(name)

FAULT_INSTALL = '''BEGIN;
DO $guard$
BEGIN
    IF EXISTS (
        SELECT 1 FROM pg_trigger
        WHERE tgname = 'oauth_recert_fail_connection_audit' AND NOT tgisinternal
    ) OR to_regprocedure('public.oauth_recert_fail_connection_audit_fn()') IS NOT NULL THEN
        RAISE EXCEPTION 'oauth recertification fault injector already exists';
    END IF;
END
$guard$;
CREATE FUNCTION public.oauth_recert_fail_connection_audit_fn()
RETURNS trigger LANGUAGE plpgsql SECURITY INVOKER
SET search_path = pg_catalog, public
AS $function$
BEGIN
    IF NEW.organization_id = '91000000-0000-0000-0000-000000000001'::uuid
       AND NEW.actor_type = 'user'
       AND NEW.actor_id = '92000000-0000-0000-0000-000000000001'
       AND NEW.entity_type = 'facebook_connection'
       AND NEW.action IN ('facebook.connection.created','facebook.connection.updated')
       AND NEW."after" ->> 'externalAccountId' = '990000000000001'
       AND current_setting('app.organization_id', true) = '91000000-0000-0000-0000-000000000001'
       AND current_setting('app.user_id', true) = '92000000-0000-0000-0000-000000000001'
    THEN
        RAISE EXCEPTION 'synthetic oauth persistence failure' USING ERRCODE = 'P0001';
    END IF;
    RETURN NEW;
END
$function$;
CREATE TRIGGER oauth_recert_fail_connection_audit
BEFORE INSERT ON public.audit_logs FOR EACH ROW
EXECUTE FUNCTION public.oauth_recert_fail_connection_audit_fn();
COMMIT;'''
FAULT_REMOVE = '''BEGIN;
DROP TRIGGER IF EXISTS oauth_recert_fail_connection_audit ON public.audit_logs;
DROP FUNCTION IF EXISTS public.oauth_recert_fail_connection_audit_fn();
COMMIT;'''


def clean_environment():
    # Preserve OS essentials only, rather than copying unknown credentials.
    allowed = {'PATH','SYSTEMROOT','WINDIR','COMSPEC','TEMP','TMP','USERPROFILE',
               'LOCALAPPDATA','APPDATA','PROGRAMFILES','PROGRAMFILES(X86)','PATHEXT'}
    child = {key:value for key,value in os.environ.items() if key.upper() in allowed}
    child['PYTHONIOENCODING'] = 'utf-8'
    child['PYTHONDONTWRITEBYTECODE'] = '1'
    return child


def command(args, *, env=None, timeout=180, disclose=False, check=True):
    safe_args = OBS.arguments(args) if OBS else [str(arg) for arg in args]
    try:
        result = subprocess.run(args, cwd=ROOT, env=env or clean_environment(),
                                capture_output=True, text=True, encoding='utf-8',
                                errors='replace', timeout=timeout)
    except Exception as error:
        if OBS:
            OBS.emit('command_exception', command=safe_args,
                     exceptionClass=type(error).__name__, message=str(error),
                     stdout=getattr(error, 'stdout', None), stderr=getattr(error, 'stderr', None))
        raise
    if OBS:
        # Docker inspect contains Config.Env. Never persist that document.
        inspected = args[0] == 'docker' and 'inspect' in args[1:3]
        OBS.emit('command', command=safe_args, exitCode=result.returncode,
                 stdout='[INSPECT ALLOW-LIST ONLY]' if inspected else result.stdout,
                 stderr=result.stderr)
    if disclose:
        print(OBS.sanitize(result.stdout) if OBS else result.stdout, end='', flush=True)
        print(OBS.sanitize(result.stderr) if OBS else result.stderr, end='', flush=True)
    if result.returncode and check:
        original = subprocess.CalledProcessError(result.returncode, safe_args,
            output=OBS.sanitize(result.stdout) if OBS else None,
            stderr=OBS.sanitize(result.stderr) if OBS else None)
        raise RuntimeError('Command failed: ' + str(args[0])) from original
    return result.stdout if check else result


def inspect_identity(container_id, run_id):
    data = json.loads(command(['docker','inspect',container_id]))[0]
    labels = data['Config']['Labels'] or {}
    assert data['Id'] == container_id and data['Name'] == '/'+NAME
    assert labels.get('com.nightclub-ai.purpose') == PURPOSE
    assert labels.get('com.nightclub-ai.run-id') == run_id
    return data


def inspect_owned(container_id, run_id, image_id):
    data = inspect_identity(container_id, run_id)
    assert data['Image'] == image_id and data['Platform'] == 'linux'
    assert data['HostConfig']['PortBindings'] == {'5432/tcp':[{'HostIp':'127.0.0.1','HostPort':'55432'}]}
    assert data['HostConfig']['Tmpfs'] == {'/var/lib/postgresql/data':'rw'}
    assert all(mount['Type'] == 'tmpfs' for mount in data['Mounts'])
    return data


def capture_failure(container_id, run_id):
    """Capture before cleanup; inspect exposes only non-secret fields."""
    for action in ('inspect', 'logs'):
        try:
            if action == 'inspect':
                data = inspect_identity(container_id, run_id)
                safe = {'Id': data['Id'], 'Name': data['Name'], 'Image': data['Image'],
                        'State': data['State'], 'Platform': data['Platform'],
                        'Labels': {key: data['Config']['Labels'][key] for key in
                                   ('com.nightclub-ai.purpose', 'com.nightclub-ai.run-id')},
                        'PortBindings': data['HostConfig']['PortBindings'],
                        'Tmpfs': data['HostConfig']['Tmpfs']}
                OBS.emit('failure_inspect', metadata=safe)
            else:
                result = command(['docker', 'logs', container_id], check=False, timeout=30)
                OBS.emit('failure_logs', exitCode=result.returncode,
                         stdout=result.stdout, stderr=result.stderr)
        except Exception as error:
            OBS.emit('capture_error', action=action, exceptionClass=type(error).__name__, message=str(error))


def cleanup_owned(container_id, run_id):
    stage('CLEANUP')
    inspect_identity(container_id, run_id)
    command(['docker','rm','--force',container_id])
    for selector in ('id='+container_id, 'name='+NAME,
                     'label=com.nightclub-ai.purpose='+PURPOSE,
                     'label=com.nightclub-ai.run-id='+run_id):
        assert not command(['docker','ps','-a','--filter',selector,'--format','{{.ID}}']).strip()
    OBS.emit('cleanup', containerId=container_id, result='PASS')


def remove_fault_if_present(admin_url):
    """Only the exact approved fixture, only in our dedicated container."""
    stage('TRIGGER_REMOVE')
    with psycopg.connect(host=admin_url.host, port=admin_url.port,
            user=admin_url.username, password=admin_url.password,
            dbname=admin_url.database, autocommit=True, connect_timeout=5) as control:
        absent = control.execute(QUERIES['residues']).fetchone()
        if absent != (True, True):
            control.execute(FAULT_REMOVE)
        assert control.execute(QUERIES['residues']).fetchone() == (True, True)
    OBS.emit('trigger_residues', absent=True)


def catalog_snapshot(admin_url):
    """Separate inspector: fixed SELECT allow-list, fresh read-only transaction."""
    with psycopg.connect(host=admin_url.host, port=admin_url.port,
            user=admin_url.username, password=admin_url.password,
            dbname=admin_url.database, autocommit=True) as inspector:
        inspector.execute('BEGIN TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY')
        try:
            assert inspector.execute("SELECT current_database(),current_user,session_user,current_setting('transaction_read_only')").fetchone() == (DATABASE,'postgres','postgres','on')
            return {key: inspector.execute(QUERIES[key]).fetchall() for key in
                    ('policies','objects','roles','memberships','grants','sequences')}
        finally:
            inspector.execute('ROLLBACK')


def record_catalog(directory, name, snapshot):
    data = (json.dumps(snapshot, sort_keys=True, indent=2)+'\n').encode('utf-8')
    (directory/name).write_bytes(data)
    return hashlib.sha256(data).hexdigest()


def run(evidence_dir, image=IMAGE):
    global OBS
    evidence_dir = evidence_dir.resolve()
    user_root = Path(os.environ['USERPROFILE']).resolve()
    if user_root not in evidence_dir.parents or ROOT in evidence_dir.parents or evidence_dir.exists():
        raise PreflightBlocked('NEW_EXTERNAL_EVIDENCE_DIRECTORY_REQUIRED')
    evidence_dir.mkdir(parents=True)
    OBS = Observation(evidence_dir)
    stage('PREFLIGHT')
    if (ROOT / '.env').exists():
        raise RuntimeError('Repository .env present; explicit no-secret-load gate blocked')
    free = shutil.disk_usage(user_root).free
    OBS.emit('space', freeBytes=free)
    if free < 3 * 1024**3:
        raise PreflightBlocked('INSUFFICIENT_DISK_SPACE_MINIMUM_3_GIB')
    command(['docker','version','--format','{{.Server.Version}}'], timeout=30)
    for filters in [('name='+NAME,),('label=com.nightclub-ai.purpose='+PURPOSE,)]:
        assert not command(['docker','ps','-a','--filter',filters[0],'--format','{{.ID}}']).strip()
    with socket.socket() as probe:
        probe.bind(('127.0.0.1',55432))
    stage('IMAGE_VERIFY')
    assert image == IMAGE, 'Only the approved fixed image is permitted'
    manifest = json.loads(command(['docker','manifest','inspect','--verbose',IMAGE]))
    assert manifest['Descriptor']['digest'] == DIGEST
    assert manifest['Descriptor']['platform'] == {'architecture':'amd64','os':'linux'}
    image = command(['docker','image','inspect',IMAGE], check=False)
    if image.returncode:
        command(['docker','pull','--platform','linux/amd64',IMAGE],timeout=600)
    image_data = json.loads(command(['docker','image','inspect',IMAGE]))[0]
    assert image_data['Architecture'] == 'amd64' and image_data['Os'] == 'linux'
    assert any(ref.endswith('@'+DIGEST) for ref in image_data['RepoDigests'])
    run_id = str(uuid4())
    assert not command(['docker','ps','-a','--filter','label=com.nightclub-ai.run-id='+run_id,'--format','{{.ID}}']).strip()
    admin_password = secrets.token_urlsafe(32)
    runtime_password = secrets.token_urlsafe(32)
    migration_password = secrets.token_urlsafe(32)
    OBS.secrets.extend((admin_password, runtime_password, migration_password))
    docker_env = clean_environment()
    docker_env['POSTGRES_PASSWORD'] = admin_password
    stage('CONTAINER_CREATE')
    container_id = command(['docker','create','--platform','linux/amd64','--name',NAME,
        '--label','com.nightclub-ai.purpose='+PURPOSE,
        '--label','com.nightclub-ai.run-id='+run_id,
        '--publish','127.0.0.1:55432:5432','--tmpfs','/var/lib/postgresql/data:rw',
        '--env','POSTGRES_PASSWORD',IMAGE],env=docker_env).strip()
    assert len(container_id) == 64 and all(c in '0123456789abcdef' for c in container_id)
    result = {'runId':run_id,'containerId':container_id,'image':IMAGE,'platform':'linux/amd64',
              'postgresql':'PENDING','cleanup':False,'casesExit':None,'head':HEAD}
    OBS.emit('container_created', containerId=container_id, runId=run_id)
    urls = None
    try:
        inspect_owned(container_id,run_id,image_data['Id'])
        stage('CONTAINER_START')
        command(['docker','start',container_id])
        stage('POSTGRES_READINESS')
        for attempt in range(60):
            ready = command(['docker','exec',container_id,'pg_isready','-U','postgres'],
                check=False, timeout=30)
            OBS.emit('readiness', attempt=attempt+1, exitCode=ready.returncode,
                     stdout=ready.stdout, stderr=ready.stderr)
            if ready.returncode == 0:
                break
            time.sleep(1)
        else:
            raise RuntimeError('Container readiness timed out')
        urls = {role:URL.create('postgresql+psycopg',username=role,password=password,
            host='127.0.0.1',port=55432,database=DATABASE) for role,password in
            [('postgres',admin_password),('nightclub_api',runtime_password),
             ('alembic_test_user',migration_password)]}
        stage('DATABASE_BOOTSTRAP')
        with psycopg.connect(host='127.0.0.1',port=55432,user='postgres',password=admin_password,
                             dbname='postgres',autocommit=True) as control:
            # Only synthetic logins inside this new container; no ALTER ROLE.
            from psycopg import sql
            stage('ROLE_PROVISIONING')
            for role,password,attributes in [
                ('alembic_test_user',migration_password,'LOGIN INHERIT CREATEDB NOSUPERUSER NOCREATEROLE NOREPLICATION NOBYPASSRLS'),
                ('nightclub_api',runtime_password,'LOGIN NOINHERIT NOCREATEDB NOSUPERUSER NOCREATEROLE NOREPLICATION NOBYPASSRLS'),
                ('nightclub_scheduler',secrets.token_urlsafe(32),'LOGIN NOINHERIT NOCREATEDB NOSUPERUSER NOCREATEROLE NOREPLICATION NOBYPASSRLS')]:
                OBS.secrets.append(password)
                control.execute(sql.SQL('CREATE ROLE {} '+attributes+' PASSWORD {}').format(
                    sql.Identifier(role),sql.Literal(password)))
            stage('DATABASE_BOOTSTRAP')
            control.execute('CREATE DATABASE nightclub_ai_oauth_recert_test OWNER alembic_test_user')
        migration_url = urls['alembic_test_user']
        stage('AUTH_STUB')
        with psycopg.connect(host='127.0.0.1',port=55432,user='alembic_test_user',
                             password=migration_password,dbname=DATABASE) as connection:
            connection.execute((ROOT/'scripts/local-dev/auth_stub.sql').read_text(encoding='utf-8'))
        child = clean_environment()
        child['DATABASE_MIGRATION_URL'] = migration_url.render_as_string(hide_password=False)
        child['OAUTH_TEST_RUNTIME_URL'] = urls['nightclub_api'].render_as_string(hide_password=False)
        child['OAUTH_TEST_INSPECTOR_URL'] = urls['postgres'].render_as_string(hide_password=False)
        child['DATABASE_RUNTIME_EXPECTED_ROLE'] = 'nightclub_api'
        evidence_output = []
        stage('ALEMBIC_UPGRADE')
        for target in ['20260907_0002','20260909_0003','20260910_0004','20260910_0005',
                       '20260922_0006','20260923_0007','20260924_0008','20260926_0009']:
            evidence_output.append(command([sys.executable,'-m','alembic','upgrade',target],env=child,disclose=True))
        expected = migration()
        migration_engine = create_engine(migration_url,hide_parameters=True)
        try:
            with migration_engine.connect() as connection:
                historical, runtime_acl, scheduler_acl, functions, _ = expected.verify_baseline(connection)
            evidence_output.append(command([sys.executable,'-m','alembic','upgrade',HEAD],env=child,disclose=True))
            with migration_engine.connect() as connection:
                expected.verify_policies(connection,historical)
                expected.BASE.BASE.verify_grants(connection)
                expected.BASE.verify_scheduler_grants(connection,function_snapshot=functions)
                assert expected.role_acl_snapshot(connection,'nightclub_api') == runtime_acl
                assert expected.role_acl_snapshot(connection,'nightclub_scheduler') == scheduler_acl
        finally:
            migration_engine.dispose()
        print('MIGRATION_CATALOG_AGAINST_SOURCE: PASS',flush=True)
        initial_catalog = catalog_snapshot(urls['postgres'])
        result['initialCatalogSha256'] = record_catalog(evidence_dir,'catalog-initial.json',initial_catalog)
        stage('POSTGRES_TESTS')
        test = command([sys.executable,'-m','pytest','backend/tests/test_facebook_oauth_postgres.py',
            '--facebook-oauth-postgres','-v','-ra','--tb=short'],env=child,
            check=False,timeout=240)
        output = OBS.sanitize(test.stdout + test.stderr)
        print(output,end='',flush=True)
        print('OAUTH_POSTGRES_EXIT:',test.returncode,flush=True)
        result['casesExit'] = test.returncode
        result['postgresql'] = 'PASS' if test.returncode == 0 else 'FAIL'
        (evidence_dir/'postgresql-tests.txt').write_text(output,encoding='utf-8')
        # These stages execute inside the unchanged opt-in test. Record only
        # what its successful assertions prove, never infer success on failure.
        for name in ('TRIGGER_INSTALL', 'TRIGGER_REMOVE'):
            OBS.states[name] = 'PROVEN_BY_PERSISTENCE_TEST' if test.returncode == 0 else 'UNCONFIRMED_TEST_FAILED'
        OBS.emit('trigger_test_evidence', test='test_real_http_persistence_failure_atomic_rollback', exitCode=test.returncode)
        stage('CATALOG_COMPARE')
        final_catalog = catalog_snapshot(urls['postgres'])
        result['finalCatalogSha256'] = record_catalog(evidence_dir,'catalog-final.json',final_catalog)
        result['catalogEqual'] = final_catalog == initial_catalog
        assert result['catalogEqual'], 'Catalog changed during OAuth checkpoint'
        # Runtime-independent observation, always a fresh READ ONLY transaction.
        with psycopg.connect(host='127.0.0.1',port=55432,user='postgres',password=admin_password,
                             dbname=DATABASE,autocommit=True) as inspector:
            inspector.execute('BEGIN TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY')
            try:
                assert inspector.execute('SELECT version_num FROM alembic_version').fetchone() == (HEAD,)
                assert inspector.execute("SELECT count(*) FROM pg_policies WHERE schemaname='public'").fetchone() == (58,)
                assert inspector.execute("SELECT NOT EXISTS(SELECT 1 FROM pg_trigger WHERE tgname='oauth_recert_fail_connection_audit'), to_regprocedure('public.oauth_recert_fail_connection_audit_fn()') IS NULL").fetchone() == (True,True)
                result['serverVersion'] = inspector.execute('SHOW server_version').fetchone()[0]
                result['policies'] = 58
                result['faultResiduesAbsent'] = True
            finally:
                inspector.execute('ROLLBACK')
        if test.returncode:
            raise RuntimeError('OAuth application test failure; no implementation repair authorized')
    except Exception as error:
        OBS.fail(error)
        capture_failure(container_id, run_id)
        if urls is not None:
            try:
                remove_fault_if_present(urls['postgres'])
            except Exception as cleanup_error:
                OBS.emit('trigger_cleanup_error', exceptionClass=type(cleanup_error).__name__, message=str(cleanup_error))
        raise
    finally:
        try:
            # Cleanup must still remove our captured ID if its configuration gate
            # failed. Never remove another run: ID, name and BOTH labels required.
            cleanup_owned(container_id,run_id)
            result['cleanup'] = True
            print('OWNED_CONTAINER_REMOVED: PASS',flush=True)
        finally:
            result['failure'] = OBS.failure
            OBS.save()
            (evidence_dir/'postgresql-result.json').write_text(json.dumps(result,indent=2)+'\n',encoding='utf-8')


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--evidence-dir',type=Path,default=Path(os.environ['USERPROFILE']) /
        'NightclubAI-Evidence/oauth-postgresql-checkpoint-20261005/observability-retry-1/postgresql')
    parser.add_argument('--image',default=IMAGE)
    args = parser.parse_args()
    try:
        run(args.evidence_dir, args.image)
    except Exception as error:
        if OBS is not None and OBS.failure is None:
            OBS.fail(error)
        print('OAUTH_CHECKPOINT_FAIL_OR_BLOCKED:',
              error.code if isinstance(error,PreflightBlocked) else type(error).__name__,flush=True)
        sys.exit(1)
