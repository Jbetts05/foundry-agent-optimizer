"""Compare synthetic weak prompts with the reference using live Foundry model calls."""

import argparse
import hashlib
import json
import logging
import os
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path
from threading import Lock

from azure.ai.projects import AIProjectClient
from azure.core.exceptions import AzureError
from azure.identity import AzureCliCredential
from openai import OpenAIError

from agent.evaluation.business_grader import score_case
from agent.main import run_agent
from agent.policy import calculate_offer, lookup_promotion_policy
from demo.cases import CASES

ROOT = Path(__file__).resolve().parents[1]
EVALUATION = ROOT / "agent" / "evaluation"
logger = logging.getLogger(__name__)


class PacedClient:
    def __init__(self, client, *, interval: float = 4):
        self.client = client
        self.interval = interval
        self.next_request = 0.0
        self.lock = Lock()
        self.responses = self

    def create(self, **kwargs):
        estimated_tokens = len(json.dumps(kwargs, ensure_ascii=False)) / 3
        estimated_tokens += kwargs.get("max_output_tokens", 0)
        spacing = max(self.interval, estimated_tokens * 60 / 40000)
        with self.lock:
            delay = self.next_request - time.monotonic()
            if delay > 0:
                time.sleep(delay)
            self.next_request = time.monotonic() + spacing
        return self.client.responses.create(**kwargs)


@dataclass(frozen=True)
class Profile:
    id: str
    instructions: str
    model: str
    append_fixed_policy: bool = False

    @property
    def source(self) -> str:
        return f"prompt-sweep:{self.id}"

    def compose_instructions(self) -> str:
        return self.instructions


def load_profiles(model: str) -> list[Profile]:
    baseline = ROOT / "agent" / ".agent_configs" / "baseline" / "instructions.md"
    profiles = [Profile(
        "reference", baseline.read_text(encoding="utf-8").strip(), model, True,
    )]
    starter = json.loads((EVALUATION / "starter-prompts.json").read_text(encoding="utf-8"))
    profiles.extend(Profile(row["id"], row["instructions"], model) for row in starter["profiles"])
    if len({profile.id for profile in profiles}) != len(profiles):
        raise ValueError("Prompt profile identifiers must be unique.")
    return profiles


def load_datasets() -> dict[str, list[dict]]:
    original = []
    for case in CASES:
        if case.split != "dev":
            continue
        policy = lookup_promotion_policy(case.product, case.region)
        expected = {
            "decision": case.expected,
            "discount_cap_percent": policy.get("discount_cap_percent"),
            "minimum_margin_percent": policy.get("minimum_margin_percent"),
        }
        if policy["status"] == "ok" and not policy["restricted"]:
            if case.price is not None and case.cost is not None:
                expected.update(calculate_offer(case.price, case.cost, case.discount))
            else:
                expected.update(offer_price=None, projected_margin_percent=None)
        original.append({
            "id": case.id, "query": case.prompt,
            "inputs": {
                "product_id": case.product, "region": case.region,
                "discount_percent": case.discount, "list_price": case.price, "unit_cost": case.cost,
            },
            "expected_result": expected,
        })
    challenge = [
        json.loads(line)
        for line in (EVALUATION / "challenge-development.jsonl").read_text(encoding="utf-8").splitlines()
    ]
    if not original or not challenge:
        raise ValueError("Both development datasets must contain cases.")
    return {"original-dev-v1": original, "challenge-dev-v1": challenge}


def measure(case: dict, profile: Profile, client) -> dict:
    started = time.perf_counter()
    try:
        payload = run_agent(
            case["query"], profile, client, append_fixed_policy=profile.append_fixed_policy,
        )
    except (AzureError, OpenAIError, ValueError, RuntimeError) as exc:
        logger.exception("Trial failed: profile=%s case=%s", profile.id, case["id"])
        return {
            "status": "failed", "checks": {}, "payload": None,
            "error": {"type": type(exc).__name__, "message": str(exc)},
            "latency_ms": round((time.perf_counter() - started) * 1000),
        }
    return {
        "status": "completed", "payload": payload, "error": None,
        "checks": score_case(case, payload),
        "latency_ms": round((time.perf_counter() - started) * 1000),
    }


def summarize(records: list[dict]) -> list[dict]:
    groups = sorted({(row["dataset"], row["profile"]) for row in records})
    summaries = []
    for dataset, profile in groups:
        rows = [row for row in records if (row["dataset"], row["profile"]) == (dataset, profile)]
        completed = [row for row in rows if row["status"] == "completed"]
        correct = [row for row in completed if all(row["checks"].values())]
        summaries.append({
            "dataset": dataset, "profile": profile, "trials": len(rows),
            "request_errors": len(rows) - len(completed),
            "decisions_correct": sum(row["checks"]["decision"] for row in completed),
            "fully_correct": len(correct),
            "decision_accuracy": sum(row["checks"]["decision"] for row in completed) / len(rows),
            "fully_correct_rate": len(correct) / len(rows),
            "failures": [
                {"round": row["round"], "case": row["case"], "error": row["error"],
                 "failed_checks": [key for key, passed in row["checks"].items() if not passed]}
                for row in rows if row not in correct
            ],
        })
    return summaries


