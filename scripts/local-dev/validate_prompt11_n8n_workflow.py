"""Offline, fail-closed validation of the portable B5 artifact, not an n8n runtime.

Only the versioned placeholder artifact is accepted. Live instance exports with
credential references or a real origin are deliberately outside this contract.
No environment, credentials, database, network or subprocess access is needed.
"""

from __future__ import annotations

import json
import math
from pathlib import Path
import re


ROOT = Path(__file__).resolve().parents[2]
WORKFLOW = ROOT / "n8n/workflows/prompt11-publish-due.json"
PATH = "/internal/automation/publish-due"
EMPTY_SHA256 = "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855"
TIMESTAMP_EXPRESSION = "={{ Math.floor(Date.now() / 1000).toString() }}"
CANONICAL_EXPRESSION = (
    '={{ "v1\\nPOST\\n/internal/automation/publish-due\\n" + $json.timestamp + '
    '"\\n' + EMPTY_SHA256 + '" }}'
)
COUNTS = (
    "selected", "processed", "published", "reconciledPublished",
    "retryScheduled", "permanentFailure", "notDue", "leaseUnavailable",
)
ERRORS = {
    "Authentication failed": "NIGHTCLUB_INTERNAL_AUTH_FAILED",
    "Contract invalid": "NIGHTCLUB_AUTOMATION_CONTRACT_INVALID",
    "Automation unavailable": "NIGHTCLUB_AUTOMATION_UNAVAILABLE",
    "Unexpected response": "NIGHTCLUB_AUTOMATION_UNEXPECTED_RESPONSE",
}
NODE_SPECS = {
    "Every minute": ("schedule", "scheduleTrigger", 1.2),
    "Capture timestamp": ("timestamp", "set", 3.4),
    "Build canonical payload": ("canonical", "set", 3.4),
    "Sign with Crypto credential": ("sign", "crypto", 2),
    "Publish due": ("request", "httpRequest", 4.2),
    "Route status": ("route", "switch", 3.2),
    "Operational counts only": ("success", "set", 3.4),
    "Authentication failed": ("auth-failed", "stopAndError", 1),
    "Contract invalid": ("contract-invalid", "stopAndError", 1),
    "Automation unavailable": ("unavailable", "stopAndError", 1),
    "Unexpected response": ("unexpected", "stopAndError", 1),
}


class WorkflowContractError(ValueError):
    """Messages are fixed structural codes, never values from an input file."""


def require(condition: bool, code: str) -> None:
    if not condition:
        raise WorkflowContractError(code)


def same(actual: object, expected: object, code: str) -> None:
    # JSON equality distinguishes booleans from numbers (Python True == 1).
    require(
        json.dumps(actual, sort_keys=True, allow_nan=False)
        == json.dumps(expected, sort_keys=True, allow_nan=False),
        code,
    )


def _unique_object(pairs: list[tuple[str, object]]) -> dict:
    result = {}
    for key, value in pairs:
        require(key not in result, "DUPLICATE_JSON_KEY")
        result[key] = value
    return result


def _reject_constant(_: str) -> None:
    raise WorkflowContractError("NONFINITE_JSON_NUMBER")


def load_workflow(path: Path = WORKFLOW) -> dict:
    try:
        return json.loads(
            path.read_text(encoding="utf-8"),
            object_pairs_hook=_unique_object,
            parse_constant=_reject_constant,
        )
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise WorkflowContractError("WORKFLOW_READ_OR_JSON_FAILURE") from exc


def canonical_bytes(expression: str, timestamp: str) -> bytes:
    """Decode only two JSON string literals joined around one stored timestamp.

    This is a restricted expression-contract check, NOT a JavaScript/n8n
    interpreter. No eval, runtime clock, general expressions or host access.
    """
    require(isinstance(timestamp, str) and bool(re.fullmatch(r"[0-9]+", timestamp)),
            "TIMESTAMP_NOT_DECIMAL")
    literal = r'("(?:[^"\\]|\\.)*")'
    match = re.fullmatch(r"=\{\{ " + literal + r" \+ \$json.timestamp \+ " + literal + r" \}\}", expression)
    require(match is not None, "CANONICAL_EXPRESSION_SHAPE")
    try:
        prefix, suffix = (json.loads(value) for value in match.groups())
    except json.JSONDecodeError as exc:
        raise WorkflowContractError("CANONICAL_LITERAL_INVALID") from exc
    return (prefix + timestamp + suffix).encode("utf-8")


def field(field_id: str, name: str, value: str, kind: str = "string") -> dict:
    return {"id": field_id, "name": name, "value": value, "type": kind}


def set_parameters(fields: list[dict]) -> dict:
    return {
        "mode": "manual", "includeOtherFields": False,
        "assignments": {"assignments": fields}, "options": {},
    }


def status_rule(code: int) -> dict:
    return {
        "conditions": {
            "options": {"caseSensitive": True, "leftValue": "", "typeValidation": "strict", "version": 2},
            "conditions": [{
                "id": f"status-{code}", "leftValue": "={{ $json.statusCode }}",
                "rightValue": code, "operator": {"type": "number", "operation": "equals"},
            }],
            "combinator": "and",
        },
        "renameOutput": True, "outputKey": str(code),
    }


def outputs(*targets: str) -> dict:
    return {"main": [[{"node": target, "type": "main", "index": 0}] for target in targets]}


