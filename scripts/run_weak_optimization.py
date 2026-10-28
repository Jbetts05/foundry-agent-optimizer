"""Calibrate exact grading, then optimize the isolated hosted weak baseline."""

import argparse
import hashlib
import json
import os
import time
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

from azure.ai.projects import AIProjectClient, models
from azure.identity import AzureCliCredential

from agent.evaluation.business_grader import grade, score_case
from demo.foundry import FoundryGateway

ROOT = Path(__file__).resolve().parents[1]
BASELINE = ROOT / "agent" / ".experiment_configs" / "baseline"
GRADER = ROOT / "agent" / "evaluation" / "business_grader.py"
DATASET = ROOT / "agent" / "evaluation" / "challenge-development.jsonl"
CONFIRMATION = ROOT / "agent" / ".foundry" / "results" / "prompt-sweep-confirmation.json"
AGENT = "promotion-policy-experiment"
GRADER_NAME = "promotion_business_correctness"


def digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def save(path: Path, state: dict):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(state, indent=2) + "\n", encoding="utf-8")


def grader_parameters() -> dict:
    return {
        "type": "python", "name": GRADER_NAME, "source": GRADER.read_text(encoding="utf-8"),
        "pass_threshold": 1.0, "image_tag": "2025-05-08",
    }


def grader_digest() -> str:
    return hashlib.sha256(json.dumps(grader_parameters(), sort_keys=True).encode()).hexdigest()


def calibration_binding(endpoint: str) -> dict:
    return {
        "endpoint": endpoint, "grader_sha256": digest(GRADER),
        "grader_parameters_sha256": grader_digest(),
        "dataset_sha256": digest(DATASET), "evidence_sha256": digest(CONFIRMATION),
        "sample_format": "foundry-item-v1",
    }


def development_cases() -> list[dict]:
    rows = [json.loads(line) for line in DATASET.read_text(encoding="utf-8").splitlines()]
    if len(rows) != 12 or len({row["id"] for row in rows}) != 12:
        raise ValueError("This experiment requires the twelve distinct reviewed development cases.")
    return rows


def calibration_content() -> list[dict]:
    cases = {case["id"]: case for case in development_cases()}
    evidence = json.loads(CONFIRMATION.read_text(encoding="utf-8"))
    content = []
    for row in evidence["records"]:
        if row["dataset"] != "challenge-dev-v1":
            continue
        if row["profile"] != "underspecified" or row["status"] != "completed":
            raise ValueError("Calibration requires completed selected-profile development evidence.")
        case = cases[row["case"]]
        expected_score = float(all(row["checks"].values()))
        sample = {"output_text": json.dumps(row["payload"])}
        item = {
            "case": case["id"], "query": case["query"],
            "ground_truth": json.dumps(case), "expected_score": expected_score,
            "response": sample["output_text"],
        }
        if score_case(case, row["payload"]) != row["checks"] or grade(sample, item) != expected_score:
            raise ValueError(f"The exact grader disagrees with preserved evidence: {case['id']}")
        content.append({"item": item})
    if len(content) != 12 or {row["item"]["expected_score"] for row in content} != {0.0, 1.0}:
        raise ValueError("Calibration needs all twelve development responses, with good and bad cases.")
    return content


