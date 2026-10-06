"""Offline harness diagnostics only: no Docker or database is executed."""
import ast
import importlib.util
import json
from pathlib import Path
import subprocess

import pytest


@pytest.fixture
def harness(tmp_path):
    path = Path(__file__).parents[2]/'scripts/local-dev/validate_facebook_oauth_postgres.py'
    spec = importlib.util.spec_from_file_location('oauth_observation_test', path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    module.OBS = module.Observation(tmp_path)
    return module


def test_original_stage_exception_exit_and_output_preserved(harness, monkeypatch):
    harness.stage('CONTAINER_START')
    monkeypatch.setattr(harness.subprocess, 'run', lambda *a, **k:
        subprocess.CompletedProcess(a[0], 17, 'safe stdout', 'technical start failure'))
    with pytest.raises(RuntimeError) as error:
        harness.command(['docker', 'start', 'a'*64])
    assert isinstance(error.value.__cause__, subprocess.CalledProcessError)
    harness.OBS.fail(error.value)
    failure = harness.OBS.failure
    assert failure == {'FAILED_STAGE': 'CONTAINER_START',
        'exceptionClass': 'CalledProcessError',
        'message': "Command '['docker', 'start', '"+'a'*64+"']' returned non-zero exit status 17.",
        'exitCode': 17}
    event = next(event for event in harness.OBS.events if event['kind'] == 'command')
    assert event['stdout'] == 'safe stdout' and event['stderr'] == 'technical start failure'


@pytest.mark.parametrize('channel', ['message', 'stdout', 'stderr', 'command'])
def test_secret_redaction_on_every_output_channel(harness, monkeypatch, channel):
    secret = 'synthetic-opaque-value-12345'
    harness.OBS.secrets.append(secret)
    payload = (secret+' password="synthetic-password" access_token=synthetic-token '
               'https://user:synthetic-password@localhost/db '
               'postgresql+psycopg://user:synthetic-password@localhost/db '
               'Authorization: Bearer synthetic-bearer code=synthetic-code '
               'encryption_key=synthetic-encryption '
               'eyJsynthetic.header.signature -----BEGIN PRIVATE KEY-----\n'
               'synthetic-private-key\n-----END PRIVATE KEY-----')
    monkeypatch.setattr(harness.subprocess, 'run', lambda *a, **k:
        subprocess.CompletedProcess(a[0], 1, payload, payload))
    with pytest.raises(RuntimeError) as caught:
        harness.command(['docker', '--password', secret, payload])
    harness.OBS.fail(ValueError(payload) if channel == 'message' else caught.value)
    persisted = (harness.OBS.directory/'observability.json').read_text(encoding='utf-8')
    for value in (secret, 'synthetic-password', 'synthetic-token', 'synthetic-bearer',
                  'synthetic-code', 'synthetic-encryption', 'eyJsynthetic', 'synthetic-private-key'):
        assert value not in persisted
    assert '[REDACTED]' in persisted


def identity(harness, container_id='a'*64, run_id='run-local'):
    return {'Id': container_id, 'Name': '/'+harness.NAME, 'Image': 'sha256:synthetic',
            'State': {'Running': False, 'Error': 'safe start error'}, 'Platform': 'linux',
            'Config': {'Env': ['NEVER_PERSIST_THIS_ENV=synthetic'], 'Labels': {
                'com.nightclub-ai.purpose': harness.PURPOSE, 'com.nightclub-ai.run-id': run_id}},
            'HostConfig': {'PortBindings': {}, 'Tmpfs': {}}}


def test_inspect_and_logs_are_captured_before_id_only_cleanup(harness, monkeypatch):
    calls = []
    def fake_run(args, **kwargs):
        calls.append(args)
        if args[:2] == ['docker', 'inspect']:
            output = json.dumps([identity(harness)])
        elif args[:2] == ['docker', 'logs']:
            output = 'safe container log'
        else:
            output = ''
        return subprocess.CompletedProcess(args, 0, output, '')
    monkeypatch.setattr(harness.subprocess, 'run', fake_run)
    harness.capture_failure('a'*64, 'run-local')
    harness.cleanup_owned('a'*64, 'run-local')
    assert calls.index(['docker', 'logs', 'a'*64]) < calls.index(['docker','rm','--force','a'*64])
    assert [args for args in calls if args[:2] == ['docker','rm']] == [['docker','rm','--force','a'*64]]
    assert 'NEVER_PERSIST_THIS_ENV' not in (harness.OBS.directory/'observability.json').read_text()
    assert {event['kind'] for event in harness.OBS.events} >= {'failure_inspect','failure_logs','cleanup'}


def test_cleanup_refuses_wrong_identity_without_removing_anything(harness, monkeypatch):
    calls = []
    def fake_run(args, **kwargs):
        calls.append(args)
        return subprocess.CompletedProcess(args, 0, json.dumps([identity(harness, run_id='other-run')]), '')
    monkeypatch.setattr(harness.subprocess, 'run', fake_run)
    with pytest.raises(AssertionError):
        harness.cleanup_owned('a'*64, 'run-local')
    assert all(args[:2] != ['docker','rm'] for args in calls)


def test_timeout_preserves_original_class_and_sanitized_details(harness, monkeypatch):
    def timeout(*args, **kwargs):
        raise subprocess.TimeoutExpired(args[0], 30, output=b'password=synthetic-password', stderr=b'safe timeout')
    monkeypatch.setattr(harness.subprocess, 'run', timeout)
    harness.stage('POSTGRES_READINESS')
    with pytest.raises(subprocess.TimeoutExpired) as caught:
        harness.command(['docker','exec','a'*64,'pg_isready'])
    harness.OBS.fail(caught.value)
    assert harness.OBS.failure['exceptionClass'] == 'TimeoutExpired'
    assert harness.OBS.failure['FAILED_STAGE'] == 'POSTGRES_READINESS'
    assert 'synthetic-password' not in (harness.OBS.directory/'observability.json').read_text()


def test_no_second_automatic_run_and_stage_inventory(harness):
    tree = ast.parse(Path(harness.__file__).read_text(encoding='utf-8'))
    main = tree.body[-1]
    calls = [node for node in ast.walk(main) if isinstance(node, ast.Call)
             and isinstance(node.func, ast.Name) and node.func.id == 'run']
    assert len(calls) == 1
    assert not any(isinstance(node, (ast.For, ast.While)) for node in ast.walk(main))
    assert harness.OBS.states['TRIGGER_INSTALL'] == 'NOT_REACHED'
    assert len(harness.STAGES) == 14


def test_first_failure_is_not_overwritten_by_cleanup_error(harness):
    harness.stage('CONTAINER_START')
    harness.OBS.fail(ValueError('safe first failure'))
    harness.stage('CLEANUP')
    harness.OBS.fail(RuntimeError('safe cleanup failure'))
    assert harness.OBS.failure['FAILED_STAGE'] == 'CONTAINER_START'
    assert harness.OBS.failure['message'] == 'safe first failure'
