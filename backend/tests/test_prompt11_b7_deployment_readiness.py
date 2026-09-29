"""Offline B7 checks and negative regressions; no environment/DB/network required."""

import importlib.util
from pathlib import Path
import subprocess
import sys

import pytest


ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "scripts/local-dev/validate_prompt11_b7_deployment_readiness.py"
spec = importlib.util.spec_from_file_location("b7_readiness", SCRIPT)
b7 = importlib.util.module_from_spec(spec)
spec.loader.exec_module(b7)


def test_current_repository_contract():
    b7.validate_repository()


def test_direct_cli_is_offline_and_reports_scope():
    result = subprocess.run([sys.executable, str(SCRIPT)], cwd=ROOT,
                            capture_output=True, text=True, check=False, timeout=45)
    assert result.returncode == 0
    assert "B7_OFFLINE_READINESS: PASS" in result.stdout
    assert "PRODUCTION_STATE: NOT_VERIFIED" in result.stdout
    assert result.stderr == ""


@pytest.mark.parametrize("instruction", [
    "Execute sql/003_rls.sql",
    "- Apply `sql/003_rls.sql` before deployment.",
    "1. Run sql/003_rls.sql",
    "psql -f sql/003_rls.sql",
    r"\i sql/003_rls.sql",
    "Use DATABASE_URL as an Alembic fallback.",
    "- Use `DATABASE_URL` for migration execution.",
    "Fall back to DATABASE_URL for Alembic.",
    "$env:DATABASE_MIGRATION_URL = $env:DATABASE_URL",
])
def test_unsafe_deployment_instruction_is_rejected(instruction):
    with pytest.raises(b7.ReadinessError):
        b7.validate_document("\n".join(b7.RULES) + "\n" + instruction)


@pytest.mark.parametrize("text", [
    "The migration 0001 instruction is SUPERSEDED; do not execute historical SQL.",
    "Do NOT execute sql/003_rls.sql.",
    "DATABASE_URL is runtime-only, never the Alembic fallback.",
])
def test_explicit_supersession_is_not_a_false_positive(text):
    b7.validate_document("\n".join(b7.RULES) + "\n" + text)


@pytest.mark.parametrize("rule", b7.RULES)
def test_authoritative_rules_cannot_be_removed(rule):
    with pytest.raises(b7.ReadinessError, match="AUTHORITATIVE_DEPLOYMENT_RULE_MISSING"):
        b7.validate_document("\n".join(item for item in b7.RULES if item != rule))


@pytest.mark.parametrize("source", [
    'query = "SELECT * FROM public.automation_runs"',
    'query = "INSERT INTO public.outbox_events VALUES (...)"',
    "from anywhere import AutomationRun",
    "session.add(OutboxEvent())",
])
def test_excluded_active_surfaces_are_rejected(source):
    with pytest.raises(b7.ReadinessError, match="EXCLUDED_AUTOMATION_SURFACE"):
        b7.validate_active_source(source)


def test_comments_and_docstrings_are_not_executable_surfaces():
    b7.validate_active_source('"""No automation_runs or outbox_events."""\n# OutboxEvent\npass')


@pytest.fixture
def evidence_tree(tmp_path, monkeypatch):
    frozen = b7.git_read(ROOT, "ls-tree", "-r", "--name-only", b7.BASELINE,
                        "backend/migrations/versions", "sql")
    paths = set(b7.PRESERVED) | set(frozen.decode().splitlines()) | {
        b7.ADR, b7.RUNBOOK, b7.REPORT, "README.md",
    }
    snapshots = {path: b7.baseline_blob(ROOT, path)
                 for path in paths if path not in {b7.RUNBOOK, b7.REPORT}}
    for path in paths:
        destination = tmp_path / path
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes((ROOT / path).read_bytes())
    monkeypatch.setattr(b7, "baseline_blob", lambda root, path: snapshots[path])
    monkeypatch.setattr(b7, "git_read", lambda root, *args: frozen)
    return tmp_path


@pytest.mark.parametrize(("path", "change"), [
    (b7.WORKFLOW, "activation"),
    (b7.B6_REPORT, "delete"),
    (b7.RUNBOOK, "rule"),
    (b7.REPORT, "baseline"),
    (b7.ADR, "history"),
    ("backend/app/core/migration_config.py", "source"),
    ("backend/migrations/versions/20260928_0010_system_automation_execution_access.py", "source"),
])
def test_readiness_rejects_missing_or_changed_evidence(evidence_tree, path, change):
    destination = evidence_tree / path
    if change == "delete":
        destination.unlink()
    else:
        text = destination.read_text(encoding="utf-8")
        if change == "activation":
            text = text.replace('"active": false', '"active": true')
        elif change == "rule":
            text = text.replace(b7.RULES[0], "Execute sql/003_rls.sql")
        elif change == "baseline":
            text = text.replace(b7.BASELINE, "unknown")
        elif change == "history":
            text = "Altered historical context\n" + text
        else:
            text += "\n# unreviewed source change\n"
        destination.write_text(text, encoding="utf-8")
    with pytest.raises((b7.ReadinessError, OSError)):
        b7.validate_repository(evidence_tree)


def test_cli_failure_never_echoes_input_or_exception(monkeypatch, capsys):
    def fail():
        raise OSError("synthetic-sensitive-value")
    monkeypatch.setattr(b7, "validate_repository", fail)
    assert b7.main() == 1
    captured = capsys.readouterr()
    assert "synthetic-sensitive-value" not in captured.out + captured.err
    assert "UNREADABLE_OR_INVALID_LOCAL_EVIDENCE" in captured.out


def test_known_crlf_materialization_is_not_content_drift(evidence_tree):
    path = evidence_tree / "backend/migrations/versions/20260909_0003_identity_rls.py"
    path.write_bytes(path.read_bytes().replace(b"\r\n", b"\n").replace(b"\n", b"\r\n"))
    b7.validate_repository(evidence_tree)


def test_hash_guarded_migration_still_requires_exact_bytes(evidence_tree):
    path = evidence_tree / "backend/migrations/versions/20260926_0009_scheduler_discovery_access.py"
    path.write_bytes(path.read_bytes().replace(b"\r\n", b"\n").replace(b"\n", b"\r\n"))
    with pytest.raises(b7.ReadinessError, match="FROZEN_MIGRATION_HASH_MISMATCH"):
        b7.validate_repository(evidence_tree)
