"""Offline B7.1 recertification checks; never reads environment or live systems."""

from __future__ import annotations

import ast
import hashlib
import importlib.util
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
HISTORICAL_B7_BASELINE = "301fe0f0c8bb2e0bff1c7f8a042e1aa2bb794529"
REVIEWED_BASELINE = "b228ea865ff4dcbf43f92b5c47caa7584c887367"
HISTORICAL_VALIDATOR = "scripts/local-dev/validate_prompt11_b7_deployment_readiness.py"
HISTORICAL_TEST = "backend/tests/test_prompt11_b7_deployment_readiness.py"
REPORT = "docs/PROMPT_11_B7_1_RECERTIFICATION.md"
RUNBOOK = "docs/runbooks/prompt11-b7-1-recertification.md"


def load_historical_validator(root: Path):
    spec = importlib.util.spec_from_file_location("prompt11_historical_b7", root / HISTORICAL_VALIDATOR)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


b7 = load_historical_validator(ROOT)
ReadinessError = b7.ReadinessError

REVIEWED = tuple(dict.fromkeys(b7.PRESERVED + (
    "backend/tests/test_publish_due_api.py",
    "backend/tests/test_facebook_security_migration.py",
    HISTORICAL_VALIDATOR,
    HISTORICAL_TEST,
    b7.REPORT,
    b7.RUNBOOK,
    b7.ADR,
    "README.md",
)))


def require(value: bool, code: str) -> None:
    if not value:
        raise ReadinessError(code)


def reviewed_blob(root: Path, path: str) -> bytes:
    return b7.git_read(root, "cat-file", "blob", f"{REVIEWED_BASELINE}:{path}")


def normalized(value: bytes) -> str:
    return value.decode("utf-8").replace("\r\n", "\n")


def validate_repository(root: Path = ROOT) -> None:
    require(b7.BASELINE == HISTORICAL_B7_BASELINE, "HISTORICAL_B7_BASELINE_CHANGED")

    for path in REVIEWED:
        current = (root / path).read_bytes()
        certified = reviewed_blob(root, path)
        if path == b7.WORKFLOW:
            require(current == certified, "PORTABLE_WORKFLOW_BYTES_CHANGED")
        else:
            require(normalized(current) == normalized(certified), "CERTIFIED_SOURCE_DRIFT")

    # B7 remains a frozen historical gate. The reviewed dependency change must
    # still differ from B7, and B7 must reject it rather than silently bless it.
    changed_path = "backend/app/modules/automation/publish_due_dependencies.py"
    require(normalized(reviewed_blob(root, changed_path))
            != normalized(b7.baseline_blob(root, changed_path)),
            "B7_1_DELTA_NOT_PRESENT")
    try:
        b7.validate_repository(root)
    except b7.ReadinessError as error:
        require(str(error) == "CERTIFIED_SOURCE_DRIFT", "HISTORICAL_B7_REJECTION_CHANGED")
    else:
        raise ReadinessError("HISTORICAL_B7_UNEXPECTEDLY_PASSES")

    frozen = b7.git_read(root, "ls-tree", "-r", "--name-only", b7.BASELINE,
                         "backend/migrations/versions", "sql")
    frozen_paths = frozen.decode().splitlines()
    require(bool(frozen_paths), "FROZEN_INPUTS_MISSING")
    for path in frozen_paths:
        require(normalized((root / path).read_bytes())
                == normalized(b7.baseline_blob(root, path)), "FROZEN_CONTENT_CHANGED")
        if path.endswith(".py"):
            tree = ast.parse((root / path).read_text(encoding="utf-8"))
            assignments = {item.targets[0].id: item.value for item in tree.body
                           if isinstance(item, ast.Assign) and isinstance(item.targets[0], ast.Name)}
            for name, value in assignments.items():
                if name.startswith("FROZEN_") and name.endswith("_SHA256"):
                    previous = assignments["PREVIOUS_PATH"]
                    require(isinstance(previous, ast.Call) and len(previous.args) == 1,
                            "FROZEN_HASH_GUARD_UNRECOGNIZED")
                    previous_name = ast.literal_eval(previous.args[0])
                    require(Path(previous_name).name == previous_name,
                            "FROZEN_HASH_TARGET_INVALID")
                    digest = hashlib.sha256(
                        (root / path).with_name(previous_name).read_bytes()
                    ).hexdigest()
                    require(digest == ast.literal_eval(value),
                            "FROZEN_MIGRATION_HASH_MISMATCH")
    current_revisions = {file.name for file in (root / "backend/migrations/versions").glob("*.py")}
    certified_revisions = {Path(path).name for path in frozen_paths if path.endswith(".py")}
    require(current_revisions == certified_revisions, "MIGRATION_SET_CHANGED")

    # Retain B7's executable-surface checks.
    for path in b7.ACTIVE_SOURCES:
        b7.validate_active_source((root / path).read_text(encoding="utf-8"))

    # The reviewed baseline pins the B5 validator and portable workflow.
    spec = importlib.util.spec_from_file_location("b7_1_certified_b5", root / b7.B5_VALIDATOR)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    try:
        module.validate_workflow(module.load_workflow(root / b7.WORKFLOW))
    except Exception:
        raise ReadinessError("B5_WORKFLOW_CONTRACT_FAILED") from None

    required_evidence = (
        HISTORICAL_B7_BASELINE,
        REVIEWED_BASELINE,
        "B7.1_RESULT: PASS",
        "HISTORICAL_B7: PRESERVED_AND_SUPERSEDED",
        "PRODUCTION_STATE: NOT_VERIFIED",
        "ACTIVATION: NOT_AUTHORIZED",
        "REAL_META_CALLS=0; REAL_N8N_CALLS=0; PRODUCTION_MUTATIONS=0; WORKFLOW_ACTIVATIONS=0",
    )
    for path in (REPORT, RUNBOOK):
        text = (root / path).read_text(encoding="utf-8")
        require(all(value in text for value in required_evidence),
                "B7_1_CERTIFICATION_EVIDENCE_MISSING")
        b7.validate_document(text)


def main() -> int:
    try:
        validate_repository()
    except ReadinessError as error:
        print(f"B7_1_RECERTIFICATION: FAIL ({error})")
        return 1
    except Exception:
        print("B7_1_RECERTIFICATION: FAIL (UNREADABLE_OR_INVALID_LOCAL_EVIDENCE)")
        return 1
    print("B7_1_RECERTIFICATION: PASS")
    print("REVIEWED_BASELINE: " + REVIEWED_BASELINE)
    print("HISTORICAL_B7: PRESERVED_AND_SUPERSEDED")
    print("PRODUCTION_STATE: NOT_VERIFIED; ACTIVATION: NOT_AUTHORIZED")
    print("REAL_META_CALLS=0; REAL_N8N_CALLS=0; PRODUCTION_MUTATIONS=0; WORKFLOW_ACTIVATIONS=0")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
