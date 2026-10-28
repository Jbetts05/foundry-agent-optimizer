import hashlib
import json
from contextlib import nullcontext
from dataclasses import asdict
from pathlib import Path
from types import SimpleNamespace

import pytest

from scripts import recover_prompt_sweep as recovery
from scripts import run_prompt_sweep as sweep


def test_pacing_preserves_requests_and_enforces_spacing(monkeypatch):
    clock = SimpleNamespace(now=100.0, calls=[])
    monkeypatch.setattr(sweep.time, "monotonic", lambda: clock.now)
    monkeypatch.setattr(sweep.time, "sleep", lambda delay: setattr(clock, "now", clock.now + delay))

    def create(**kwargs):
        clock.calls.append((clock.now, kwargs))
        clock.now += 1
        return "response"

    client = sweep.PacedClient(SimpleNamespace(responses=SimpleNamespace(create=create)))
    for _ in range(3):
        assert client.responses.create(model="model", input="Request") == "response"
    assert [when for when, _ in clock.calls] == [100, 104, 108]
    assert all(kwargs == {"model": "model", "input": "Request"} for _, kwargs in clock.calls)


def test_larger_context_and_output_budget_increase_request_spacing(monkeypatch):
    clock = SimpleNamespace(now=100.0, calls=[])
    monkeypatch.setattr(sweep.time, "monotonic", lambda: clock.now)
    monkeypatch.setattr(sweep.time, "sleep", lambda delay: setattr(clock, "now", clock.now + delay))

    def create(**kwargs):
        clock.calls.append(clock.now)
        return "response"

    client = sweep.PacedClient(SimpleNamespace(responses=SimpleNamespace(create=create)))
    arguments = {"model": "model", "input": "x" * 12000, "max_output_tokens": 1600}
    client.responses.create(**arguments)
    client.responses.create(**arguments)
    assert clock.calls[1] - clock.calls[0] > 8


def test_recovery_retries_only_transport_failures_and_keeps_original_evidence(monkeypatch, tmp_path):
    endpoint = "https://project.example"
    monkeypatch.setenv("FOUNDRY_PROJECT_ENDPOINT", endpoint)
    context = {
        "endpoint": endpoint, "model": "model",
        "profiles": [asdict(profile) for profile in sweep.load_profiles("model")],
        "datasets": sweep.load_datasets(),
    }
    for key, path in (
        ("runtime_sha256", sweep.ROOT / "agent" / "main.py"),
        ("policy_sha256", sweep.ROOT / "agent" / "policy.py"),
        ("runner_sha256", Path(sweep.__file__)),
    ):
        context[key] = hashlib.sha256(path.read_bytes()).hexdigest()
    records = [
        {"trial_id": "business-error", "dataset": "original-dev-v1", "profile": "underspecified",
         "round": 1, "case": "dev-cap", "status": "completed", "checks": {"decision": False},
         "payload": {}, "error": None, "latency_ms": 1},
        {"trial_id": "request-error", "dataset": "original-dev-v1", "profile": "underspecified",
         "round": 1, "case": "dev-missing", "status": "failed", "checks": {},
         "payload": None, "error": {"type": "RateLimitError", "message": "429"}, "latency_ms": 1},
    ]
    source = tmp_path / "original.json"
    source.write_text(json.dumps({"context": context, "records": records, "holdout_used": False}))
    original_bytes = source.read_bytes()
    output = tmp_path / "recovered.json"
    measured = []
    monkeypatch.setattr(recovery, "live_client", lambda context: nullcontext(None))

    def measure(case, profile, client):
        measured.append(case["id"])
        return {
            "status": "completed", "checks": {"decision": False}, "payload": {},
            "error": None, "latency_ms": 2,
        }

    monkeypatch.setattr(sweep, "measure", measure)
    state = recovery.recover(source, output)
    assert measured == ["dev-missing"]
    assert state["records"][0]["checks"]["decision"] is False
    assert state["records"][1]["checks"]["decision"] is False
    assert state["records"][1]["initial_failure"]["error"]["type"] == "RateLimitError"
    assert len(state["records"][1]["recovery_attempts"]) == 1
    assert source.read_bytes() == original_bytes
    recovery.recover(source, output)
    assert measured == ["dev-missing"]
    with pytest.raises(ValueError, match="separate recovery output"):
        recovery.recover(source, source)
