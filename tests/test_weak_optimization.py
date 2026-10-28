import copy
import inspect
import json
from types import SimpleNamespace

import pytest
import yaml
from azure.ai.agentserver.optimization import load_config

from agent import experiment, main
from agent.evaluation.business_grader import grade, score_case
from scripts import run_weak_optimization as runner
from scripts.run_prompt_sweep import load_datasets


def payload_for(case):
    inputs = case["inputs"]
    calls = [{
        "name": "lookup_promotion_policy",
        "arguments": {"product_id": inputs["product_id"], "region": inputs["region"]},
    }]
    if case["expected_result"].get("offer_price") is not None:
        calls.append({
            "name": "calculate_offer",
            "arguments": {
                key: inputs[key] for key in ("list_price", "unit_cost", "discount_percent")
            },
        })
    return {"result": copy.deepcopy(case["expected_result"]), "trace": {"tool_calls": calls}}


def test_separate_entrypoint_preserves_default_host_contract(monkeypatch):
    calls = []
    monkeypatch.setattr(experiment, "create_host", lambda **kwargs: calls.append(kwargs))
    experiment.create_experiment_host()
    assert calls == [{
        "append_fixed_policy": False, "config_dir": runner.ROOT / "agent" / ".experiment_configs",
    }]
    assert inspect.signature(main.create_host).parameters["append_fixed_policy"].default is True
    manifest = yaml.safe_load((runner.ROOT / "azure.yaml").read_text(encoding="utf-8"))
    services = manifest["services"]
    assert services["promotion-policy"]["codeConfiguration"]["entryPoint"] == "main.py"
    assert services[runner.AGENT]["codeConfiguration"]["entryPoint"] == "reviewed.py"
    assert services[runner.AGENT]["env"]["OPTIMIZATION_LOCAL_DIR"] == ".reviewed_configs"


def test_weak_baseline_and_candidate_configuration_precedence(monkeypatch):
    for name in ("OPTIMIZATION_CONFIG", "OPTIMIZATION_CANDIDATE_ID", "OPTIMIZATION_RESOLVE_ENDPOINT"):
        monkeypatch.delenv(name, raising=False)
    profile = json.loads(
        (runner.ROOT / "agent" / "evaluation" / "starter-prompts.json").read_text(encoding="utf-8"),
    )
    config = load_config(config_dir=runner.BASELINE.parent)
    selected = next(row for row in profile["profiles"] if row["id"] == "underspecified")
    assert config.compose_instructions().strip() == selected["instructions"]
    monkeypatch.setenv("OPTIMIZATION_CONFIG", json.dumps({
        "model": "gpt-4.1-mini", "instructions": "A reviewed optimized instruction.",
    }))
    candidate = load_config(config_dir=runner.BASELINE.parent)
    assert candidate.compose_instructions() == "A reviewed optimized instruction."


@pytest.mark.parametrize("case", runner.development_cases(), ids=lambda case: case["id"])
def test_python_grader_accepts_every_exact_development_result(case):
    payload = payload_for(case)
    assert all(score_case(case, payload).values())
    assert grade({"output_text": json.dumps(payload)}, {"ground_truth": json.dumps(case)}) == 1


@pytest.mark.parametrize("case", load_datasets()["original-dev-v1"], ids=lambda case: case["id"])
def test_shared_checks_preserve_original_cases_without_expected_calculation_fields(case):
    assert all(score_case(case, payload_for(case)).values())


def test_python_grader_rejects_invented_cost_even_with_correct_final_fields():
    case = runner.development_cases()[0]
    payload = payload_for(case)
    payload["trace"]["tool_calls"].append({
        "name": "calculate_offer",
        "arguments": {"list_price": 240, "unit_cost": 0, "discount_percent": 20},
    })
    assert grade({"output_text": json.dumps(payload)}, {"ground_truth": json.dumps(case)}) == 0


@pytest.mark.parametrize("generated", [False, True])
def test_python_grader_uses_foundry_documented_item_mapping(generated):
    case = runner.development_cases()[0]
    output = json.dumps(payload_for(case))
    item = {"ground_truth": json.dumps(case)}
    item.update({"sample": {"output_text": output}} if generated else {"response": output})
    assert grade({}, item) == 1


def test_python_grader_rejects_malformed_response_but_raises_for_bad_ground_truth(capsys):
    case = runner.development_cases()[0]
    assert grade({"output_text": "not JSON"}, {"ground_truth": json.dumps(case)}) == 0
    assert "malformed agent output" in capsys.readouterr().out
    with pytest.raises(json.JSONDecodeError):
        grade({"output_text": "{}"}, {"ground_truth": "invalid reference"})


