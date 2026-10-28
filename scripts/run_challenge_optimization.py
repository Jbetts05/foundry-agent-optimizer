"""Run the real optimizer on development challenges without applying any candidate."""

import argparse
import hashlib
import json
import time
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

from azure.ai.projects import models
from azure.identity import AzureCliCredential

from demo.foundry import FoundryGateway

ROOT = Path(__file__).resolve().parents[1]
DATASET = ROOT / "agent" / "evaluation" / "challenge-development.jsonl"


def challenge_test_cases() -> list[models.AgentOptimizationTargetCompletionTestCase]:
    rows = [json.loads(line) for line in DATASET.read_text(encoding="utf-8").splitlines()]
    if not rows:
        raise ValueError("The development challenge dataset is empty.")
    cases = []
    for row in rows:
        if not row["query"].strip() or not row["ground_truth"].strip():
            raise ValueError(f"Challenge {row['id']} needs a query and grading rubric.")
        cases.append(models.AgentOptimizationTargetCompletionTestCase(
            query=row["query"],
            ground_truth=(
                row["ground_truth"]
                + "\nExpected structured fields: "
                + json.dumps(row["expected_result"], sort_keys=True)
                + "\nInspect trace.tool_calls as well as the final result. "
                "Do not accept invented calculator inputs just because the final decision is correct."
            ),
        ))
    return cases


def run(state_path: Path, wait_seconds: int):
    gateway = FoundryGateway(credential=AzureCliCredential(process_timeout=60))
    if not gateway.configured:
        raise RuntimeError("Configure the project endpoint and pinned baseline version first.")
    cases = challenge_test_cases()
    binding = {
        "endpoint": gateway.endpoint, "agent": gateway.agent_name,
        "baseline_version": gateway.baseline_version,
        "dataset_sha256": hashlib.sha256(DATASET.read_bytes()).hexdigest(),
        "agent_model": gateway.agent_model, "eval_model": gateway.eval_model,
        "optimization_model": gateway.optimization_model,
    }
    if state_path.exists():
        state = json.loads(state_path.read_text(encoding="utf-8"))
        if state["binding"] != binding:
            raise ValueError("This state file belongs to different experiment inputs.")
    else:
        state = {
            "operation_id": str(uuid4()), "binding": binding,
            "created_at": datetime.now(UTC).isoformat(), "development_cases": len(cases),
            "holdout_used": False, "starter_profile": "original-deployed-v1",
        }
    state_path.parent.mkdir(parents=True, exist_ok=True)

    def save():
        state_path.write_text(json.dumps(state, indent=2) + "\n", encoding="utf-8")

    save()
    if not state.get("job_id"):
        with gateway.project() as project:
            deployments = {deployment.name for deployment in project.deployments.list()}
            missing = {gateway.agent_model, gateway.eval_model, gateway.optimization_model} - deployments
            if missing:
                raise RuntimeError(f"Required model deployments are missing: {sorted(missing)}")
            baseline = project.agents.get_version(gateway.agent_name, gateway.baseline_version)
            if not isinstance(baseline.definition, models.HostedAgentDefinition):
                raise TypeError("The selected baseline is not a hosted agent.")
        state["job_id"] = gateway.start_optimization(state["operation_id"], test_cases=cases)
        save()
        print(json.dumps({"submitted_job": state["job_id"], "development_cases": len(cases)}),
              flush=True)

    deadline = time.monotonic() + wait_seconds
    previous = None
    while True:
        record = gateway.optimization(state["job_id"])
        state["result"] = record
        state["last_checked_at"] = datetime.now(UTC).isoformat()
        save()
        progress = {
            "job_id": record["id"], "status": record["status"],
            "baseline_score": record.get("baseline_score"), "best_score": record.get("best_score"),
            "candidates": [
                {key: candidate[key] for key in ("id", "name", "status", "score", "error")}
                for candidate in record["candidates"]
            ],
            "warnings": record["warnings"], "error": record.get("error"),
        }
        if progress != previous:
            print(json.dumps(progress), flush=True)
            previous = progress
        if record["status"] not in {"queued", "in_progress"}:
            return record
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError(f"Optimization is still running; resume using {state_path}.")
        time.sleep(min(30, remaining))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--state", type=Path, default=ROOT / ".azure" / "challenge-optimization.json")
    parser.add_argument("--wait-seconds", type=int, default=1800)
    args = parser.parse_args()
    if args.wait_seconds < 0:
        parser.error("--wait-seconds must be nonnegative.")
    run(args.state, args.wait_seconds)
