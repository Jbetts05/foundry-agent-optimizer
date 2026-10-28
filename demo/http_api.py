import json
import logging
import os
import threading
import time
from contextlib import contextmanager
from pathlib import Path
from uuid import UUID

from azure.core.exceptions import AzureError
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from openai import OpenAIError
from pydantic import BaseModel, ConfigDict, Field

from demo import experiment
from demo.cases import CASE_BY_ID, CASES, score
from demo.foundry import FoundryGateway

logger = logging.getLogger("optimizer-demo")
CLOUD_ERRORS = (AzureError, OpenAIError, ValueError, RuntimeError, KeyError, TypeError)


class RunRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    case_id: str = Field(max_length=80)
    text: str = Field(min_length=1, max_length=6000)
    version: str = Field(pattern=r"^[1-9][0-9]{0,9}$")


class OptimizeRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    operation_id: UUID


class ApplyRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    candidate_id: str = Field(min_length=1, max_length=160, pattern=r"^[A-Za-z0-9_-]+$")


class Limiter:
    """A modest per-instance guard, not a durable or global spending cap."""

    def __init__(self):
        self.lock = threading.Lock()
        self.starts: dict[str, list[float]] = {}
        self.active = 0

    @contextmanager
    def guard(self, kind, limit, seconds):
        now = time.monotonic()
        with self.lock:
            starts = [stamp for stamp in self.starts.get(kind, []) if now - stamp < seconds]
            self.starts[kind] = starts
            if self.active >= 4 or len(starts) >= limit:
                raise HTTPException(429, "This demo instance is busy or its usage limit was reached.")
            starts.append(now)
            self.active += 1
        try:
            yield
        finally:
            with self.lock:
                self.active -= 1


