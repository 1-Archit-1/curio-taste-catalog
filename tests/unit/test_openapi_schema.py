"""Contract and security tests for the control-plane OpenAPI schema."""

from typing import Any

from curio.api.app import create_app
from curio.api.openapi import export_control_plane_openapi
from curio.settings import Settings

SETTINGS = Settings(_env_file=None, database_url="postgresql://unused@127.0.0.1:1/unused")

# Fields that must never appear anywhere in the OpenAPI schema (request or response).
FORBIDDEN_ANYWHERE = frozenset(
    {
        "encrypted_key",
        "ciphertext",
        "private_key",
        "key_private_key",
        "token_hash",
    }
)

# Fields that must never appear in any HTTP response schema.
FORBIDDEN_IN_RESPONSES = FORBIDDEN_ANYWHERE | frozenset(
    {
        "api_key",
        "secret",
        "login_token",
        "session_token",
    }
)


def _iter_property_names(node: Any, components: dict[str, Any], seen: set[str]) -> set[str]:
    """Recursively collect all object property names reachable from an OpenAPI schema node."""
    names: set[str] = set()
    if not isinstance(node, dict):
        return names

    ref = node.get("$ref")
    if isinstance(ref, str) and ref.startswith("#/components/schemas/"):
        schema_name = ref.removeprefix("#/components/schemas/")
        if schema_name not in seen:
            seen.add(schema_name)
            names |= _iter_property_names(components.get(schema_name), components, seen)

    props = node.get("properties")
    if isinstance(props, dict):
        for prop_name, prop_schema in props.items():
            names.add(prop_name)
            names |= _iter_property_names(prop_schema, components, seen)

    for key in ("items", "additionalProperties"):
        sub = node.get(key)
        if isinstance(sub, dict):
            names |= _iter_property_names(sub, components, seen)

    for key in ("anyOf", "oneOf", "allOf"):
        variants = node.get(key)
        if isinstance(variants, list):
            for variant in variants:
                names |= _iter_property_names(variant, components, seen)

    return names


def test_every_control_plane_route_declares_typed_response_model():
    schema = create_app(SETTINGS).openapi()
    api_paths = {p: ops for p, ops in schema["paths"].items() if p.startswith("/api/")}
    assert api_paths, "Expected at least one /api/* route in OpenAPI schema"

    for path, operations in api_paths.items():
        for method, op in operations.items():
            if method.lower() not in {"get", "post", "put", "patch", "delete"}:
                continue
            responses = op.get("responses", {})
            ok_resp = responses.get("200") or responses.get("201")
            assert ok_resp is not None, f"{method.upper()} {path} missing 200/201 response"
            json_schema = ok_resp.get("content", {}).get("application/json", {}).get("schema")
            assert json_schema is not None, f"{method.upper()} {path} missing JSON response schema"
            assert "$ref" in json_schema, (
                f"{method.upper()} {path} should return a named Pydantic response model, "
                f"got {json_schema!r}"
            )


def test_openapi_schema_exposes_no_encrypted_key_or_response_key_values():
    schema = create_app(SETTINGS).openapi()
    components = schema.get("components", {}).get("schemas", {})

    # 1. Nowhere in the OpenAPI schema (requests or responses) may ciphertext/hash fields exist.
    all_props: set[str] = set()
    for comp_schema in components.values():
        all_props |= _iter_property_names(comp_schema, components, set())
    leaked_anywhere = all_props & FORBIDDEN_ANYWHERE
    assert not leaked_anywhere, f"Forbidden fields found in OpenAPI schema: {leaked_anywhere}"

    # 2. No response schema on any route may expose an API key value or secret token field.
    for path, operations in schema["paths"].items():
        for method, op in operations.items():
            if not isinstance(op, dict):
                continue
            for status_code, resp in op.get("responses", {}).items():
                json_schema = (
                    resp.get("content", {}).get("application/json", {}).get("schema")
                    if isinstance(resp, dict)
                    else None
                )
                if json_schema is None:
                    continue
                resp_props = _iter_property_names(json_schema, components, set())
                leaked = resp_props & FORBIDDEN_IN_RESPONSES
                assert not leaked, (
                    f"{method.upper()} {path} ({status_code}) response schema leaks: {leaked}"
                )


def test_control_plane_openapi_export_excludes_addon_and_health_routes():
    exported = export_control_plane_openapi()
    paths = list(exported["paths"].keys())
    assert paths, "Expected exported control-plane paths"
    assert all(p.startswith("/api/") for p in paths)
    assert "/healthz" not in paths
    assert not any("manifest.json" in p for p in paths)
