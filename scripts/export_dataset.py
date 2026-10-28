"""Export the selected development cases for the optional azd evaluation workflow."""

import json
from pathlib import Path

from demo.cases import CASES

destination = Path(__file__).resolve().parents[1] / "agent" / "evaluation" / "development.jsonl"
destination.parent.mkdir(parents=True, exist_ok=True)
rows = [
    json.dumps({
        "query": case.prompt,
        "ground_truth": f"The required decision is {case.expected}. "
        "Apply Contoso policy faithfully, use policy lookup and calculate when possible. "
        "Never invent missing information.",
    })
    for case in CASES if case.split == "dev"
]
destination.write_text("\n".join(rows) + "\n", encoding="utf-8")
