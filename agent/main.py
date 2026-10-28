"""Responses-protocol host with real model/tool execution and optimizer config."""

import asyncio
import json
import logging
import os
from pathlib import Path

from azure.ai.agentserver.optimization import load_config
from azure.ai.agentserver.responses import (
    CreateResponse,
    ResponseContext,
    ResponseEventStream,
    ResponsesAgentServerHost,
)
from azure.ai.agentserver.responses.models import (
    ResponseUsage,
    ResponseUsageInputTokensDetails,
    ResponseUsageOutputTokensDetails,
)
from azure.ai.projects import AIProjectClient
from azure.identity import DefaultAzureCredential

try:
    from .policy import FUNCTIONS, POLICY, TOOLS, Decision
except ImportError:
    from policy import FUNCTIONS, POLICY, TOOLS, Decision

logger = logging.getLogger("promotion-policy")
logging.basicConfig(level=logging.INFO)


def run_agent(text: str, config, client, *, append_fixed_policy: bool = True) -> dict:
    """One independent turn; no shared conversation state between comparisons."""
    instructions = config.compose_instructions()
    model = config.model
    if not instructions or not model:
        raise RuntimeError("Optimizer configuration must include instructions and a model.")
    effective_instructions = (
        instructions + "\n\n## Fixed business contract\n" + POLICY
        if append_fixed_policy else instructions
    )
    messages: list[dict] = [{"role": "user", "content": text}]
    trace: list[dict] = []
    usage_totals = {"input_tokens": 0, "output_tokens": 0, "total_tokens": 0}
    usage_known = True
    cached_tokens = 0
    reasoning_tokens = 0
    for _ in range(8):
        response = client.responses.create(
            model=model,
            instructions=effective_instructions,
            input=messages,
            tools=TOOLS,
            text={
                "format": {
                    "type": "json_schema", "name": "promotion_decision",
                    "schema": Decision.model_json_schema(), "strict": True,
                }
            },
            store=False,
            max_output_tokens=1600,
        )
        if response.usage is None:
            usage_known = False
        else:
            for key in usage_totals:
                usage_totals[key] += getattr(response.usage, key)
            if response.usage.input_tokens_details:
                cached_tokens += response.usage.input_tokens_details.cached_tokens or 0
            if response.usage.output_tokens_details:
                reasoning_tokens += response.usage.output_tokens_details.reasoning_tokens or 0
        messages.extend(item.model_dump(exclude_none=True) for item in response.output)
        calls = [item for item in response.output if item.type == "function_call"]
        if not calls:
            result = Decision.model_validate_json(response.output_text)
            return {
                "result": result.model_dump(),
                "trace": {
                    "model": model, "config_source": config.source,
                    "instructions": instructions, "policy": POLICY,
                    "policy_in_prompt": append_fixed_policy,
                    "schema": Decision.model_json_schema(), "tool_calls": trace,
                },
                "usage": {
                    **usage_totals,
                    "cached_tokens": cached_tokens,
                    "reasoning_tokens": reasoning_tokens,
                } if usage_known else None,
            }
        for call in calls:
            if call.name not in FUNCTIONS:
                raise ValueError(f"Unknown tool requested: {call.name}")
            arguments = json.loads(call.arguments)
            try:
                output = FUNCTIONS[call.name](**arguments)
            except (ValueError, TypeError) as exc:
                logger.warning("Invalid tool arguments for %s: %s", call.name, exc)
                output = {"error": str(exc)}
            trace.append({"name": call.name, "arguments": arguments, "output": output})
            messages.append({
                "type": "function_call_output", "call_id": call.call_id,
                "output": json.dumps(output),
            })
    raise RuntimeError("The agent exceeded its eight-step tool-call limit.")


def create_host(*, append_fixed_policy: bool = True, config_dir: Path | None = None):
    credential = DefaultAzureCredential()
    config = load_config(
        config_dir=config_dir or Path(__file__).parent / ".agent_configs", credential=credential,
    )
    if config is None:
        raise RuntimeError("The baseline optimization configuration is missing.")
    configured_model = os.environ.get("AZURE_AI_MODEL_DEPLOYMENT_NAME")
    if configured_model and config.source.startswith("local:"):
        config.model = configured_model
    endpoint = os.environ.get("FOUNDRY_PROJECT_ENDPOINT") or os.environ["AZURE_AI_PROJECT_ENDPOINT"]
    project = AIProjectClient(endpoint=endpoint, credential=credential)
    client = project.get_openai_client()
    host = ResponsesAgentServerHost()
    logger.info("Config source=%s model=%s", config.source, config.model)

    @host.response_handler
    async def respond(
        request: CreateResponse, context: ResponseContext, cancellation: asyncio.Event,
    ):
        text = await context.get_input_text()
        if not text:
            raise ValueError("A promotion request is required.")
        payload = await asyncio.to_thread(
            run_agent, text, config, client, append_fixed_policy=append_fixed_policy,
        )
        usage = None
        if payload["usage"] is not None:
            measured = payload["usage"]
            usage = ResponseUsage(
                input_tokens=measured["input_tokens"],
                output_tokens=measured["output_tokens"],
                total_tokens=measured["total_tokens"],
                input_tokens_details=ResponseUsageInputTokensDetails(
                    cached_tokens=measured["cached_tokens"],
                ),
                output_tokens_details=ResponseUsageOutputTokensDetails(
                    reasoning_tokens=measured["reasoning_tokens"],
                ),
            )
        stream = ResponseEventStream(response_id=context.response_id, request=request)

        async def events():
            yield stream.emit_created()
            yield stream.emit_in_progress()
            message = stream.add_output_item_message()
            yield message.emit_added()
            content = message.add_text_content()
            yield content.emit_added()
            output = json.dumps(payload)
            yield content.emit_delta(output)
            yield content.emit_text_done(output)
            yield content.emit_done()
            yield message.emit_done()
            yield stream.emit_completed(usage=usage)

        return events()

    return host


if __name__ == "__main__":
    create_host().run()
