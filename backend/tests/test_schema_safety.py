"""Offline schema boundaries; external retrieval is intercepted, never sent."""
from contextlib import asynccontextmanager
from io import BytesIO
import json
import urllib.request

from fastapi import HTTPException
import pytest

from aiopsstudio import connectors
from aiopsstudio import schema_safety
from aiopsstudio.config import ConfigError, ConfigService
from m1_fixtures import analysis_defaults
from test_runtime import runtime

REMOTE = "https://schema.example.invalid/rule"


SCHEMA_POSITIONS = [
    lambda child: {"$defs": {"payload": child}},
    lambda child: {"properties": {"payload": child}},
    lambda child: {"patternProperties": {".*": child}},
    lambda child: {"dependentSchemas": {"payload": child}},
    lambda child: {"items": child},
    lambda child: {"prefixItems": [child]},
    lambda child: {"allOf": [child]},
    lambda child: {"anyOf": [child]},
    lambda child: {"if": child},
    lambda child: {"then": child},
    lambda child: {"else": child},
    lambda child: {"additionalProperties": child},
]


@pytest.fixture
def retrievals(monkeypatch):
    requests = []

    def capture(request, *args, **kwargs):
        requests.append(getattr(request, "full_url", str(request)))
        # The old validator's actual retrieval hook receives a controlled reply;
        # no HTTP, DNS, provider or source connection is ever opened by this test.
        return BytesIO(json.dumps({"$schema": "https://json-schema.org/draft/2020-12/schema",
                                  "type": "object"}).encode())

    monkeypatch.setattr(urllib.request, "urlopen", capture)
    return requests


@pytest.mark.parametrize("keyword", ["$ref", "$dynamicRef"])
def test_config_rejects_external_schema_reference_keywords(keyword):
    with pytest.raises(ConfigError):
        ConfigService._local_schema({"type": "object", "properties": {"payload": {keyword: REMOTE}}})


@pytest.mark.parametrize("wrap", SCHEMA_POSITIONS)
@pytest.mark.parametrize("keyword", ["$ref", "$dynamicRef"])
def test_reference_policy_visits_applicable_subschema_positions(keyword, wrap):
    with pytest.raises(ConfigError):
        ConfigService._local_schema(wrap({keyword: REMOTE}))


@pytest.mark.parametrize("keyword", ["examples", "default", "const", "enum"])
def test_business_reference_keys_in_instance_data_are_not_schema_references(keyword):
    data = {"$ref": REMOTE, "$dynamicRef": REMOTE}
    value = [data] if keyword in {"examples", "enum"} else data
    schema = {"type": "object", "properties": {"payload": {keyword: value}}}
    ConfigService._local_schema(schema)
    connectors._check_tool_schema({"inputSchema": schema, "outputSchema": schema})


@pytest.mark.parametrize("keyword, anchor", [("$ref", "$anchor"), ("$dynamicRef", "$dynamicAnchor")])
def test_local_definitions_and_static_or_dynamic_anchors_remain_usable(keyword, anchor, retrievals):
    schema = {"type": "object", "$defs": {"text": {anchor: "text", "type": "string"}},
              "properties": {"payload": {keyword: "#text"}}}
    validator = schema_safety.offline_validator(schema)
    validator.validate({"payload": "valid"})
    assert not validator.is_valid({"payload": 12})
    schema["properties"]["payload"] = {"$ref": "#/$defs/text"}
    schema_safety.offline_validator(schema).validate({"payload": "valid"})
    assert retrievals == []


@pytest.mark.parametrize("keyword", ["$ref", "$dynamicRef"])
@pytest.mark.parametrize("uri", [REMOTE, "file:///synthetic-unavailable/schema.json"])
def test_registry_denies_retrieval_even_if_preflight_is_accidentally_skipped(monkeypatch, retrievals, keyword, uri):
    monkeypatch.setattr(schema_safety, "check_local_schema", lambda schema: None)
    with pytest.raises(Exception):
        schema_safety.offline_validator({keyword: uri}).validate({})
    assert retrievals == []


@pytest.mark.asyncio
@pytest.mark.parametrize("keyword", ["$ref", "$dynamicRef"])
async def test_legacy_mcp_schema_never_retrieves_external_resources(monkeypatch, retrievals, keyword):
    sessions = []

    @asynccontextmanager
    async def forbidden_session(*args, **kwargs):
        sessions.append(True)
        raise connectors.ConnectorError("synthetic transport boundary")
        yield  # pragma: no cover

    monkeypatch.setattr(connectors, "_session", forbidden_session)
    # Simulate a legacy/missed static preflight: actual validation must still
    # refuse retrieval, independently of the schema-node admission check.
    monkeypatch.setattr(schema_safety, "check_local_schema", lambda schema: None)
    server = {"id": "legacy-schema-source", "allowed_tools": ["read"], "tools": [
        {"name": "read", "readonly": True, "inputSchema": {keyword: REMOTE}}]}
    with pytest.raises(Exception):
        await connectors.mcp_call(server, None, "read", {})
    assert retrievals == []
    assert sessions == []


@pytest.mark.asyncio
async def test_legacy_skill_dynamic_reference_is_rejected_before_retrieval(runtime, retrievals):
    skill = runtime.store.get("skill", "skill_fixture")
    skill["versions"][0]["snapshot"]["input_schema"] = {"$dynamicRef": REMOTE}
    runtime.store.save("skill", skill["id"], skill)
    rejected = False
    try:
        await runtime.submit({**analysis_defaults(), "skill_id": skill["id"], "client_request_id": "offline-skill-schema"})
    except HTTPException as error:
        rejected = error.status_code == 422
    assert retrievals == []
    assert rejected
    assert runtime.store.list("run") == []


@pytest.mark.asyncio
async def test_skill_input_validator_stays_offline_without_static_admission(runtime, retrievals, monkeypatch):
    skill = runtime.store.get("skill", "skill_fixture")
    skill["versions"][0]["snapshot"]["input_schema"] = {"$dynamicRef": REMOTE}
    runtime.store.save("skill", skill["id"], skill)
    monkeypatch.setattr(runtime.app.state.config, "validate", lambda *args: {"valid": True})
    monkeypatch.setattr(schema_safety, "check_local_schema", lambda schema: None)
    with pytest.raises(HTTPException) as error:
        await runtime.submit({**analysis_defaults(), "skill_id": skill["id"], "client_request_id": "offline-legacy-input"})
    assert error.value.status_code == 422
    assert retrievals == []
    assert runtime.store.list("run") == []
