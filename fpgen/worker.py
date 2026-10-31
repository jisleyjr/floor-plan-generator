"""Process queued floor-plan jobs.

Run exactly one worker::

    python -m fpgen.worker

Multiple worker processes are outside this version. The worker claims one
job at a time. On startup it returns interrupted jobs to the queue, or fails
them when they have been recovered more than 3 times, and deletes their
partial files. Succeeded and failed jobs expire 24 hours after they finish.
Expired records and artifacts are removed at startup and about once a minute.
Queued and running jobs are never expired.

SIGINT or SIGTERM stops new claims and lets the active job finish. A kill
that does not get that far is repaired by the next worker's startup recovery.
An API key is not required.
"""
from __future__ import annotations

import copy
import json
import logging
import signal
import sys
import threading
import time
from collections.abc import Callable
from typing import Any

import matplotlib.pyplot as plt
from sqlalchemy.exc import OperationalError

from fpgen.artifacts import (
    LAYOUT_FILENAME,
    PNG_FILENAME,
    SVG_FILENAME,
    publish_staging,
    remove_job_artifacts,
    remove_unreferenced_artifacts,
    reset_staging,
)
from fpgen.db import get_engine
from fpgen.generate import generate_layout
from fpgen.jobs import (
    JobRecord,
    claim_next,
    known_job_ids,
    mark_failed,
    mark_succeeded,
    recover_interrupted,
    remove_expired,
)
from fpgen.render import render_layout
from fpgen.settings import ConfigurationError, Settings, get_settings
from fpgen.validate import validate_layout, validation_summary

logger = logging.getLogger("fpgen.worker")

IDLE_POLL_SECONDS = 1.0
CLEANUP_INTERVAL_SECONDS = 60.0
INTERNAL_ERROR = "The job failed because of an internal error."

_SINGLE_WORKER = (
    "Floor plan worker ready. This version runs a single worker; "
    "multiple worker processes are outside this version."
)


class JobProcessingError(Exception):
    """An expected failure that should be stored on the job."""

    def __init__(self, code: str, message: str) -> None:
        self.code = code
        self.message = message
        super().__init__(message)


def load_runtime() -> Settings:
    """Open the configured database and return the shared settings.

    This checks that the database can be contacted. It does not create tables
    and does not require an API key.
    """
    from sqlalchemy import text

    settings = get_settings()
    engine = get_engine(settings)
    try:
        with engine.connect() as connection:
            connection.execute(text("SELECT 1"))
    finally:
        engine.dispose()
    return settings


def startup(settings: Settings) -> list[JobRecord]:
    """Recover interrupted jobs and delete expired artifacts."""
    try:
        recovered = recover_interrupted(settings=settings)
    except OperationalError as exc:
        if _is_missing_queue(exc):
            raise ConfigurationError(
                "The job queue tables are missing. Run `alembic upgrade head` "
                "and start the worker again."
            ) from exc
        raise
    for job in recovered:
        remove_job_artifacts(settings, job.id)
    cleanup_expired(settings)
    return recovered


def cleanup_expired(settings: Settings, *, now=None) -> list[JobRecord]:
    """Delete expired succeeded/failed jobs and artifacts that no longer have a row.

    Queued and running rows are left untouched, including any in-progress
    staging directory for a job that is still running.
    """
    removed = remove_expired(now=now, settings=settings)
    for job in removed:
        remove_job_artifacts(settings, job.id)
    remove_unreferenced_artifacts(settings, known_job_ids(settings=settings))
    return removed


def process_job(job: JobRecord, settings: Settings) -> JobRecord:
    """Generate or render one running job and publish its files before succeeding.

    The database session is not held across generation or rendering. Partial
    files are removed when the job fails. Matplotlib figures are closed either way.
    """
    try:
        layout = _layout_for(job)
        summary = _require_valid(layout)
        _write_artifacts(settings, job, layout)
        publish_staging(settings, job.id)
        return mark_succeeded(
            job.id,
            {
                "validation": summary,
                "files": {
                    "json": LAYOUT_FILENAME,
                    "png": PNG_FILENAME,
                    "svg": SVG_FILENAME,
                },
            },
            settings=settings,
        )
    except JobProcessingError as exc:
        remove_job_artifacts(settings, job.id)
        return mark_failed(job.id, exc.code, exc.message, settings=settings)
    except Exception:
        logger.exception("Unexpected failure processing job %s", job.id)
        remove_job_artifacts(settings, job.id)
        return mark_failed(job.id, "internal_error", INTERNAL_ERROR, settings=settings)
    finally:
        plt.close("all")


def run_once(settings: Settings) -> JobRecord | None:
    """Claim and finish the oldest queued job, if there is one."""
    job = claim_next(settings=settings)
    if job is None:
        return None
    return process_job(job, settings)


