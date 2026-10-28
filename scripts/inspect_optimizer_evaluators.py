"""Cache the existing Foundry evaluator contracts without creating or running evaluations."""

import json
import os
from pathlib import Path

from azure.ai.projects import AIProjectClient
from azure.identity import AzureCliCredential


def main():
    endpoint = os.environ["FOUNDRY_PROJECT_ENDPOINT"]
    with AzureCliCredential(process_timeout=60) as credential, AIProjectClient(
        endpoint=endpoint, credential=credential, allow_preview=True,
    ) as project:
        evaluators = list(project.evaluators.list(type="all", limit=100))
    output = Path(__file__).resolve().parents[1] / ".azure" / "optimizer-evaluators.json"
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps([item.as_dict() for item in evaluators], indent=2), encoding="utf-8")
    for evaluator in evaluators:
        print(json.dumps({
            "name": evaluator.name, "version": evaluator.version,
            "type": evaluator.definition.type,
            "data_schema": evaluator.definition.data_schema,
            "init_parameters": evaluator.definition.init_parameters,
        }))


if __name__ == "__main__":
    main()
