"""Portable JSON contract tests only; these do not execute JavaScript or n8n."""

from __future__ import annotations

import copy
import hashlib
import hmac
import importlib.util
import json
from pathlib import Path
import re
import subprocess
import sys

import pytest

from backend.app.modules.automation.internal_auth import canonical_payload


ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "scripts/local-dev/validate_prompt11_n8n_workflow.py"
spec = importlib.util.spec_from_file_location("prompt11_workflow_validator", SCRIPT)
validator = importlib.util.module_from_spec(spec)
spec.loader.exec_module(validator)

# Public, synthetic owner-supplied fixture; never use a deployed secret here.
SYNTHETIC_SECRET = b"test-n8n-internal-secret-32-bytes-minimum-0001"
TIMESTAMP = "1760000000"
EXPECTED_SIGNATURE = "v1=ccf29f320317b9d0a6ce79b201b3294a448128a859777517b882145ae8390de4"


@pytest.fixture
def workflow():
    return validator.load_workflow()


def node(workflow, name):
    return next(item for item in workflow["nodes"] if item["name"] == name)


def test_versioned_workflow_passes_closed_contract(workflow):
    validator.validate_workflow(workflow)


def test_workflow_payload_matches_b1_and_frozen_hmac_fixture(workflow):
    fields = node(workflow, "Build canonical payload")["parameters"]["assignments"]["assignments"]
    expression = next(item["value"] for item in fields if item["name"] == "canonicalPayload")
    payload = validator.canonical_bytes(expression, TIMESTAMP)
    assert payload == canonical_payload(
        method="POST", path="/internal/automation/publish-due", timestamp=TIMESTAMP, raw_body=b"",
    )
    assert hashlib.sha256(b"").hexdigest() == validator.EMPTY_SHA256
    assert b"\r" not in payload
    assert payload.count(b"\n") == 4
    assert not payload.endswith(b"\n")
    signature = "v1=" + hmac.new(SYNTHETIC_SECRET, payload, hashlib.sha256).hexdigest()
    assert signature == EXPECTED_SIGNATURE
    assert re.fullmatch(r"v1=[0-9a-f]{64}", signature)


def test_timestamp_is_generated_once_and_reused(workflow):
    encoded = json.dumps(workflow)
    assert encoded.count("Date.now()") == 1
    fields = node(workflow, "Capture timestamp")["parameters"]["assignments"]["assignments"]
    assert fields[0]["value"] == "={{ Math.floor(Date.now() / 1000).toString() }}"
    assert fields[0]["type"] == "string"
    headers = node(workflow, "Publish due")["parameters"]["headerParameters"]["parameters"]
    assert headers == [
        {"name": "X-N8N-Timestamp", "value": "={{ $json.timestamp }}"},
        {"name": "X-N8N-Signature", "value": "={{ 'v1=' + $json.hmac.toLowerCase() }}"},
    ]


def test_portable_artifact_has_no_credential_payload_or_real_host(workflow):
    encoded = json.dumps(workflow)
    assert SYNTHETIC_SECRET.decode() not in encoded
    assert "N8N_INTERNAL_SECRET" not in encoded
    assert all("credentials" not in item for item in workflow["nodes"])
    assert all("secret" not in item["parameters"] for item in workflow["nodes"])
    assert node(workflow, "Publish due")["parameters"]["url"] == (
        "https://nightclub-api.example.invalid/internal/automation/publish-due"
    )
    assert workflow["active"] is False