def create_app(gateway=None, *, serve_web=True):
    gateway = gateway or FoundryGateway()
    experimental = getattr(gateway, "experiment", False)
    demo_cases = (
        {row["id"]: row for split in ("development", "holdout") for row in experiment.cases(split)}
        if experimental else {}
    )
    limiter = Limiter()
    mutation_lock = threading.Lock()
    app = FastAPI(title="Promotion Policy Optimizer", docs_url=None, redoc_url=None)

    @app.middleware("http")
    async def headers_and_origin(request: Request, call_next):
        if request.method == "POST":
            origin = request.headers.get("origin")
            # Static Web Apps can forward its public host through a different API host.
            allowed = os.environ.get("DEMO_ORIGIN", "").rstrip("/")
            host = request.headers.get("host", "")
            origins = {f"https://{host}", f"http://{host}"}
            if allowed:
                origins.add(allowed)
            if request.headers.get("sec-fetch-site") == "cross-site" or (origin and origin not in origins):
                return JSONResponse({"detail": "Cross-origin requests are not allowed."}, 403)
            if not request.headers.get("content-type", "").lower().startswith("application/json"):
                return JSONResponse({"detail": "Use Content-Type: application/json."}, 415)
            body = await request.body()
            if len(body) > 32768:
                return JSONResponse({"detail": "The request exceeds 32 KiB."}, 413)
        response = await call_next(request)
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Cache-Control"] = "no-store"
        return response

    def require_ready():
        if not gateway.configured:
            raise HTTPException(503, "The Foundry agent has not been configured.")

    @contextmanager
    def cloud():
        try:
            yield
        except CLOUD_ERRORS as exc:
            upstream_status = getattr(exc, "status_code", None)
            logger.error(
                "Foundry operation failed: %s (upstream status: %s)",
                type(exc).__name__, upstream_status,
            )
            if upstream_status == 503:
                raise HTTPException(
                    503, "The Foundry service is temporarily unavailable. Retry this operation later.",
                ) from exc
            detail = (
                str(exc)[:300] if isinstance(exc, (ValueError, RuntimeError, TypeError))
                else f"Foundry request failed ({type(exc).__name__}). Check project access and deployment."
            )
            raise HTTPException(502, detail) from exc

    @app.get("/api/health")
    def health():
        return {"status": "ok", "configured": gateway.configured}

    @app.get("/api/catalog")
    def catalog():
        result = {
            "cases": (
                experiment.public_cases() if experimental
                else [case.public() for case in CASES if case.split == "test"]
            ),
            "configured": gateway.configured,
            "agent_name": gateway.agent_name, "model": gateway.agent_model,
            "v1": gateway.baseline_version,
        }
        if experimental:
            result.update({
                "source_optimization_job": experiment.OPTIMIZER_JOB,
                "version_provenance": json.loads(
                    (experiment.EVALUATION / "reviewed-candidate.json").read_text(encoding="utf-8"),
                ),
                "candidate_application_enabled": False,
                "provenance": (
                    "Optimizer development result: 5/12 to 8/12. "
                    "V2 adds a reviewed fix: use the Germany cap returned by the tool unchanged. "
                    "Measure both versions below; development and holdout results stay separate."
                ),
                "v2": (
                    {"version": gateway.reviewed_version, "reviewed": True}
                    if gateway.reviewed_version else None
                ),
            })
        return result

    @app.post("/api/run")
    def run(body: RunRequest):
        require_ready()
        if experimental:
            valid_case = body.case_id in demo_cases
        else:
            valid_case = body.case_id in CASE_BY_ID and CASE_BY_ID[body.case_id].split == "test"
        if not valid_case:
            raise HTTPException(422, "Choose a valid demonstration case.")
        if not body.text.strip():
            raise HTTPException(422, "Enter a promotion request.")
        with limiter.guard("run", 100, 3600), cloud():
            gateway.require_version(body.version)
            payload = gateway.invoke(body.text.strip(), body.version)
            if experimental:
                case = demo_cases[body.case_id]
                payload["checks"] = (
                    experiment.checks(case, payload) if body.text.strip() == case["query"] else None
                )
            else:
                payload["checks"] = (
                    score(CASE_BY_ID[body.case_id], payload["result"], payload["trace"]["tool_calls"])
                    if body.text.strip() == CASE_BY_ID[body.case_id].prompt else None
                )
            return {"status": "completed", **payload}

    @app.get("/api/versions/{version}")
    def version(version: str):
        require_ready()
        with cloud():
            gateway.require_version(version)
        return {"version": version}

    @app.post("/api/optimizations", status_code=202)
    def optimize(body: OptimizeRequest):
        require_ready()
        with mutation_lock, limiter.guard(
            "optimization", int(os.environ.get("DEMO_OPTIMIZATIONS_PER_DAY", "3")), 86400,
        ), cloud():
            job_id = gateway.start_optimization(str(body.operation_id))
            return {"id": job_id, "job_id": job_id, "status": "queued"}

    @app.get("/api/optimizations/{identifier}")
    def optimization(identifier: str):
        require_ready()
        with cloud():
            result = gateway.optimization(identifier)
        return {**result, "job_id": identifier}

    @app.post("/api/optimizations/{identifier}/apply")
    def apply(identifier: str, body: ApplyRequest):
        require_ready()
        if experimental:
            raise HTTPException(
                409, "This demo pins a reviewed V2. New candidates need review before deployment.",
            )
        with mutation_lock, limiter.guard("deployment", 6, 86400), cloud():
            record = gateway.optimization(identifier)
            if record["status"] != "succeeded":
                raise HTTPException(409, "Wait for this optimization to finish.")
            candidates = {candidate["id"]: candidate for candidate in record.get("candidates", [])}
            selected = candidates.get(body.candidate_id)
            if (
                not selected or body.candidate_id == record.get("baseline_id")
                or selected.get("status") != "completed" or not selected.get("instructions")
            ):
                raise HTTPException(422, "Select a completed optimized candidate from this run.")
            return gateway.deploy_candidate(identifier, body.candidate_id)

    if serve_web:
        web = Path(__file__).resolve().parents[1] / "web"
        app.mount("/", StaticFiles(directory=web, html=True), name="web")
    return app


app = create_app(serve_web=(Path(__file__).resolve().parents[1] / "web").is_dir())
