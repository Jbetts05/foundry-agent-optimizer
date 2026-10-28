import json
from contextlib import contextmanager, nullcontext
from types import SimpleNamespace

import pytest

from agent.main import run_agent
from agent.policy import POLICY, Decision, calculate_offer, lookup_promotion_policy
from demo.cases import CASES
from scripts import run_prompt_sweep as sweep

CHALLENGES = sweep.load_datasets()["challenge-dev-v1"]


def correct_payload(case):
    result = {
        "decision": "clarify", "reason": "Checked against policy.",
        "discount_cap_percent": None, "minimum_margin_percent": None,
        "offer_price": None, "projected_margin_percent": None,
        **case["expected_result"],
    }
    inputs = case["inputs"]
    lookup = {"product_id": inputs["product_id"], "region": inputs["region"]}
    calls = [{
        "name": "lookup_promotion_policy", "arguments": lookup,
        "output": lookup_promotion_policy(**lookup),
    }]
    if result["offer_price"] is not None:
        arguments = {
            key: inputs[key] for key in ("list_price", "unit_cost", "discount_percent")
        }
        calls.append({
            "name": "calculate_offer", "arguments": arguments,
            "output": calculate_offer(**arguments),
        })
    return {"result": result, "trace": {"tool_calls": calls}}


@pytest.mark.parametrize("append_policy", [True, False])
def test_policy_mode_is_explicit_and_preserves_default_host_behavior(append_policy):
    captured = []
    result = correct_payload(CHALLENGES[0])["result"]

    def respond(**kwargs):
        captured.append(kwargs)
        return SimpleNamespace(usage=None, output=[], output_text=json.dumps(result))

    client = SimpleNamespace(responses=SimpleNamespace(create=respond))
    profile = sweep.Profile("experimental", "Review this request.", "model")
    options = {} if append_policy else {"append_fixed_policy": False}
    payload = run_agent("A request", profile, client, **options)
    effective = captured[0]["instructions"]
    assert (POLICY in effective) is append_policy
    assert payload["trace"]["policy_in_prompt"] is append_policy
    assert captured[0]["store"] is False
    assert captured[0]["text"]["format"]["schema"] == Decision.model_json_schema()
    assert payload["usage"] is None


@pytest.mark.parametrize("case", CHALLENGES, ids=lambda case: case["id"])
def test_exact_challenge_answer_and_authoritative_tool_calls_pass(case):
    assert all(sweep.score_case(case, correct_payload(case)).values())


def test_correct_clarification_does_not_hide_invented_zero_cost():
    case = CHALLENGES[0]
    payload = correct_payload(case)
    payload["trace"]["tool_calls"].append({
        "name": "calculate_offer",
        "arguments": {"list_price": 240, "unit_cost": 0, "discount_percent": 20},
        "output": calculate_offer(240, 0, 20),
    })
    checks = sweep.score_case(case, payload)
    assert checks["decision"] is True
    assert checks["authoritative_calculator_inputs"] is False


def test_correct_numbers_do_not_hide_missing_tool_execution():
    case = CHALLENGES[2]
    payload = correct_payload(case)
    payload["trace"]["tool_calls"] = []
    checks = sweep.score_case(case, payload)
    assert checks["decision"] is True
    assert checks["policy_lookup"] is False
    assert checks["calculator_executed"] is False


def test_one_cent_rounding_error_is_not_accepted_as_exact():
    case = CHALLENGES[5]
    payload = correct_payload(case)
    payload["result"]["projected_margin_percent"] = 20
    assert sweep.score_case(case, payload)["projected_margin_percent"] is False


def test_stale_cost_is_wrong_even_if_final_answer_is_correct():
    case = CHALLENGES[9]
    payload = correct_payload(case)
    payload["trace"]["tool_calls"][1]["arguments"]["unit_cost"] = 125
    assert sweep.score_case(case, payload)["authoritative_calculator_inputs"] is False


def test_sweep_uses_two_development_versions_and_never_original_holdouts():
    datasets = sweep.load_datasets()
    assert len(datasets["original-dev-v1"]) == 8
    assert len(datasets["challenge-dev-v1"]) == 12
    queries = {row["query"] for cases in datasets.values() for row in cases}
    assert not queries.intersection(case.prompt for case in CASES if case.split == "test")
    profiles = sweep.load_profiles("model")
    assert profiles[0].id == "reference"
    assert profiles[0].append_fixed_policy is True
    assert all(not profile.append_fixed_policy for profile in profiles[1:])


def test_failed_request_counts_as_an_incorrect_trial():
    records = [
        {"dataset": "data", "profile": "weak", "round": 1, "case": "a",
         "status": "completed", "checks": {"decision": True}, "error": None},
        {"dataset": "data", "profile": "weak", "round": 2, "case": "a",
         "status": "failed", "checks": {}, "error": {"message": "Timeout"}},
    ]
    summary, = sweep.summarize(records)
    assert summary["fully_correct_rate"] == 0.5
    assert summary["decision_accuracy"] == 0.5
    assert summary["request_errors"] == 1


def test_resume_preserves_trials_and_rejects_changed_experiment(monkeypatch, tmp_path):
    measured = []
    monkeypatch.setenv("FOUNDRY_PROJECT_ENDPOINT", "https://project.example")
    monkeypatch.setenv("FOUNDRY_AGENT_MODEL", "model")
    monkeypatch.setattr(sweep, "AzureCliCredential", lambda **kwargs: nullcontext(None))

    @contextmanager
    def project(**kwargs):
        yield SimpleNamespace(
            deployments=SimpleNamespace(list=lambda: [SimpleNamespace(name="model")]),
            get_openai_client=lambda **kwargs: nullcontext(None),
        )

    def measure(case, profile, client):
        measured.append(case["id"])
        return {
            "status": "completed", "checks": {"decision": True}, "error": None,
            "payload": {}, "latency_ms": 1,
        }

    monkeypatch.setattr(sweep, "AIProjectClient", project)
    monkeypatch.setattr(sweep, "measure", measure)
    output = tmp_path / "sweep.json"
    state = sweep.execute(output, rounds=2, workers=2, profile_ids=["underspecified"])
    assert len(state["records"]) == 40
    assert state["holdout_used"] is False
    sweep.execute(output, rounds=2, workers=1, profile_ids=["underspecified"])
    assert len(measured) == 40
    with pytest.raises(ValueError, match="different prompts"):
        sweep.execute(output, rounds=3, workers=1, profile_ids=["underspecified"])
