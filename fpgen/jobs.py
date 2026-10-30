"""Queue operations.

Sessions stay short: each function commits or rolls back before it returns
and does not hold a transaction open while a plan is generated or rendered.
Statements are ordinary SQLAlchemy updates and selects. SQLite-specific SQL
does not belong here; connection settings live in ``fpgen.db``.
"""
from __future__ import annotations

import copy
import json
import uuid
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy import func, select, update
from sqlalchemy.orm import Session

from fpgen.db import session_scope
from fpgen.models import Job, QueueCoordination
from fpgen.settings import Settings

QUEUED = "queued"
RUNNING = "running"
SUCCEEDED = "succeeded"
FAILED = "failed"
JOB_TYPES = frozenset({"generate", "render"})
QUEUE_LIMIT = 100
MAX_RECOVERIES = 3
RECOVERY_LIMIT_CODE = "recovery_limit"
TERMINAL_RETENTION = timedelta(hours=24)
_COORDINATION_ID = 1


class JobQueueError(Exception):
    """Base error for queue operations."""


class QueueFullError(JobQueueError):
    """Raised when the number of queued jobs is already at the limit."""

    def __init__(self, limit: int = QUEUE_LIMIT) -> None:
        self.limit = limit
        super().__init__(f"Job queue is full ({limit} queued jobs). Try again later.")


class JobNotFoundError(JobQueueError):
    """Raised when a job id is not in the database."""

    def __init__(self, job_id: uuid.UUID) -> None:
        self.job_id = job_id
        super().__init__(f"Job {job_id} was not found.")


class JobStateError(JobQueueError):
    """Raised when a job is not in the state a transition requires."""

    def __init__(self, job_id: uuid.UUID, status: str) -> None:
        self.job_id = job_id
        self.status = status
        super().__init__(f"Job {job_id} is {status} and cannot make that transition.")


class _ClaimMissed(Exception):
    """The oldest queued row was no longer queued when the update ran."""


@dataclass(frozen=True)
class JobRecord:
    id: uuid.UUID
    job_type: str
    status: str
    request_payload: dict[str, Any]
    created_at: datetime
    started_at: datetime | None
    completed_at: datetime | None
    expires_at: datetime | None
    result_metadata: dict[str, Any] | None
    error_code: str | None
    error_message: str | None
    queue_position: int
    recovery_count: int


@contextmanager
def _transaction(settings: Settings | None) -> Iterator[Session]:
    with session_scope(settings) as session:
        try:
            yield session
            session.commit()
        except Exception:
            session.rollback()
            raise


def enqueue(
    job_type: str,
    request_payload: Mapping[str, Any],
    *,
    settings: Settings | None = None,
) -> JobRecord:
    """Insert a queued job and commit it before returning.

    Waiting jobs are limited to ``QUEUE_LIMIT`` queued rows. Running jobs are
    not counted. The coordination row is updated in this same transaction so
    concurrent submissions serialize on that row.
    """
    kind = _job_type(job_type)
    payload = _json_object(request_payload, "request payload")
    now = _utcnow()
    with _transaction(settings) as session:
        _lock_admission(session)
        if _count_queued(session) >= QUEUE_LIMIT:
            raise QueueFullError(QUEUE_LIMIT)
        position = _allocate_position(session)
        job = Job(
            id=uuid.uuid4(),
            job_type=kind,
            status=QUEUED,
            request_payload=payload,
            created_at=now,
            started_at=None,
            completed_at=None,
            expires_at=None,
            result_metadata=None,
            error_code=None,
            error_message=None,
            queue_position=position,
            recovery_count=0,
        )
        session.add(job)
        session.flush()
        return _record(job)


def fetch_job(job_id: uuid.UUID, *, settings: Settings | None = None) -> JobRecord | None:
    with session_scope(settings) as session:
        job = session.get(Job, job_id)
        if job is None:
            return None
        return _record(job)


