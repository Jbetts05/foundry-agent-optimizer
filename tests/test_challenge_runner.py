import json
from contextlib import contextmanager
from types import SimpleNamespace

import pytest
from azure.ai.projects import models

from scripts import run_challenge_optimization as runner


@pytest.fixture
def experiment(monkeypatch):
    state = SimpleNamespace(submissions=0, polls=0, fail_poll=False)
    gateway = SimpleNamespace(
        configured=True, endpoint="https://project.example", agent_name="agent",
        baseline_version="1", agent_model="model", eval_model="model",
        optimization_model="optimizer",
    )
    baseline = models.HostedAgentDefinition(
        cpu="1", memory="2Gi",
        code_configuration=models.CodeConfiguration(runtime="python_3_13", entry_point="main.py"),
    )

    @contextmanager
    def project():
        yield SimpleNamespace(
            deployments=SimpleNamespace(list=lambda: [
                SimpleNamespace(name="model"), SimpleNamespace(name="optimizer"),
            ]),
            agents=SimpleNamespace(get_version=lambda *args: SimpleNamespace(definition=baseline)),
        )

    def submit(operation_id, *, test_cases):
        state.submissions += 1
        assert len(test_cases) == 12
        return "recorded-job"

    def poll(job_id):
        state.polls += 1
        assert job_id == "recorded-job"
        if state.fail_poll:
            raise TimeoutError("Local monitoring lost its connection.")
        return {
            "id": job_id, "status": "succeeded", "candidates": [], "warnings": [],
            "baseline_score": 1.0, "best_score": 1.0,
        }

    gateway.project = project
    gateway.start_optimization = submit
    gateway.optimization = poll
    monkeypatch.setattr(runner, "FoundryGateway", lambda **kwargs: gateway)
    return gateway, state


def test_runner_resumes_recorded_job_without_another_submission(experiment, tmp_path):
    gateway, state = experiment
    path = tmp_path / "experiment.json"
    runner.run(path, wait_seconds=0)
    runner.run(path, wait_seconds=0)
    saved = json.loads(path.read_text())
    assert state.submissions == 1
    assert state.polls == 2
    assert saved["development_cases"] == 12
    assert saved["holdout_used"] is False
    assert saved["starter_profile"] == "original-deployed-v1"
    assert saved["job_id"] == "recorded-job"
    gateway.baseline_version = "2"
    with pytest.raises(ValueError, match="different experiment inputs"):
        runner.run(path, wait_seconds=0)
    assert state.submissions == 1


def test_monitoring_failure_keeps_job_id_for_safe_resume(experiment, tmp_path):
    _, state = experiment
    path = tmp_path / "experiment.json"
    state.fail_poll = True
    with pytest.raises(TimeoutError, match="Local monitoring"):
        runner.run(path, wait_seconds=0)
    assert json.loads(path.read_text())["job_id"] == "recorded-job"
    state.fail_poll = False
    runner.run(path, wait_seconds=0)
    assert state.submissions == 1