@pytest.mark.parametrize(("status", "destination", "error"), [
    (200, "Operational counts only", None),
    (401, "Authentication failed", "NIGHTCLUB_INTERNAL_AUTH_FAILED"),
    (422, "Contract invalid", "NIGHTCLUB_AUTOMATION_CONTRACT_INVALID"),
    (503, "Automation unavailable", "NIGHTCLUB_AUTOMATION_UNAVAILABLE"),
    (201, "Unexpected response", "NIGHTCLUB_AUTOMATION_UNEXPECTED_RESPONSE"),
    (301, "Unexpected response", "NIGHTCLUB_AUTOMATION_UNEXPECTED_RESPONSE"),
    (302, "Unexpected response", "NIGHTCLUB_AUTOMATION_UNEXPECTED_RESPONSE"),
    (307, "Unexpected response", "NIGHTCLUB_AUTOMATION_UNEXPECTED_RESPONSE"),
    (308, "Unexpected response", "NIGHTCLUB_AUTOMATION_UNEXPECTED_RESPONSE"),
    (500, "Unexpected response", "NIGHTCLUB_AUTOMATION_UNEXPECTED_RESPONSE"),
])
def test_status_rules_and_actual_graph_have_correct_destinations(workflow, status, destination, error):
    # Inspect the frozen numeric-equality rule data; this is not n8n execution.
    validator.validate_workflow(workflow)
    rules = node(workflow, "Route status")["parameters"]["rules"]["values"]
    index = next((index for index, rule in enumerate(rules)
                  if rule["conditions"]["conditions"][0]["rightValue"] == status), len(rules))
    assert workflow["connections"]["Route status"]["main"][index] == [
        {"node": destination, "type": "main", "index": 0},
    ]
    if error:
        assert node(workflow, destination)["parameters"] == {
            "errorType": "errorMessage", "errorMessage": error,
        }


def test_success_projection_has_one_small_item_and_no_auth_material(workflow):
    parameters = node(workflow, "Operational counts only")["parameters"]
    assert parameters["includeOtherFields"] is False
    assert "duplicateItem" not in parameters
    fields = parameters["assignments"]["assignments"]
    # Resolve only simple property paths from the JSON, without eval or JS.
    sample = {"body": {"data": dict.fromkeys(validator.COUNTS, 0),
                       "meta": {"correlationId": "synthetic-correlation"}},
              "timestamp": TIMESTAMP, "hmac": "synthetic", "headers": {}}
    output = {}
    for item in fields:
        match = re.fullmatch(r"=\{\{ \$json\.([A-Za-z.]+) \}\}", item["value"])
        assert match is not None
        value = sample
        for part in match.group(1).split("."):
            value = value[part]
        output[item["name"]] = value
    assert [output] == [{**dict.fromkeys(validator.COUNTS, 0), "correlationId": "synthetic-correlation"}]


# Independent mutations check fail-closed handling, including extra fields that
# n8n could otherwise accept and that could silently change execution semantics.
@pytest.mark.parametrize(("target", "path", "value"), [
    (None, ("active",), True),
    (None, ("active",), 0),
    (None, ("name",), "Other workflow"),
    (None, ("pinData",), {"synthetic": "forbidden"}),
    (None, ("settings", "timezone"), "America/Mexico_City"),
    (None, ("settings", "saveExecutionProgress"), True),
    (None, ("settings", "executionTimeout"), -1),
    (None, ("settings", "redactionPolicy"), "none"),
    ("Every minute", ("parameters", "rule", "interval", 0, "minutesInterval"), 2),
    ("Capture timestamp", ("parameters", "assignments", "assignments", 0, "value"), "={{ Date.now() }}"),
    ("Build canonical payload", ("parameters", "assignments", "assignments", 1, "value"), "={{ $json.timestamp }}"),
    ("Sign with Crypto credential", ("typeVersion",), 1),
    ("Sign with Crypto credential", ("credentials",), {"crypto": {"id": "nonportable"}}),
    ("Sign with Crypto credential", ("parameters", "secret"), "synthetic-forbidden-payload"),
    ("Sign with Crypto credential", ("parameters", "action"), "hash"),
    ("Sign with Crypto credential", ("parameters", "type"), "SHA1"),
    ("Sign with Crypto credential", ("parameters", "encoding"), "base64"),
    ("Publish due", ("parameters", "method"), "GET"),
    ("Publish due", ("parameters", "url"), "https://other.example.invalid/internal/automation/publish-due"),
    ("Publish due", ("parameters", "url"), "http://nightclub-api.example.invalid/internal/automation/publish-due"),
    ("Publish due", ("parameters", "url"), "https://nightclub-api.example.invalid/internal/automation/publish-due?x=1"),
    ("Publish due", ("parameters", "authentication"), "genericCredentialType"),
    ("Publish due", ("parameters", "sendBody"), True),
    ("Publish due", ("parameters", "body"), "{}"),
    ("Publish due", ("parameters", "sendQuery"), True),
    ("Publish due", ("parameters", "headerParameters", "parameters", 0, "name"), "Authorization"),
    ("Publish due", ("parameters", "headerParameters", "parameters", 0, "name"), "X-Organization-Id"),
    ("Publish due", ("parameters", "headerParameters", "parameters", 0, "name"), "Idempotency-Key"),
    ("Publish due", ("parameters", "headerParameters", "parameters", 0, "name"), "Cookie"),
    ("Publish due", ("parameters", "options", "redirect", "redirect", "followRedirects"), True),
    ("Publish due", ("parameters", "options", "allowUnauthorizedCerts"), True),
    ("Publish due", ("parameters", "options", "timeout"), 0),
    ("Publish due", ("parameters", "options", "response", "response", "neverError"), False),
    ("Publish due", ("parameters", "options", "response", "response", "fullResponse"), False),
    ("Publish due", ("retryOnFail",), True),
    ("Publish due", ("continueOnFail",), True),
    ("Publish due", ("disabled",), True),
    ("Publish due", ("onError",), "continueRegularOutput"),
    ("Route status", ("parameters", "options", "fallbackOutput"), "none"),
    ("Route status", ("parameters", "rules", "values", 0, "conditions", "conditions", 0, "rightValue"), 302),
    ("Operational counts only", ("parameters", "includeOtherFields"), True),
    ("Operational counts only", ("parameters", "duplicateItem"), True),
    ("Authentication failed", ("parameters", "errorMessage"), "={{ $json.body }}"),
])
def test_validator_rejects_security_contract_drift(workflow, target, path, value):
    current = workflow if target is None else node(workflow, target)
    for key in path[:-1]:
        current = current[key]
    current[path[-1]] = value
    with pytest.raises(validator.WorkflowContractError):
        validator.validate_workflow(workflow)