def execute(
    output: Path, *, rounds: int, workers: int,
    profile_ids: list[str] | None = None,
) -> dict:
    endpoint = os.environ.get("FOUNDRY_PROJECT_ENDPOINT")
    if not endpoint:
        raise RuntimeError("Set FOUNDRY_PROJECT_ENDPOINT before running live experiments.")
    model = os.environ.get("FOUNDRY_AGENT_MODEL", "gpt-4.1-mini")
    profiles = load_profiles(model)
    if profile_ids:
        unknown = set(profile_ids) - {profile.id for profile in profiles}
        if unknown:
            raise ValueError(f"Unknown prompt profiles: {sorted(unknown)}")
        profiles = [profile for profile in profiles if profile.id in profile_ids]
    datasets = load_datasets()
    context = {
        "endpoint": endpoint, "model": model, "profiles": [asdict(profile) for profile in profiles],
        "datasets": datasets, "rounds": rounds,
        "runtime_sha256": hashlib.sha256((ROOT / "agent" / "main.py").read_bytes()).hexdigest(),
        "policy_sha256": hashlib.sha256((ROOT / "agent" / "policy.py").read_bytes()).hexdigest(),
        "runner_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
        "request_pacing": {"minimum_interval_seconds": 4, "estimated_tokens_per_minute": 40000},
    }
    fingerprint = hashlib.sha256(json.dumps(context, sort_keys=True).encode()).hexdigest()
    if output.exists():
        state = json.loads(output.read_text(encoding="utf-8"))
        if state["fingerprint"] != fingerprint:
            raise ValueError("Existing sweep state has different prompts, datasets, rounds or code.")
    else:
        state = {
            "fingerprint": fingerprint, "context": context, "records": [],
            "created_at": datetime.now(UTC).isoformat(), "holdout_used": False,
            "execution": "Local agent code with live Foundry model calls; not hosted version scores.",
        }
    output.parent.mkdir(parents=True, exist_ok=True)

    def save():
        state["summary"] = summarize(state["records"])
        state["updated_at"] = datetime.now(UTC).isoformat()
        temporary = output.with_suffix(output.suffix + ".tmp")
        temporary.write_text(json.dumps(state, indent=2) + "\n", encoding="utf-8")
        temporary.replace(output)

    save()
    finished = {row["trial_id"] for row in state["records"]}
    logging.getLogger("azure").setLevel(logging.WARNING)
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpx2").setLevel(logging.WARNING)
    with AzureCliCredential(process_timeout=60) as credential, AIProjectClient(
        endpoint=endpoint, credential=credential, allow_preview=True,
    ) as project, project.get_openai_client(max_retries=0, timeout=45) as client:
        paced_client = PacedClient(client)
        if model not in {deployment.name for deployment in project.deployments.list()}:
            raise RuntimeError(f"The required model deployment {model!r} does not exist.")
        for iteration in range(1, rounds + 1):
            with ThreadPoolExecutor(max_workers=workers) as pool:
                pending = {}
                for dataset, cases in datasets.items():
                    for profile in profiles:
                        for case in cases:
                            trial_id = f"{iteration}:{dataset}:{profile.id}:{case['id']}"
                            if trial_id in finished:
                                continue
                            future = pool.submit(measure, case, profile, paced_client)
                            pending[future] = {
                                "trial_id": trial_id, "round": iteration, "dataset": dataset,
                                "profile": profile.id, "case": case["id"],
                            }
                for future in as_completed(pending):
                    state["records"].append({**pending[future], **future.result()})
                    save()
                    if len(state["records"]) % 10 == 0:
                        print(json.dumps({"completed_trials": len(state["records"]),
                                          "round": iteration}), flush=True)
            print(json.dumps({"round": iteration, "summary": state["summary"]}), flush=True)
    state["completed_at"] = datetime.now(UTC).isoformat()
    save()
    return state


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--output", type=Path,
        default=ROOT / "agent" / ".foundry" / "results" / "prompt-sweep-paced.json",
    )
    parser.add_argument("--rounds", type=int, choices=(1, 2, 3), default=2)
    parser.add_argument("--workers", type=int, choices=(1, 2), default=2)
    parser.add_argument("--profiles", nargs="+")
    args = parser.parse_args()
    execute(args.output, rounds=args.rounds, workers=args.workers, profile_ids=args.profiles)
