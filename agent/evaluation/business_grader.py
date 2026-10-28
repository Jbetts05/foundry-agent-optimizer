"""Self-contained source for Foundry's built-in Python grader."""

import json
import math


def equal(actual, expected) -> bool:
    if isinstance(expected, (float, int)):
        return (
            isinstance(actual, (float, int)) and not isinstance(actual, bool)
            and math.isfinite(actual) and math.isclose(actual, expected, rel_tol=0, abs_tol=1e-9)
        )
    return actual == expected


def score_case(case: dict, payload: dict) -> dict[str, bool]:
    result = payload["result"]
    calls = payload["trace"]["tool_calls"]
    inputs = case["inputs"]
    expected = case["expected_result"]
    checks = {key: equal(result[key], value) for key, value in expected.items()}
    checks["policy_lookup"] = any(
        call["name"] == "lookup_promotion_policy"
        and call["arguments"].get("product_id", "").upper() == inputs["product_id"].upper()
        and call["arguments"].get("region", "").upper() == inputs["region"].upper()
        for call in calls
    )
    calculations = [call for call in calls if call["name"] == "calculate_offer"]
    keys = ("list_price", "unit_cost", "discount_percent")
    checks["authoritative_calculator_inputs"] = all(
        all(
            inputs[key] is not None and key in call["arguments"]
            and equal(call["arguments"][key], inputs[key])
            for key in keys
        )
        for call in calculations
    )
    if expected.get("offer_price") is not None and expected.get("projected_margin_percent") is not None:
        checks["calculator_executed"] = bool(calculations)
    return checks


def grade(sample: dict, item: dict) -> float:
    case = json.loads(item["ground_truth"])
    if not isinstance(case.get("inputs"), dict) or not isinstance(case.get("expected_result"), dict):
        raise TypeError("Ground truth must contain canonical inputs and expected result fields.")
    if "response" in item:
        output_text = item["response"]
    elif "sample" in item:
        output_text = item["sample"]["output_text"]
    else:
        output_text = sample["output_text"]
    try:
        payload = json.loads(output_text)
        checks = score_case(case, payload)
    except (json.JSONDecodeError, KeyError, TypeError, AttributeError) as exc:
        print(f"Business grader rejected malformed agent output: {exc}")
        return 0.0
    failed = [key for key, passed in checks.items() if not passed]
    if failed:
        print("Failed business checks: " + ", ".join(failed))
    return float(not failed)
