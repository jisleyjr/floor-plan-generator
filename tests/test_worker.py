"""Worker lifecycle: one job at a time, artifacts, recovery, and expiration."""
from __future__ import annotations

import json
import logging
import os
import subprocess
import sys
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from fpgen.artifacts import published_dir, staging_dir
from fpgen.jobs import claim_next, enqueue, fetch_job
from fpgen.settings import Settings, get_settings
from fpgen.worker import (
    CLEANUP_INTERVAL_SECONDS,
    IDLE_POLL_SECONDS,
    INTERNAL_ERROR,
    cleanup_expired,
    process_job,
    run_once,
    serve,
    startup,
)

ROOT = Path(__file__).resolve().parents[1]


def _migrate(url: str) -> subprocess.CompletedProcess[str]:
    env = os.environ.copy()
    for name in ("DATABASE_URL", "FLOORPLAN_DATA_DIR", "FLOORPLAN_API_KEY"):
        env.pop(name, None)
    env["DATABASE_URL"] = url
    return subprocess.run(
        [sys.executable, "-m", "alembic", "upgrade", "head"],
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
    upgraded = _migrate(url)
    assert upgraded.returncode == 0, upgraded.stderr
    return get_settings()


def _cottage() -> dict:
    return json.loads((ROOT / "examples" / "cottage_2bd.json").read_text(encoding="utf-8"))


def _broken() -> dict:
    return json.loads((ROOT / "examples" / "broken_layout.json").read_text(encoding="utf-8"))


def _claim_and_finish(settings: Settings):
    claimed = claim_next(settings=settings)
    assert claimed is not None
    return process_job(claimed, settings)


def test_worker_documents_a_single_process() -> None:
    text = (ROOT / "fpgen" / "worker.py").read_text(encoding="utf-8")
    assert "python -m fpgen.worker" in text
    assert "Multiple worker processes are outside this version" in text


def test_generate_and_render_jobs_publish_a_plan(settings: Settings) -> None:
    generated = enqueue(
        "generate",
        {"beds": 1, "baths": 1, "sqft": 620, "seed": 1, "title": "Café — plan"},
        settings=settings,
    )
    rendered = enqueue(
        "render",
        {"layout": _cottage(), "dpi": 72, "title": "Cottage"},
        settings=settings,
    )

    first = run_once(settings)
    second = run_once(settings)
    assert run_once(settings) is None

    assert first is not None and second is not None
    assert first.id == generated.id
    assert second.id == rendered.id
    for finished in (first, second):
        assert finished.status == "succeeded"
        assert finished.completed_at is not None
        assert finished.expires_at is not None
        assert finished.expires_at - finished.completed_at == timedelta(hours=24)
        summary = finished.result_metadata["validation"]
        assert summary["ok"] is True
        assert summary["errors"] == []
        assert isinstance(summary["warnings"], list)
        assert set(summary["areas"]) == {"living_sqft", "garage_sqft", "other_unconditioned_sqft"}
        json.dumps(summary)
        directory = published_dir(settings, finished.id)
        layout_path = directory / "layout.json"
        png_path = directory / "plan.png"
        svg_path = directory / "plan.svg"
        assert layout_path.is_file() and png_path.is_file() and svg_path.is_file()
        assert png_path.read_bytes().startswith(b"\x89PNG")
        assert "<svg" in svg_path.read_text(encoding="utf-8")
        assert not staging_dir(settings, finished.id).exists()
        saved = json.loads(layout_path.read_text(encoding="utf-8"))
        assert saved["rooms"]
        assert saved["footprint"]

    saved_title = json.loads(
        (published_dir(settings, generated.id) / "layout.json").read_text(encoding="utf-8")
    )
    assert "Café — plan" in (published_dir(settings, generated.id) / "plan.svg").read_text(encoding="utf-8")
    again = enqueue(
        "generate",
        {"beds": 1, "baths": 1, "sqft": 620, "seed": 1},
        settings=settings,
    )
    other = enqueue(
        "generate",
        {"beds": 1, "baths": 1, "sqft": 620, "seed": 2},
        settings=settings,
    )
    repeated = _claim_and_finish(settings)
    different = _claim_and_finish(settings)
    repeated_layout = json.loads(
        (published_dir(settings, repeated.id) / "layout.json").read_text(encoding="utf-8")
    )
    different_layout = json.loads(
        (published_dir(settings, different.id) / "layout.json").read_text(encoding="utf-8")
    )
    assert repeated.id == again.id
    assert repeated_layout == saved_title
    assert different.id == other.id
    assert different_layout != saved_title


def test_render_keeps_the_submitted_layout_text(settings: Settings) -> None:
    layout = _cottage()
    del layout["rooms"][0]["name"]
    job = enqueue("render", {"layout": layout, "dpi": 72}, settings=settings)

    finished = _claim_and_finish(settings)

    assert finished.status == "succeeded"
    saved = json.loads((published_dir(settings, finished.id) / "layout.json").read_text(encoding="utf-8"))
    assert "name" not in saved["rooms"][0]
    assert "name" not in fetch_job(job.id, settings=settings).request_payload["layout"]["rooms"][0]


def test_invalid_layouts_fail_the_job_without_publishing(settings: Settings, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("fpgen.worker.generate_layout", lambda *args, **kwargs: _broken())
    generated = enqueue("generate", {"beds": 3, "baths": 2, "sqft": 1600, "seed": 1}, settings=settings)
    rendered = enqueue("render", {"layout": _broken()}, settings=settings)

    failed_generate = _claim_and_finish(settings)
    failed_render = _claim_and_finish(settings)

    for failed in (failed_generate, failed_render):
        assert failed.status == "failed"
        assert failed.error_code == "validation_failed"
        assert "Layout failed validation." in failed.error_message
        assert "overlap" in failed.error_message
        assert failed.expires_at - failed.completed_at == timedelta(hours=24)
        assert not published_dir(settings, failed.id).exists()
        assert not staging_dir(settings, failed.id).exists()
    assert failed_generate.id == generated.id
    assert failed_render.id == rendered.id


def test_a_generator_that_cannot_place_rooms_fails_the_job(
    settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    def no_layout(*args, **kwargs):
        raise RuntimeError("could not find a valid layout for this program; try more sq ft")

    monkeypatch.setattr("fpgen.worker.generate_layout", no_layout)
    job = enqueue("generate", {"beds": 1, "baths": 1, "sqft": 620, "seed": 1}, settings=settings)

    failed = _claim_and_finish(settings)

    assert failed.id == job.id
    assert failed.error_code == "generation_failed"
    assert "could not find a valid layout" in failed.error_message
    assert not published_dir(settings, job.id).exists()


def test_impossible_specs_and_missing_layouts_are_readable_failures(settings: Settings) -> None:
    tiny = enqueue("generate", {"beds": 3, "baths": 2, "sqft": 100, "seed": 1}, settings=settings)
    missing = enqueue("render", {"title": "No layout"}, settings=settings)

    failed_spec = _claim_and_finish(settings)
    failed_request = _claim_and_finish(settings)

    assert failed_spec.id == tiny.id
    assert failed_spec.status == "failed"
    assert failed_spec.error_code == "invalid_request"
    assert "too small" in failed_spec.error_message
    assert failed_request.id == missing.id
    assert failed_request.error_code == "invalid_request"
    assert "layout" in failed_request.error_message
    assert not published_dir(settings, tiny.id).exists()


def test_unexpected_errors_are_generic_and_figures_are_closed(
    settings: Settings, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    import matplotlib.pyplot as plt

    def explode(layout, out_base, dpi=170, title=None):
        plt.figure()
        raise RuntimeError("needle-secret")

    monkeypatch.setattr("fpgen.worker.render_layout", explode)
    job = enqueue("render", {"layout": _cottage()}, settings=settings)

    with caplog.at_level(logging.ERROR, logger="fpgen.worker"):
        failed = _claim_and_finish(settings)

    assert failed.id == job.id
    assert failed.status == "failed"
    assert failed.error_code == "internal_error"
    assert failed.error_message == INTERNAL_ERROR
    assert "needle-secret" not in failed.error_message
    assert "needle-secret" in caplog.text
    assert plt.get_fignums() == []
    assert not published_dir(settings, job.id).exists()
    assert not staging_dir(settings, job.id).exists()


def test_startup_recovers_an_interrupted_job_and_drops_partial_files(settings: Settings) -> None:
    job = enqueue("generate", {"beds": 1, "baths": 1, "sqft": 620, "seed": 1}, settings=settings)
    assert claim_next(settings=settings) is not None
    staging = staging_dir(settings, job.id)
    staging.mkdir(parents=True)
    (staging / "plan.png").write_bytes(b"partial")
    published = published_dir(settings, job.id)
    published.mkdir(parents=True)
    (published / "plan.png").write_bytes(b"partial")

    recovered = startup(settings)

    assert [item.id for item in recovered] == [job.id]
    assert fetch_job(job.id, settings=settings).status == "queued"
    assert fetch_job(job.id, settings=settings).recovery_count == 1
    assert not staging.exists()
    assert not published.exists()

    finished = run_once(settings)
    assert finished is not None
    assert finished.id == job.id
    assert finished.status == "succeeded"
    assert (published_dir(settings, job.id) / "plan.png").read_bytes().startswith(b"\x89PNG")


def test_startup_fails_a_job_recovered_more_than_three_times(settings: Settings) -> None:
    from fpgen.jobs import recover_interrupted

    job = enqueue("render", {"layout": _cottage()}, settings=settings)
    for _ in range(3):
        assert claim_next(settings=settings) is not None
        assert recover_interrupted(settings=settings)[0].status == "queued"
    assert claim_next(settings=settings) is not None
    partial = staging_dir(settings, job.id)
    partial.mkdir(parents=True)
    (partial / "plan.svg").write_text("<svg/>", encoding="utf-8")

    recovered = startup(settings)

    assert recovered[0].id == job.id
    assert recovered[0].status == "failed"
    assert recovered[0].recovery_count == 4
    assert recovered[0].error_code == "recovery_limit"
    stored = fetch_job(job.id, settings=settings)
    assert stored.status == "failed"
    assert stored.error_message
    assert not partial.exists()
    assert run_once(settings) is None


def test_expired_artifacts_are_removed_and_active_jobs_stay(settings: Settings) -> None:
    expired = enqueue("render", {"layout": _cottage(), "dpi": 72}, settings=settings)
    finished = _claim_and_finish(settings)
    directory = published_dir(settings, expired.id)
    assert directory.is_dir()
    assert finished.expires_at is not None

    from fpgen.db import session_scope
    from fpgen.models import Job

    past = datetime.now(timezone.utc) - timedelta(minutes=1)
    with session_scope(settings) as session:
        row = session.get(Job, expired.id)
        assert row is not None
        row.expires_at = past
        session.commit()

    running = enqueue("render", {"layout": _cottage()}, settings=settings)
    assert claim_next(settings=settings).id == running.id
    waiting = enqueue("generate", {"beds": 1, "baths": 1, "sqft": 620, "seed": 1}, settings=settings)
    running_staging = staging_dir(settings, running.id)
    running_staging.mkdir(parents=True)
    (running_staging / "plan.png").write_bytes(b"in-progress")

    removed = cleanup_expired(settings)

    assert [item.id for item in removed] == [expired.id]
    assert fetch_job(expired.id, settings=settings) is None
    assert not directory.exists()
    assert fetch_job(waiting.id, settings=settings).status == "queued"
    assert fetch_job(running.id, settings=settings).status == "running"
    assert (running_staging / "plan.png").read_bytes() == b"in-progress"


def test_shutdown_finishes_the_active_job_and_claims_nothing_else(settings: Settings) -> None:
    first = enqueue("render", {"layout": _cottage(), "dpi": 72}, settings=settings)
    second = enqueue("render", {"layout": _cottage(), "dpi": 72}, settings=settings)
    started = threading.Event()
    release = threading.Event()
    seen: list = []
    depth = {"current": 0, "max": 0}

    def process(job, settings):
        depth["current"] += 1
        depth["max"] = max(depth["max"], depth["current"])
        seen.append(job.id)
        started.set()
        assert release.wait(timeout=5)
        try:
            return process_job(job, settings)
        finally:
            depth["current"] -= 1

    stop = threading.Event()
    thread = threading.Thread(target=lambda: serve(settings, stop=stop, process=process), daemon=True)
    thread.start()
    assert started.wait(timeout=5)
    stop.set()
    release.set()
    thread.join(timeout=10)

    assert not thread.is_alive()
    assert seen == [first.id]
    assert depth["max"] == 1
    assert fetch_job(first.id, settings=settings).status == "succeeded"
    assert fetch_job(second.id, settings=settings).status == "queued"


def test_idle_worker_cleans_expired_jobs_every_minute(settings: Settings) -> None:
    clock = {"t": 0.0}
    cleaned: list[float] = []

    def monotonic() -> float:
        return clock["t"]

    def wait(seconds: float) -> bool:
        assert seconds == IDLE_POLL_SECONDS
        clock["t"] += seconds
        return clock["t"] > 70

    def cleanup(settings):
        cleaned.append(clock["t"])

    def claim(settings=None):
        return None

    stop = threading.Event()
    serve(
        settings,
        stop=stop,
        claim=claim,
        cleanup=cleanup,
        startup_fn=lambda settings: [],
        monotonic=monotonic,
        wait=wait,
    )

    assert cleaned == [CLEANUP_INTERVAL_SECONDS]


def test_worker_exits_when_the_queue_tables_are_missing(tmp_path: Path) -> None:
    data_dir = tmp_path / "runtime"
    result = subprocess.run(
        [sys.executable, "-m", "fpgen.worker"],
        cwd=ROOT,
        env={
            **os.environ,
            "DATABASE_URL": "sqlite:///" + (data_dir / "floorplan.sqlite").as_posix(),
            "FLOORPLAN_DATA_DIR": str(data_dir),
            "FLOORPLAN_API_KEY": "",
        },
        capture_output=True,
        text=True,
        timeout=15,
    )

    assert result.returncode == 1
    assert "ERROR:" in result.stderr
    assert "alembic upgrade head" in result.stderr
    assert "Traceback" not in result.stderr
