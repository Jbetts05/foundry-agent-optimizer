import json
from pathlib import Path

import pytest

from agent.policy import Decision, calculate_offer, lookup_promotion_policy
from demo.cases import CASES
from scripts.run_challenge_optimization import challenge_test_cases

ROOT = Path(__file__).resolve().parents[1]
EVALUATION = ROOT / "agent" / "evaluation"


def load(split):
    path = EVALUATION / f"challenge-{split}.jsonl"
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]


DEVELOPMENT = load("development")
HOLDOUT = load("holdout")


def test_challenge_splits_are_separate_and_do_not_relabel_the_original_cases():
    assert len(DEVELOPMENT) == 12
    assert len(HOLDOUT) == 8
    all_cases = DEVELOPMENT + HOLDOUT
    assert len({case["id"] for case in all_cases}) == len(all_cases)
    assert len({case["query"] for case in all_cases}) == len(all_cases)
    assert not {case["query"] for case in all_cases}.intersection(case.prompt for case in CASES)
    original_inputs = {
        (case.product, case.region, case.discount, case.price, case.cost) for case in CASES
    }
    assert not {
        tuple(case["inputs"][key] for key in (
            "product_id", "region", "discount_percent", "list_price", "unit_cost",
        ))
        for case in all_cases
    }.intersection(original_inputs)


@pytest.mark.parametrize("case", DEVELOPMENT + HOLDOUT, ids=lambda case: case["id"])
def test_challenge_answers_match_the_unchanged_business_contract(case):
    inputs = case["inputs"]
    policy = lookup_promotion_policy(inputs["product_id"], inputs["region"])
    expected = case["expected_result"]
    Decision.model_validate({**expected, "reason": case["ground_truth"]})
    assert case["ground_truth"] and case["category"] and case["query"]
    calculations = {"offer_price": None, "projected_margin_percent": None}
    if policy["status"] == "unknown":
        decision = "clarify"
        cap = floor = None
    else:
        cap = policy["discount_cap_percent"]
        floor = policy["minimum_margin_percent"]
        valid = (
            inputs["list_price"] is not None and inputs["list_price"] > 0
            and inputs["unit_cost"] is not None and inputs["unit_cost"] >= 0
            and 0 <= inputs["discount_percent"] <= 99
        )
        if valid and not policy["restricted"]:
            calculations = calculate_offer(
                inputs["list_price"], inputs["unit_cost"], inputs["discount_percent"],
            )
        if policy["restricted"]:
            decision = "reject"
        elif inputs["discount_percent"] > cap:
            decision = "escalate"
        elif not valid:
            decision = "clarify"
        else:
            decision = "reject" if calculations["projected_margin_percent"] < floor else "approve"
    assert expected == {
        "decision": decision, "discount_cap_percent": cap,
        "minimum_margin_percent": floor, **calculations,
    }


def test_missing_cost_and_confirmed_zero_cost_have_different_ground_truth():
    missing = next(case for case in DEVELOPMENT if case["id"] == "challenge-dev-missing-cost")
    zero = next(case for case in DEVELOPMENT if case["id"] == "challenge-dev-explicit-zero")
    assert missing["inputs"]["unit_cost"] is None
    assert missing["expected_result"]["decision"] == "clarify"
    assert zero["inputs"]["unit_cost"] == 0
    assert zero["expected_result"]["decision"] == "approve"


def test_optimizer_challenges_use_only_development_queries_and_include_tool_rubrics():
    cases = challenge_test_cases()
    assert {case.query for case in cases} == {row["query"] for row in DEVELOPMENT}
    assert not {case.query for case in cases}.intersection(row["query"] for row in HOLDOUT)
    assert len(cases) == 12
    for case in cases:
        assert "Inspect trace.tool_calls" in case.ground_truth
        assert "Expected structured fields:" in case.ground_truth


def test_rounding_challenge_really_changes_the_decision_if_order_is_wrong():
    case = next(case for case in DEVELOPMENT if case["id"] == "challenge-dev-half-up")
    assert case["expected_result"]["decision"] == "approve"
    assert case["expected_result"]["projected_margin_percent"] == 20
    unrounded_offer = 1.25 * (1 - 10 / 100)
    wrong_margin = (unrounded_offer - 0.904) / unrounded_offer * 100
    assert wrong_margin < 20


def test_starter_prompts_are_disclosed_separate_hypotheses_not_public_v1():
    catalog = json.loads((EVALUATION / "starter-prompts.json").read_text(encoding="utf-8"))
    assert catalog["deployed"] is True
    assert catalog["deployment"] == {
        "profile": "underspecified", "azd_service": "promotion-policy-experiment",
    }
    assert "not the original hosted V1" in catalog["status"]
    assert catalog["measurements"]["execution"] == "local-agent-code-live-model"
    assert len(catalog["profiles"]) == 3
    assert len({profile["id"] for profile in catalog["profiles"]}) == 3
    baseline = (ROOT / "agent" / ".agent_configs" / "baseline" / "instructions.md").read_text()
    for profile in catalog["profiles"]:
        assert profile["instructions"].strip() != baseline.strip()
        assert profile["deliberate_weakness"] and profile["hypothesis"]
    assert set(catalog["keep_fixed"]) == {"model", "business policy", "tools", "output schema"}
