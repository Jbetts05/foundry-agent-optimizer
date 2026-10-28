"""Export the actual optimizer winner with only the reviewed DE tool-semantics repair."""

import hashlib
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
JOB = ROOT / ".azure" / "weak-optimization-pinned-image.json"
DESTINATION = ROOT / "agent" / ".reviewed_configs" / "baseline"
REPLACEMENTS = {
    "4. In region DE, reduce the looked-up discount cap by 5 percentage points yourself. "
    "This adjusted value is the applicable cap to report and enforce.":
    "4. The lookup tool already applies the regional adjustment, including the 5-point "
    "reduction for DE. Use its returned discount_cap_percent unchanged; never subtract it again.",
    "- In DE, report the reduced cap after subtracting 5 points.":
    "- In DE, report the region-adjusted cap returned by the lookup tool without subtracting again.",
    '- "Requested discount 12% exceeds DE cap 5%"':
    '- "Requested discount 12% exceeds the returned DE cap 10%"',
}


def prepare():
    state = json.loads(JOB.read_text(encoding="utf-8"))
    result = state["result"]
    candidate = next(row for row in result["candidates"] if row["id"] == result["best_id"])
    if (
        result["status"] != "succeeded" or candidate["status"] != "completed"
        or candidate["id"] == result["baseline_id"] or not candidate["instructions"]
    ):
        raise ValueError("A completed non-baseline optimizer winner is required.")
    original = candidate["instructions"]
    reviewed = original
    for before, after in REPLACEMENTS.items():
        if reviewed.count(before) != 1:
            raise ValueError(f"The expected reviewed instruction was not unique: {before}")
        reviewed = reviewed.replace(before, after, 1)
    provenance = {
        "job_id": result["id"], "candidate_id": candidate["id"],
        "baseline_score": result["baseline_score"], "optimizer_score": candidate["score"],
        "original_instructions_sha256": hashlib.sha256(original.encode()).hexdigest(),
        "reviewed_instructions_sha256": hashlib.sha256(reviewed.encode()).hexdigest(),
        "manual_changes": [
            {"before": before, "after": after} for before, after in REPLACEMENTS.items()
        ],
        "holdout_used_for_repair": False,
    }
    DESTINATION.mkdir(parents=True, exist_ok=True)
    instructions = DESTINATION / "instructions.md"
    if instructions.exists() and instructions.read_text(encoding="utf-8").strip() != reviewed:
        raise ValueError("Refusing to overwrite a different reviewed configuration.")
    instructions.write_text(reviewed + "\n", encoding="utf-8")
    (DESTINATION / "metadata.yaml").write_text(
        "model: gpt-4.1-mini\ninstruction_file: instructions.md\n", encoding="utf-8",
    )
    (ROOT / "agent" / "evaluation" / "reviewed-candidate.json").write_text(
        json.dumps(provenance, indent=2) + "\n", encoding="utf-8",
    )
    print(json.dumps(provenance, indent=2))


if __name__ == "__main__":
    prepare()
