import json
from contextlib import contextmanager
from datetime import UTC, datetime
from types import SimpleNamespace

import pytest
from azure.ai.projects import models

from demo.cases import CASES
from demo.foundry import FoundryGateway


@pytest.fixture
def cloud(monkeypatch):
    monkeypatch.setenv("FOUNDRY_BASELINE_VERSION", "1")
    gateway = FoundryGateway()
    baseline = models.HostedAgentDefinition(
        cpu="1", memory="2Gi",
        code_configuration=models.CodeConfiguration(runtime="python_3_13", entry_point="main.py"),
        environment_variables={"EXISTING_SETTING": "preserved"},
    )
    candidate = models.AgentOptimizationCandidate(
        candidate_id="candidate", job_id="job", name="Candidate",
        started_at=datetime(2026, 1, 1, tzinfo=UTC),
        status=models.AgentOptimizationCandidateStatus.COMPLETED,
        output=models.AgentOptimizationAgentCandidateOutput(mutations=[
            models.AgentOptimizationInstructionsMutation(value="Optimized instructions"),
        ]),
    )
    state = SimpleNamespace(created=[], versions=[], request=None)

    def create_version(name, **kwargs):
        state.created.append(kwargs)
        version = SimpleNamespace(version="2", metadata=kwargs["metadata"])
        state.versions.append(version)
        return version

    def submit(**kwargs):
        state.request = kwargs
        return SimpleNamespace(details={"job_id": "job"})

    project = SimpleNamespace(agents=SimpleNamespace(
        list_versions=lambda name: iter(state.versions),
        get_version=lambda name, version: SimpleNamespace(definition=baseline, metadata={}),
        get_optimization_candidate=lambda job, identifier: candidate,
        create_version=create_version,
        begin_create_optimization_job=submit,
    ))

    @contextmanager
    def connection():
        yield project

    monkeypatch.setattr(gateway, "project", connection)
    return gateway, project, baseline, candidate, state


def test_real_optimizer_payload_keeps_holdout_cases_out(cloud):
    gateway, _, _, _, state = cloud
    assert gateway.start_optimization("request-id") == "job"
    submitted = state.request
    assert submitted["operation_id"] == "request-id"
    assert submitted["polling"] is False
    job = submitted["job"]
    config = job.optimization_configuration
    queries = {case.query for case in config.evaluation_configuration.training_set.source.test_cases}
    assert queries == {case.prompt for case in CASES if case.split == "dev"}
    assert not queries.intersection(case.prompt for case in CASES if case.split == "test")
    assert config.candidate_search_configuration.max_candidates == 2
    assert config.agent_optimization_space.target_attributes == [models.TargetAttribute.INSTRUCTIONS]
    assert job.target_configuration.version == "1"


def test_invocation_uses_hosted_endpoint_and_pins_requested_version(cloud):
    gateway, project, _, _, state = cloud
    payload = {
        "result": {
            "decision": "clarify", "reason": "Missing product.",
            "discount_cap_percent": None, "minimum_margin_percent": None,
            "offer_price": None, "projected_margin_percent": None,
        },
        "trace": {"tool_calls": []},
    }

    def respond(**kwargs):
        state.invocation = kwargs
        return SimpleNamespace(output_text=json.dumps(payload), id="response")

    @contextmanager
    def client(**kwargs):
        state.client_options = kwargs
        yield SimpleNamespace(responses=SimpleNamespace(create=respond))

    project.get_openai_client = client
    result = gateway.invoke("Review this promotion", "2")
    assert state.client_options == {"agent_name": "promotion-policy", "max_retries": 0}
    assert state.invocation["extra_body"]["agent_reference"] == {
        "type": "agent_reference", "name": "promotion-policy", "version": "2",
    }
    assert result["version"] == "2"
    assert result["response_id"] == "response"


