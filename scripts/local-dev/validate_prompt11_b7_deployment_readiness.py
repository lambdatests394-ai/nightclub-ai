"""Offline B7 artifact checks, not proof of deployed infrastructure readiness.

Reads local Git objects, source text and JSON only. Never loads Settings/.env,
starts the application, imports migrations, connects to a DB or calls a service.
Frozen migration comments are historical text, not deployment instructions.
"""

from __future__ import annotations

import ast
import hashlib
import importlib.util
from pathlib import Path
import re
import subprocess


ROOT = Path(__file__).resolve().parents[2]
BASELINE = "301fe0f0c8bb2e0bff1c7f8a042e1aa2bb794529"
B5_BASELINE = "a0746aaeb102dc594fa614e0c9a2a908e4143356"
B6_REPORT = "docs/PROMPT_11_B6_LIVE_N8N_CERTIFICATION.md"
RUNBOOK = "docs/runbooks/prompt11-b7-production-deployment-readiness.md"
REPORT = "docs/PROMPT_11_B7_DEPLOYMENT_READINESS.md"
ADR = "docs/adr/ADR-004-async-runtime-and-sync-alembic.md"
B5_VALIDATOR = "scripts/local-dev/validate_prompt11_n8n_workflow.py"
WORKFLOW = "n8n/workflows/prompt11-publish-due.json"
RULES = (
    "`sql/003_rls.sql` MUST NOT be executed.",
    "Alembic requires `DATABASE_MIGRATION_URL`; no fallback to `DATABASE_URL`.",
)
ADR_MARKER = "## Aclaración de despliegue aprobada — 2026-09-29 (Prompt 11 B7)"
ACTIVE_SOURCES = (
    "backend/app/api/internal/automation.py",
    "backend/app/modules/automation/internal_auth.py",
    "backend/app/modules/automation/coordinator.py",
    "backend/app/modules/automation/discovery.py",
    "backend/app/modules/automation/publish_due_dependencies.py",
    "backend/app/modules/automation/executor.py",
    "backend/app/modules/automation/repository.py",
    "backend/app/modules/automation/audit.py",
)
PRESERVED = ACTIVE_SOURCES + (
    "backend/app/core/config.py", "backend/app/core/database.py",
    "backend/app/core/scheduler_database.py", "backend/app/core/database_security.py",
    "backend/app/core/migration_config.py", "backend/migrations/env.py",
    "backend/app/main.py", "backend/app/modules/identity/policy.py",
    "backend/app/modules/integrations/credentials.py",
    "backend/app/modules/integrations/facebook_provider.py",
    "requirements.txt", "alembic.ini", WORKFLOW, B5_VALIDATOR, B6_REPORT,
)
EXCLUDED = re.compile(r"\b(?:automation_runs|outbox_events|AutomationRun|OutboxEvent)\b")


class ReadinessError(ValueError):
    """Only fixed safe error codes are reported by the command line."""


def require(value: bool, code: str) -> None:
    if not value:
        raise ReadinessError(code)


def git_read(root: Path, *arguments: str) -> bytes:
    result = subprocess.run(
        ["git", "-C", str(root), *arguments], capture_output=True, check=False, timeout=30,
    )
    require(result.returncode == 0, "LOCAL_CERTIFIED_GIT_EVIDENCE_UNAVAILABLE")
    return result.stdout


def baseline_blob(root: Path, path: str) -> bytes:
    return git_read(root, "cat-file", "blob", f"{BASELINE}:{path}")


def normalized_text(value: bytes) -> str:
    return value.decode("utf-8").replace("\r\n", "\n")