def validate_workflow(workflow: dict) -> None:
    require(isinstance(workflow, dict), "WORKFLOW_OBJECT_REQUIRED")
    same(sorted(workflow), ["active", "connections", "name", "nodes", "settings"], "WORKFLOW_FIELDS")
    same(workflow["name"], "Night Club AI — Publish Due", "WORKFLOW_NAME")
    same(workflow["active"], False, "WORKFLOW_MUST_BE_INACTIVE")
    same(workflow["settings"], {
        "executionOrder": "v1", "timezone": "UTC", "saveExecutionProgress": False,
        "saveManualExecutions": False, "saveDataSuccessExecution": "none",
        "saveDataErrorExecution": "all", "redactionPolicy": "all", "executionTimeout": 120,
    }, "WORKFLOW_SETTINGS")
    require(isinstance(workflow["nodes"], list) and len(workflow["nodes"]) == 11, "NODE_COUNT")
    nodes = {}
    for node in workflow["nodes"]:
        require(isinstance(node, dict), "NODE_OBJECT_REQUIRED")
        same(sorted(node), ["id", "name", "parameters", "position", "retryOnFail", "type", "typeVersion"], "NODE_FIELDS")
        name = node["name"]
        require(isinstance(name, str) and name in NODE_SPECS and name not in nodes, "NODE_ALLOWLIST")
        node_id, node_type, version = NODE_SPECS[name]
        same(node["id"], node_id, "NODE_ID")
        same(node["type"], "n8n-nodes-base." + node_type, "NODE_TYPE")
        same(node["typeVersion"], version, "NODE_VERSION")
        same(node["retryOnFail"], False, "NO_AUTOMATIC_RETRY")
        position = node["position"]
        require(isinstance(position, list) and len(position) == 2 and all(
            type(value) in (int, float) and math.isfinite(value) for value in position
        ), "NODE_POSITION")
        nodes[name] = node

    def params(name: str, expected: dict, code: str) -> None:
        same(nodes[name]["parameters"], expected, code)

    params("Every minute", {"rule": {"interval": [{"field": "minutes", "minutesInterval": 1}]}}, "SCHEDULE")
    params("Capture timestamp", set_parameters([
        field("timestamp-value", "timestamp", TIMESTAMP_EXPRESSION),
    ]), "TIMESTAMP_CAPTURE")
    params("Build canonical payload", set_parameters([
        field("stored-timestamp", "timestamp", "={{ $json.timestamp }}"),
        field("canonical-value", "canonicalPayload", CANONICAL_EXPRESSION),
    ]), "CANONICAL_PAYLOAD")
    params("Sign with Crypto credential", {
        "action": "hmac", "type": "SHA256", "binaryData": False,
        "value": "={{ $json.canonicalPayload }}", "dataPropertyName": "hmac", "encoding": "hex",
    }, "CRYPTO_CREDENTIAL_ONLY")
    params("Publish due", {
        "method": "POST", "url": "https://nightclub-api.example.invalid" + PATH,
        "authentication": "none", "sendQuery": False, "sendBody": False, "sendHeaders": True,
        "headerParameters": {"parameters": [
            {"name": "X-N8N-Timestamp", "value": "={{ $json.timestamp }}"},
            {"name": "X-N8N-Signature", "value": "={{ 'v1=' + $json.hmac.toLowerCase() }}"},
        ]},
        "options": {
            "allowUnauthorizedCerts": False,
            "redirect": {"redirect": {"followRedirects": False}},
            "timeout": 90000,
            "response": {"response": {"fullResponse": True, "neverError": True, "responseFormat": "autodetect"}},
        },
    }, "HTTP_CONTRACT")
    params("Route status", {
        "mode": "rules", "looseTypeValidation": False,
        "rules": {"values": [status_rule(code) for code in (200, 401, 422, 503)]},
        "options": {"fallbackOutput": "extra", "renameFallbackOutput": "other", "allMatchingOutputs": False},
    }, "STATUS_BRANCHES")
    params("Operational counts only", set_parameters([
        field(name, name, "={{ $json.body.data." + name + " }}", "number") for name in COUNTS
    ] + [field("correlationId", "correlationId", "={{ $json.body.meta.correlationId }}")]), "SUCCESS_PROJECTION")
    for name, error in ERRORS.items():
        params(name, {"errorType": "errorMessage", "errorMessage": error}, "FIXED_ERROR")

    # Exact graph also rejects hidden routes, retry cycles, multiple requests,
    # disconnected signing, bypassed status handling and data-leaking outputs.
    same(workflow["connections"], {
        "Every minute": outputs("Capture timestamp"),
        "Capture timestamp": outputs("Build canonical payload"),
        "Build canonical payload": outputs("Sign with Crypto credential"),
        "Sign with Crypto credential": outputs("Publish due"),
        "Publish due": outputs("Route status"),
        "Route status": outputs("Operational counts only", *ERRORS),
    }, "EXECUTION_GRAPH")


def main() -> int:
    try:
        validate_workflow(load_workflow())
    except WorkflowContractError as exc:
        print(f"OFFLINE_WORKFLOW_CONTRACT: FAIL ({exc})")
        return 1
    except (ValueError, TypeError, KeyError, RecursionError):
        # Never dump input values, parser tracebacks or an untrusted export.
        print("OFFLINE_WORKFLOW_CONTRACT: FAIL (INVALID_STRUCTURE)")
        return 1
    print("OFFLINE_WORKFLOW_CONTRACT: PASS")
    print("NODES: 11; ACTIVE: false; SCHEDULE: 1 minute; TIMEZONE: UTC")
    print("HTTP: POST; BODY: empty; REDIRECTS: false; RETRIES: false")
    print("STATUS_BRANCHES: 200, 401, 422, 503, fallback")
    print("LIVE_N8N_RUNTIME: NOT_CERTIFIED; REAL_N8N_CALLS: 0; REAL_META_CALLS: 0")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