def test_explicit_development_cases_replace_rather_than_mix_with_default_dataset(cloud):
    gateway, _, _, _, state = cloud
    cases = [models.AgentOptimizationTargetCompletionTestCase(
        query="A new development request", ground_truth="Check exact calculator inputs.",
    )]
    gateway.start_optimization("challenge-request", test_cases=cases)
    configuration = state.request["job"].optimization_configuration
    submitted = configuration.evaluation_configuration.training_set.source.test_cases
    assert [(case.query, case.ground_truth) for case in submitted] == [
        ("A new development request", "Check exact calculator inputs."),
    ]
    assert state.request["job"].target_configuration.version == "1"
    assert configuration.candidate_search_configuration.max_candidates == 2


def test_empty_explicit_dataset_is_not_silently_replaced_with_default_cases(cloud):
    gateway, _, _, _, state = cloud
    with pytest.raises(ValueError, match="at least one"):
        gateway.start_optimization("challenge-request", test_cases=[])
    assert state.request is None


def test_explicit_credential_is_preserved_without_changing_default_identity_settings():
    from azure.identity import AzureCliCredential

    credential = AzureCliCredential(process_timeout=60)
    assert FoundryGateway(credential=credential).credential is credential


def test_experiment_selects_its_own_baseline_and_exact_grader(cloud, tmp_path):
    gateway, _, _, _, state = cloud
    (tmp_path / "instructions.md").write_text("The separate weak prompt.", encoding="utf-8")
    gateway.baseline_directory = tmp_path
    evaluator = models.AgentOptimizationEvaluator(
        name="builtin.python_grader", version="1",
        initialization_parameters={"type": "python", "source": "def grade(s, i): return 0"},
    )
    gateway.start_optimization("isolated-request", evaluators=[evaluator])
    configuration = state.request["job"].optimization_configuration
    assert configuration.baseline_agent_configuration.system_prompt == "The separate weak prompt."
    assert configuration.evaluation_configuration.evaluators[0].as_dict() == evaluator.as_dict()


def test_explicit_empty_grader_list_does_not_fall_back_to_task_adherence(cloud):
    gateway, _, _, _, state = cloud
    with pytest.raises(ValueError, match="at least one evaluator"):
        gateway.start_optimization("isolated-request", evaluators=[])
    assert state.request is None


def test_challenge_demo_optimizes_exactly_development_with_the_business_grader(cloud):
    from demo.experiment import ROOT, cases

    gateway, _, _, _, state = cloud
    gateway.experiment = True
    gateway.baseline_directory = ROOT / ".experiment_configs" / "baseline"
    gateway.start_optimization("public-challenge-run")
    configuration = state.request["job"].optimization_configuration
    evaluation = configuration.evaluation_configuration
    queries = {case.query for case in evaluation.training_set.source.test_cases}
    assert queries == {case["query"] for case in cases("development")}
    assert queries.isdisjoint(case["query"] for case in cases("holdout"))
    assert evaluation.evaluators[0].name == "builtin.python_grader"
    assert evaluation.evaluators[0].initialization_parameters["image_tag"] == "2025-05-08"
    assert "helpful sales assistant" in configuration.baseline_agent_configuration.system_prompt


def test_configured_reviewed_version_and_experiment_baseline_are_pinned(monkeypatch):
    from demo.experiment import ROOT

    monkeypatch.setenv("DEMO_PROFILE", "challenge")
    monkeypatch.setenv("FOUNDRY_AGENT_NAME", "promotion-policy-experiment")
    monkeypatch.setenv("FOUNDRY_BASELINE_VERSION", "1")
    monkeypatch.setenv("DEMO_REVIEWED_VERSION", "2")
    gateway = FoundryGateway()
    assert gateway.baseline_directory == ROOT / ".experiment_configs" / "baseline"
    assert gateway.approved_versions == {"1", "2"}
    monkeypatch.setenv("DEMO_REVIEWED_VERSION", "1")
    with pytest.raises(ValueError, match="separate pinned reviewed"):
        FoundryGateway()
    monkeypatch.setenv("FOUNDRY_AGENT_NAME", "promotion-policy")
    with pytest.raises(ValueError, match="separate experiment agent"):
        FoundryGateway()