def serve(
    settings: Settings,
    *,
    stop: threading.Event,
    process: Callable[..., JobRecord] = process_job,
    claim: Callable[..., JobRecord | None] = claim_next,
    cleanup: Callable[..., list[JobRecord]] = cleanup_expired,
    startup_fn: Callable[[Settings], list[JobRecord]] = startup,
    monotonic: Callable[[], float] = time.monotonic,
    wait: Callable[[float], bool] | None = None,
    poll_seconds: float = IDLE_POLL_SECONDS,
) -> None:
    """Recover, then claim one job at a time until ``stop`` is set.

    Idle polls wait ``poll_seconds`` (one second by default). Expired artifacts
    are cleaned at startup and again every minute. After ``stop`` is set the
    active job still finishes, and no later job is claimed.
    """
    pause = wait if wait is not None else stop.wait
    startup_fn(settings)
    logger.info("%s", _SINGLE_WORKER)
    next_cleanup = monotonic() + CLEANUP_INTERVAL_SECONDS
    while not stop.is_set():
        if monotonic() >= next_cleanup:
            try:
                cleanup(settings)
            except Exception:
                logger.exception("Expired job cleanup failed")
            next_cleanup = monotonic() + CLEANUP_INTERVAL_SECONDS
        job = claim(settings=settings)
        if job is None:
            if pause(poll_seconds):
                return
            continue
        try:
            process(job, settings)
        except Exception:
            logger.exception("Worker could not record the result of job %s", job.id)


def main() -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    stop = threading.Event()
    _install_signals(stop)
    try:
        settings = load_runtime()
        serve(settings, stop=stop)
    except ConfigurationError as exc:
        sys.stderr.write(f"ERROR: {exc}\n")
        return 1
    except Exception as exc:
        logger.exception("Worker stopped")
        sys.stderr.write(f"ERROR: {exc}\n")
        return 1
    return 0


def _install_signals(stop: threading.Event) -> None:
    def handle(signum: int, frame: object) -> None:
        logger.info("Shutdown requested (%s); the active job will finish", signum)
        stop.set()

    signal.signal(signal.SIGINT, handle)
    signal.signal(signal.SIGTERM, handle)


def _layout_for(job: JobRecord) -> dict[str, Any]:
    payload = job.request_payload
    if job.job_type == "generate":
        return _generate(payload)
    if job.job_type == "render":
        layout = payload.get("layout")
        if not isinstance(layout, dict):
            raise JobProcessingError("invalid_request", "Render jobs require a layout object.")
        return copy.deepcopy(layout)
    raise JobProcessingError("invalid_request", f"Unsupported job type {job.job_type!r}.")


def _generate(payload: dict[str, Any]) -> dict[str, Any]:
    try:
        return generate_layout(
            payload.get("beds", 3),
            payload.get("baths", 2),
            payload.get("sqft", 1600),
            payload.get("seed", 1),
            laundry=payload.get("laundry", None),
            garage=payload.get("garage", 0),
            shape=payload.get("shape", "rect"),
            garage_side=payload.get("garage_side", None),
        )
    except ValueError as exc:
        raise JobProcessingError("invalid_request", str(exc)) from exc
    except RuntimeError as exc:
        raise JobProcessingError("generation_failed", str(exc)) from exc
    except TypeError as exc:
        raise JobProcessingError(
            "invalid_request", "Generation request has invalid parameter types."
        ) from exc


def _require_valid(layout: dict[str, Any]) -> dict[str, Any]:
    try:
        encoded = json.dumps(layout)
    except (TypeError, ValueError) as exc:
        raise JobProcessingError("invalid_request", "Layout must be JSON-serializable.") from exc
    report = validate_layout(json.loads(encoded))
    if not report.ok:
        raise JobProcessingError("validation_failed", _validation_message(report))
    return validation_summary(report)


def _validation_message(report: Any) -> str:
    lines = ["Layout failed validation."]
    lines.extend(str(error) for error in report.errors)
    return "\n".join(lines)


def _write_artifacts(settings: Settings, job: JobRecord, layout: dict[str, Any]) -> None:
    staging = reset_staging(settings, job.id)
    (staging / LAYOUT_FILENAME).write_text(
        json.dumps(layout, indent=2),
        encoding="utf-8",
    )
    try:
        render_layout(
            layout,
            str(staging / "plan"),
            dpi=_dpi(job.request_payload),
            title=_title(job.request_payload),
        )
    except JobProcessingError:
        raise
    except ValueError as exc:
        raise JobProcessingError("render_failed", str(exc)) from exc
    except OSError as exc:
        raise JobProcessingError("render_failed", f"Could not write the drawing: {exc}") from exc
    for name in (LAYOUT_FILENAME, PNG_FILENAME, SVG_FILENAME):
        path = staging / name
        if not path.is_file() or path.stat().st_size <= 0:
            raise JobProcessingError("render_failed", f"Rendering did not produce {name}.")


def _dpi(payload: dict[str, Any]) -> int:
    dpi = payload.get("dpi", 170)
    if isinstance(dpi, bool) or not isinstance(dpi, int):
        raise JobProcessingError("invalid_request", "dpi must be an integer.")
    return dpi


def _title(payload: dict[str, Any]) -> str | None:
    if "title" not in payload or payload.get("title") is None:
        return None
    title = payload["title"]
    if not isinstance(title, str) or not title.strip():
        raise JobProcessingError("invalid_request", "title must be a non-empty string when provided.")
    return title


def _is_missing_queue(exc: BaseException) -> bool:
    text = str(exc).lower()
    return any(phrase in text for phrase in ("no such table", "doesn't exist", "does not exist"))


if __name__ == "__main__":
    raise SystemExit(main())