def test_calibration_rechecks_frozen_scores_without_using_holdout(tmp_path, monkeypatch):
    rows = []
    for index, case in enumerate(runner.development_cases()):
        payload = payload_for(case)
        if index % 2:
            payload["result"]["decision"] = "incorrect"
        rows.append({
            "dataset": "challenge-dev-v1", "profile": "underspecified",
            "case": case["id"], "status": "completed", "payload": payload,
            "checks": score_case(case, payload),
        })
    path = tmp_path / "confirmation.json"
    path.write_text(json.dumps({"records": rows}), encoding="utf-8")
    monkeypatch.setattr(runner, "CONFIRMATION", path)
    content = runner.calibration_content()
    assert len(content) == 12
    assert sum(row["item"]["expected_score"] for row in content) == 6
    rows[0]["checks"]["decision"] = False
    path.write_text(json.dumps({"records": rows}), encoding="utf-8")
    with pytest.raises(ValueError, match="disagrees"):
        runner.calibration_content()


def test_agent_package_excludes_evaluation_and_historical_outputs():
    exclusions = (runner.ROOT / "agent" / ".agentignore").read_text(encoding="utf-8").splitlines()
    assert "evaluation/" in exclusions
    assert ".foundry/" in exclusions


def test_grader_image_is_explicit_and_part_of_experiment_fingerprint(monkeypatch):
    assert runner.grader_parameters()["image_tag"] == "2025-05-08"
    initial = runner.grader_digest()
    parameters = runner.grader_parameters()
    parameters["image_tag"] = "different-image"
    monkeypatch.setattr(runner, "grader_parameters", lambda: parameters)
    assert runner.grader_digest() != initial


@pytest.fixture
def saved_run(tmp_path, monkeypatch):
    gateway = SimpleNamespace(
        configured=True, endpoint="https://example.test", agent_name=runner.AGENT,
        baseline_version="1", agent_model="gpt-4.1-mini", optimization_model="gpt-5.4",
    )
    monkeypatch.setattr(runner, "FoundryGateway", lambda **kwargs: gateway)
    monkeypatch.setattr(runner, "digest", lambda path: str(path))
    monkeypatch.setattr(runner, "grader_digest", lambda: "grader-parameters")
    calibration = runner.calibration_binding(gateway.endpoint)
    calibration_path = tmp_path / "calibration.json"
    runner.save(calibration_path, {"verified": True, "binding": calibration})
    binding = {
        **calibration, "agent": gateway.agent_name, "baseline_version": gateway.baseline_version,
        "instructions_sha256": runner.digest(runner.BASELINE / "instructions.md"),
        "agent_model": gateway.agent_model, "optimization_model": gateway.optimization_model,
        "runtime_sha256": runner.digest(runner.ROOT / "agent" / "main.py"),
        "entrypoint_sha256": runner.digest(runner.ROOT / "agent" / "experiment.py"),
        "policy_sha256": runner.digest(runner.ROOT / "agent" / "policy.py"),
    }
    state_path = tmp_path / "optimization.json"
    runner.save(state_path, {
        "binding": binding, "operation_id": "original-operation", "job_id": "existing-job",
    })
    return gateway, state_path, calibration_path


def test_resume_never_resubmits_or_reinvokes_the_agent(saved_run):
    gateway, state_path, calibration_path = saved_run
    gateway.optimization = lambda job: {"id": job, "status": "in_progress"}
    with pytest.raises(TimeoutError, match="resume"):
        runner.optimize(state_path, calibration_path, 0)
    state = json.loads(state_path.read_text(encoding="utf-8"))
    assert state["operation_id"] == "original-operation"
    assert state["job_id"] == "existing-job"


def test_failed_optimizer_job_is_not_a_successful_command(saved_run):
    gateway, state_path, calibration_path = saved_run
    gateway.optimization = lambda job: {"id": job, "status": "failed", "error": "grader failure"}
    with pytest.raises(RuntimeError, match="grader failure"):
        runner.optimize(state_path, calibration_path, 0)
    assert json.loads(state_path.read_text(encoding="utf-8"))["result"]["status"] == "failed"


def test_public_agent_and_changed_baselines_are_rejected(saved_run):
    gateway, state_path, calibration_path = saved_run
    gateway.agent_name = "promotion-policy"
    with pytest.raises(ValueError, match="isolated experiment"):
        runner.optimize(state_path, calibration_path, 0)
    gateway.agent_name = runner.AGENT
    gateway.baseline_version = "2"
    with pytest.raises(ValueError, match="different experiment inputs"):
        runner.optimize(state_path, calibration_path, 0)


def test_uncertain_submissions_require_reconciliation_not_another_job(saved_run):
    _, state_path, calibration_path = saved_run
    state = json.loads(state_path.read_text(encoding="utf-8"))
    state.pop("job_id")
    state["submission_started_at"] = "recorded-before-submission"
    runner.save(state_path, state)
    with pytest.raises(RuntimeError, match="reconcile"):
        runner.optimize(state_path, calibration_path, 0)


def test_read_only_status_does_not_need_current_calibration_or_source_hashes(saved_run):
    gateway, state_path, _ = saved_run
    gateway.optimization = lambda job: {"id": job, "status": "succeeded"}
    state = json.loads(state_path.read_text(encoding="utf-8"))
    state["binding"]["grader_sha256"] = "archived-source-from-submission"
    runner.save(state_path, state)
    result = runner.status(state_path, 0)
    assert result == {"id": "existing-job", "status": "succeeded"}