def test_candidate_creates_separate_version_without_mutating_baseline(cloud):
    gateway, _, baseline, _, state = cloud
    original = baseline.as_dict()
    deployed = gateway.deploy_candidate("job", "candidate")
    assert deployed["version"] == "2"
    assert baseline.as_dict() == original
    definition = state.created[0]["definition"]
    assert definition.code_configuration.as_dict() == baseline.code_configuration.as_dict()
    assert definition.environment_variables["EXISTING_SETTING"] == "preserved"
    config = json.loads(definition.environment_variables["OPTIMIZATION_CONFIG"])
    assert config == {"instructions": "Optimized instructions", "model": "gpt-4.1-mini"}
    assert state.created[0]["metadata"]["optimizer_baseline"] == "1"
    assert gateway.baseline_version == "1"


def test_reapplying_same_candidate_reuses_existing_version(cloud):
    gateway, _, _, _, state = cloud
    gateway.deploy_candidate("job", "candidate")
    gateway.deploy_candidate("job", "candidate")
    assert len(state.created) == 1


def test_model_changes_are_not_accepted_in_instruction_experiment(cloud):
    gateway, _, _, candidate, state = cloud
    candidate.output.mutations = [
        models.AgentOptimizationModelMutation(value="another-model"),
    ]
    with pytest.raises(TypeError, match="Only instruction"):
        gateway.deploy_candidate("job", "candidate")
    assert not state.created


def test_candidate_must_have_completed_evaluation(cloud):
    gateway, _, _, candidate, state = cloud
    candidate.status = "evaluating"
    with pytest.raises(ValueError, match="not completed"):
        gateway.deploy_candidate("job", "candidate")
    assert not state.created


def test_version_must_be_linked_to_pinned_baseline(cloud):
    gateway, _, _, _, _ = cloud
    with pytest.raises(ValueError, match="not an optimized version"):
        gateway.require_version("9")
    gateway.require_version("1")


@pytest.mark.parametrize("status", [
    models.JobStatus.QUEUED, models.JobStatus.IN_PROGRESS, models.JobStatus.SUCCEEDED,
])
def test_optimizer_lifecycle_serializes_sdk_enum_values(cloud, status):
    gateway, project, _, candidate, _ = cloud
    candidate.candidate_id = "candidate"
    candidate.name = "Candidate"
    candidate.evaluation = None
    project.agents.get_optimization_job = lambda identifier: SimpleNamespace(
        status=status, warnings=[], error=None, result=None,
        target_configuration=models.AgentOptimizationFoundryAgentTargetConfiguration(
            name=gateway.agent_name, version=gateway.baseline_version,
        ),
    )
    project.agents.list_optimization_candidates = lambda *args, **kwargs: iter([candidate])
    result = gateway.optimization("job")
    assert result["status"] == status.value
    assert result["candidates"][0]["status"] == "completed"


def test_failed_candidate_preserves_upstream_error_details(cloud):
    gateway, project, _, candidate, _ = cloud
    candidate.candidate_id = "candidate"
    candidate.name = "Candidate"
    candidate.status = models.AgentOptimizationCandidateStatus.FAILED
    candidate["error"] = {"message": "No candidate failure reason was recorded."}
    candidate.evaluation = None
    candidate.output = None
    project.agents.get_optimization_job = lambda identifier: SimpleNamespace(
        status=models.JobStatus.SUCCEEDED, warnings=[], error=None, result=None,
        target_configuration=models.AgentOptimizationFoundryAgentTargetConfiguration(
            name=gateway.agent_name, version=gateway.baseline_version,
        ),
    )
    project.agents.list_optimization_candidates = lambda *args, **kwargs: iter([candidate])
    result = gateway.optimization("job")
    assert result["candidates"][0]["status"] == "failed"
    assert result["candidates"][0]["error"] == "No candidate failure reason was recorded."
    assert result["candidates"][0]["instructions"] is None
