"""Offline B7.1 recertification and negative regressions."""

import importlib.util
import json
from pathlib import Path
import subprocess
import sys

import pytest


ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "scripts/local-dev/validate_prompt11_b7_1_recertification.py"
spec = importlib.util.spec_from_file_location("b7_1_recertification", SCRIPT)
b7_1 = importlib.util.module_from_spec(spec)
spec.loader.exec_module(b7_1)
b7 = b7_1.b7


def test_current_repository_passes_explicit_b7_1_recertification():
    b7_1.validate_repository()


def test_cli_reports_new_and_historical_certification_boundaries():
    result = subprocess.run([sys.executable, str(SCRIPT)], cwd=ROOT,
                            capture_output=True, text=True, check=False, timeout=45)
    assert result.returncode == 0
    assert "B7_1_RECERTIFICATION: PASS" in result.stdout
    assert f"REVIEWED_BASELINE: {b7_1.REVIEWED_BASELINE}" in result.stdout
    assert "HISTORICAL_B7: PRESERVED_AND_SUPERSEDED" in result.stdout
    assert "PRODUCTION_STATE: NOT_VERIFIED; ACTIVATION: NOT_AUTHORIZED" in result.stdout
    assert result.stderr == ""


def test_historical_b7_still_rejects_the_b7_1_source():
    with pytest.raises(b7.ReadinessError, match="CERTIFIED_SOURCE_DRIFT"):
        b7.validate_repository()


@pytest.fixture
def evidence_tree(tmp_path, monkeypatch):
    frozen_listing = b7.git_read(ROOT, "ls-tree", "-r", "--name-only", b7.BASELINE,
                                 "backend/migrations/versions", "sql")
    frozen_paths = frozen_listing.decode().splitlines()
    reviewed = {path: b7_1.reviewed_blob(ROOT, path) for path in b7_1.REVIEWED}
    historical_paths = set(b7.PRESERVED) | set(frozen_paths) | {b7.ADR}
    historical = {path: b7.baseline_blob(ROOT, path) for path in historical_paths}

    for path, content in reviewed.items():
        destination = tmp_path / path
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(content)
    for path in frozen_paths:
        destination = tmp_path / path
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(historical[path])
    for path in (b7_1.REPORT, b7_1.RUNBOOK):
        destination = tmp_path / path
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes((ROOT / path).read_bytes())

    monkeypatch.setattr(b7_1, "reviewed_blob", lambda root, path: reviewed[path])
    monkeypatch.setattr(b7, "baseline_blob", lambda root, path: historical[path])
    monkeypatch.setattr(b7, "git_read", lambda root, *args: frozen_listing)
    return tmp_path


@pytest.mark.parametrize("path", [
    "backend/app/modules/automation/publish_due_dependencies.py",
    "backend/app/modules/integrations/credentials.py",
    b7_1.HISTORICAL_VALIDATOR,
])
def test_unreviewed_source_or_historical_gate_change_is_rejected(evidence_tree, path):
    target = evidence_tree / path
    target.write_bytes(target.read_bytes() + b"\n# unreviewed change\n")
    with pytest.raises(b7.ReadinessError, match="CERTIFIED_SOURCE_DRIFT"):
        b7_1.validate_repository(evidence_tree)


def test_workflow_activation_is_rejected(evidence_tree):
    target = evidence_tree / b7.WORKFLOW
    workflow = json.loads(target.read_text(encoding="utf-8"))
    workflow["active"] = True
    target.write_text(json.dumps(workflow), encoding="utf-8")
    with pytest.raises(b7.ReadinessError, match="PORTABLE_WORKFLOW_BYTES_CHANGED"):
        b7_1.validate_repository(evidence_tree)


def test_historical_migration_content_change_is_rejected(evidence_tree):
    target = evidence_tree / "backend/migrations/versions/20260907_0001_initial_schema.py"
    target.write_bytes(target.read_bytes() + b"\n# unreviewed migration change\n")
    with pytest.raises(b7.ReadinessError, match="FROZEN_CONTENT_CHANGED"):
        b7_1.validate_repository(evidence_tree)


def test_byte_exact_migration_guard_rejects_eol_rewrite(evidence_tree):
    target = evidence_tree / "backend/migrations/versions/20260926_0009_scheduler_discovery_access.py"
    target.write_bytes(target.read_bytes().replace(b"\r\n", b"\n").replace(b"\n", b"\r\n"))
    with pytest.raises(b7.ReadinessError, match="FROZEN_MIGRATION_HASH_MISMATCH"):
        b7_1.validate_repository(evidence_tree)


@pytest.mark.parametrize("path", [b7_1.REPORT, b7_1.RUNBOOK])
def test_b7_1_evidence_cannot_hide_the_historical_boundary(evidence_tree, path):
    target = evidence_tree / path
    text = target.read_text(encoding="utf-8")
    target.write_text(text.replace("HISTORICAL_B7: PRESERVED_AND_SUPERSEDED", ""),
                      encoding="utf-8")
    with pytest.raises(b7.ReadinessError, match="B7_1_CERTIFICATION_EVIDENCE_MISSING"):
        b7_1.validate_repository(evidence_tree)


@pytest.mark.parametrize("path", [b7_1.REPORT, b7_1.RUNBOOK])
def test_b7_1_authoritative_rules_cannot_be_removed(evidence_tree, path):
    target = evidence_tree / path
    text = target.read_text(encoding="utf-8")
    target.write_text(text.replace(b7.RULES[0], ""), encoding="utf-8")
    with pytest.raises(b7.ReadinessError, match="AUTHORITATIVE_DEPLOYMENT_RULE_MISSING"):
        b7_1.validate_repository(evidence_tree)
