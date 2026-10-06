"""Separate current-source evidence gate. Never replaces or weakens historical B7."""
from pathlib import Path
import argparse
import difflib
import hashlib
import json
import os
import subprocess
import sys
import tempfile
from uuid import uuid4

ROOT = Path(__file__).resolve().parents[2]
CHECKPOINT = '1a1684af14c42b81b23feb67d0d5f413a5c33ad1'
DELIVERY = (
    'backend/tests/conftest.py',
    'backend/tests/facebook_oauth_postgres_safety.py',
    'backend/tests/test_facebook_oauth_postgres.py',
    'scripts/local-dev/validate_facebook_oauth_postgres.py',
    'scripts/local-dev/validate_facebook_oauth_recertification.py',
    'docs/facebook-oauth-reviewed-source-manifest.json',
    'docs/runbooks/facebook-oauth-postgres-recertification.md',
    'docs/FACEBOOK_OAUTH_POSTGRESQL_RECERTIFICATION.md',
)


def validate_sources(root=ROOT):
    manifest = json.loads((root/'docs/facebook-oauth-reviewed-source-manifest.json').read_text(encoding='utf-8'))
    assert manifest['checkpoint'] == CHECKPOINT and len(manifest['files']) == 16
    assert len({row['path'] for row in manifest['files']}) == 16
    for row in manifest['files']:
        path = root / row['path']
        assert path.resolve().is_relative_to(root.resolve())
        current = path.read_bytes()
        assert hashlib.sha256(current).hexdigest() == row['sha256'], row['path']
        assert hashlib.sha256(current.replace(b'\r\n',b'\n')).hexdigest() == row['sha256LF'], row['path']
    # Preserve certified automation, migrations and policies. The two composition
    # inputs config/main are covered by the explicit current OAuth manifest.
    protected = subprocess.run(['git','ls-tree','-r','--name-only',CHECKPOINT,
        'backend/app/modules/automation','backend/app/api/internal',
        'backend/app/core/database.py','backend/app/core/database_security.py',
        'backend/app/core/scheduler_database.py','backend/app/core/migration_config.py',
        'backend/app/modules/identity','backend/migrations','sql','n8n','requirements.txt'],
        cwd=root,capture_output=True,check=True).stdout.decode().splitlines()
    for path in protected:
        old = subprocess.run(['git','cat-file','blob',CHECKPOINT+':'+path],
            cwd=root,capture_output=True,check=True).stdout
        current = (root/path).read_bytes()
        if path.startswith('n8n/'):
            assert current == old, path
        else:
            assert current.replace(b'\r\n',b'\n') == old.replace(b'\r\n',b'\n'), path
    return len(protected)


def validate_evidence(path):
    report = json.loads(path.read_text(encoding='utf-8'))
    assert report['postgresql'] == 'PASS' and report['casesExit'] == 0
    assert report['cleanup'] is True and report['faultResiduesAbsent'] is True
    assert report['head'] == '20260928_0010' and report['policies'] == 58
    assert report['platform'] == 'linux/amd64'
    assert report['catalogEqual'] is True
    snapshots = []
    for label in ('initial','final'):
        data = (path.parent/('catalog-'+label+'.json')).read_bytes()
        assert hashlib.sha256(data).hexdigest() == report[label+'CatalogSha256']
        snapshots.append(json.loads(data))
    assert snapshots[0] == snapshots[1]
    assert len(snapshots[0]['policies']) == 58


def record_delivery(evidence_dir):
    """Export only authorized source/docs and hashes; never environments/log dumps."""
    evidence_dir = evidence_dir.resolve()
    user_root = Path(os.environ['USERPROFILE']).resolve()
    if user_root not in evidence_dir.parents or ROOT in evidence_dir.parents or evidence_dir.exists():
        raise ValueError('New external evidence directory required')
    evidence_dir.mkdir(parents=True)
    hashes, diff = [], []
    for name in DELIVERY:
        data = (ROOT/name).read_bytes()
        old = subprocess.run(['git','cat-file','blob',CHECKPOINT+':'+name],
            cwd=ROOT,capture_output=True)
        previous = old.stdout if old.returncode == 0 else b''
        hashes.append({'path':name,'sha256':hashlib.sha256(data).hexdigest(),
            'sha256LF':hashlib.sha256(data.replace(b'\r\n',b'\n')).hexdigest(),
            'beforeGitBlobSha256':hashlib.sha256(previous).hexdigest() if previous else None,
            'beforeGitBlobSha256LF':hashlib.sha256(previous.replace(b'\r\n',b'\n')).hexdigest() if previous else None})
        diff.extend(difflib.unified_diff(previous.decode('utf-8').splitlines(True),
            data.decode('utf-8').splitlines(True),fromfile='a/'+name if previous else '/dev/null',
            tofile='b/'+name))
    (evidence_dir/'checkpoint.diff').write_text(''.join(diff),encoding='utf-8')
    (evidence_dir/'checkpoint-hashes.json').write_text(json.dumps(hashes,indent=2)+'\n',encoding='utf-8')
    for name in ('docs/FACEBOOK_OAUTH_POSTGRESQL_RECERTIFICATION.md',
                 'docs/facebook-oauth-reviewed-source-manifest.json'):
        (evidence_dir/Path(name).name).write_bytes((ROOT/name).read_bytes())
    print('SANITIZED_DELIVERY_EVIDENCE:',evidence_dir)


