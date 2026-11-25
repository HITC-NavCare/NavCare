"""docs/openapi.yaml (the spec we publish on SwaggerHub) must match what the API really does.

These tests compare it with the app: the paths, the parameters of each path, and the fields of
real answers. If one fails, update the spec and the code together.
"""

from pathlib import Path

import pytest
import yaml
from fastapi.testclient import TestClient

from app.api.main import app

SPEC_PATH = Path(__file__).resolve().parents[1] / "docs" / "openapi.yaml"
SPEC = yaml.safe_load(SPEC_PATH.read_text(encoding="utf-8"))
APP_SPEC = app.openapi()  # what FastAPI generates from the code (/openapi.json)
client = TestClient(app)


def resolve(node):
    """Follow a local $ref such as '#/components/schemas/Error'."""
    while isinstance(node, dict) and "$ref" in node:
        target = SPEC
        for part in node["$ref"].lstrip("#/").split("/"):
            target = target[part]
        node = target
    return node


def parameter_names(spec, path):
    return {(param["in"], param["name"].lower())
            for param in (resolve(p) for p in spec["paths"][path]["get"].get("parameters", []))}


def test_spec_is_openapi_3_with_the_same_paths_as_the_app():
    assert SPEC["openapi"].startswith("3.")
    assert set(SPEC["paths"]) == set(APP_SPEC["paths"])
    for path, operations in SPEC["paths"].items():
        assert set(operations) == set(APP_SPEC["paths"][path]), path


@pytest.mark.parametrize("path", sorted(APP_SPEC["paths"]))
def test_same_parameters_as_the_app(path):
    assert parameter_names(SPEC, path) == parameter_names(APP_SPEC, path)


def test_every_path_but_health_needs_the_api_key():
    scheme = SPEC["components"]["securitySchemes"]["ApiKeyAuth"]
    assert (scheme["type"], scheme["in"], scheme["name"]) == ("apiKey", "header", "X-API-Key")
    assert SPEC["security"] == [{"ApiKeyAuth": []}]  # the default for every path
    assert SPEC["paths"]["/v1/health"]["get"]["security"] == []  # ...except health
    for path in ("/v1/recommendations", "/v1/hospitals", "/v1/hospitals/{hospital_id}"):
        assert "security" not in SPEC["paths"][path]["get"], path
        assert {"401", "429"} <= set(SPEC["paths"][path]["get"]["responses"]), path


def check_fields(body, schema, where):
    """Every field in the body is in the schema, and every required field is in the body."""
    schema = resolve(schema)
    properties = schema.get("properties", {})
    assert set(body) <= set(properties), f"{where}: not in the spec: {set(body) - set(properties)}"
    missing = set(schema.get("required", [])) - set(body)
    assert not missing, f"{where}: required in the spec but missing: {missing}"


def test_health_answer_matches_the_spec():
    body = client.get("/v1/health").json()
    check_fields(body, SPEC["components"]["schemas"]["HealthResponse"], "health")


def test_error_answer_matches_the_spec():
    body = client.get("/v1/recommendations", params={"lat": 0, "lon": 0}).json()
    schema = SPEC["components"]["schemas"]["Error"]
    check_fields(body, schema, "error")
    check_fields(body["error"], schema["properties"]["error"], "error.error")
    codes = schema["properties"]["error"]["properties"]["code"]["enum"]
    assert body["error"]["code"] in codes


def test_recommendation_example_matches_the_spec():
    # The spec's own example must follow its schema (SwaggerHub shows it to developers).
    ok = SPEC["paths"]["/v1/recommendations"]["get"]["responses"]["200"]
    example = ok["content"]["application/json"]["example"]
    schemas = SPEC["components"]["schemas"]
    check_fields(example, schemas["RecommendationResponse"], "example")
    for item in example["recommendations"]:
        check_fields(item, schemas["Recommendation"], "example recommendation")
        check_fields(item["hospital"], schemas["HospitalSummary"], "example hospital")


def merged(schema):
    """An allOf schema as one object schema: properties and required lists combined."""
    schema = resolve(schema)
    if "allOf" not in schema:
        return schema
    parts = [merged(part) for part in schema["allOf"]]
    return {"properties": {k: v for p in parts for k, v in p.get("properties", {}).items()},
            "required": [r for p in parts for r in p.get("required", [])]}


def test_hospital_answers_match_the_spec(monkeypatch):
    from app.services import hospital_status_getter
    from tests.factories import make_er_data, make_status

    monkeypatch.setattr(hospital_status_getter, "get_er_data",
                        lambda: make_er_data({"51218097": make_status()}))
    schemas = SPEC["components"]["schemas"]
    listing = client.get("/v1/hospitals").json()
    check_fields(listing, schemas["HospitalListResponse"], "hospital list")
    statuses = 0
    for hospital in listing["hospitals"]:
        check_fields(hospital, merged(schemas["Hospital"]), "hospital")
        if hospital["status"] is not None:
            check_fields(hospital["status"], schemas["LiveStatus"], "status")
            statuses += 1
    assert statuses == 1
    detail = client.get("/v1/hospitals/51218097").json()
    check_fields(detail, schemas["HospitalDetailResponse"], "hospital detail")

    ok = SPEC["paths"]["/v1/hospitals"]["get"]["responses"]["200"]
    example = ok["content"]["application/json"]["example"]
    check_fields(example, schemas["HospitalListResponse"], "list example")
    check_fields(example["hospitals"][0], merged(schemas["Hospital"]), "list example hospital")
    check_fields(example["hospitals"][0]["status"], schemas["LiveStatus"], "example status")
