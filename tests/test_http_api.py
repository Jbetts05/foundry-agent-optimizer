import uuid

import pytest
from azure.core.exceptions import HttpResponseError
from fastapi.testclient import TestClient

from demo.cases import CASE_BY_ID
from demo.http_api import create_app


class TestGateway:
    __test__ = False
    configured = True
    agent_name = "test-agent"
    agent_model = "test-model"
    baseline_version = "1"

    def __init__(self):
        self.submissions = []
        self.inputs = []
        self.deployments = 0

    def require_version(self, version):
        if version not in {"1", "2"}:
            raise ValueError("This is not an optimized version of the configured baseline.")

    def invoke(self, text, version):
        self.inputs.append((text, version))
        return {
            "result": {
                "decision": "approve", "reason": "Eligible promotion.",
                "discount_cap_percent": 15, "minimum_margin_percent": 18,
                "offer_price": 540, "projected_margin_percent": 35.19,
            },
            "trace": {"tool_calls": [{"name": "lookup_promotion_policy"}]},
            "usage": None, "version": version, "latency_ms": 1, "response_id": "response-test",
        }

    def start_optimization(self, operation_id):
        self.submissions.append(operation_id)
        return "cloud-job"

    def optimization(self, job_id):
        return {
            "id": job_id, "status": "succeeded", "baseline_id": "baseline",
            "candidates": [
                {"id": "baseline", "instructions": None, "status": "completed"},
                {"id": "candidate", "instructions": "Improved", "status": "completed"},
            ],
        }

    def deploy_candidate(self, job_id, candidate_id):
        self.deployments += 1
        return {"version": "2", "candidate_id": candidate_id, "job_id": job_id}


@pytest.fixture
def service():
    gateway = TestGateway()
    with TestClient(create_app(gateway)) as client:
        yield client, gateway


def test_public_page_and_catalog(service):
    client, _ = service
    assert client.get("/").status_code == 200
    assert client.get("/app.js").status_code == 200
    catalog = client.get("/api/catalog").json()
    assert catalog["configured"] is True
    assert all(case["split"] == "test" for case in catalog["cases"])


def test_each_http_call_measures_exactly_one_pinned_version(service):
    client, gateway = service
    case = CASE_BY_ID["approve"]
    result = client.post("/api/run", json={
        "case_id": case.id, "text": case.prompt, "version": "1",
    })
    assert result.status_code == 200
    assert result.json()["usage"] is None
    assert all(check["passed"] for check in result.json()["checks"])
    assert gateway.inputs == [(case.prompt, "1")]
    assert client.get("/api/comparisons/anything").status_code == 404


def test_custom_request_has_no_canned_score(service):
    client, _ = service
    response = client.post("/api/run", json={
        "case_id": "approve", "text": "My own request", "version": "1",
    })
    assert response.json()["checks"] is None


def test_optimization_submission_returns_cloud_id_and_forwards_idempotency_key(service):
    client, gateway = service
    operation = str(uuid.uuid4())
    response = client.post("/api/optimizations", json={"operation_id": operation})
    assert response.status_code == 202
    assert response.json() == {"id": "cloud-job", "job_id": "cloud-job", "status": "queued"}
    assert gateway.submissions == [operation]
    assert client.get("/api/optimizations/cloud-job").json()["status"] == "succeeded"


def test_only_valid_completed_candidate_can_be_applied(service):
    client, gateway = service
    path = "/api/optimizations/cloud-job/apply"
    assert client.post(path, json={"candidate_id": "outside-job"}).status_code == 422
    assert client.post(path, json={"candidate_id": "baseline"}).status_code == 422
    assert client.post(path, json={"candidate_id": "candidate"}).json()["version"] == "2"
    assert gateway.deployments == 1
    assert gateway.baseline_version == "1"


def test_versions_are_validated_before_invocation(service):
    client, gateway = service
    assert client.get("/api/versions/3").status_code == 502
    response = client.post("/api/run", json={
        "case_id": "approve", "text": "Test", "version": "3",
    })
    assert response.status_code == 502
    assert not gateway.inputs


def test_cross_site_requests_cannot_start_optimization(service):
    client, gateway = service
    response = client.post("/api/optimizations", json={"operation_id": str(uuid.uuid4())},
                           headers={"Origin": "https://foreign.test"})
    assert response.status_code == 403
    assert gateway.submissions == []


def test_configured_static_web_app_origin_is_accepted(service, monkeypatch):
    client, _ = service
    monkeypatch.setenv("DEMO_ORIGIN", "https://demo.azurestaticapps.net")
    response = client.post("/api/run", json={
        "case_id": "approve", "text": "Test", "version": "1",
    }, headers={"Origin": "https://demo.azurestaticapps.net"})
    assert response.status_code == 200


@pytest.mark.parametrize("override", [
    {"case_id": "dev-approve"}, {"text": "x" * 6001}, {"text": " "},
    {"version": "latest"}, {"extra": True},
])
def test_bad_requests_are_rejected(service, override):
    client, gateway = service
    response = client.post("/api/run", json={
        "case_id": "approve", "text": "Test", "version": "1", **override,
    })
    assert response.status_code == 422
    assert not gateway.inputs


def test_cloud_failure_does_not_return_sample_output(service):
    client, gateway = service

    def fail(text, version):
        raise RuntimeError("Foundry capacity is unavailable")

    gateway.invoke = fail
    response = client.post("/api/run", json={
        "case_id": "approve", "text": "Test", "version": "1",
    })
    assert response.status_code == 502
    assert "Foundry capacity" in response.json()["detail"]
    assert "result" not in response.json()


def test_upstream_service_outage_is_not_reported_as_bad_project_access(service):
    client, gateway = service

    def fail(operation_id):
        error = HttpResponseError("no healthy upstream")
        error.status_code = 503
        raise error

    gateway.start_optimization = fail
    response = client.post("/api/optimizations", json={"operation_id": str(uuid.uuid4())})
    assert response.status_code == 503
    assert "temporarily unavailable" in response.json()["detail"]
    assert "result" not in response.json()