def run_local_checks(evidence_dir):
    """Record raw offline outputs separately; historical failures stay visible."""
    evidence_dir = evidence_dir.resolve()
    user_root = Path(os.environ['USERPROFILE']).resolve()
    if user_root not in evidence_dir.parents or ROOT in evidence_dir.parents or evidence_dir.exists():
        raise ValueError('New external evidence directory required')
    evidence_dir.mkdir(parents=True)
    allowed = {'PATH','SYSTEMROOT','WINDIR','COMSPEC','TEMP','TMP','USERPROFILE',
               'LOCALAPPDATA','APPDATA','PROGRAMFILES','PROGRAMFILES(X86)','PATHEXT'}
    environment = {key:value for key,value in os.environ.items() if key.upper() in allowed}
    environment['PYTHONIOENCODING'] = 'utf-8'
    environment['PYTHONDONTWRITEBYTECODE'] = '1'
    temp_root = Path(tempfile.gettempdir())/('nightclub-oauth-full-'+uuid4().hex)
    temp_root.mkdir()
    suites = {
        'backend': ['backend/tests'],
        'automation': [
            'backend/tests/test_automation_config.py',
            'backend/tests/test_automation_internal_auth.py',
            'backend/tests/test_scheduler_database.py',
            'backend/tests/test_scheduler_security_migration.py',
            'backend/tests/test_system_automation_security_migration.py',
            'backend/tests/test_publish_due_discovery.py',
            'backend/tests/test_publish_due_coordinator.py',
            'backend/tests/test_publish_due_api.py',
            'backend/tests/test_prompt11_n8n_workflow_contract.py'],
    }
    results = {}
    # Only suites whose explicit files exist; never silently drop a requested file.
    for name,paths in suites.items():
        if any(not (ROOT/path).exists() for path in paths):
            results[name] = {'exit':None,'blocked':'Expected suite path missing'}
            print(name.upper()+': BLOCKED (suite path missing)')
            continue
        result = subprocess.run([sys.executable,'-m','pytest',*paths,'-q','-ra',
            '--tb=short','-p','no:cacheprovider','--basetemp',str(temp_root/name)],
            cwd=ROOT,env=environment,capture_output=True,text=True,encoding='utf-8',
            errors='replace',timeout=600)
        output = result.stdout+result.stderr
        (evidence_dir/(name+'-pytest.txt')).write_text(output,encoding='utf-8')
        results[name] = {'exit':result.returncode,'outputSha256':hashlib.sha256(output.encode()).hexdigest()}
        print(output,end='')
        print(name.upper()+'_EXIT:',result.returncode)
    (evidence_dir/'offline-results.json').write_text(json.dumps(results,indent=2)+'\n',encoding='utf-8')
    return results


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--postgres-evidence',type=Path)
    parser.add_argument('--run-local-checks',type=Path,metavar='NEW_EXTERNAL_EVIDENCE_DIR')
    parser.add_argument('--record-delivery',type=Path,metavar='NEW_EXTERNAL_EVIDENCE_DIR')
    arguments = parser.parse_args()
    try:
        count = validate_sources()
        print('OAUTH_SOURCES: PASS (16); PRESERVED_AUTOMATION_INPUTS:',count)
        if arguments.record_delivery:
            record_delivery(arguments.record_delivery)
        if arguments.run_local_checks:
            results = run_local_checks(arguments.run_local_checks)
            if any(row['exit'] != 0 for row in results.values()):
                print('FULL_SUITE_NOT_APPROVED; HISTORICAL_REJECTIONS_REMAIN_VISIBLE')
                sys.exit(1)
        if arguments.postgres_evidence:
            validate_evidence(arguments.postgres_evidence)
            print('POSTGRESQL_EVIDENCE: PASS; HUMAN_REVIEW_REQUIRED')
        else:
            print('POSTGRESQL_EVIDENCE: PENDING; OAUTH_NOT_CERTIFIED')
            sys.exit(2)
    except Exception:
        print('OAUTH_RECERTIFICATION: FAIL; no secret values reported')
        sys.exit(1)
