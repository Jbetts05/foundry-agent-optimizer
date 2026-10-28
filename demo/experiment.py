"""Shared exact grading and development/holdout boundaries for the hosted demo."""

import json
from pathlib import Path

from azure.ai.projects import models

from agent.evaluation.business_grader import score_case

ROOT = Path(__file__).resolve().parents[1] / "agent"
EVALUATION = ROOT / "evaluation"
OPTIMIZER_JOB = "opt_d7ab515d2f484fb082b6bd5c5a922046"


def cases(split: str) -> list[dict]:
    if split not in {"development", "holdout"}:
        raise ValueError("Choose the development or holdout split.")
    path = EVALUATION / f"challenge-{split}.jsonl"
    return [
        {**json.loads(line), "split": split}
        for line in path.read_text(encoding="utf-8").splitlines() if line.strip()
    ]


def public_cases() -> list[dict]:
    return [
        {
            "id": case["id"], "title": (
                case["id"].removeprefix("challenge-").removeprefix("dev-")
                .removeprefix("test-").replace("-", " ").capitalize()
            ),
            "prompt": case["query"], "expected": case["expected_result"]["decision"],
            "split": case["split"],
        }
        for split in ("development", "holdout") for case in cases(split)
    ]


def checks(case: dict, payload: dict) -> list[dict]:
    return [
        {"label": key.replace("_", " ").capitalize(), "passed": passed}
        for key, passed in score_case(case, payload).items()
    ]


def training_cases() -> list[models.AgentOptimizationTargetCompletionTestCase]:
    return [
        models.AgentOptimizationTargetCompletionTestCase(
            query=case["query"], ground_truth=json.dumps(case),
        )
        for case in cases("development")
    ]


def evaluators() -> list[models.AgentOptimizationEvaluator]:
    return [models.AgentOptimizationEvaluator(
        name="builtin.python_grader", version="1", initialization_parameters={
            "type": "python", "name": "promotion_business_correctness",
            "source": (EVALUATION / "business_grader.py").read_text(encoding="utf-8"),
            "pass_threshold": 1.0, "image_tag": "2025-05-08",
        },
    )]