def calibrate(state_path: Path, wait_seconds: int):
    content = calibration_content()
    binding = calibration_binding(os.environ["FOUNDRY_PROJECT_ENDPOINT"])
    state = json.loads(state_path.read_text(encoding="utf-8")) if state_path.exists() else {
        "binding": binding, "holdout_used": False,
    }
    if state["binding"] != binding:
        raise ValueError("Calibration inputs differ from the saved state.")
    with AIProjectClient(
        endpoint=binding["endpoint"], credential=AzureCliCredential(process_timeout=60),
        allow_preview=True,
    ) as project, project.get_openai_client() as client:
        if not state.get("eval_id"):
            evaluation = client.evals.create(
                name="Isolated promotion business grader calibration",
                data_source_config={
                    "type": "custom", "include_sample_schema": True,
                    "item_schema": {
                        "type": "object", "properties": {
                            "case": {"type": "string"}, "query": {"type": "string"},
                            "ground_truth": {"type": "string"}, "expected_score": {"type": "number"},
                            "response": {"type": "string"},
                        },
                        "required": ["case", "query", "ground_truth", "expected_score", "response"],
                    },
                },
                testing_criteria=[grader_parameters()],
            )
            state["eval_id"] = evaluation.id
            save(state_path, state)
        if not state.get("run_id"):
            run = client.evals.runs.create(
                eval_id=state["eval_id"], name="Twelve preserved development responses",
                data_source={"type": "jsonl", "source": {"type": "file_content", "content": content}},
            )
            state["run_id"] = run.id
            save(state_path, state)
        deadline = time.monotonic() + wait_seconds
        while True:
            run = client.evals.runs.retrieve(eval_id=state["eval_id"], run_id=state["run_id"])
            state["run"] = run.model_dump(mode="json")
            save(state_path, state)
            if run.status not in {"queued", "in_progress"}:
                break
            if time.monotonic() >= deadline:
                raise TimeoutError(f"Calibration is still running; resume using {state_path}.")
            time.sleep(min(10, max(0, deadline - time.monotonic())))
        if run.status != "completed":
            raise RuntimeError(f"Remote grader calibration failed: {state['run']}")
        if run.result_counts.errored != 0:
            raise ValueError("Calibration contains grading errors, not just incorrect answers.")
        outputs = list(client.evals.runs.output_items.list(
            eval_id=state["eval_id"], run_id=state["run_id"],
        ))
        state["outputs"] = [output.model_dump(mode="json") for output in outputs]
        save(state_path, state)
        expected = {row["item"]["case"]: row["item"]["expected_score"] for row in content}
        observed = {}
        for output in state["outputs"]:
            case = output["datasource_item"]["case"]
            results = [result for result in output["results"] if result["name"] == GRADER_NAME]
            if len(results) != 1:
                raise ValueError(f"Missing or ambiguous remote grade for {case}: {output}")
            observed[case] = results[0]["score"]
        if observed != expected or len(outputs) != len(content):
            raise ValueError(f"Remote grader differs from the known outcomes: {observed}")
        state["verified"] = True
        save(state_path, state)
        print(json.dumps({
            "calibration": "verified", "cases": len(expected),
            "correct": sum(expected.values()), "incorrect": len(expected) - sum(expected.values()),
            "eval_id": state["eval_id"], "run_id": state["run_id"],
        }), flush=True)


def optimize(
    state_path: Path, calibration_path: Path, wait_seconds: int, *, retry_of: str | None = None,
):
    calibration = json.loads(calibration_path.read_text(encoding="utf-8"))
    gateway = FoundryGateway(
        credential=AzureCliCredential(process_timeout=60), baseline_directory=BASELINE,
    )
    if not gateway.configured or gateway.agent_name != AGENT:
        raise ValueError("Configure the isolated experiment agent and its actual pinned version.")
    expected_calibration = calibration_binding(gateway.endpoint)
    if not calibration.get("verified") or calibration["binding"] != expected_calibration:
        raise ValueError("This exact grader must pass remote calibration before optimization.")
    cases = development_cases()
    binding = {
        **expected_calibration, "agent": gateway.agent_name,
        "baseline_version": gateway.baseline_version, "instructions_sha256": digest(
            BASELINE / "instructions.md",
        ),
        "agent_model": gateway.agent_model, "optimization_model": gateway.optimization_model,
        "runtime_sha256": digest(ROOT / "agent" / "main.py"),
        "entrypoint_sha256": digest(ROOT / "agent" / "experiment.py"),
        "policy_sha256": digest(ROOT / "agent" / "policy.py"),
    }
    state = json.loads(state_path.read_text(encoding="utf-8")) if state_path.exists() else {
        "binding": binding, "operation_id": str(uuid4()), "holdout_used": False,
        "starter_profile": "underspecified", "development_cases": len(cases),
        "created_at": datetime.now(UTC).isoformat(),
        "retry_of": retry_of,
    }
    if state["binding"] != binding:
        raise ValueError("Saved optimizer state belongs to different experiment inputs.")
    save(state_path, state)
    if not state.get("job_id"):
        if state.get("submission_started_at"):
            raise RuntimeError("Submission outcome is uncertain; reconcile it before resubmitting.")
        if "smoke" not in state:
            payload = gateway.invoke(cases[0]["query"], gateway.baseline_version)
            trace = payload["trace"]
            instructions = (BASELINE / "instructions.md").read_text(encoding="utf-8").strip()
            if trace["policy_in_prompt"] is not False or trace["instructions"].strip() != instructions:
                raise ValueError("The hosted agent is not running the approved weak baseline.")
            if trace["model"] != gateway.agent_model:
                raise ValueError("The hosted experiment model differs from the measured model.")
            state["smoke"] = {"payload": payload, "checks": score_case(cases[0], payload)}
            save(state_path, state)
        with gateway.project() as project:
            baseline = project.agents.get_version(gateway.agent_name, gateway.baseline_version)
            if not isinstance(baseline.definition, models.HostedAgentDefinition):
                raise TypeError("The isolated baseline is not a hosted agent.")
        state["submission_started_at"] = datetime.now(UTC).isoformat()
        save(state_path, state)
        state["job_id"] = gateway.start_optimization(
            state["operation_id"],
            test_cases=[
                models.AgentOptimizationTargetCompletionTestCase(
                    query=case["query"], ground_truth=json.dumps(case),
                )
                for case in cases
            ],
            evaluators=[models.AgentOptimizationEvaluator(
                name="builtin.python_grader", version="1",
                initialization_parameters=grader_parameters(),
            )],
        )
        save(state_path, state)
        print(json.dumps({"submitted_job": state["job_id"], "agent": AGENT}), flush=True)
    return monitor(gateway, state_path, state, wait_seconds)


