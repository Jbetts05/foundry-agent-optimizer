import pytest

from agent.policy import Decision, calculate_offer, lookup_promotion_policy
from demo.cases import CASES


def test_margin_uses_revenue_not_cost():
    assert calculate_offer(100, 64, 20) == {
        "offer_price": 80.0, "projected_margin_percent": 20.0,
    }


def test_german_cap_is_lower():
    assert lookup_promotion_policy("LAPTOP", "DE")["discount_cap_percent"] == 15


@pytest.mark.parametrize("args", [(0, 5, 10), (100, -1, 10), (100, 5, 100), (float("nan"), 1, 0)])
def test_invalid_calculation_is_explicit(args):
    with pytest.raises(ValueError):
        calculate_offer(*args)


def test_unknown_product_is_not_a_default_policy():
    assert lookup_promotion_policy("TABLET", "UK")["status"] == "unknown"


def test_datasets_do_not_overlap():
    dev = {case.prompt for case in CASES if case.split == "dev"}
    test = {case.prompt for case in CASES if case.split == "test"}
    assert dev and test and not dev.intersection(test)


@pytest.mark.parametrize("case", CASES, ids=lambda case: case.id)
def test_canned_expectations_match_frozen_policy(case):
    policy = lookup_promotion_policy(case.product, case.region)
    if policy["status"] == "unknown":
        expected = "clarify"
    elif policy["restricted"]:
        expected = "reject"
    elif case.discount > policy["discount_cap_percent"]:
        expected = "escalate"
    elif case.price is None or case.cost is None:
        expected = "clarify"
    else:
        margin = calculate_offer(case.price, case.cost, case.discount)["projected_margin_percent"]
        expected = "reject" if margin < policy["minimum_margin_percent"] else "approve"
    assert case.expected == expected


def test_schema_rejects_made_up_decision():
    with pytest.raises(ValueError):
        Decision(
            decision="maybe", reason="Unknown", discount_cap_percent=None,
            minimum_margin_percent=None, offer_price=None, projected_margin_percent=None,
        )
