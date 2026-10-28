"""Retry infrastructure failures with pacing; never retry incorrect business answers."""

import argparse
import hashlib
import json
import logging
import os
import time
from contextlib import contextmanager
from dataclasses import asdict
from datetime import UTC, datetime
from pathlib import Path

from azure.ai.projects import AIProjectClient
from azure.identity import AzureCliCredential

from scripts import run_prompt_sweep as sweep

TRANSIENT_ERRORS = {
    "RateLimitError", "APITimeoutError", "APIConnectionError",
    "ServiceRequestError", "ServiceResponseError",
}


def validate_source(state: dict):
    context = state["context"]
    if context["endpoint"] != os.environ.get("FOUNDRY_PROJECT_ENDPOINT"):
        raise ValueError("Recovery must use the same configured Foundry endpoint.")
    for key, path in (
        ("runtime_sha256", sweep.ROOT / "agent" / "main.py"),
        ("policy_sha256", sweep.ROOT / "agent" / "policy.py"),
        ("runner_sha256", Path(sweep.__file__)),
    ):
        if hashlib.sha256(path.read_bytes()).hexdigest() != context[key]:
            raise ValueError(f"Cannot mix measurements after changing {path.name}.")
    if context["datasets"] != sweep.load_datasets():
        raise ValueError("Development datasets changed since the original measurements.")
    current_profiles = {profile.id: asdict(profile) for profile in sweep.load_profiles(context["model"])}
    if any(current_profiles[profile["id"]] != profile for profile in context["profiles"]):
        raise ValueError("Prompt profiles changed since the original measurements.")
    if state["holdout_used"]:
        raise ValueError("This experiment must not use holdouts.")


@contextmanager
def live_client(context):
    logging.getLogger("azure").setLevel(logging.WARNING)
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpx2").setLevel(logging.WARNING)
    with AzureCliCredential(process_timeout=60) as credential, AIProjectClient(
        endpoint=context["endpoint"], credential=credential, allow_preview=True,
    ) as project, project.get_openai_client(max_retries=0, timeout=45) as client:
        yield sweep.PacedClient(client)


def recover(source: Path, output: Path) -> dict:
    if source.resolve() == output.resolve():
        raise ValueError("Keep the original sweep evidence; choose a separate recovery output.")
    original = json.loads(source.read_text(encoding="utf-8"))
    validate_source(original)
    source_hash = hashlib.sha256(source.read_bytes()).hexdigest()
    if output.exists():
        state = json.loads(output.read_text(encoding="utf-8"))
        if state["source_sha256"] != source_hash:
            raise ValueError("Recovery output belongs to a different source sweep.")
    else:
        state = {
            **original, "created_at": datetime.now(UTC).isoformat(),
            "source_sweep": str(source), "source_sha256": source_hash,
            "initial_request_failures": sum(
                row["status"] == "failed" for row in original["records"]
            ),
            "recovery_request_interval_seconds": 4,
        }
        state.pop("completed_at", None)
    output.parent.mkdir(parents=True, exist_ok=True)

    def save():
        state["summary"] = sweep.summarize(state["records"])
        state["updated_at"] = datetime.now(UTC).isoformat()
        temporary = output.with_suffix(output.suffix + ".tmp")
        temporary.write_text(json.dumps(state, indent=2) + "\n", encoding="utf-8")
        temporary.replace(output)

    save()
    context = state["context"]
    profiles = {row["id"]: sweep.Profile(**row) for row in context["profiles"]}
    datasets = {
        name: {row["id"]: row for row in rows} for name, rows in context["datasets"].items()
    }
    with live_client(context) as client:
        for record in state["records"]:
            if record["status"] != "failed":
                continue
            record.setdefault("initial_failure", {
                "error": record["error"], "latency_ms": record["latency_ms"],
            })
            attempts = record.setdefault("recovery_attempts", [])
            while (
                record["status"] == "failed" and record["error"]["type"] in TRANSIENT_ERRORS
                and len(attempts) < 3
            ):
                case = datasets[record["dataset"]][record["case"]]
                measured = sweep.measure(case, profiles[record["profile"]], client)
                attempts.append({**measured, "measured_at": datetime.now(UTC).isoformat()})
                record.update(measured)
                save()
                print(json.dumps({
                    "trial": record["trial_id"], "status": record["status"],
                    "attempt": len(attempts), "remaining_failed_trials": sum(
                        row["status"] == "failed" for row in state["records"]
                    ),
                }), flush=True)
                if (
                    record["status"] == "failed" and record["error"]["type"] in TRANSIENT_ERRORS
                    and len(attempts) < 3
                ):
                    time.sleep(60)
    state["completed_at"] = datetime.now(UTC).isoformat()
    save()
    print(json.dumps({"summary": state["summary"]}), flush=True)
    return state


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--source", type=Path,
        default=sweep.ROOT / "agent" / ".foundry" / "results" / "prompt-sweep-paced.json",
    )
    parser.add_argument(
        "--output", type=Path,
        default=sweep.ROOT / "agent" / ".foundry" / "results" / "prompt-sweep-paced-recovered.json",
    )
    args = parser.parse_args()
    recover(args.source, args.output)
