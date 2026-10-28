import hashlib
import json

import pytest
from fastapi.testclient import TestClient

from agent import reviewed
from demo import experiment
from demo.http_api import create_app
from scripts import prepare_reviewed_demo as prepare
from tests.test_http_api import TestGateway


def test_reviewed_prompt_has_exactly_the_documented_semantics_repair():
    instructions = (prepare.DESTINATION / "instructions.md").read_text(encoding="utf-8").strip()
    provenance = json.loads(
        (prepare.ROOT / "agent" / "evaluation" / "reviewed-candidate.json").read_text(encoding="utf-8"),
    )
    assert hashlib.sha256(instructions.encode()).hexdigest() == (
        provenance["reviewed_instructions_sha256"]
    )
    original = instructions
    for before, after in prepare.REPLACEMENTS.items():
        assert before not in instructions and instructions.count(after) == 1
        original = original.replace(after, before, 1)
    assert hashlib.sha256(original.encode()).hexdigest() == (
        provenance["original_instructions_sha256"]
    )
    assert provenance["optimizer_score"] == pytest.approx(8 / 12)
    assert provenance["holdout_used_for_repair"] is False


def test_reviewed_host_uses_separate_config_without_fixed_prompt(monkeypatch):
    calls = []
    monkeypatch.setattr(reviewed, "create_host", lambda **kwargs: calls.append(kwargs))
    reviewed.create_reviewed_host()
    assert calls == [{
        "append_fixed_policy": False,
        "config_dir": prepare.ROOT / "agent" / ".reviewed_configs",
    }]


def test_development_training_does_not_contain_any_holdout():
    training = experiment.training_cases()
    assert len(training) == 12
    assert {case.query for case in training}.isdisjoint(
        case["query"] for case in experiment.cases("holdout")
    )
    assert experiment.evaluators()[0].initialization_parameters["image_tag"] == "2025-05-08"


def test_public_experiment_pins_reviewed_v2_and_exposes_only_valid_case_scoring():
    gateway = TestGateway()
    gateway.experiment = True
    gateway.reviewed_version = "2"
    case = experiment.cases("holdout")[0]
    payload = {
        "result": {**case["expected_result"], "reason": "Missing cost."},
        "trace": {"tool_calls": [{
            "name": "lookup_promotion_policy",
            "arguments": {"product_id": "LAPTOP", "region": "DE"},
        }]},
        "version": "2",
    }
    gateway.invoke = lambda text, version: dict(payload)
    with TestClient(create_app(gateway)) as client:
        catalog = client.get("/api/catalog").json()
        assert catalog["v2"] == {"version": "2", "reviewed": True}
        assert len(catalog["cases"]) == 20
        assert sum(row["split"] == "holdout" for row in catalog["cases"]) == 8
        assert catalog["candidate_application_enabled"] is False
        assert "reviewed fix" in catalog["provenance"]
        assert catalog["source_optimization_job"] == experiment.OPTIMIZER_JOB
        assert "optimization_job" not in catalog
        assert catalog["version_provenance"]["optimizer_score"] == pytest.approx(8 / 12)
        response = client.post("/api/run", json={
            "case_id": case["id"], "text": case["query"], "version": "2",
        })
        assert response.status_code == 200
        assert all(check["passed"] for check in response.json()["checks"])
        custom = client.post("/api/run", json={
            "case_id": case["id"], "text": "An edited request.", "version": "2",
        })
        assert custom.json()["checks"] is None
        assert client.post("/api/optimizations/job/apply", json={
            "candidate_id": "candidate",
        }).status_code == 409
        assert gateway.deployments == 0
