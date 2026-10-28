"""Package shared source for Static Web Apps without maintaining duplicate code."""

import shutil
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def stage():
    for package, filenames in {
        "demo": ["__init__.py", "http_api.py", "cases.py", "foundry.py", "experiment.py"],
        "agent": ["__init__.py", "policy.py"],
    }.items():
        destination = ROOT / "api" / package
        destination.mkdir(parents=True, exist_ok=True)
        for filename in filenames:
            shutil.copy2(ROOT / package / filename, destination / filename)
    baseline = Path("agent") / ".agent_configs" / "baseline"
    shutil.copytree(ROOT / baseline, ROOT / "api" / baseline, dirs_exist_ok=True)
    experiment = Path("agent") / ".experiment_configs" / "baseline"
    shutil.copytree(ROOT / experiment, ROOT / "api" / experiment, dirs_exist_ok=True)
    evaluation = ROOT / "api" / "agent" / "evaluation"
    evaluation.mkdir(parents=True, exist_ok=True)
    for filename in (
        "business_grader.py", "challenge-development.jsonl", "challenge-holdout.jsonl",
        "reviewed-candidate.json",
    ):
        shutil.copy2(ROOT / "agent" / "evaluation" / filename, evaluation / filename)


if __name__ == "__main__":
    stage()