def validate_active_source(text: str) -> None:
    # Exclude docstrings/comments, but examine executable names, imports and SQL
    # strings. Historical model declarations are not an active execution path.
    tree = ast.parse(text)
    for item in ast.walk(tree):
        if isinstance(item, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
            if item.body and isinstance(item.body[0], ast.Expr):
                first = item.body[0].value
                if isinstance(first, ast.Constant) and isinstance(first.value, str):
                    item.body = item.body[1:]
    for item in ast.walk(tree):
        values = []
        if isinstance(item, ast.Name):
            values.append(item.id)
        elif isinstance(item, ast.Attribute):
            values.append(item.attr)
        elif isinstance(item, ast.alias):
            values.append(item.name)
        elif isinstance(item, ast.Constant) and isinstance(item.value, str):
            values.append(item.value)
        require(not any(EXCLUDED.search(value) for value in values), "EXCLUDED_AUTOMATION_SURFACE")


def validate_document(text: str, *, require_rules: bool = True) -> None:
    if require_rules:
        require(all(rule in text for rule in RULES), "AUTHORITATIVE_DEPLOYMENT_RULE_MISSING")
    # Defined unsafe imperative/command grammar, not a universal prose parser.
    plain = text.replace("`", "")
    for line in plain.splitlines():
        require(not re.search(
            r"(?i)(?:\bpsql\b[^\n]*(?:-f\s*|<\s*)|\\i\s+)\S*sql[/\\]003_rls\.sql", line,
        ), "HISTORICAL_SQL_EXECUTION_COMMAND")
        require(not re.search(
            r"(?i)^\s*(?:[-*]|\d+[.)])?\s*(?:run|execute|apply|ejecuta|ejecutar|aplica)\s+"
            r"(?:the\s+)?(?:historical\s+)?(?:script\s+)?sql[/\\]003_rls\.sql", line,
        ), "HISTORICAL_SQL_EXECUTION_INSTRUCTION")
        require(not re.search(
            r"(?i)^\s*(?:[-*]|\d+[.)])?\s*(?:use|usa|usar|fall\s+back\s+to)\s+DATABASE_URL\b"
            r"[^\n]*(?:alembic|migration|fallback|migraci)", line,
        ), "RUNTIME_MIGRATION_FALLBACK_INSTRUCTION")
        require(not re.search(
            r"(?i)DATABASE_MIGRATION_URL\s*=\s*[\"']?\$?(?:env:)?DATABASE_URL\b", line,
        ), "RUNTIME_MIGRATION_CREDENTIAL_COPY")


def validate_repository(root: Path = ROOT) -> None:
    # Pin reviewed executable inputs so an offline PASS cannot bless unrelated
    # application/security edits. Reading Git objects never refreshes the index.
    for path in PRESERVED:
        current = (root / path).read_bytes()
        certified = baseline_blob(root, path)
        if path == WORKFLOW:
            require(current == certified, "PORTABLE_WORKFLOW_BYTES_CHANGED")
        else:
            require(normalized_text(current) == normalized_text(certified), "CERTIFIED_SOURCE_DRIFT")

    frozen = git_read(root, "ls-tree", "-r", "--name-only", BASELINE, "backend/migrations/versions", "sql")
    frozen_paths = frozen.decode().splitlines()
    require(bool(frozen_paths), "FROZEN_INPUTS_MISSING")
    for path in frozen_paths:
        require(normalized_text((root / path).read_bytes())
                == normalized_text(baseline_blob(root, path)), "FROZEN_CONTENT_CHANGED")
        if path.endswith(".py"):
            tree = ast.parse((root / path).read_text(encoding="utf-8"))
            assignments = {item.targets[0].id: item.value for item in tree.body
                           if isinstance(item, ast.Assign) and isinstance(item.targets[0], ast.Name)}
            # Unlike Git EOL/stat noise, the certified migration hash guards
            # require byte identity. Inspect them without importing a migration.
            for name, value in assignments.items():
                if name.startswith("FROZEN_") and name.endswith("_SHA256"):
                    previous = assignments["PREVIOUS_PATH"]
                    require(isinstance(previous, ast.Call) and len(previous.args) == 1,
                            "FROZEN_HASH_GUARD_UNRECOGNIZED")
                    previous_name = ast.literal_eval(previous.args[0])
                    require(Path(previous_name).name == previous_name, "FROZEN_HASH_TARGET_INVALID")
                    digest = hashlib.sha256((root / path).with_name(previous_name).read_bytes()).hexdigest()
                    require(digest == ast.literal_eval(value), "FROZEN_MIGRATION_HASH_MISMATCH")
    current_revisions = {file.name for file in (root / "backend/migrations/versions").glob("*.py")}
    certified_revisions = {Path(path).name for path in frozen_paths if path.endswith(".py")}
    require(current_revisions == certified_revisions, "MIGRATION_SET_CHANGED")

    for path in ACTIVE_SOURCES:
        validate_active_source((root / path).read_text(encoding="utf-8"))

    # Execute only the baseline-verified, stdlib-only B5 artifact validator.
    spec = importlib.util.spec_from_file_location("b7_certified_b5_validator", root / B5_VALIDATOR)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    try:
        module.validate_workflow(module.load_workflow(root / WORKFLOW))
    except Exception:
        raise ReadinessError("B5_WORKFLOW_CONTRACT_FAILED") from None

    b6 = (root / B6_REPORT).read_text(encoding="utf-8")
    require(B5_BASELINE in b6 and "2.40.7" in b6 and "v24.21.0" in b6
            and "Both returned HTTP 200." in b6 and "20260928_0010" in b6,
            "B6_CERTIFICATION_EVIDENCE_MISSING")
    # B6 report belongs to BASELINE; it naturally references its earlier execution
    # baseline, rather than predicting the SHA of its own future commit.
    for path in (RUNBOOK, REPORT):
        text = (root / path).read_text(encoding="utf-8")
        require(BASELINE in text, "B7_CERTIFICATION_BASELINE_MISSING")
        validate_document(text)
    validate_document((root / "README.md").read_text(encoding="utf-8"), require_rules=False)
    adr = (root / ADR).read_text(encoding="utf-8")
    require(adr.count(ADR_MARKER) == 1, "ADR_SUPERSESSION_MISSING")
    history, clarification = adr.split(ADR_MARKER)
    require(history.rstrip() == normalized_text(baseline_blob(root, ADR)).rstrip(), "ADR_HISTORY_CHANGED")
    validate_document(clarification)


def main() -> int:
    try:
        validate_repository()
    except ReadinessError as error:
        print(f"B7_OFFLINE_READINESS: FAIL ({error})")
        return 1
    except Exception:
        # File/parse/Git failures must never echo input values or source contents.
        print("B7_OFFLINE_READINESS: FAIL (UNREADABLE_OR_INVALID_LOCAL_EVIDENCE)")
        return 1
    print("B7_OFFLINE_READINESS: PASS")
    print("CERTIFIED_BASELINE: " + BASELINE)
    print("B5_WORKFLOW: PASS; B6_LOCAL_EVIDENCE: PRESERVED")
    print("MIGRATION_HEAD: 20260928_0010 (source evidence, not a live catalog query)")
    print("HISTORICAL_SQL: FORBIDDEN; ALEMBIC: MIGRATION_CREDENTIAL_ONLY")
    print("EXCLUDED_SURFACES: automation_runs, outbox_events")
    print("PRODUCTION_STATE: NOT_VERIFIED; ACTIVATION: NOT_AUTHORIZED")
    print("REAL_META_CALLS=0; REAL_N8N_CALLS=0; PRODUCTION_MUTATIONS=0; WORKFLOW_ACTIVATIONS=0")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
