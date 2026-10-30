"""Job queue persistence without an HTTP server."""
from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path
from uuid import uuid4

import pytest
from sqlalchemy import func, inspect, select, text
from sqlalchemy.orm import Session

from fpgen.db import get_engine, session_scope
from fpgen.jobs import (
    QUEUE_LIMIT,
    RECOVERY_LIMIT_CODE,
    JobNotFoundError,
    JobStateError,
    QueueFullError,
    claim_next,
    enqueue,
    fetch_job,
    mark_failed,
    mark_succeeded,
    recover_interrupted,
    remove_expired,
)
from fpgen.models import Job, QueueCoordination
from fpgen.settings import Settings, get_settings

ROOT = Path(__file__).resolve().parents[1]


def _migrate(url: str, *args: str) -> subprocess.CompletedProcess[str]:
    env = os.environ.copy()
    for name in ("DATABASE_URL", "FLOORPLAN_DATA_DIR", "FLOORPLAN_API_KEY"):
        env.pop(name, None)
    env["DATABASE_URL"] = url
    return subprocess.run(
        [sys.executable, "-m", "alembic", *args],
        cwd=ROOT,
        env=env,
        capture_output=True,
        text=True,
    )


@pytest.fixture
def settings(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Settings:
    database = tmp_path / "queue.sqlite"
    url = "sqlite:///" + database.as_posix()
    monkeypatch.setenv("DATABASE_URL", url)
    monkeypatch.setenv("FLOORPLAN_DATA_DIR", str(tmp_path / "data"))
    upgraded = _migrate(url, "upgrade", "head")
    assert upgraded.returncode == 0, upgraded.stderr
    return get_settings()


def _count(settings: Settings, status: str | None = None) -> int:
    with session_scope(settings) as session:
        query = select(func.count()).select_from(Job)
        if status is not None:
            query = query.where(Job.status == status)
        return int(session.scalar(query) or 0)


def _gate(settings: Settings) -> tuple[int, int]:
    with session_scope(settings) as session:
        gate = session.get(QueueCoordination, 1)
        assert gate is not None
        return gate.admission_nonce, gate.next_queue_position


def test_migration_creates_the_queue_and_can_be_reapplied(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    database = tmp_path / "migrated.sqlite"
    url = "sqlite:///" + database.as_posix()
    monkeypatch.setenv("DATABASE_URL", url)

    first = _migrate(url, "upgrade", "head")
    second = _migrate(url, "upgrade", "head")

    assert first.returncode == 0, first.stderr
    assert second.returncode == 0, second.stderr
    engine = get_engine(get_settings())
    try:
        columns = {column["name"] for column in inspect(engine).get_columns("jobs")}
        assert columns == {
            "id",
            "job_type",
            "status",
            "request_payload",
            "created_at",
            "started_at",
            "completed_at",
            "expires_at",
            "result_metadata",
            "error_code",
            "error_message",
            "queue_position",
            "recovery_count",
        }
        indexes = inspect(engine).get_indexes("jobs")
        assert any(index["column_names"] == ["status", "queue_position"] for index in indexes)
        with engine.connect() as connection:
            nonce, position = connection.execute(
                text("SELECT admission_nonce, next_queue_position FROM queue_coordination WHERE id = 1")
            ).one()
        assert nonce == 0
        assert position == 1
    finally:
        engine.dispose()


def test_enqueued_job_persists_across_connections(settings: Settings) -> None:
    payload = {"title": "Café — 房间", "beds": 3, "notes": ["north", "south"]}

    created = enqueue("generate", payload, settings=settings)
    engine = get_engine(settings)
    try:
        with engine.connect() as connection:
            stored = connection.execute(text("SELECT request_payload FROM jobs")).scalar_one()
        if isinstance(stored, str):
            stored = json.loads(stored)
        assert stored["title"] == "Café — 房间"
    finally:
        engine.dispose()

    loaded = fetch_job(created.id, settings=get_settings())

    assert loaded is not None
    assert loaded == created
    assert loaded.status == "queued"
    assert loaded.job_type == "generate"
    assert loaded.request_payload == payload
    assert loaded.created_at.tzinfo is not None
    assert loaded.created_at.utcoffset() == timedelta(0)
    assert loaded.recovery_count == 0
    assert loaded.started_at is None
    loaded.request_payload["beds"] = 9
    assert fetch_job(created.id, settings=settings).request_payload["beds"] == 3


def test_claims_follow_enqueue_order(settings: Settings) -> None:
    created = [enqueue("render", {"name": name}, settings=settings) for name in ("first", "second", "third")]

    claimed = [claim_next(settings=settings) for _ in range(3)]

    assert [job.id for job in claimed] == [job.id for job in created]
    assert [job.request_payload["name"] for job in claimed] == ["first", "second", "third"]
    assert all(job.status == "running" and job.started_at is not None for job in claimed)
    assert claim_next(settings=settings) is None


def test_only_one_caller_can_claim_a_queued_job(settings: Settings) -> None:
    enqueue("generate", {"seed": 1}, settings=settings)
    barrier = threading.Barrier(2)
    results: list[object] = [None, None]
    errors: list[BaseException] = []

    def claim(index: int) -> None:
        try:
            barrier.wait(timeout=5)
            results[index] = claim_next(settings=settings)
        except BaseException as exc:  # pragma: no cover - reported below
            errors.append(exc)

    threads = [threading.Thread(target=claim, args=(index,)) for index in (0, 1)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=10)

    assert errors == []
    assert all(not thread.is_alive() for thread in threads)
    winners = [job for job in results if job is not None]
    assert len(winners) == 1
    assert winners[0].status == "running"
    assert fetch_job(winners[0].id, settings=settings).status == "running"
    assert claim_next(settings=settings) is None


def test_queue_limit_counts_queued_jobs_only(settings: Settings) -> None:
    for number in range(QUEUE_LIMIT):
        enqueue("generate", {"n": number}, settings=settings)
    with pytest.raises(QueueFullError, match="100"):
        enqueue("generate", {"n": "overflow"}, settings=settings)
    assert _count(settings, "queued") == QUEUE_LIMIT

    claimed = claim_next(settings=settings)
    assert claimed is not None
    accepted = enqueue("render", {"n": "after-claim"}, settings=settings)

    assert accepted.status == "queued"
    assert _count(settings, "queued") == QUEUE_LIMIT
    assert _count(settings, "running") == 1
    with pytest.raises(QueueFullError):
        enqueue("generate", {"n": "still-full"}, settings=settings)
    assert fetch_job(accepted.id, settings=settings) is not None
    assert fetch_job(claimed.id, settings=settings).status == "running"


def test_concurrent_enqueues_cannot_pass_the_queued_limit(settings: Settings) -> None:
    for number in range(QUEUE_LIMIT - 1):
        enqueue("generate", {"n": number}, settings=settings)
    barrier = threading.Barrier(2)
    outcomes: list[str] = []
    errors: list[BaseException] = []

    def submit(seed: int) -> None:
        try:
            barrier.wait(timeout=5)
            enqueue("generate", {"n": seed}, settings=settings)
            outcomes.append("accepted")
        except QueueFullError:
            outcomes.append("full")
        except BaseException as exc:  # pragma: no cover - reported below
            errors.append(exc)

    threads = [threading.Thread(target=submit, args=(1000 + index,)) for index in (0, 1)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=10)

    assert errors == []
    assert sorted(outcomes) == ["accepted", "full"]
    assert _count(settings, "queued") == QUEUE_LIMIT


def test_failed_enqueue_rolls_back_the_job_and_coordination_row(
    settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    enqueue("generate", {"seed": 1}, settings=settings)
    nonce, position = _gate(settings)
    original_commit = Session.commit

    def fail(self: Session) -> None:
        raise RuntimeError("forced transaction failure")

    monkeypatch.setattr(Session, "commit", fail)
    with pytest.raises(RuntimeError, match="forced transaction failure"):
        enqueue("render", {"seed": 2}, settings=settings)

    monkeypatch.setattr(Session, "commit", original_commit)
    assert _count(settings) == 1
    assert _gate(settings) == (nonce, position)
    assert 2 not in _payload_seeds(settings)


def _payload_seeds(settings: Settings) -> list[object]:
    with session_scope(settings) as session:
        payloads = session.scalars(select(Job.request_payload)).all()
    return [payload.get("seed") for payload in payloads]


def test_success_and_failure_are_stored_for_a_running_job(settings: Settings) -> None:
    queued = enqueue("generate", {"seed": 4}, settings=settings)
    with pytest.raises(JobStateError):
        mark_succeeded(queued.id, {"ok": True}, settings=settings)

    running = claim_next(settings=settings)
    assert running is not None
    expires = datetime(2030, 1, 2, 3, 4, 5, tzinfo=timezone.utc)
    succeeded = mark_succeeded(running.id, {"files": ["plan.png"]}, expires_at=expires, settings=settings)

    assert succeeded.status == "succeeded"
    assert succeeded.completed_at is not None
    assert succeeded.completed_at.utcoffset() == timedelta(0)
    assert succeeded.expires_at == expires
    assert succeeded.result_metadata == {"files": ["plan.png"]}
    assert succeeded.error_code is None
    assert fetch_job(running.id, settings=settings) == succeeded

    other = claim_next(settings=settings)
    assert other is None
    failed_job = enqueue("render", {"layout": {"title": "坏"}}, settings=settings)
    claim_next(settings=settings)
    failed = mark_failed(
        failed_job.id,
        "validation_failed",
        "Rooms overlap.",
        expires_at=expires,
        settings=settings,
    )
    assert failed.status == "failed"
    assert failed.error_code == "validation_failed"
    assert failed.error_message == "Rooms overlap."
    assert failed.expires_at == expires
    with pytest.raises(JobNotFoundError):
        mark_succeeded(uuid4(), {}, settings=settings)


def test_expired_terminal_jobs_are_removed_and_active_jobs_stay(settings: Settings) -> None:
    past = datetime.now(timezone.utc) - timedelta(hours=1)
    future = datetime.now(timezone.utc) + timedelta(days=2)

    expired = enqueue("generate", {"name": "expired"}, settings=settings)
    claim_next(settings=settings)
    mark_succeeded(expired.id, {"files": []}, expires_at=past, settings=settings)

    kept = enqueue("generate", {"name": "kept"}, settings=settings)
    claim_next(settings=settings)
    mark_failed(kept.id, "render_failed", "nope", expires_at=future, settings=settings)

    running = enqueue("render", {"name": "running"}, settings=settings)
    claim_next(settings=settings)
    waiting = enqueue("render", {"name": "waiting"}, settings=settings)

    removed = remove_expired(settings=settings)

    assert [job.id for job in removed] == [expired.id]
    assert fetch_job(expired.id, settings=settings) is None
    assert fetch_job(kept.id, settings=settings).status == "failed"
    assert fetch_job(waiting.id, settings=settings).status == "queued"
    assert fetch_job(running.id, settings=settings).status == "running"


def test_interrupted_jobs_keep_their_id_until_the_recovery_cap(settings: Settings) -> None:
    created = enqueue("generate", {"seed": 7}, settings=settings)
    assert claim_next(settings=settings) is not None

    for expected in (1, 2, 3):
        recovered = recover_interrupted(settings=settings)
        assert len(recovered) == 1
        assert recovered[0].id == created.id
        assert recovered[0].status == "queued"
        assert recovered[0].recovery_count == expected
        assert recovered[0].started_at is None
        assert claim_next(settings=settings).id == created.id

    failed = recover_interrupted(settings=settings)
    assert failed[0].id == created.id
    assert failed[0].status == "failed"
    assert failed[0].recovery_count == 4
    assert failed[0].error_code == RECOVERY_LIMIT_CODE
    assert failed[0].error_message
    assert failed[0].completed_at is not None
    assert fetch_job(created.id, settings=settings).status == "failed"
    assert claim_next(settings=settings) is None
    assert recover_interrupted(settings=settings) == []


def test_queue_operations_do_not_use_sqlite_specific_sql() -> None:
    source = (ROOT / "fpgen" / "jobs.py").read_text(encoding="utf-8").lower()
    for snippet in (
        "pragma",
        "begin immediate",
        "on conflict",
        "json_extract",
        "import sqlite3",
        "dialects.sqlite",
    ):
        assert snippet not in source


def test_enqueue_rejects_a_payload_that_is_not_a_json_object(settings: Settings) -> None:
    with pytest.raises(TypeError):
        enqueue("generate", ["beds", 3], settings=settings)
    with pytest.raises(ValueError):
        enqueue("explore", {"seed": 1}, settings=settings)
    assert _count(settings) == 0
