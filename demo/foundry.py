import copy
import json
import os
import time
from enum import Enum
from pathlib import Path

from azure.ai.projects import AIProjectClient, models
from azure.core.credentials import TokenCredential
from azure.identity import ClientSecretCredential, DefaultAzureCredential

from agent.policy import Decision
from demo.cases import CASES

BASELINE = Path(__file__).resolve().parents[1] / "agent" / ".agent_configs" / "baseline"


def status_value(value: str | Enum) -> str:
    return str(value.value) if isinstance(value, Enum) else value


class FoundryGateway:
    def __init__(
        self, *, credential: TokenCredential | None = None, baseline_directory: Path | None = None,
    ):
        self.experiment = os.environ.get("DEMO_PROFILE", "") == "challenge"
        experiment_baseline = BASELINE.parents[1] / ".experiment_configs" / "baseline"
        self.baseline_directory = baseline_directory or (
            experiment_baseline if self.experiment else BASELINE
        )
        self.endpoint = os.environ.get("FOUNDRY_PROJECT_ENDPOINT", "")
        self.agent_name = os.environ.get("FOUNDRY_AGENT_NAME", "promotion-policy")
        self.baseline_version = os.environ.get("FOUNDRY_BASELINE_VERSION", "")
        self.agent_model = os.environ.get("FOUNDRY_AGENT_MODEL", "gpt-4.1-mini")
        self.eval_model = os.environ.get("FOUNDRY_EVAL_MODEL", self.agent_model)
        self.optimization_model = os.environ.get("FOUNDRY_OPTIMIZATION_MODEL", "gpt-5.4")
        self.reviewed_version = os.environ.get("DEMO_REVIEWED_VERSION", "") if self.experiment else ""
        if self.experiment and self.agent_name != "promotion-policy-experiment":
            raise ValueError("The challenge demo must target the separate experiment agent.")
        identity = [
            os.environ.get(name) for name in
            ("DEMO_TENANT_ID", "DEMO_CLIENT_ID", "DEMO_CLIENT_SECRET")
        ]
        if credential is not None:
            self.credential = credential
        elif any(identity):
            if not all(identity):
                raise RuntimeError("All three DEMO service-principal settings are required.")
            self.credential = ClientSecretCredential(
                *identity, connection_timeout=5, read_timeout=10, retry_total=0,
            )
        else:
            self.credential = DefaultAzureCredential()
        self.approved_versions = {self.baseline_version}
        if self.reviewed_version:
            if self.reviewed_version == self.baseline_version or not self.reviewed_version.isdigit():
                raise ValueError("Configure a separate pinned reviewed version.")
            self.approved_versions.add(self.reviewed_version)

    @property
    def configured(self) -> bool:
        return bool(self.endpoint and self.baseline_version)

    def project(self):
        if not self.configured:
            raise RuntimeError("Set FOUNDRY_PROJECT_ENDPOINT and FOUNDRY_BASELINE_VERSION first.")
        return AIProjectClient(
            endpoint=self.endpoint, credential=self.credential,
            connection_timeout=5, read_timeout=15, retry_total=0, allow_preview=True,
        )

    def require_version(self, version: str):
        if version in self.approved_versions:
            return
        with self.project() as project:
            deployed = project.agents.get_version(self.agent_name, version)
        metadata = deployed.metadata or {}
        if metadata.get("optimizer_baseline") != self.baseline_version:
            raise ValueError("This is not an optimized version of the configured baseline.")
        self.approved_versions.add(version)

    def invoke(self, text: str, version: str) -> dict:
        started = time.perf_counter()
        with self.project() as project, project.get_openai_client(
            agent_name=self.agent_name, max_retries=0,
        ) as client:
            response = client.responses.create(
                input=text,
                extra_body={
                    "agent_reference": {
                        "type": "agent_reference", "name": self.agent_name, "version": version,
                    },
                },
                timeout=32,
            )
        payload = json.loads(response.output_text)
        payload["result"] = Decision.model_validate(payload["result"]).model_dump()
        payload["latency_ms"] = round((time.perf_counter() - started) * 1000)
        payload["version"] = version
        payload["response_id"] = response.id
        return payload

    def start_optimization(
        self, operation_id: str, *,
        test_cases: list[models.AgentOptimizationTargetCompletionTestCase] | None = None,
        evaluators: list[models.AgentOptimizationEvaluator] | None = None,
    ) -> str:
        if test_cases is None:
            if self.experiment:
                from demo.experiment import training_cases

                test_cases = training_cases()
            else:
                test_cases = [
                    models.AgentOptimizationTargetCompletionTestCase(
                        query=case.prompt,
                        ground_truth=f"The required decision is {case.expected}. "
                        "Apply Contoso policy faithfully, use the lookup tool, calculate when possible "
                        "and never invent missing data. Return all structured numeric fields.",
                    )
                    for case in CASES if case.split == "dev"
                ]
        if not test_cases:
            raise ValueError("Optimization requires at least one development case.")
        if evaluators is None:
            if self.experiment:
                from demo.experiment import evaluators as business_evaluators

                evaluators = business_evaluators()
            else:
                evaluators = [models.AgentOptimizationEvaluator(name="builtin.task_adherence")]
        if not evaluators:
            raise ValueError("Optimization requires at least one evaluator.")
        request = models.AgentOptimizationJob(
            display_name="Promotion policy configuration",
            target_configuration=models.AgentOptimizationFoundryAgentTargetConfiguration(
                name=self.agent_name, version=self.baseline_version,
            ),
            optimization_model_configuration=models.AgentOptimizationModelConfiguration(
                model=self.optimization_model,
            ),
            optimization_configuration=models.AgentOptimizationConfiguration(
                evaluation_configuration=models.AgentOptimizationEvaluationConfiguration(
                    training_set=models.AgentOptimizationTargetCompletionEvaluationSet(
                        source=models.AgentOptimizationTargetCompletionInlineDataSource(
                            test_cases=test_cases,
                        ),
                    ),
                    evaluators=evaluators,
                    evaluation_model=models.EvaluationModelConfiguration(model=self.eval_model),
                    max_concurrent_agent_runs=1,
                ),
                candidate_search_configuration=models.AgentOptimizationCandidateSearchConfiguration(
                    max_candidates=2,
                ),
                baseline_agent_configuration=models.AgentOptimizationBaselineAgentConfiguration(
                    system_prompt=(self.baseline_directory / "instructions.md").read_text(
                        encoding="utf-8",
                    ).strip(),
                    current_model=self.agent_model,
                ),
                agent_optimization_space=models.AgentOptimizationSpace(
                    target_attributes=[models.TargetAttribute.INSTRUCTIONS],
                ),
            ),
        )
        with self.project() as project:
            poller = project.agents.begin_create_optimization_job(
                job=request, operation_id=operation_id, polling=False,
            )
            job_id = poller.details.get("job_id")
            if not isinstance(job_id, str):
                raise TypeError("Foundry did not return an optimization job ID.")
            return job_id

    def optimization(self, job_id: str) -> dict:
        with self.project() as project:
            job = project.agents.get_optimization_job(job_id)
            target = job.target_configuration
            if (
                not isinstance(target, models.AgentOptimizationFoundryAgentTargetConfiguration)
                or target.name != self.agent_name or target.version != self.baseline_version
            ):
                raise ValueError("This optimization does not belong to the configured agent baseline.")
            candidates = list(project.agents.list_optimization_candidates(
                job_id, expand=[models.AgentOptimizationCandidateExpand.MUTATIONS],
            ))
        result = {
            "id": job_id, "status": status_value(job.status), "candidates": [],
            "warnings": job.warnings or [],
            "baseline_instructions": (self.baseline_directory / "instructions.md").read_text(
                encoding="utf-8",
            ),
        }
        if job.error:
            result["error"] = job.error.message
        if job.result and job.result.candidate_summary:
            summary = job.result.candidate_summary
            result.update({
                "baseline_score": summary.baseline_score,
                "best_score": summary.best_score, "best_id": summary.best_id,
                "baseline_id": summary.baseline_id,
            })
        for candidate in candidates:
            instructions = None
            # The current SDK preserves candidate errors as additional wire fields.
            error = candidate.as_dict().get("error")
            error_message = None
            if error is not None:
                if not isinstance(error, dict) or not isinstance(error.get("message"), str):
                    raise TypeError("Foundry returned malformed candidate error details.")
                error_message = error["message"]
            if candidate.output and isinstance(
                candidate.output, models.AgentOptimizationAgentCandidateOutput,
            ):
                for mutation in candidate.output.mutations or []:
                    if isinstance(mutation, models.AgentOptimizationInstructionsMutation):
                        instructions = mutation.value
            result["candidates"].append({
                "id": candidate.candidate_id, "name": candidate.name,
                "status": status_value(candidate.status),
                "score": candidate.evaluation.score if candidate.evaluation else None,
                "instructions": instructions,
                "error": error_message,
            })
        return result

    def deploy_candidate(self, job_id: str, candidate_id: str) -> dict:
        with self.project() as project:
            for version in project.agents.list_versions(self.agent_name):
                metadata = version.metadata or {}
                if (
                    metadata.get("optimizer_job") == job_id
                    and metadata.get("optimizer_candidate") == candidate_id
                    and metadata.get("optimizer_baseline") == self.baseline_version
                ):
                    return {
                        "version": version.version, "candidate_id": candidate_id, "job_id": job_id,
                    }
            candidate = project.agents.get_optimization_candidate(job_id, candidate_id)
            if status_value(candidate.status) != "completed":
                raise ValueError("This candidate has not completed evaluation.")
            if not isinstance(candidate.output, models.AgentOptimizationAgentCandidateOutput):
                raise TypeError("The candidate does not contain an agent configuration.")
            instructions = None
            for mutation in candidate.output.mutations or []:
                if isinstance(mutation, models.AgentOptimizationInstructionsMutation):
                    instructions = mutation.value
                else:
                    raise TypeError("Only instruction changes are allowed in this experiment.")
            if not instructions:
                raise ValueError("This candidate has no instruction changes to deploy.")
            baseline = project.agents.get_version(self.agent_name, self.baseline_version)
            if not isinstance(baseline.definition, models.HostedAgentDefinition):
                raise TypeError("The baseline must be a Foundry hosted agent.")
            definition = copy.deepcopy(baseline.definition)
            environment = dict(definition.environment_variables or {})
            environment.pop("OPTIMIZATION_CANDIDATE_ID", None)
            environment.pop("OPTIMIZATION_RESOLVE_ENDPOINT", None)
            environment["OPTIMIZATION_CONFIG"] = json.dumps({
                "instructions": instructions, "model": self.agent_model,
            })
            definition.environment_variables = environment
            deployed = project.agents.create_version(
                self.agent_name, definition=definition,
                description=f"Optimizer candidate {candidate_id}",
                metadata={
                    "optimizer_job": job_id, "optimizer_candidate": candidate_id,
                    "optimizer_baseline": self.baseline_version,
                },
            )
        self.approved_versions.add(deployed.version)
        return {
            "version": deployed.version, "candidate_id": candidate_id, "job_id": job_id,
            "instructions": instructions,
        }