def claim_next(*, settings: Settings | None = None) -> JobRecord | None:
    """Claim the oldest queued job.

    The status change is a conditional update from queued to running. The
    claim is kept only when that update changes exactly one row.
    """
    now = _utcnow()
    try:
        with _transaction(settings) as session:
            job_id = session.scalar(
                select(Job.id).where(Job.status == QUEUED).order_by(Job.queue_position.asc()).limit(1)
            )
            if job_id is None:
                return None
            result = session.execute(
                update(Job)
                .where(Job.id == job_id, Job.status == QUEUED)
                .values(status=RUNNING, started_at=now)
                .execution_options(synchronize_session=False)
            )
            if result.rowcount != 1:
                raise _ClaimMissed
            job = session.get(Job, job_id, populate_existing=True)
            if job is None:
                raise _ClaimMissed
            return _record(job)
    except _ClaimMissed:
        return None


def mark_succeeded(
    job_id: uuid.UUID,
    result_metadata: Mapping[str, Any],
    *,
    expires_at: datetime | None = None,
    settings: Settings | None = None,
) -> JobRecord:
    """Mark a running job succeeded. Only a running job can complete."""
    metadata = _json_object(result_metadata, "result metadata")
    now = _utcnow()
    expiry = _expiry(expires_at, now)
    with _transaction(settings) as session:
        job = _transition(
            session,
            job_id,
            from_status=RUNNING,
            values={
                "status": SUCCEEDED,
                "completed_at": now,
                "expires_at": expiry,
                "result_metadata": metadata,
                "error_code": None,
                "error_message": None,
            },
        )
        return _record(job)


def mark_failed(
    job_id: uuid.UUID,
    error_code: str,
    error_message: str,
    *,
    expires_at: datetime | None = None,
    settings: Settings | None = None,
) -> JobRecord:
    """Mark a running job failed. Only a running job can fail this way."""
    code = _error_code(error_code)
    message = _error_message(error_message)
    now = _utcnow()
    expiry = _expiry(expires_at, now)
    with _transaction(settings) as session:
        job = _transition(
            session,
            job_id,
            from_status=RUNNING,
            values={
                "status": FAILED,
                "completed_at": now,
                "expires_at": expiry,
                "error_code": code,
                "error_message": message,
            },
        )
        return _record(job)


def recover_interrupted(*, settings: Settings | None = None) -> list[JobRecord]:
    """Return running jobs to the queue, or fail them past the recovery cap.

    Each recovered job keeps its id. The recovery count increments every time.
    A job recovered more than three times is marked failed instead of queued.
    The returned records are the jobs whose partial files the caller may remove.
    """
    now = _utcnow()
    expiry = now + TERMINAL_RETENTION
    with _transaction(settings) as session:
        jobs = list(
            session.scalars(select(Job).where(Job.status == RUNNING).order_by(Job.queue_position.asc()))
        )
        records: list[JobRecord] = []
        for job in jobs:
            job.recovery_count += 1
            if job.recovery_count > MAX_RECOVERIES:
                job.status = FAILED
                job.completed_at = now
                job.expires_at = expiry
                job.error_code = RECOVERY_LIMIT_CODE
                job.error_message = "Job was interrupted more than 3 times and was not restarted."
            else:
                job.status = QUEUED
                job.started_at = None
            records.append(_record(job))
        return records


def known_job_ids(*, settings: Settings | None = None) -> set[uuid.UUID]:
    """Return every job id still stored. Used to drop artifact directories."""
    with session_scope(settings) as session:
        return set(session.scalars(select(Job.id)).all())


def remove_expired(
    *,
    now: datetime | None = None,
    settings: Settings | None = None,
) -> list[JobRecord]:
    """Delete succeeded or failed jobs whose expiry is at or before ``now``.

    Queued and running jobs are never removed, even if an expiry is set.
    """
    moment = _require_utc(now) if now is not None else _utcnow()
    with _transaction(settings) as session:
        jobs = list(
            session.scalars(
                select(Job)
                .where(
                    Job.status.in_((SUCCEEDED, FAILED)),
                    Job.expires_at.is_not(None),
                    Job.expires_at <= moment,
                )
                .order_by(Job.queue_position.asc())
            )
        )
        records = [_record(job) for job in jobs]
        for job in jobs:
            session.delete(job)
        return records


