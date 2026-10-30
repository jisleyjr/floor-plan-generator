"""FastAPI application.

``GET /health`` is unauthenticated. Every other route requires
``Authorization: Bearer <FLOORPLAN_API_KEY>``. The comparison hashes both
keys first so it does not leak the secret's length. Do not attach that
dependency to the application itself.

Submission validates the request and enqueues it. Generation and rendering
happen in the worker, not inside the request.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import logging
import math
import sys
import uuid
from typing import Any

from fastapi import Depends, FastAPI, HTTPException, Request, Response, Security
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from fpgen.artifacts import LAYOUT_FILENAME, published_dir
from fpgen.db import get_engine
from fpgen.generate import check_generation_request
from fpgen.jobs import QueueFullError, enqueue, fetch_job
from fpgen.render import RenderTooLarge, ensure_render_budget
from fpgen.schemas import GenerateRequest, RenderRequest, ValidateRequest
from fpgen.settings import ConfigurationError, Settings, get_settings
from fpgen.validate import validate_layout, validation_summary

logger = logging.getLogger("fpgen.api")

POLL_AFTER_SECONDS = 2
RETRY_AFTER_SECONDS = 30
MAX_REQUEST_BYTES = 1024 * 1024
_bearer = HTTPBearer(auto_error=False)


def api_keys_match(provided: str, expected: str) -> bool:
    """Compare two API keys without leaking the expected key through timing."""
    if not isinstance(provided, str) or not isinstance(expected, str):
        return False
    return hmac.compare_digest(
        hashlib.sha256(provided.encode("utf-8")).digest(),
        hashlib.sha256(expected.encode("utf-8")).digest(),
    )


def create_app() -> FastAPI:
    settings = get_settings()
    settings.require_api_key()
    engine = get_engine(settings)
    application = FastAPI(title="Floor Plan API")
    application.state.settings = settings
    application.state.engine = engine

    @application.exception_handler(RequestValidationError)
    async def validation_error(request: Request, exc: RequestValidationError) -> JSONResponse:
        # Echoing the submitted value would put Infinity/NaN into the body and
        # the JSON encoder would turn a 422 into a 500.
        return JSONResponse(status_code=422, content={"detail": _json_safe(exc.errors())})

    @application.middleware("http")
    async def limit_request_body(request: Request, call_next):
        if request.method in {"POST", "PUT", "PATCH"}:
            too_large = JSONResponse(
                status_code=422,
                content={
                    "detail": {
                        "code": "request_too_large",
                        "message": "Request body exceeds 1 MiB.",
                    }
                },
            )
            declared = request.headers.get("content-length")
            if declared is not None:
                try:
                    size = int(declared)
                except ValueError:
                    return JSONResponse(
                        status_code=422,
                        content={"detail": {"code": "invalid_request", "message": "Content-Length must be an integer."}},
                    )
                if size > MAX_REQUEST_BYTES:
                    return too_large
            body = await request.body()
            if len(body) > MAX_REQUEST_BYTES:
                return too_large
        return await call_next(request)

    def require_bearer(
        credentials: HTTPAuthorizationCredentials | None = Security(_bearer),
    ) -> str:
        if credentials is None or credentials.scheme.lower() != "bearer" or not credentials.credentials:
            raise HTTPException(
                status_code=401,
                detail={"code": "unauthorized", "message": "Missing bearer token."},
                headers={"WWW-Authenticate": "Bearer"},
            )
        if not api_keys_match(credentials.credentials, settings.require_api_key()):
            raise HTTPException(
                status_code=401,
                detail={"code": "unauthorized", "message": "Invalid API key."},
                headers={"WWW-Authenticate": "Bearer"},
            )
        return credentials.credentials

    @application.get("/health")
    def health() -> dict[str, str]:
        return {"status": "ok"}

    @application.post("/v1/jobs/generate", status_code=202, dependencies=[Depends(require_bearer)])
    def generate_job(body: GenerateRequest, request: Request, response: Response) -> dict[str, Any]:
        try:
            check_generation_request(
                body.beds, body.baths, body.sqft, body.garage, body.shape, body.garage_side
            )
        except ValueError as exc:
            raise _unprocessable("invalid_request", str(exc)) from exc
        job = _enqueue(request, "generate", body.model_dump(mode="json"))
        return _accepted(job, response)

    @application.post("/v1/jobs/render", status_code=202, dependencies=[Depends(require_bearer)])
    def render_job(body: RenderRequest, request: Request, response: Response) -> dict[str, Any]:
        report = _validate_copy(body.layout)
        if not report.ok:
            raise HTTPException(
                status_code=422,
                detail={
                    "code": "validation_failed",
                    "message": "Layout failed validation.",
                    "errors": list(report.errors),
                    "warnings": list(report.warnings),
                },
            )
        try:
            ensure_render_budget(body.layout, body.dpi)
        except RenderTooLarge as exc:
            raise _unprocessable("image_too_large", str(exc)) from exc
        job = _enqueue(
            request,
            "render",
            {"layout": body.layout, "title": body.title, "dpi": body.dpi},
        )
        return _accepted(job, response)

    @application.get("/v1/jobs/{job_id}", dependencies=[Depends(require_bearer)])
    def read_job(job_id: uuid.UUID, request: Request) -> dict[str, Any]:
        current = request.app.state.settings
        job = fetch_job(job_id, settings=current)
        if job is None:
            raise HTTPException(
                status_code=404,
                detail={"code": "not_found", "message": "Job not found."},
            )
        payload: dict[str, Any] = {
            "id": str(job.id),
            "job_type": job.job_type,
            "status": job.status,
            "created_at": _iso(job.created_at),
            "started_at": _iso(job.started_at),
            "completed_at": _iso(job.completed_at),
            "expires_at": _iso(job.expires_at),
        }
        if job.status == "succeeded":
            metadata = job.result_metadata or {}
            payload["validation"] = metadata.get("validation")
            payload["layout"] = _published_layout(current, job.id)
            payload["files"] = {
                "png": f"/v1/jobs/{job.id}/files/png",
                "svg": f"/v1/jobs/{job.id}/files/svg",
                "json": f"/v1/jobs/{job.id}/files/json",
            }
        elif job.status == "failed":
            payload["error"] = {"code": job.error_code, "message": job.error_message}
        return payload

    @application.post("/v1/layouts/validate", dependencies=[Depends(require_bearer)])
    def validate_layout_route(body: ValidateRequest) -> dict[str, Any]:
        try:
            report = _validate_copy(body.layout)
        except HTTPException:
            raise
        except Exception as exc:
            logger.exception("Unexpected validation failure")
            raise HTTPException(
                status_code=500,
                detail={"code": "internal_error", "message": "The layout could not be validated."},
            ) from exc
        return validation_summary(report)

    return application


def _enqueue(request: Request, job_type: str, payload: dict[str, Any]):
    try:
        return enqueue(job_type, payload, settings=request.app.state.settings)
    except QueueFullError as exc:
        raise HTTPException(
            status_code=429,
            detail={"code": "queue_full", "message": str(exc)},
            headers={"Retry-After": str(RETRY_AFTER_SECONDS)},
        ) from exc


def _accepted(job, response: Response) -> dict[str, Any]:
    status_url = f"/v1/jobs/{job.id}"
    response.headers["Location"] = status_url
    return {
        "id": str(job.id),
        "status": job.status,
        "status_url": status_url,
        "poll_after_seconds": POLL_AFTER_SECONDS,
    }


def _validate_copy(layout: dict[str, Any]):
    try:
        encoded = json.dumps(layout)
    except (TypeError, ValueError) as exc:
        raise _unprocessable("invalid_request", "Layout must be JSON-serializable.") from exc
    return validate_layout(json.loads(encoded))


def _published_layout(settings: Settings, job_id: uuid.UUID) -> dict[str, Any] | None:
    path = published_dir(settings, job_id) / LAYOUT_FILENAME
    if not path.is_file():
        return None
    return json.loads(path.read_text(encoding="utf-8"))


def _iso(value) -> str | None:
    if value is None:
        return None
    return value.isoformat()


def _json_safe(value: object) -> object:
    if isinstance(value, float):
        if math.isnan(value):
            return "NaN"
        if math.isinf(value):
            return "Infinity" if value > 0 else "-Infinity"
        return value
    if isinstance(value, BaseException):
        return str(value)
    if isinstance(value, dict):
        return {str(key): _json_safe(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_safe(item) for item in value]
    if isinstance(value, (str, int, bool)) or value is None:
        return value
    return str(value)


def _unprocessable(code: str, message: str) -> HTTPException:
    return HTTPException(status_code=422, detail={"code": code, "message": message})


def _application_or_exit() -> FastAPI:
    try:
        return create_app()
    except ConfigurationError as exc:
        message = str(exc)
    sys.stderr.write(f"ERROR: {message}\n")
    raise SystemExit(1)


app = _application_or_exit()