def status(state_path: Path, wait_seconds: int):
    state = json.loads(state_path.read_text(encoding="utf-8"))
    gateway = FoundryGateway(
        credential=AzureCliCredential(process_timeout=60), baseline_directory=BASELINE,
    )
    binding = state["binding"]
    if (
        not gateway.configured or gateway.agent_name != AGENT
        or binding["endpoint"] != gateway.endpoint or binding["agent"] != gateway.agent_name
        or binding["baseline_version"] != gateway.baseline_version
    ):
        raise ValueError("The saved job does not match the configured isolated agent baseline.")
    if not state.get("job_id"):
        raise ValueError("There is no submitted job to monitor.")
    return monitor(gateway, state_path, state, wait_seconds)


def monitor(gateway, state_path: Path, state: dict, wait_seconds: int):
    deadline = time.monotonic() + wait_seconds
    previous = None
    while True:
        record = gateway.optimization(state["job_id"])
        state["result"] = record
        state["last_checked_at"] = datetime.now(UTC).isoformat()
        save(state_path, state)
        progress = {**record, "baseline_instructions": None}
        if progress != previous:
            print(json.dumps(progress), flush=True)
            previous = progress
        if record["status"] not in {"queued", "in_progress"}:
            if record["status"] != "succeeded":
                raise RuntimeError(
                    f"Optimizer ended with status {record['status']}: {record.get('error')}",
                )
            return record
        if time.monotonic() >= deadline:
            raise TimeoutError(f"Optimization is still running; resume using {state_path}.")
        time.sleep(min(30, max(0, deadline - time.monotonic())))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=["calibrate", "optimize", "status"])
    parser.add_argument("--state", type=Path)
    parser.add_argument(
        "--calibration", type=Path,
        default=ROOT / ".azure" / "weak-grader-calibration-current.json",
    )
    parser.add_argument("--retry-of", help="Record the failed job ID for an explicitly approved retry.")
    parser.add_argument("--wait-seconds", type=int, default=1800)
    args = parser.parse_args()
    if args.wait_seconds < 0:
        parser.error("--wait-seconds must be nonnegative.")
    if args.action == "calibrate":
        calibrate(args.state or args.calibration, args.wait_seconds)
    elif args.action == "optimize":
        optimize(args.state or ROOT / ".azure" / "weak-optimization-pinned-image.json",
                 args.calibration, args.wait_seconds, retry_of=args.retry_of)
    else:
        status(args.state or ROOT / ".azure" / "weak-optimization-pinned-image.json",
               args.wait_seconds)