def _lock_admission(session: Session) -> None:
    result = session.execute(
        update(QueueCoordination)
        .where(QueueCoordination.id == _COORDINATION_ID)
        .values(admission_nonce=QueueCoordination.admission_nonce + 1)
        .execution_options(synchronize_session=False)
    )
    if result.rowcount != 1:
        raise JobQueueError(
            "Queue coordination row is missing. Run alembic upgrade head before enqueueing jobs."
        )


def _allocate_position(session: Session) -> int:
    gate = session.get(QueueCoordination, _COORDINATION_ID, populate_existing=True)
    if gate is None:
        raise JobQueueError(
            "Queue coordination row is missing. Run alembic upgrade head before enqueueing jobs."
        )
    position = int(gate.next_queue_position)
    gate.next_queue_position = position + 1
    return position


def _count_queued(session: Session) -> int:
    return int(session.scalar(select(func.count()).select_from(Job).where(Job.status == QUEUED)) or 0)


def _transition(session: Session, job_id: uuid.UUID, *, from_status: str, values: dict[str, Any]) -> Job:
    result = session.execute(
        update(Job)
        .where(Job.id == job_id, Job.status == from_status)
        .values(**values)
        .execution_options(synchronize_session=False)
    )
    if result.rowcount != 1:
        status = session.scalar(select(Job.status).where(Job.id == job_id))
        if status is None:
            raise JobNotFoundError(job_id)
        raise JobStateError(job_id, status)
    job = session.get(Job, job_id, populate_existing=True)
    if job is None:
        raise JobNotFoundError(job_id)
    return job


def _record(job: Job) -> JobRecord:
    return JobRecord(
        id=job.id,
        job_type=job.job_type,
        status=job.status,
        request_payload=_copy_json(job.request_payload) or {},
        created_at=_require_utc(job.created_at),
        started_at=_optional_utc(job.started_at),
        completed_at=_optional_utc(job.completed_at),
        expires_at=_optional_utc(job.expires_at),
        result_metadata=_copy_json(job.result_metadata),
        error_code=job.error_code,
        error_message=job.error_message,
        queue_position=int(job.queue_position),
        recovery_count=int(job.recovery_count),
    )


def _copy_json(value: dict[str, Any] | None) -> dict[str, Any] | None:
    if value is None:
        return None
    copied = copy.deepcopy(value)
    if not isinstance(copied, dict):
        raise TypeError("JSON column must contain an object")
    return copied


def _json_object(value: object, label: str) -> dict[str, Any]:
    if isinstance(value, (str, bytes)) or not isinstance(value, Mapping):
        raise TypeError(f"{label} must be a JSON object")
    try:
        encoded = json.dumps(value)
    except (TypeError, ValueError) as exc:
        raise TypeError(f"{label} must be JSON-serializable") from exc
    decoded = json.loads(encoded)
    if not isinstance(decoded, dict):
        raise TypeError(f"{label} must be a JSON object")
    return decoded


def _job_type(job_type: str) -> str:
    if not isinstance(job_type, str) or job_type not in JOB_TYPES:
        raise ValueError("job_type must be 'generate' or 'render'")
    return job_type


def _error_code(error_code: str) -> str:
    if not isinstance(error_code, str) or not error_code.strip():
        raise ValueError("error_code must be a non-empty string")
    code = error_code.strip()
    if len(code) > 64:
        raise ValueError("error_code must be 64 characters or fewer")
    return code


def _error_message(error_message: str) -> str:
    if not isinstance(error_message, str) or not error_message.strip():
        raise ValueError("error_message must be a non-empty string")
    return error_message


def _expiry(expires_at: datetime | None, now: datetime) -> datetime:
    if expires_at is None:
        return now + TERMINAL_RETENTION
    return _require_utc(expires_at)


def _optional_utc(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    return _require_utc(value)


def _require_utc(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("timestamps must be timezone-aware")
    return value.astimezone(timezone.utc)


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)