@pytest.mark.parametrize("kind", ["code", "executeCommand", "ssh", "postgres", "mySql", "supabase", "webhook", "@custom/agent"])
def test_validator_rejects_unapproved_node_types(workflow, kind):
    node(workflow, "Capture timestamp")["type"] = "n8n-nodes-base." + kind
    with pytest.raises(validator.WorkflowContractError):
        validator.validate_workflow(workflow)


@pytest.mark.parametrize("mutation", ["loop", "bypass", "extra", "duplicate", "missing"])
def test_validator_rejects_graph_and_node_set_drift(workflow, mutation):
    if mutation == "loop":
        workflow["connections"]["Unexpected response"] = validator.outputs("Publish due")
    elif mutation == "bypass":
        workflow["connections"]["Publish due"] = validator.outputs("Operational counts only")
    elif mutation == "extra":
        workflow["nodes"].append(copy.deepcopy(workflow["nodes"][0]))
    elif mutation == "duplicate":
        workflow["nodes"][1] = copy.deepcopy(workflow["nodes"][0])
    else:
        del workflow["connections"]["Build canonical payload"]
    with pytest.raises(validator.WorkflowContractError):
        validator.validate_workflow(workflow)


@pytest.mark.parametrize("text", ['{"active":false,"active":true}', '{"x":NaN}', '{"x":Infinity}', '{bad'])
def test_loader_rejects_ambiguous_or_malformed_json(tmp_path, text):
    path = tmp_path / "invalid.json"
    path.write_text(text, encoding="utf-8")
    with pytest.raises(validator.WorkflowContractError):
        validator.load_workflow(path)


def test_validator_cli_is_directly_executable_without_credentials():
    completed = subprocess.run([sys.executable, str(SCRIPT)], cwd=ROOT, env={},
                               capture_output=True, text=True, timeout=30, check=False)
    assert completed.returncode == 0
    assert "OFFLINE_WORKFLOW_CONTRACT: PASS" in completed.stdout
    assert "LIVE_N8N_RUNTIME: NOT_CERTIFIED" in completed.stdout
    assert completed.stderr == ""


def test_validator_failure_does_not_echo_untrusted_values(workflow, monkeypatch, capsys):
    workflow["name"] = "synthetic-sensitive-marker"
    monkeypatch.setattr(validator, "load_workflow", lambda: workflow)
    assert validator.main() == 1
    output = capsys.readouterr()
    assert output.out == "OFFLINE_WORKFLOW_CONTRACT: FAIL (WORKFLOW_NAME)\n"
    assert "synthetic-sensitive-marker" not in output.out + output.err
